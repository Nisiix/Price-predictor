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

import json
import os

from ..config import DATA_DIR, EXPERIMENTAL_HORIZONS, HORIZONS, PRED_LEN, TIMEFRAMES, settings

# Models the dashboard can switch between at runtime: key -> (model, tokenizer, max context, refresh factor)
MODELS = {
    "mini": ("NeoQuasar/Kronos-mini", "NeoQuasar/Kronos-Tokenizer-2k", 2048, 1.0),
    "base": ("NeoQuasar/Kronos-base", "NeoQuasar/Kronos-Tokenizer-base", 512, 6.0),  # ~20x slower on CPU
}
# user rule: how many tokens (by market cap) each model analyses; None = the whole universe
MODEL_UNIVERSE = {"mini": 12, "base": None}
MODEL_FILE = DATA_DIR / "model.json"


def _saved_model_key() -> str:
    env = os.getenv("PP_KRONOS_MODEL", "")
    for k, v in MODELS.items():
        if env == v[0]:
            return k
    try:
        k = json.loads(MODEL_FILE.read_text()).get("model")
        if k in MODELS:
            return k
    except Exception:
        pass
    return "base"
from ..market.feed import MarketFeed
from .engine import KronosEngine, SeriesInput, monte_carlo_paths
from .probability import best_trade, fan, horizon_stats

log = logging.getLogger("predict")

MIN_BARS = {"5s": 48, "1m": 64, "5m": 64, "15m": 64}
MAX_AGE = {"5s": 5.0, "1m": 30.0, "5m": 120.0, "15m": 300.0}  # refresh interval for the focus token
SCAN_AGE = 150.0  # refresh interval for universe scan on 1m (Checker)
BOARD_AGE = {"5s": 40.0, "1m": 90.0, "5m": 150.0, "15m": 300.0}  # refresh for the horizon chosen on the dashboard


