"""Controller agent: stores every trade, finds what works, writes the report."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections import defaultdict
from datetime import datetime

from ..config import settings

log = logging.getLogger("controller")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY, symbol TEXT, base TEXT, side TEXT, leverage REAL, margin REAL,
    entry REAL, exit REAL, opened_ts REAL, closed_ts REAL, reason TEXT, pnl REAL, roe REAL,
    fees REAL, funding REAL, mode TEXT, origin TEXT, signal TEXT
);
CREATE TABLE IF NOT EXISTS calibration (
    ts REAL, label TEXT, symbol TEXT, p_up REAL, ret REAL
);
CREATE TABLE IF NOT EXISTS signals (
    ts REAL, due REAL, symbol TEXT, price REAL, p_up_4h REAL, entry_ok INTEGER, eligible INTEGER,
    extra_risk REAL, reasons TEXT, model TEXT, samples INTEGER, ret REAL, PRIMARY KEY (symbol, ts)
);
"""
# 4h buckets of the Kronos long probability (the breakout entry needs it above 50%)
PROB_EDGES = [0.5, 0.6, 0.7, 0.75, 0.8]
PROB_LABELS = ["<50%", "50-60%", "60-70%", "70-75%", "75-80%", ">80%"]
# the user's risk tiers (margin 10/7/5/3% of capital)
RISK_EDGES = [0.25, 0.30, 0.35, 0.40]
RISK_LABELS = ["fino al 25%", "25-30%", "30-35%", "35-40%", "oltre 40%"]
# predicted 4h amplitude (mean |4h return| of the paths); the breakout entry needs at least settings.min_move (1.5%)
AMP_EDGES = [0.005, 0.01, 0.015, 0.02, 0.03]
AMP_LABELS = ["<0.5%", "0.5-1%", "1-1.5%", "1.5-2%", "2-3%", ">3%"]
COSTS = 2 * (settings.taker_fee + settings.slippage)  # round trip, on the price


def _bucket(x: float | None, edges: list[float], labels: list[str]) -> str:
    if x is None:
        return "n/d"
    for e, lab in zip(edges, labels):
        if x < e:
            return lab
    return labels[-1]


