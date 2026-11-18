"""Walk-forward backtest of the Executor's 4-hour long rules on Raydium Perps (Orderly) history.

What it measures
  1. Calibration: for every forecast, the Kronos long probability at 4 h against the real 4 h return, by bucket
     (50-60%, 60-70%, 70-75%, 75-80%, >80%), with the share above the round-trip costs and the 10-90% coverage.
  2. The strategy as it runs live: entry only above 70% at 4 h, median above price + costs, BTC not
     falling over 4 h / 24 h, entry when the price reaches the predicted entry point (the median of the paths'
     lows) within 4 h, a fresh confirmation forecast there with more samples; exits by the live rules: protective
     stop at +2% NET on the margin armed 1 ATR above it, close on a confirmed 4 h reversal only when in profit,
     losing positions never closed (liquidation only).
  3. The same exits after RANDOM entries on the same tokens: if the strategy does not beat it, the forecast is
     not adding anything.

Not replicated: the Checker's extra-risk score (indicators, historical analogs, Analyst macro risk), so every trade
uses one leverage (--lev) and the result is reported per trade on the margin, not on a sized account.
Reversal checks happen at each scheduled forecast (--every bars), not every 5 minutes as live.

Run from backend/:   python -m tools.backtest_4h            (Kronos-base, 10 tokens, 30 days, one forecast every 4 h)
Forecasts are cached in data/backtest/ so a second run with other rules costs nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd

from app.agents.indicators import atr_pct
from app.config import DATA_DIR, settings
from app.predict.engine import KronosEngine, SeriesInput, monte_carlo_paths
from app.predict.probability import predicted_dip
from app.predict.service import MODELS, _volumes

BAR = 900
STEP4 = 16
PRED = 18  # as live: 4 h + 2 steps of slack
COSTS = 2 * (settings.taker_fee + settings.slippage)
OUT = DATA_DIR / "backtest"


# ------------------------------------------------------------------ data
async def fetch(symbols: list[str], days: int) -> dict[str, np.ndarray]:
    import httpx

    out = {}
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "price-predictor-backtest/1.0"}) as cli:
        for sym in symbols:
            rows: dict[int, list] = {}
            to = int(time.time() // BAR * BAR)
            start = to - days * 86400 - 600 * BAR  # + context and warm-up
            while to > start:
                r = await cli.get(f"{settings.orderly_api}/v1/tv/kline_history",
                                  params={"symbol": sym, "resolution": "15m", "from": max(start, to - 1000 * BAR), "to": to})
                d = r.json()
                if d.get("s") != "ok" or not d.get("t"):
                    break
                for t, o, h, l, c, v in zip(d["t"], d["o"], d["h"], d["l"], d["c"], d["v"]):
                    rows[int(t)] = [t, o, h, l, c, v]
                to = int(min(d["t"])) - 1
                await asyncio.sleep(0.4)
            bars = np.array([rows[k] for k in sorted(rows)], dtype=float)
            bars = bars[bars[:, 0] + BAR <= time.time()]  # closed candles only
            out[sym] = bars
            print(f"  {sym}: {len(bars)} candles da 15 minuti")
    return out


async def universe(top: int) -> list[str]:
    from app.market.feed import MarketFeed

    feed = MarketFeed()
    await feed.build_universe()
    ranked = sorted(feed.markets.values(), key=lambda m: m.rank)
    return [m.symbol for m in ranked[:top]]


# ------------------------------------------------------------------ forecasts
def forecast_summaries(engine: KronosEngine | None, sym: str, bars: np.ndarray, idx: list[int], lookback: int,
                       samples: int, batch: int, cache_dir: Path, tag: str) -> dict[int, dict]:
    """Kronos at the close of each bar in `idx`: the numbers the rules need, cached on disk."""
    path = cache_dir / f"{sym}_{tag}.json"
    cache = {int(k): v for k, v in json.loads(path.read_text()).items()} if path.exists() else {}
    todo = [i for i in idx if i not in cache and i >= lookback]
    for k in range(0, len(todo), batch):
        chunk = todo[k:k + batch]
        series, prices = [], []
        for i in chunk:
            w = bars[i - lookback + 1:i + 1]
            ts = pd.to_datetime(w[:, 0], unit="s")
            fut = pd.to_datetime(w[-1, 0] + BAR * np.arange(1, PRED + 1), unit="s")
            series.append(SeriesInput(w[:, 1:5], ts, fut, _volumes(w[:, 5])))
            prices.append(float(w[-1, 4]))
        paths_list = engine.sample_paths(series, PRED, samples, settings.temperature, settings.top_p) if engine else [None] * len(chunk)
        for i, s, paths, price in zip(chunk, series, paths_list, prices):
            if paths is None:
                paths = monte_carlo_paths(s.ohlc, PRED, samples)
            c4 = paths[:, STEP4 - 1, 3]
            dip, dip_at = predicted_dip(paths, STEP4, settings.dip_quantile)
            r4 = c4 / price - 1
            cache[i] = {"p4": float((c4 > price).mean()), "med4": float(np.median(c4)),
                        "q10": float(np.quantile(r4, 0.1)), "q90": float(np.quantile(r4, 0.9)), "dip": dip, "dip_at": dip_at,
                        "price": price}
        path.write_text(json.dumps(cache))
        print(f"  {sym} [{tag}]: {min(k + batch, len(todo))}/{len(todo)} previsioni", end="\r")
    if todo:
        print()
    return cache


# ------------------------------------------------------------------ rules
def btc_ok(btc: np.ndarray | None, t: float) -> bool:
    if btc is None:
        return True
    j = int(np.searchsorted(btc[:, 0], t, side="right")) - 1
    if j < 96:
        return True
    c = btc[:, 4]
    return c[j] / c[j - 16] - 1 >= -settings.btc_filter_4h and c[j] / c[j - 96] - 1 >= -settings.btc_filter_24h


def simulate(bars: np.ndarray, entry_i: int, entry: float, lev: float, checks: dict[int, dict]) -> dict:
    """One long from bar `entry_i` (filled at `entry`) under the live exit rules. ROE on the margin, net."""
    fee, slip = settings.taker_fee, settings.slippage
    e = entry * (1 + slip)
    move = settings.lock_profit_pct / 100 / lev + 2 * fee
    level = e * (1 + move) / (1 - slip)
    # cross margin sized as live: the liquidation stays at least liq_min_distance (50%) below the entry; a loss on
    # the margin can therefore exceed -100% (it is paid by the rest of the account)
    liq = e * (1 - settings.liq_min_distance)
    locked, rev = False, 0
    for i in range(entry_i + 1, len(bars)):
        o, h, l, c = bars[i, 1:5]
        if l <= liq:
            return {"exit": "liquidazione", "roe": ((liq * (1 - slip) / e - 1) - 2 * fee) * lev * 100, "bars": i - entry_i}
        if locked and l <= level:
            px = min(o, level) * (1 - slip)
            return {"exit": "stop di protezione", "roe": ((px / e - 1) - 2 * fee) * lev * 100, "bars": i - entry_i}
        if not locked:
            atr = max(atr_pct(bars[max(0, i - 40):i]) if i > 15 else 0.0, 0.002)
            if h >= level * (1 + settings.lock_atr_mult * atr):
                locked = True
        f = checks.get(i)
        if f is not None:
            rev = rev + 1 if f["p4"] < 0.5 - settings.exit_margin else 0
            net = (c * (1 - slip) / e - 1) - 2 * fee
            if rev >= settings.exit_confirmations and net >= 0:
                return {"exit": "inversione", "roe": net * lev * 100, "bars": i - entry_i}
    c = bars[-1, 4]
    return {"exit": "ancora aperta", "roe": ((c * (1 - slip) / e - 1) - 2 * fee) * lev * 100, "bars": len(bars) - 1 - entry_i}


def run_rules(sym, bars, fc, conf_fc, btc, start_i, lev, engine, args, cache_dir):
    """Signals, entries and exits for one token under the live rules."""
    trades = []
    busy_until = -1
    sched = sorted(i for i in fc if i >= start_i)
    for i in sched:
        if i <= busy_until or i + STEP4 >= len(bars):
            continue
        f = fc[i]
        if not (f["p4"] > settings.min_confidence
                and f["med4"] > f["price"] * (1 + COSTS) and btc_ok(btc, bars[i, 0])):
            continue
        target = f["dip"] * (1 + settings.entry_tolerance)
        hit = next((j for j in range(i + 1, min(i + 1 + STEP4, len(bars))) if bars[j, 3] <= target), None)
        if hit is None:
            continue
        if args.confirm:  # fresh forecast with more samples at the close of the bar that reached the entry point
            cf = forecast_summaries(engine, sym, bars, [hit], args.lookback, settings.decision_samples, 1, cache_dir,
                                    f"{args.model}_confirm")[hit]
            if not (cf["p4"] > settings.min_confidence and cf["med4"] > cf["price"] * (1 + COSTS)):
                continue
        entry = min(bars[hit, 1], target)
        t = simulate(bars, hit, entry, lev, fc)
        t.update({"symbol": sym, "t": int(bars[hit, 0]), "p4": f["p4"]})
        trades.append(t)
        busy_until = hit + t["bars"]  # one position per token, as live
    return trades


def random_trades(sym, bars, fc, start_i, lev, n, rng):
    out = []
    pool = [i for i in fc if i >= start_i and i + STEP4 < len(bars)]
    for i in rng.choice(pool, size=min(n, len(pool)), replace=False) if pool else []:
        t = simulate(bars, int(i), float(bars[i, 4]), lev, fc)
        t.update({"symbol": sym, "t": int(bars[i, 0])})
        out.append(t)
    return out


# ------------------------------------------------------------------ report
EDGES = [(0.5, "<50%"), (0.6, "50-60%"), (0.7, "60-70%"), (0.75, "70-75%"), (0.8, "75-80%"), (9, ">80%")]


def bucket(p):
    return next(lab for e, lab in EDGES if p < e)


def calibration(data, fcs, start):
    rows = {lab: [] for _, lab in EDGES}
    for sym, fc in fcs.items():
        b = data[sym]
        for i, f in fc.items():
            if i < start[sym] or i + STEP4 >= len(b):
                continue
            r = b[i + STEP4, 4] / f["price"] - 1
            rows[bucket(f["p4"])].append((r, f["q10"] <= r <= f["q90"]))
    lines = ["| Probabilità long a 4 ore | Previsioni | Rialzo reale | Oltre i costi | Ritorno medio | Copertura 10-90% |",
             "|---|---:|---:|---:|---:|---:|"]
    for lab, rs in rows.items():
        if rs:
            r = np.array([x[0] for x in rs])
            lines.append(f"| {lab} | {len(rs)} | {(r > 0).mean() * 100:.1f}% | {(r > COSTS).mean() * 100:.1f}% | "
                         f"{r.mean() * 100:+.3f}% | {np.mean([x[1] for x in rs]) * 100:.0f}% |")
    return lines


def trade_table(title, trades):
    if not trades:
        return [f"### {title}", "", "Nessuna operazione.", ""]
    roe = np.array([t["roe"] for t in trades])
    closed = [t for t in trades if t["exit"] != "ancora aperta"]
    lines = [f"### {title}", "",
             f"Operazioni {len(trades)} · ROE medio {roe.mean():+.2f}% · mediana {np.median(roe):+.2f}% · "
             f"peggiore {roe.min():+.1f}% · chiuse in profitto {sum(t['roe'] > 0 for t in closed)}/{len(closed)}", "",
             "| Uscita | Operazioni | ROE medio | Durata media (ore) |", "|---|---:|---:|---:|"]
    for ex in ("stop di protezione", "inversione", "liquidazione", "ancora aperta"):
        ts = [t for t in trades if t["exit"] == ex]
        if ts:
            lines.append(f"| {ex} | {len(ts)} | {np.mean([t['roe'] for t in ts]):+.2f}% | {np.mean([t['bars'] for t in ts]) / 4:.1f} |")
    return lines + [""]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--top", type=int, default=10, help="first N tokens by market cap with a perp")
    ap.add_argument("--symbols", default="", help="comma-separated Orderly symbols, instead of --top")
    ap.add_argument("--model", choices=list(MODELS), default="base")
    ap.add_argument("--lookback", type=int, default=0, help="15m candles of context (0 = what the live engine uses)")
    ap.add_argument("--samples", type=int, default=settings.n_samples)
    ap.add_argument("--every", type=int, default=16, help="bars between two forecasts of a token (16 = every 4 h)")
    ap.add_argument("--batch", type=int, default=8, help="forecasts per Kronos call")
    ap.add_argument("--lev", type=float, default=15)
    ap.add_argument("--no-confirm", dest="confirm", action="store_false")
    ap.add_argument("--offline", action="store_true", help="no Kronos: bootstrap paths (checks the pipeline only)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    if not args.lookback:
        args.lookback = settings.base_lookback if args.model == "base" else settings.lookback

    cache_dir = OUT
    cache_dir.mkdir(parents=True, exist_ok=True)
    syms = [s for s in args.symbols.split(",") if s] or asyncio.run(universe(args.top))
    print(f"Token: {', '.join(syms)}")
    need = list(dict.fromkeys(syms + ["PERP_BTC_USDC"]))
    data = asyncio.run(fetch(need, args.days))
    engine = None
    if not args.offline:
        name, tok, ctx, _ = MODELS[args.model]
        engine = KronosEngine(name, tok, ctx)
        engine.load()
        if not engine.ready:
            raise SystemExit(f"Kronos non caricato: {engine.error}")
    tag = f"{args.model}_lb{args.lookback}_s{args.samples}" + ("_offline" if args.offline else "")

    fcs, start = {}, {}
    t0 = time.time()
    for sym in syms:
        b = data[sym]
        first = max(args.lookback, len(b) - args.days * 96)
        start[sym] = first
        idx = list(range(first, len(b), args.every))
        fcs[sym] = forecast_summaries(engine, sym, b, idx, args.lookback, args.samples, args.batch, cache_dir, tag)
    elapsed = time.time() - t0

    rng = np.random.default_rng(args.seed)
    strat, rand = [], []
    for sym in syms:
        tr = run_rules(sym, data[sym], fcs[sym], None, data.get("PERP_BTC_USDC"), start[sym], args.lev, engine, args, cache_dir)
        strat += tr
        rand += random_trades(sym, data[sym], fcs[sym], start[sym], args.lev, max(len(tr), 5), rng)

    lines = [f"# Backtest regole a 4 ore ({time.strftime('%d/%m/%Y %H:%M')})", "",
             f"Modello Kronos-{args.model}, {args.samples} campioni, contesto {args.lookback} candele, una previsione ogni "
             f"{args.every * 15} minuti per token, {args.days} giorni, {len(syms)} token, leva {args.lev:g}x, costi andata e "
             f"ritorno {COSTS * 100:.2f}% del nozionale. Filtro BTC: -{settings.btc_filter_4h * 100:g}% a 4 ore, "
             f"-{settings.btc_filter_24h * 100:g}% a 24 ore. Previsioni in {elapsed / 60:.1f} minuti."
             + (" MODALITÀ OFFLINE: percorsi bootstrap, non Kronos." if args.offline else ""), "",
             "## Calibrazione: la probabilità a 4 ore contro il ritorno reale", ""]
    lines += calibration(data, fcs, start)
    lines += ["", "## Strategia contro ingressi casuali (stesse uscite)", ""]
    lines += trade_table("Regole dell'Executor", strat)
    lines += trade_table("Ingressi casuali", rand)
    if strat and rand:
        d = np.mean([t["roe"] for t in strat]) - np.mean([t["roe"] for t in rand])
        se = math.sqrt(np.var([t["roe"] for t in strat]) / len(strat) + np.var([t["roe"] for t in rand]) / len(rand))
        lines.append(f"Differenza di ROE medio strategia meno casuale: {d:+.2f} punti (errore standard {se:.2f}).")
    report = "\n".join(lines) + "\n"
    out = cache_dir / f"report_{tag}_{args.days}d.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"Rapporto salvato in {out}")


if __name__ == "__main__":
    main()