class PredictionService:
    def __init__(self, feed: MarketFeed) -> None:
        self.feed = feed
        self.model_key = _saved_model_key()
        name, tok, ctx, _ = MODELS[self.model_key]
        self.engine = KronosEngine(name, tok, ctx)
        self.switching: str | None = None
        self.decider = KronosEngine(settings.decision_model, settings.decision_tokenizer, 512) if settings.decision_model else None
        self.cache: dict[tuple[str, str], dict] = {}
        self.focus: set[str] = set()
        self.priority: set[str] = set()  # tokens with open positions
        self._rr = 0
        self._rr_board = 0
        self._turn = 0
        self.board_horizons: set[str] = {"1h", "4h"}  # always: 4h (Executor) and 1h (best-horizon per token)
        self.calib = Calibration()
        self.busy_with: str | None = None
        self.inferences = 0

    # ------------------------------------------------------------------ core
    def _run_tf(self, sym: str, tf: str, engine: KronosEngine | None = None) -> dict | None:
        engine = engine or self.engine
        sec = TIMEFRAMES[tf]
        bars = self.feed.store.get(sym, tf, settings.lookback + 1)
        if len(bars) and bars[-1, 0] + sec > time.time():
            bars = bars[:-1]  # the last candle is still forming: Kronos sees only closed candles
        bars = bars[-settings.lookback:]
        if len(bars) < MIN_BARS[tf]:
            return None
        pred_len = PRED_LEN[tf]
        ts = pd.to_datetime(bars[:, 0], unit="s")
        fut = pd.to_datetime(bars[-1, 0] + sec * np.arange(1, pred_len + 1), unit="s")
        ohlc = bars[:, 1:5]
        last = float(self.feed.store.last_price(sym) or ohlc[-1, 3])
        source = "kronos"
        paths = None
        if engine.ready:
            paths = engine.sample_paths(
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
            "source": source, "latency": engine.last_latency_s, "samples": int(paths.shape[0]),
            "model": engine.model_name.split("/")[-1],
            "paths": paths, "fan": fan(paths), "future_t": (bars[-1, 0] + sec * np.arange(1, pred_len + 1)).tolist(),
            "trade": trade,
        }

    def active_symbols(self) -> list[str]:
        """Tokens analysed with the current model: the first N by market cap (mini: 12, base: all)."""
        ranked = sorted(self.feed.markets.values(), key=lambda m: m.rank)
        n = MODEL_UNIVERSE.get(self.model_key)
        return [m.symbol for m in (ranked[:n] if n else ranked)]

    @property
    def factor(self) -> float:
        """Refresh intervals scale with the model's CPU cost."""
        return float(os.getenv("PP_SCAN_FACTOR", MODELS[self.model_key][3]))

    async def switch_model(self, key: str) -> None:
        """Load the other model in the background, then swap it in; forecasts refresh progressively."""
        if key not in MODELS or key == self.model_key or self.switching:
            return
        self.switching = key
        try:
            name, tok, ctx, _ = MODELS[key]
            eng = KronosEngine(name, tok, ctx)
            await asyncio.to_thread(eng.load)
            if eng.ready:
                self.engine, self.model_key = eng, key
                try:
                    MODEL_FILE.write_text(json.dumps({"model": key}))
                except Exception:
                    pass
                log.info("switched Kronos model to %s", name)
        finally:
            self.switching = None

    def run_decision(self, sym: str) -> dict | None:
        """4h/8h forecast with the large model (falls back to the scanning model while it loads)."""
        if self.decider is None or not self.decider.ready:
            return self._run_tf(sym, "15m")
        res = self._run_tf(sym, "15m", self.decider)
        if res:
            self.cache[(sym, "15m:base")] = res
        return res

    def decision_cache(self, sym: str, max_age: float = 900.0) -> dict | None:
        """Freshest 4h forecast for decisions: the large model's when recent enough, else the scanner's."""
        big = self.cache.get((sym, "15m:base"))
        if big and time.time() - big["ts"] <= max_age:
            return big
        return self.cache.get((sym, "15m"))

    def _job(self) -> tuple[str, str] | None:
        now = time.time()

        def stale(sym: str, tf: str, age: float) -> bool:
            c = self.cache.get((sym, tf))
            return c is None or now - c["ts"] > age

        for sym in list(self.focus):
            for tf in ("1m", "5m", "15m"):  # sub-minute horizons are no longer shown on the dashboard
                if stale(sym, tf, MAX_AGE[tf] * min(self.factor, 2.0)) and self.feed.store.count(sym, tf) >= MIN_BARS[tf]:
                    return sym, tf
        for sym in list(self.priority):
            # open positions: keep the 4h forecast fresh for the exits, with the large model when loaded
            key = "15m:base" if self.decider is not None and self.decider.ready else "15m"
            if stale(sym, key, 180.0) and self.feed.store.count(sym, "15m") >= MIN_BARS["15m"]:
                return sym, key
        # alternate the Checker scan (1m) with the scan of the dashboard's horizon timeframe
        board_tfs = sorted({HORIZONS[h][1] for h in self.board_horizons if h in HORIZONS} or {"1m"})
        self._turn += 1
        board_jobs = [("board", board_tfs[(self._turn // 2) % len(board_tfs)])]
        order = board_jobs + [("checker", "1m")] if self._turn % 2 else [("checker", "1m")] + board_jobs
        for kind, tf in order:
            job = self._scan(tf, (BOARD_AGE[tf] if kind == "board" else SCAN_AGE) * self.factor, kind, stale)
            if job:
                return job
        return None

    def _scan(self, tf: str, age: float, kind: str, stale) -> tuple[str, str] | None:
        syms = [s for s in self.active_symbols() if self.feed.store.count(s, tf) >= MIN_BARS[tf]]
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
        if self.decider is not None:
            asyncio.create_task(asyncio.to_thread(self.decider.load))  # loads in the background
        while True:
            job = self._job()
            if job is None:
                await asyncio.sleep(0.5)
                continue
            sym, tf = job
            self.busy_with = f"{sym}:{tf}"
            try:
                if tf == "15m:base":
                    res = await asyncio.to_thread(self.run_decision, sym)
                else:
                    res = await asyncio.to_thread(self._run_tf, sym, tf)
                    if res:
                        self.cache[(sym, tf)] = res
                if res:
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
            st = horizon_stats(c["paths"], last, step)  # vs the live price, not the price at inference time
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
        for sym in self.active_symbols():
            c = self.cache.get((sym, tf))
            if not c:
                continue
            closes = c["paths"][:, step - 1, 3]
            live = self.feed.store.last_price(sym) or c["last"]
            p_up = float((closes > live).mean())
            rows[sym] = {
                "p_up": p_up, "dir": "up" if p_up >= 0.5 else "down", "conf": max(p_up, 1 - p_up),
                "exp_ret": float((closes / live - 1).mean()), "age": now - c["ts"], "source": c["source"],
            }
        return {"h": label, "tf": tf, "rows": rows}

    def snapshot(self, sym: str) -> dict:
        res = {"symbol": sym, "horizons": self.horizons(sym), "fans": {}, "trade": None}
        for tf in ("5s", "1m", "5m", "15m"):
            c = self.cache.get((sym, tf))
            if c:
                res["fans"][tf] = {"t": c["future_t"], **c["fan"], "anchor": c["last"], "bar_t": c["bar_t"],
                                   "source": c["source"], "age": time.time() - c["ts"]}
                if tf == "1m":
                    res["trade"] = c["trade"]
        return res

    def status(self) -> dict:
        return {
            "model": self.engine.model_name, "model_key": self.model_key, "switching": self.switching,
            "models": list(MODELS), "ready": self.engine.ready, "error": self.engine.error,
            "decision_model": settings.decision_model, "decision_ready": bool(self.decider and self.decider.ready),
            "decision_error": self.decider.error if self.decider else None,
            "device": self.engine.device, "latency": self.engine.last_latency_s, "busy": self.busy_with,
            "inferences": self.inferences, "samples": settings.n_samples, "lookback": settings.lookback,
            "scanned": len({s for (s, tf) in self.cache if tf == "15m"} & set(self.active_symbols())),
            "active": len(self.active_symbols()),
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