class Controller:
    def __init__(self, bus) -> None:
        self.bus = bus
        self.db = sqlite3.connect(settings.db_path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        # databases made before these columns. signals.entry_ok now means "the roof was broken while this forecast
        # was in use" (it was "price at the predicted entry point" before the breakout entry)
        for table, col in (("calibration", "covered INTEGER"), ("signals", "amplitude REAL"), ("signals", "bo_price REAL"),
                           ("signals", "bo_due REAL"), ("signals", "bo_ret REAL")):
            try:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        self.db.commit()
        self.predictor = None  # set by main (for calibration)
        self.executor = None  # set by main (open positions in the report)

    # --------------------------------------------------------------- input
    def record_trade(self, p) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{p.id}-{int(p.opened_ts)}", p.symbol, p.base, "long" if p.side > 0 else "short", p.leverage, p.margin,
             p.entry, p.exit, p.opened_ts, p.exit_ts, p.reason, p.pnl, p.roe, p.fees, p.funding, p.mode, p.origin,
             json.dumps(p.signal, default=str)),
        )
        self.db.commit()
        self.bus.publish("report", self.report())

    def record_calibration(self, rows: list[dict]) -> None:
        if not rows:
            return
        now = time.time()
        self.db.executemany("INSERT INTO calibration (ts, label, symbol, p_up, ret, covered) VALUES (?,?,?,?,?,?)",
                            [(now, r["label"], r["symbol"], r["p_up"], r["ret"], r.get("covered")) for r in rows])
        self.db.commit()

    # --------------------------------------------------------------- signal register
    def record_signal(self, row: dict) -> None:
        """One row per new 4h forecast of a token (first Checker evaluation), executed or not."""
        self.db.execute(
            "INSERT OR IGNORE INTO signals (ts, due, symbol, price, p_up_4h, entry_ok, eligible, extra_risk, reasons,"
            " model, samples, amplitude) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["ts"], row["due"], row["symbol"], row["price"], row["p_up_4h"], int(row.get("breakout", False)),
             int(row["eligible"]), row.get("extra_risk"), json.dumps(row.get("reasons") or []), row.get("model", ""),
             row.get("samples", 0), row.get("amplitude")),
        )
        self.db.commit()

    def record_breakout(self, symbol: str, ts: float, price: float, due: float, eligible: bool) -> None:
        """The price broke the roof while the forecast `ts` was in use: the price then (the entry a long would get)
        and when its 4h result is due. A later eligible breakout of the same forecast replaces a non-eligible one."""
        self.db.execute(
            "UPDATE signals SET entry_ok = 1, eligible = MAX(eligible, ?), bo_price = ?, bo_due = ? "
            "WHERE symbol = ? AND ts = ? AND (bo_price IS NULL OR (? = 1 AND eligible = 0))",
            (int(eligible), price, due, symbol, ts, int(eligible)),
        )
        self.db.commit()

    def resolve_signals(self, price_of) -> int:
        """Fill in the real 4h return of the signals whose horizon has elapsed."""
        now = time.time()
        due = self.db.execute("SELECT symbol, ts, price FROM signals WHERE ret IS NULL AND due <= ?", (now,)).fetchall()
        done = 0
        for sym, ts, p0 in due:
            p1 = price_of(sym)
            if p1 and p0:
                self.db.execute("UPDATE signals SET ret = ? WHERE symbol = ? AND ts = ?", (p1 / p0 - 1, sym, ts))
                done += 1
        due = self.db.execute("SELECT symbol, ts, bo_price FROM signals WHERE bo_price IS NOT NULL AND bo_ret IS NULL "
                              "AND bo_due <= ?", (now,)).fetchall()
        for sym, ts, p0 in due:
            p1 = price_of(sym)
            if p1 and p0:
                self.db.execute("UPDATE signals SET bo_ret = ? WHERE symbol = ? AND ts = ?", (p1 / p0 - 1, sym, ts))
                done += 1
        if done:
            self.db.commit()
        return done

    def signal_stats(self) -> dict:
        """How the 4h forecasts did.
        `amplitude`: by predicted amplitude, the real size of the 4h move (|return|): the breakout entry relies on
        Kronos telling a large move from a small one. `all`: by long probability, from the forecast's price.
        `breakout` / `eligible`: from the price at the roof breakout (what a long would have got), all breakouts and
        those that passed every rule. `win` = the 4h return beat the round-trip costs."""
        rows = self.db.execute("SELECT p_up_4h, amplitude, ret FROM signals WHERE ret IS NOT NULL").fetchall()
        bo = self.db.execute("SELECT p_up_4h, eligible, bo_ret FROM signals WHERE bo_ret IS NOT NULL").fetchall()

        def agg(sel):
            out = []
            for lab in PROB_LABELS:
                rs = [r[-1] for r in sel if _bucket(r[0], PROB_EDGES, PROB_LABELS) == lab]
                if rs:
                    out.append({"key": lab, "n": len(rs), "up": sum(1 for r in rs if r > 0) / len(rs),
                                "win": sum(1 for r in rs if r > COSTS) / len(rs), "avg_ret": sum(rs) / len(rs)})
            return out

        amp = []
        for lab in AMP_LABELS:
            rs = [r for r in rows if r[1] is not None and _bucket(r[1], AMP_EDGES, AMP_LABELS) == lab]
            if rs:
                amp.append({"key": lab, "n": len(rs), "pred": sum(r[1] for r in rs) / len(rs),
                            "real": sum(abs(r[2]) for r in rs) / len(rs), "up": sum(1 for r in rs if r[2] > 0) / len(rs)})
        return {"amplitude": amp, "all": agg(rows), "breakout": agg(bo), "eligible": agg([r for r in bo if r[1]]),
                "pending": self.db.execute("SELECT COUNT(*) FROM signals WHERE ret IS NULL").fetchone()[0]}

    def open_view(self) -> dict | None:
        """Open positions: their unrealized result is part of the strategy's result (losers are never closed)."""
        if self.executor is None:
            return None
        ps = self.executor.open_positions()
        if not ps:
            return {"n": 0, "unrealized": 0.0, "losing": 0, "worst": None}
        worst = min(ps, key=lambda p: p.pnl)
        return {"n": len(ps), "unrealized": sum(p.pnl for p in ps), "losing": sum(1 for p in ps if p.pnl < 0),
                "worst": {"base": worst.base, "pnl": worst.pnl, "move": worst.side * ((worst.last_price or worst.entry) / worst.entry - 1),
                          "hours": (time.time() - worst.opened_ts) / 3600}}

    # --------------------------------------------------------------- analysis
    def trades(self, limit: int = 500) -> list[dict]:
        cur = self.db.execute("SELECT * FROM trades ORDER BY closed_ts DESC LIMIT ?", (limit,))
        cols = [c[0] for c in cur.description]
        out = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            d["signal"] = json.loads(d["signal"] or "{}")
            out.append(d)
        return out

    @staticmethod
    def _agg(rows: list[dict]) -> dict:
        n = len(rows)
        wins = sum(1 for r in rows if r["pnl"] > 0)
        pnl = sum(r["pnl"] for r in rows)
        gross_win = sum(r["pnl"] for r in rows if r["pnl"] > 0)
        gross_loss = -sum(r["pnl"] for r in rows if r["pnl"] <= 0)
        return {
            "n": n, "wins": wins, "win_rate": wins / n if n else 0.0, "pnl": pnl,
            "avg_roe": sum(r["roe"] for r in rows) / n if n else 0.0,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        }

    def _group(self, rows: list[dict], key) -> list[dict]:
        g: dict[str, list] = defaultdict(list)
        for r in rows:
            g[key(r)].append(r)
        out = [{"key": k, **self._agg(v)} for k, v in g.items()]
        out.sort(key=lambda x: (x["pnl"], x["win_rate"]), reverse=True)
        return out

    def calibration(self) -> list[dict]:
        cur = self.db.execute(
            "SELECT label, COUNT(*), AVG(CASE WHEN (p_up > 0.5) = (ret > 0) THEN 1.0 ELSE 0.0 END),"
            " AVG((p_up - (ret > 0)) * (p_up - (ret > 0))), AVG(covered) FROM calibration WHERE ret != 0 GROUP BY label"
        )
        order = ["10s", "30s", "1m", "5m", "15m", "30m", "1h", "4h"]
        # coverage: share of real returns inside the forecast's 10-90% band; ~80% if the cone is honest,
        # lower means the cone is too narrow (top_p cuts the tails)
        rows = [{"label": l, "n": n, "hit_rate": h, "brier": b, "coverage": c} for l, n, h, b, c in cur.fetchall()]
        rows.sort(key=lambda r: order.index(r["label"]) if r["label"] in order else 99)
        return rows

    def report(self) -> dict:
        rows = self.trades(5000)
        sig = lambda r, k: r["signal"].get(k)  # noqa: E731
        groups = {
            "per_token": self._group(rows, lambda r: r["base"]),
            "per_lato": self._group(rows, lambda r: r["side"]),
            "per_leva": self._group(rows, lambda r: f"{int(r['leverage'])}x"),
            "per_pattern": self._group(rows, lambda r: sig(r, "regime") or "n/d"),
            "per_prob_kronos": self._group(rows, lambda r: _bucket(sig(r, "conf_4h") or sig(r, "kronos_p_tp"), PROB_EDGES, PROB_LABELS)),
            "per_rischio": self._group(rows, lambda r: _bucket(sig(r, "extra_risk"), RISK_EDGES, RISK_LABELS)),
            "per_motivo_chiusura": self._group(rows, lambda r: r["reason"].split(" (")[0] if r["reason"] else "n/d"),
            "per_modalita": self._group(rows, lambda r: r["mode"]),
        }
        insights = self._insights(rows, groups)
        return {"summary": self._agg(rows), "groups": groups, "insights": insights,
                "calibration": self.calibration(), "signals": self.signal_stats(), "open": self.open_view(),
                "recent": rows[:50], "generated": time.time()}

    def _insights(self, rows, groups) -> list[str]:
        out = []
        ov = self.open_view()
        if ov and ov["losing"]:
            w = ov["worst"]
            out.append(f"{ov['losing']} posizioni aperte in perdita, risultato non realizzato {ov['unrealized']:+.2f} USDC: "
                       f"non è nel win rate delle operazioni chiuse. Peggiore {w['base']} {w['move'] * 100:+.1f}% da {w['hours']:.0f} h.")
        if len(rows) < 5:
            out.append(f"Storico ancora ridotto ({len(rows)} operazioni): servono almeno 20-30 chiusure per conclusioni affidabili.")
        for name, label in (("per_token", "token"), ("per_pattern", "pattern"), ("per_prob_kronos", "fascia di probabilità Kronos")):
            g = [x for x in groups[name] if x["n"] >= 3]
            if g:
                best, worst = g[0], g[-1]
                out.append(f"Miglior {label}: {best['key']} (win rate {best['win_rate'] * 100:.0f}%, PnL {best['pnl']:+.2f} USDC su {best['n']} operazioni).")
                if worst is not best and worst["pnl"] < 0:
                    out.append(f"Peggior {label}: {worst['key']} (win rate {worst['win_rate'] * 100:.0f}%, PnL {worst['pnl']:+.2f} USDC).")
        liq = [r for r in rows if r["reason"].startswith("liquidazione")]
        if liq:
            out.append(f"{len(liq)} liquidazioni: valutare leve più basse sui token più volatili.")
        an = [r for r in rows if r["reason"].startswith("Analyst")]
        if an:
            s = self._agg(an)
            out.append(f"{len(an)} chiusure forzate dall'Analyst (PnL complessivo {s['pnl']:+.2f} USDC).")
        return out

    def markdown(self) -> str:
        r = self.report()
        s = r["summary"]
        lines = [
            "# Report Controller — Price predictor",
            f"Generato il {datetime.now().strftime('%d/%m/%Y %H:%M')}",
            "",
            f"**Operazioni:** {s['n']} · **Win rate:** {s['win_rate'] * 100:.1f}% · **PnL:** {s['pnl']:+.2f} USDC · **ROE medio:** {s['avg_roe']:+.2f}%",
            "",
            "## Osservazioni",
            *[f"- {i}" for i in r["insights"]],
            "",
        ]
        titles = {"per_token": "Token", "per_lato": "Lato", "per_leva": "Leva", "per_pattern": "Pattern di mercato",
                  "per_prob_kronos": "Probabilità Kronos all'apertura", "per_rischio": "Rischio aggiuntivo all'apertura",
                  "per_motivo_chiusura": "Motivo di chiusura", "per_modalita": "Modalità"}
        for k, t in titles.items():
            lines += [f"## {t}", "", "| Gruppo | Operazioni | Win rate | PnL (USDC) | ROE medio |", "|---|---:|---:|---:|---:|"]
            for g in r["groups"][k]:
                lines.append(f"| {g['key']} | {g['n']} | {g['win_rate'] * 100:.0f}% | {g['pnl']:+.2f} | {g['avg_roe']:+.2f}% |")
            lines.append("")
        ov = r["open"]
        if ov is not None:
            lines += ["## Posizioni aperte", "", f"**Aperte:** {ov['n']} · **In perdita:** {ov['losing']} · **Risultato non realizzato:** {ov['unrealized']:+.2f} USDC", ""]
        lines += ["## Affidabilità delle previsioni (verificate a scadenza)", "",
                  "| Orizzonte | Previsioni | Direzione corretta | Brier | Copertura 10-90% (attesa ~80%) |", "|---|---:|---:|---:|---:|"]
        for c in r["calibration"]:
            cov = f"{c['coverage'] * 100:.0f}%" if c.get("coverage") is not None else "n/d"
            lines.append(f"| {c['label']} | {c['n']} | {c['hit_rate'] * 100:.1f}% | {c['brier']:.3f} | {cov} |")
        sg = r["signals"]
        lines += ["", "## Registro segnali: ampiezza prevista contro movimento reale a 4 ore", "",
                  "| Ampiezza prevista | Previsioni | Media prevista | Movimento reale medio | Rialzo |", "|---|---:|---:|---:|---:|"]
        for g in sg["amplitude"]:
            lines.append(f"| {g['key']} | {g['n']} | {g['pred'] * 100:.2f}% | {g['real'] * 100:.2f}% | {g['up'] * 100:.0f}% |")
        for key, title in (("all", "tutte le previsioni, dal prezzo della previsione"),
                           ("breakout", "rotture del tetto, dal prezzo di rottura"),
                           ("eligible", "rotture idonee per l'Executor, dal prezzo di rottura")):
            lines += ["", f"## Registro segnali a 4 ore: {title}", "",
                      "| Probabilità long | Segnali | Rialzo a 4 ore | Oltre i costi | Ritorno medio |", "|---|---:|---:|---:|---:|"]
            for g in sg[key]:
                lines.append(f"| {g['key']} | {g['n']} | {g['up'] * 100:.0f}% | {g['win'] * 100:.0f}% | {g['avg_ret'] * 100:+.2f}% |")
        lines.append(f"\nSegnali in attesa di scadenza: {sg['pending']}")
        return "\n".join(lines) + "\n"

    async def run(self) -> None:
        while True:
            try:
                if self.predictor is not None:
                    rows = self.predictor.calib.resolve(self.predictor.feed.store.last_price)
                    self.record_calibration(rows)
                    self.resolve_signals(self.predictor.feed.store.last_price)
                self.bus.publish("report", self.report())
            except Exception:
                log.exception("controller cycle failed")
            await asyncio.sleep(10)
