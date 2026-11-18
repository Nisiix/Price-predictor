"""Walk-forward backtest of the Executor's 4-hour BREAKOUT longs on Raydium Perps (Orderly) history.

The idea under test (not verified on real data before this run): Kronos is better at telling WHEN a large move is
coming than its direction, so a long is opened only once a large predicted move has started upwards, i.e. once the
price breaks the last hour's high (the roof). Nothing is opened if the price falls instead.

What it measures
  1. Amplitude: for every forecast, the predicted 4h amplitude (mean |4h return| of the paths) against the real
     |4h return|, by bucket, and its rank correlation with the real move next to the naive guess "as large as the
     last 4 hours' move". If Kronos does not beat the naive guess, the amplitude filter adds nothing.
  2. Probability calibration: the Kronos 4h long probability against the real 4h return, by bucket.
  3. The live rules: predicted amplitude >= PP_MIN_MOVE (1.5%), Kronos not against the long (> 50% from the fill
     price), BTC not falling over 4 h / 24 h, entry when a 15m candle trades above roof x (1 + 0.1%) while the
     forecast is at most 45 minutes old, filled at that level (or at the open if it gapped above, skipped if more
     than 0.5% above the roof), a fresh confirmation forecast with more samples on the candles closed before the
     breakout. Exits as live: protective stop at +2% NET on the margin armed 1 ATR above it, close on a confirmed
     4h reversal only when not at a loss, break-even exit (net break-even or better while the forecast is bearish at
     15 min, 1 h and 4 h), losing positions never closed (liquidation only, cross margin at 50% from the entry).
  4. The same entries WITHOUT the break-even exit, the same rules WITHOUT the BTC filter (and the breakouts it
     blocked), every breakout WITHOUT the Kronos filters, and RANDOM entries, all with the same exits: what the
     forecast, the BTC filter and the break-even exit each add.

Not replicated: the Checker's extra-risk score (indicators, historical analogs, Analyst macro risk), so every trade
uses one leverage (--lev) and the result is reported per trade on the margin, not on a sized account. Exit checks
(reversal, break-even) use the scheduled forecasts (--every bars) instead of a new forecast every 5 minutes as live;
--every 4 is closer to live and costs twice as much as the default 8.

Run from backend/:   python -m tools.backtest_4h      (Kronos-base, 10 tokens, 30 days, one forecast every 2 h)
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
def forecast_paths(engine: KronosEngine | None, sym: str, bars: np.ndarray, idx: list[int], lookback: int,
                   samples: int, batch: int, cache_dir: Path, tag: str) -> dict[int, dict]:
    """Kronos at the close of each bar in `idx`: the sampled closes of every step (S x 18) and the anchor price,
    cached on disk. Every rule reads them like the live engine: re-anchored on the price, read `k` steps ahead."""
    path = cache_dir / f"{sym}_{tag}.json"
    cache = {int(k): v for k, v in json.loads(path.read_text()).items()} if path.exists() else {}
    todo = [i for i in idx if i not in cache and i >= lookback]
    for k in range(0, len(todo), batch):
        chunk = todo[k:k + batch]
        series = []
        for i in chunk:
            w = bars[i - lookback + 1:i + 1]
            ts = pd.to_datetime(w[:, 0], unit="s")
            fut = pd.to_datetime(w[-1, 0] + BAR * np.arange(1, PRED + 1), unit="s")
            series.append(SeriesInput(w[:, 1:5], ts, fut, _volumes(w[:, 5])))
        paths_list = engine.sample_paths(series, PRED, samples, settings.temperature, settings.top_p) if engine else [None] * len(chunk)
        for i, s_, paths in zip(chunk, series, paths_list):
            if paths is None:
                paths = monte_carlo_paths(s_.ohlc, PRED, samples)
            cache[i] = {"price": float(bars[i, 4]), "cl": np.round(paths[:, :, 3], 8).tolist()}
        path.write_text(json.dumps(cache))
        print(f"  {sym} [{tag}]: {min(k + batch, len(todo))}/{len(todo)} previsioni", end="\r")
    if todo:
        print()
    for v in cache.values():
        if not isinstance(v["cl"], np.ndarray):
            v["cl"] = np.asarray(v["cl"], dtype=float)
    return cache


def p_up(f: dict, k: int, ref: float) -> float:
    """Long probability `k`+1 steps ahead against price `ref` (paths re-anchored on the forecast's price)."""
    return float((f["cl"][:, k] > ref).mean())


def amplitude(f: dict, k: int) -> float:
    return float(np.abs(f["cl"][:, k] / f["price"] - 1).mean())


# ------------------------------------------------------------------ rules
def btc_ok(btc: np.ndarray | None, t: float) -> bool:
    if btc is None:
        return True
    j = int(np.searchsorted(btc[:, 0], t, side="right")) - 1
    if j < 96:
        return True
    c = btc[:, 4]
    return c[j] / c[j - 16] - 1 >= -settings.btc_filter_4h and c[j] / c[j - 96] - 1 >= -settings.btc_filter_24h


def simulate(bars: np.ndarray, entry_i: int, entry: float, lev: float, checks: dict[int, dict], be_exit: bool = True) -> dict:
    """One long filled at `entry` during bar `entry_i`, under the live exit rules. ROE on the margin, net."""
    fee, slip = settings.taker_fee, settings.slippage
    e = entry * (1 + slip)
    move = settings.lock_profit_pct / 100 / lev + 2 * fee
    level = e * (1 + move) / (1 - slip)
    be_px = e * (1 + 2 * fee) / (1 - slip)  # closing here nets zero
    # cross margin sized as live: the liquidation stays at least liq_min_distance (50%) below the entry; a loss on
    # the margin can therefore exceed -100% (it is paid by the rest of the account)
    liq = e * (1 - settings.liq_min_distance)
    locked, rev, bearish = False, 0, False
    roe = lambda px: ((px * (1 - slip) / e - 1) - 2 * fee) * lev * 100  # noqa: E731
    for i in range(entry_i, len(bars)):
        o, h, l, c = bars[i, 1:5]
        if i > entry_i:
            if l <= liq:
                return {"exit": "liquidazione", "roe": roe(liq), "bars": i - entry_i}
            if locked and l <= level:
                return {"exit": "stop di protezione", "roe": roe(min(o, level)), "bars": i - entry_i}
            if bearish and h >= be_px:
                return {"exit": "uscita in pari", "roe": roe(max(o, be_px)), "bars": i - entry_i}
        if not locked:
            atr = max(atr_pct(bars[max(0, i - 40):i]) if i > 15 else 0.0, 0.002)
            if h >= level * (1 + settings.lock_atr_mult * atr):
                locked = True
        f = checks.get(i)
        if f is not None:  # a new forecast at this candle's close
            rev = rev + 1 if p_up(f, STEP4 - 1, f["price"]) < 0.5 - settings.exit_margin else 0
            net = (c * (1 - slip) / e - 1) - 2 * fee
            if rev >= settings.exit_confirmations and net >= 0:
                return {"exit": "inversione", "roe": net * lev * 100, "bars": i - entry_i}
            bearish = be_exit and all(p_up(f, k, f["price"]) < settings.be_exit_max_up for k in (0, 3, STEP4 - 1))
            if bearish and net >= 0:
                return {"exit": "uscita in pari", "roe": net * lev * 100, "bars": i - entry_i}
    c = bars[-1, 4]
    return {"exit": "ancora aperta", "roe": roe(c), "bars": len(bars) - 1 - entry_i}


def breakouts(bars: np.ndarray, fc: dict[int, dict], start_i: int, every: int):
    """Every candle that trades above its roof while a forecast at most 45 minutes old is in use:
    (bar, forecast bar, steps elapsed, roof, fill price)."""
    age = max(1, int(settings.max_forecast_age_s // BAR))
    for i in sorted(x for x in fc if x >= start_i):
        for j in range(i + 1, min(i + 1 + min(every, age), len(bars) - STEP4)):
            if j < settings.roof_bars:
                continue
            roof = float(bars[j - settings.roof_bars:j, 2].max())
            trigger = roof * (1 + settings.breakout_buffer)
            if bars[j, 2] > trigger:
                fill = max(float(bars[j, 1]), trigger)
                if fill <= roof * (1 + settings.breakout_max_chase):
                    yield j, i, j - i - 1, roof, fill


def run_rules(sym, bars, fc, btc, start_i, lev, engine, args, cache_dir, kronos=True, be_exit=True):
    """Breakout entries and exits for one token under the live rules (kronos=False: every breakout, no forecast
    filter; the exits still use the forecasts)."""
    trades, busy_until = [], -1
    for j, i, el, roof, fill in breakouts(bars, fc, start_i, args.every):
        if j <= busy_until or not btc_ok(btc, bars[j, 0]):
            continue
        f, k4 = fc[i], STEP4 - 1 + el  # 4 hours ahead of the breakout on that older forecast
        if kronos:
            if not (amplitude(f, k4) >= settings.min_move and p_up(f, k4, fill) > settings.min_confidence):
                continue
            if args.confirm:  # fresh forecast with more samples on the candles closed before the breakout
                cf = forecast_paths(engine, sym, bars, [j - 1], args.lookback, settings.decision_samples, 1, cache_dir,
                                    f"{args.tag}_confirm")[j - 1]
                if not (amplitude(cf, STEP4 - 1) >= settings.min_move and p_up(cf, STEP4 - 1, cf["price"]) > settings.min_confidence):
                    continue
        t = simulate(bars, j, fill, lev, fc, be_exit)
        t.update({"symbol": sym, "t": int(bars[j, 0]), "amp": amplitude(f, k4), "p4": p_up(f, k4, fill),
                  "ret4": float(bars[j + STEP4, 4] / fill - 1)})
        trades.append(t)
        busy_until = j + t["bars"]  # one position per token, as live
    return trades


def random_trades(sym, bars, fc, start_i, lev, n, rng):
    out = []
    pool = [i for i in fc if i >= start_i and i + STEP4 < len(bars)]
    for i in rng.choice(pool, size=min(n, len(pool)), replace=False) if pool else []:
        t = simulate(bars, int(i) + 1, float(bars[int(i) + 1, 1]), lev, fc)
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
            q10, q90 = np.quantile(f["cl"][:, STEP4 - 1] / f["price"] - 1, [0.1, 0.9])
            rows[bucket(p_up(f, STEP4 - 1, f["price"]))].append((r, q10 <= r <= q90))
    lines = ["| Probabilità long a 4 ore | Previsioni | Rialzo reale | Oltre i costi | Ritorno medio | Copertura 10-90% |",
             "|---|---:|---:|---:|---:|---:|"]
    for lab, rs in rows.items():
        if rs:
            r = np.array([x[0] for x in rs])
            lines.append(f"| {lab} | {len(rs)} | {(r > 0).mean() * 100:.1f}% | {(r > COSTS).mean() * 100:.1f}% | "
                         f"{r.mean() * 100:+.3f}% | {np.mean([x[1] for x in rs]) * 100:.0f}% |")
    return lines


AMP = [(0.005, "<0.5%"), (0.01, "0.5-1%"), (0.015, "1-1.5%"), (0.02, "1.5-2%"), (0.03, "2-3%"), (9, ">3%")]


def _rank_corr(a, b) -> float:
    if len(a) < 3:
        return float("nan")
    ra, rb = pd.Series(a).rank().to_numpy(), pd.Series(b).rank().to_numpy()
    return float(np.corrcoef(ra, rb)[0, 1])


def amplitude_table(data, fcs, start):
    """Does Kronos tell a large 4h move from a small one? Predicted amplitude against the real |4h return|."""
    pred, real, naive = [], [], []
    rows = {lab: [] for _, lab in AMP}
    for sym, fc in fcs.items():
        b = data[sym]
        for i, f in fc.items():
            if i < start[sym] or i + STEP4 >= len(b) or i < STEP4:
                continue
            a, r = amplitude(f, STEP4 - 1), abs(b[i + STEP4, 4] / f["price"] - 1)
            pred.append(a)
            real.append(r)
            naive.append(abs(f["price"] / b[i - STEP4, 4] - 1))  # the last 4 hours' move, as the guess
            rows[next(lab for e, lab in AMP if a < e)].append(r)
    lines = ["| Ampiezza prevista a 4 ore | Previsioni | Movimento reale medio | Reale oltre l'1,5% |", "|---|---:|---:|---:|"]
    for lab, rs in rows.items():
        if rs:
            r = np.array(rs)
            lines.append(f"| {lab} | {len(rs)} | {r.mean() * 100:.2f}% | {(r >= settings.min_move).mean() * 100:.0f}% |")
    lines += ["", f"Correlazione di rango tra ampiezza prevista e movimento reale: Kronos {_rank_corr(pred, real):+.2f}, "
                  f"stima ingenua (movimento delle 4 ore precedenti) {_rank_corr(naive, real):+.2f}. "
                  "Se Kronos non supera la stima ingenua, il filtro sull'ampiezza non aggiunge nulla."]
    return lines


EXITS = ("stop di protezione", "inversione", "uscita in pari", "liquidazione", "ancora aperta")


def trade_table(title, trades):
    if not trades:
        return [f"### {title}", "", "Nessuna operazione.", ""]
    roe = np.array([t["roe"] for t in trades])
    closed = [t for t in trades if t["exit"] != "ancora aperta"]
    lines = [f"### {title}", "",
             f"Operazioni {len(trades)} · ROE medio {roe.mean():+.2f}% · mediana {np.median(roe):+.2f}% · "
             f"peggiore {roe.min():+.1f}% · chiuse in profitto {sum(t['roe'] > 0 for t in closed)}/{len(closed)}"
             + (f" · rialzo a 4 ore dall'ingresso {np.mean([t['ret4'] > COSTS for t in trades]) * 100:.0f}% oltre i costi"
                if "ret4" in trades[0] else ""), "",
             "| Uscita | Operazioni | ROE medio | Durata media (ore) |", "|---|---:|---:|---:|"]
    for ex in EXITS:
        ts = [t for t in trades if t["exit"] == ex]
        if ts:
            lines.append(f"| {ex} | {len(ts)} | {np.mean([t['roe'] for t in ts]):+.2f}% | {np.mean([t['bars'] for t in ts]) / 4:.1f} |")
    return lines + [""]


def diff_line(name, a, b):
    if not a or not b:
        return []
    d = np.mean([t["roe"] for t in a]) - np.mean([t["roe"] for t in b])
    se = math.sqrt(np.var([t["roe"] for t in a]) / len(a) + np.var([t["roe"] for t in b]) / len(b))
    return [f"- {name}: {d:+.2f} punti di ROE medio (errore standard {se:.2f}).", ""]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--top", type=int, default=10, help="first N tokens by market cap with a perp")
    ap.add_argument("--symbols", default="", help="comma-separated Orderly symbols, instead of --top")
    ap.add_argument("--model", choices=list(MODELS), default="base")
    ap.add_argument("--lookback", type=int, default=0, help="15m candles of context (0 = what the live engine uses)")
    ap.add_argument("--samples", type=int, default=settings.n_samples)
    ap.add_argument("--every", type=int, default=8, help="bars between two forecasts of a token (8 = every 2 h)")
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
    args.tag = f"v2_{args.model}_lb{args.lookback}" + ("_offline" if args.offline else "")
    tag = f"{args.tag}_s{args.samples}"

    fcs, start = {}, {}
    t0 = time.time()
    for sym in syms:
        b = data[sym]
        first = max(args.lookback, len(b) - args.days * 96)
        start[sym] = first
        idx = list(range(first, len(b), args.every))
        fcs[sym] = forecast_paths(engine, sym, b, idx, args.lookback, args.samples, args.batch, cache_dir, tag)

    rng = np.random.default_rng(args.seed)
    strat, no_be, plain, rand, no_btc = [], [], [], [], []
    btc = data.get("PERP_BTC_USDC")
    for sym in syms:
        b, fc = data[sym], fcs[sym]
        tr = run_rules(sym, b, fc, btc, start[sym], args.lev, engine, args, cache_dir)
        strat += tr
        no_be += run_rules(sym, b, fc, btc, start[sym], args.lev, engine, args, cache_dir, be_exit=False)
        plain += run_rules(sym, b, fc, btc, start[sym], args.lev, engine, args, cache_dir, kronos=False)
        no_btc += run_rules(sym, b, fc, None, start[sym], args.lev, engine, args, cache_dir)
        rand += random_trades(sym, b, fc, start[sym], args.lev, max(len(tr), 5), rng)
    elapsed = time.time() - t0

    lines = [f"# Backtest ingresso in rottura a 4 ore ({time.strftime('%d/%m/%Y %H:%M')})", "",
             f"Modello Kronos-{args.model}, {args.samples} campioni ({settings.decision_samples} nella conferma), contesto "
             f"{args.lookback} candele, una previsione ogni {args.every * 15} minuti per token, {args.days} giorni, "
             f"{len(syms)} token, leva {args.lev:g}x, costi andata e ritorno {COSTS * 100:.2f}% del nozionale. "
             f"Ampiezza minima {settings.min_move * 100:g}%, Kronos oltre il {settings.min_confidence * 100:.0f}%, tetto = "
             f"massimo delle ultime {settings.roof_bars} candele da 15 minuti, ingresso oltre +{settings.breakout_buffer * 100:g}%, "
             f"non oltre +{settings.breakout_max_chase * 100:g}%. Filtro BTC: -{settings.btc_filter_4h * 100:g}% a 4 ore, "
             f"-{settings.btc_filter_24h * 100:g}% a 24 ore. Previsioni e regole in {elapsed / 60:.1f} minuti."
             + (" MODALITÀ OFFLINE: percorsi bootstrap, non Kronos." if args.offline else ""), "",
             "## Kronos riconosce i movimenti ampi?", ""]
    lines += amplitude_table(data, fcs, start)
    lines += ["", "## Calibrazione: la probabilità a 4 ore contro il ritorno reale", ""]
    lines += calibration(data, fcs, start)
    lines += ["", "## Operazioni (stesse uscite, ROE netto sul margine)", ""]
    lines += trade_table("Regole dell'Executor: rottura + ampiezza + Kronos + conferma, con uscita in pari", strat)
    lines += trade_table("Stessi ingressi senza uscita in pari", no_be)
    lines += trade_table("Stesse regole senza il filtro BTC", no_btc)
    lines += trade_table("Di queste, solo quelle che il filtro BTC avrebbe bloccato",
                         [t for t in no_btc if not btc_ok(btc, t["t"])])
    lines += trade_table("Tutte le rotture, senza filtri Kronos", plain)
    lines += trade_table("Ingressi casuali", rand)
    lines += ["## Confronti", ""]
    lines += diff_line("Uscita in pari (con meno senza)", strat, no_be)
    lines += diff_line("Filtri Kronos sulla rottura (regole meno rotture senza filtri)", strat, plain)
    lines += diff_line("Strategia meno ingressi casuali", strat, rand)
    report = "\n".join(lines) + "\n"
    out = cache_dir / f"report_{tag}_{args.days}d.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"Rapporto salvato in {out}")


if __name__ == "__main__":
    main()
