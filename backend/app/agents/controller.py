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
"""


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
        self.db.commit()
        self.predictor = None  # set by main (for calibration)

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
        self.db.executemany("INSERT INTO calibration VALUES (?,?,?,?,?)",
                            [(now, r["label"], r["symbol"], r["p_up"], r["ret"]) for r in rows])
        self.db.commit()

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
            " AVG((p_up - (ret > 0)) * (p_up - (ret > 0))) FROM calibration WHERE ret != 0 GROUP BY label"
        )
        order = ["10s", "30s", "1m", "5m", "15m", "30m", "1h", "4h"]
        rows = [{"label": l, "n": n, "hit_rate": h, "brier": b} for l, n, h, b in cur.fetchall()]
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
            "per_prob_kronos": self._group(rows, lambda r: _bucket(sig(r, "kronos_p_tp"), [0.4, 0.5, 0.6, 0.7], ["<40%", "40-50%", "50-60%", "60-70%", ">70%"])),
            "per_rischio": self._group(rows, lambda r: _bucket(sig(r, "extra_risk"), [0.3, 0.45, 0.6], ["basso <30%", "medio 30-45%", "alto 45-60%", ">60%"])),
            "per_motivo_chiusura": self._group(rows, lambda r: r["reason"].split(" (")[0] if r["reason"] else "n/d"),
            "per_modalita": self._group(rows, lambda r: r["mode"]),
        }
        insights = self._insights(rows, groups)
        return {"summary": self._agg(rows), "groups": groups, "insights": insights,
                "calibration": self.calibration(), "recent": rows[:50], "generated": time.time()}

    def _insights(self, rows, groups) -> list[str]:
        out = []
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
        lines += ["## Affidabilità delle previsioni (verificate a scadenza)", "", "| Orizzonte | Previsioni | Direzione corretta | Brier |", "|---|---:|---:|---:|"]
        for c in r["calibration"]:
            lines.append(f"| {c['label']} | {c['n']} | {c['hit_rate'] * 100:.1f}% | {c['brier']:.3f} |")
        return "\n".join(lines) + "\n"

    async def run(self) -> None:
        while True:
            try:
                if self.predictor is not None:
                    rows = self.predictor.calib.resolve(self.predictor.feed.store.last_price)
                    self.record_calibration(rows)
                self.bus.publish("report", self.report())
            except Exception:
                log.exception("controller cycle failed")
            await asyncio.sleep(10)
