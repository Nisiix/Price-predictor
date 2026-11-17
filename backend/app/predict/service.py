"""Schedules Kronos inference across the universe and caches the results.

Priority (CPU budget): focus token on the dashboard > tokens with open
positions > round-robin scan of the universe for the Checker.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

import numpy as np
import pandas as pd

from ..config import EXPERIMENTAL_HORIZONS, HORIZONS, PRED_LEN, TIMEFRAMES, settings
from ..market.feed import MarketFeed
from .engine import KronosEngine, SeriesInput, monte_carlo_paths
from .probability import best_trade, fan, horizon_stats

log = logging.getLogger("predict")

MIN_BARS = {"5s": 48, "1m": 64, "5m": 64}
MAX_AGE = {"5s": 5.0, "1m": 30.0, "5m": 120.0}  # refresh interval for the focus token
SCAN_AGE = 150.0  # refresh interval for universe scan on 1m (Checker)
BOARD_AGE = {"5s": 40.0, "1m": 90.0, "5m": 150.0}  # refresh for the horizon chosen on the dashboard


class PredictionService:
    def __init__(self, feed: MarketFeed) -> None:
        self.feed = feed
        self.engine = KronosEngine(settings.kronos_model, settings.kronos_tokenizer, settings.kronos_max_context)
        self.cache: dict[tuple[str, str], dict] = {}
        self.focus: set[str] = set()
        self.priority: set[str] = set()  # tokens with open positions
        self._rr = 0
        self._rr_board = 0
        self._turn = 0
        self.board_horizons: set[str] = {"15m"}  # horizons the open dashboards sort their token list by
        self.calib = Calibration()
        self.busy_with: str | None = None
        self.inferences = 0

    # ------------------------------------------------------------------ core
    def _run_tf(self, sym: str, tf: str) -> dict | None:
        bars = self.feed.store.get(sym, tf, settings.lookback)
        if len(bars) < MIN_BARS[tf]:
            return None
        sec = TIMEFRAMES[tf]
        pred_len = PRED_LEN[tf]
        ts = pd.to_datetime(bars[:, 0], unit="s")
        fut = pd.to_datetime(bars[-1, 0] + sec * np.arange(1, pred_len + 1), unit="s")
        ohlc = bars[:, 1:5]
        last = float(self.feed.store.last_price(sym) or ohlc[-1, 3])
        source = "kronos"
        paths = None
        if self.engine.ready:
            paths = self.engine.sample_paths(
                [SeriesInput(ohlc, ts, fut)], pred_len, settings.n_samples, settings.temperature, settings.top_p
            )[0]
            if paths is None:
                source = "flat"
        if paths is None:
            paths = monte_carlo_paths(ohlc, pred_len, settings.n_samples)
            source = source if source == "flat" else "montecarlo"
        # Re-anchor the sampled cloud on the live price (the model forecasts
        # from the last closed bar, the live tick may already have moved).
        anchor = float(ohlc[-1, 3])
        if anchor > 0:
            paths = paths * (last / anchor)
        m = self.feed.markets.get(sym)
        trade = None
        if tf in ("1m",) and m:
            fee_rt = 2 * (settings.taker_fee + settings.slippage)
            trade = best_trade(
                paths, last, settings.leverage_choices, settings.take_profit_pct, settings.stop_loss_pct,
                m.mmr, fee_rt, m.max_leverage,
            )
        self.inferences += 1
        return {
            "symbol": sym, "tf": tf, "ts": time.time(), "bar_t": float(bars[-1, 0]), "last": last,
            "source": source, "latency": self.engine.last_latency_s, "samples": int(paths.shape[0]),
            "paths": paths, "fan": fan(paths), "future_t": (bars[-1, 0] + sec * np.arange(1, pred_len + 1)).tolist(),
            "trade": trade,
        }

    def _job(self) -> tuple[str, str] | None:
        now = time.time()

        def stale(sym: str, tf: str, age: float) -> bool:
            c = self.cache.get((sym, tf))
            return c is None or now - c["ts"] > age

        for sym in list(self.focus):
            for tf in ("5s", "1m", "5m"):
                if stale(sym, tf, MAX_AGE[tf]) and self.feed.store.count(sym, tf) >= MIN_BARS[tf]:
                    return sym, tf
        for sym in list(self.priority):
            if stale(sym, "1m", 30.0) and self.feed.store.count(sym, "1m") >= MIN_BARS["1m"]:
                return sym, "1m"
        # alternate the Checker scan (1m) with the scan of the dashboard's horizon timeframe
        board_tfs = sorted({HORIZONS[h][1] for h in self.board_horizons if h in HORIZONS} or {"1m"})
        self._turn += 1
        board_jobs = [("board", board_tfs[(self._turn // 2) % len(board_tfs)])]
        order = board_jobs + [("checker", "1m")] if self._turn % 2 else [("checker", "1m")] + board_jobs
        for kind, tf in order:
            job = self._scan(tf, BOARD_AGE[tf] if kind == "board" else SCAN_AGE, kind, stale)
            if job:
                return job
        return None

    def _scan(self, tf: str, age: float, kind: str, stale) -> tuple[str, str] | None:
        syms = [s for s in self.feed.markets if self.feed.store.count(s, tf) >= MIN_BARS[tf]]
        if not syms:
            return None
        attr = "_rr_board" if kind == "board" else "_rr"
        # stalest first, so a horizon switch refreshes the whole list quickly
        best = None
        for _ in range(len(syms)):
            i = (getattr(self, attr) + 1) % len(syms)
            setattr(self, attr, i)
            sym = syms[i]
            if stale(sym, tf, age):
                c = self.cache.get((sym, tf))
                t = c["ts"] if c else 0.0
                if best is None or t < best[0]:
                    best = (t, sym)
                if t == 0.0:
                    break
        return (best[1], tf) if best else None

    async def run(self) -> None:
        await asyncio.to_thread(self.engine.load)
        while True:
            job = self._job()
            if job is None:
                await asyncio.sleep(0.5)
                continue
            sym, tf = job
            self.busy_with = f"{sym}:{tf}"
            try:
                res = await asyncio.to_thread(self._run_tf, sym, tf)
                if res:
                    self.cache[(sym, tf)] = res
                    self.calib.register(res)
            except Exception:
                log.exception("prediction failed for %s %s", sym, tf)
                await asyncio.sleep(1)
            self.busy_with = None
            await asyncio.sleep(0)

    # --------------------------------------------------------------- outputs
    def horizons(self, sym: str) -> dict:
        """Per-horizon distribution assembled from the cached timeframes."""
        out = {}
        last = self.feed.store.last_price(sym)
        for label, (secs, tf, step) in HORIZONS.items():
            c = self.cache.get((sym, tf))
            if not c or not last:
                out[label] = None
                continue
            # paths were anchored on the price at inference time; express as returns vs that price
            st = horizon_stats(c["paths"], c["last"], step)
            st.update({
                "tf": tf, "step": step, "seconds": secs, "source": c["source"], "age": time.time() - c["ts"],
                "experimental": label in EXPERIMENTAL_HORIZONS, "accuracy": self.calib.summary().get(label),
            })
            out[label] = st
        return out

    def board(self, label: str) -> dict:
        """Probability for one horizon across the whole universe (dashboard token list)."""
        secs, tf, step = HORIZONS[label]
        rows = {}
        now = time.time()
        for sym in self.feed.markets:
            c = self.cache.get((sym, tf))
            if not c:
                continue
            closes = c["paths"][:, step - 1, 3]
            p_up = float((closes > c["last"]).mean())
            rows[sym] = {
                "p_up": p_up, "dir": "up" if p_up >= 0.5 else "down", "conf": max(p_up, 1 - p_up),
                "exp_ret": float((closes / c["last"] - 1).mean()), "age": now - c["ts"], "source": c["source"],
            }
        return {"h": label, "tf": tf, "rows": rows}

    def snapshot(self, sym: str) -> dict:
        res = {"symbol": sym, "horizons": self.horizons(sym), "fans": {}, "trade": None}
        for tf in ("5s", "1m", "5m"):
            c = self.cache.get((sym, tf))
            if c:
                res["fans"][tf] = {"t": c["future_t"], **c["fan"], "anchor": c["last"], "bar_t": c["bar_t"],
                                   "source": c["source"], "age": time.time() - c["ts"]}
                if tf == "1m":
                    res["trade"] = c["trade"]
        return res

    def status(self) -> dict:
        return {
            "model": settings.kronos_model, "ready": self.engine.ready, "error": self.engine.error,
            "device": self.engine.device, "latency": self.engine.last_latency_s, "busy": self.busy_with,
            "inferences": self.inferences, "samples": settings.n_samples, "lookback": settings.lookback,
            "scanned": sum(1 for (s, tf) in self.cache if tf == "1m"),
        }


class Calibration:
    """Scores every prediction once its horizon has elapsed (live hit rate)."""

    def __init__(self) -> None:
        self.pending: list[tuple[float, str, str, float, float, float, float]] = []
        self.stats: dict[str, dict] = defaultdict(lambda: {"n": 0, "hits": 0, "brier": 0.0, "covered": 0})

    def register(self, res: dict) -> None:
        for label, (secs, tf, step) in HORIZONS.items():
            if tf != res["tf"]:
                continue
            st = horizon_stats(res["paths"], res["last"], step)
            self.pending.append((res["ts"] + secs, label, res["symbol"], res["last"], st["p_up"], st["q10"], st["q90"]))
        if len(self.pending) > 20000:
            self.pending = self.pending[-20000:]

    def resolve(self, price_of) -> list[dict]:
        now = time.time()
        done, keep = [], []
        for item in self.pending:
            due, label, sym, p0, p_up, q10, q90 = item
            if due > now:
                keep.append(item)
                continue
            p1 = price_of(sym)
            if not p1:
                continue
            r = p1 / p0 - 1
            if r == 0:
                continue  # unchanged price: no direction to score (same rule as the Controller)
            up = 1.0 if r > 0 else 0.0
            s = self.stats[label]
            s["n"] += 1
            s["hits"] += int((p_up > 0.5) == (r > 0))
            s["brier"] += (p_up - up) ** 2
            s["covered"] += int(q10 <= r <= q90)
            done.append({"label": label, "symbol": sym, "p_up": p_up, "ret": r})
        self.pending = keep
        return done

    def summary(self) -> dict:
        out = {}
        for label, s in self.stats.items():
            n = s["n"]
            if n:
                out[label] = {"n": n, "hit_rate": s["hits"] / n, "brier": s["brier"] / n, "coverage": s["covered"] / n}
        return out
