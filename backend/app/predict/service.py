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
MODEL_UNIVERSE = {"mini": 12, "base": settings.base_tokens or None}  # base, 0: automatic (auto_tokens)
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
from .probability import best_trade, fan, horizon_index, horizon_stats

log = logging.getLogger("predict")


def _volumes(v: np.ndarray) -> np.ndarray:
    """Volumes for Kronos. Candles still built from live ticks (not yet replaced by the exchange's) carry volume 0:
    they get the average volume of the last 20 exchange candles, so a missing value does not read as a dead market."""
    v = np.nan_to_num(np.asarray(v, dtype=float), nan=0.0)
    known = v[v > 0]
    if not len(known):
        return v
    fill = float(known[-20:].mean())
    return np.where(v > 0, v, fill)

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
        self.scan_latency: float | None = None  # moving average of one scan forecast (s)
        self._auto_n: int | None = None
        self._auto_ts = 0.0

    # ------------------------------------------------------------------ core
    def _run_tf(self, sym: str, tf: str, engine: KronosEngine | None = None, n_samples: int | None = None) -> dict | None:
        engine = engine or self.engine
        n_samples = n_samples or settings.n_samples
        sec = TIMEFRAMES[tf]
        lookback = settings.base_lookback if engine.model_name.endswith("Kronos-base") else settings.lookback
        bars = self.feed.store.get(sym, tf, lookback + 1)
        if len(bars) and bars[-1, 0] + sec > time.time():
            bars = bars[:-1]  # the last candle is still forming: Kronos sees only closed candles
        bars = bars[-lookback:]
        if len(bars) < MIN_BARS[tf]:
            return None
        pred_len = PRED_LEN[tf]
        ts = pd.to_datetime(bars[:, 0], unit="s")
        fut = pd.to_datetime(bars[-1, 0] + sec * np.arange(1, pred_len + 1), unit="s")
        ohlc = bars[:, 1:5]
        volume = _volumes(bars[:, 5])
        last = float(self.feed.store.last_price(sym) or ohlc[-1, 3])
        source = "kronos"
        paths = None
        if engine.ready:
            paths = engine.sample_paths(
                [SeriesInput(ohlc, ts, fut, volume)], pred_len, n_samples, settings.temperature, settings.top_p
            )[0]
            if paths is None:
                source = "flat"
        if paths is None:
            paths = monte_carlo_paths(ohlc, pred_len, n_samples)
            source = source if source == "flat" else "montecarlo"
        # Re-anchor the sampled cloud on the live price (the model forecasts
        # from the last closed bar, the live tick may already have moved).
        anchor = float(ohlc[-1, 3])
        if anchor > 0:
            paths = paths * (last / anchor)
        m = self.feed.markets.get(sym)
        trade = None
        if tf == "15m" and m:
            # TP / SL / liquidation over the 4 hours ahead, for reference on the dashboard
            fee_rt = 2 * (settings.taker_fee + settings.slippage)
            trade = best_trade(
                paths[:, :HORIZONS["4h"][2]], last, settings.leverage_choices, settings.take_profit_pct, settings.stop_loss_pct,
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

    def auto_tokens(self, universe: int) -> int:
        """Kronos-base, automatic: how many tokens one scan cycle can refresh at the measured time per forecast.
        85% of the cycle goes to the scan; the open positions' forecasts and the focused token come out of it.
        Re-evaluated at most every 15 minutes, so the list does not flicker."""
        now = time.time()
        if self._auto_n is not None and now - self._auto_ts < 900:
            return self._auto_n
        lat = self.scan_latency or 22.0  # before the first measurements: ~22 s (128 bars, 18 steps, estimate)
        budget = 0.85 * settings.scan_age_s - lat * (len(self.priority) + 0.5)
        n = int(max(settings.base_tokens_min, min(universe, budget // lat)))
        if self._auto_n is None or abs(n - self._auto_n) >= 2:
            self._auto_n = n
        # before the first measured forecasts, look again in a minute
        self._auto_ts = now if self.scan_latency is not None else now - 840
        return self._auto_n

    def active_symbols(self) -> list[str]:
        """Tokens analysed with the current model: the first N by market cap (mini: 12; base: PP_BASE_TOKENS, or
        automatic from the measured speed when it is 0)."""
        ranked = sorted(self.feed.markets.values(), key=lambda m: m.rank)
        n = MODEL_UNIVERSE.get(self.model_key)
        if self.model_key == "base" and not n and ranked:
            n = self.auto_tokens(len(ranked))
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

    def run_decision(self, sym: str, n_samples: int | None = None) -> dict | None:
        """Fresh 4h forecast for a decision, with the decision model when one is configured and loaded (else the
        scanning model) and `decision_samples` sampled futures. The result is RETURNED to the caller and also cached,
        both as the decision forecast and as the token's latest forecast: before, without a decision model, it was
        computed and then thrown away, and the entry was decided on the older cached forecast."""
        engine = self.decider if self.decider is not None and self.decider.ready else None
        res = self._run_tf(sym, "15m", engine, n_samples or settings.decision_samples)
        if res:
            res["decision"] = True
            self.cache[(sym, "15m:decision")] = res
            self.cache[(sym, "15m")] = res  # newest forecast of the token: token list, exits and Checker use it too
        return res

    def decision_cache(self, sym: str, max_age: float = 900.0) -> dict | None:
        """Freshest 4h forecast for decisions: the latest decision forecast when recent enough and not older than
        the scan, else the scanner's."""
        big = self.cache.get((sym, "15m:decision"))
        scan = self.cache.get((sym, "15m"))
        if big and time.time() - big["ts"] <= max_age and (scan is None or big["ts"] >= scan["ts"]):
            return big
        return scan

    def _job(self) -> tuple[str, str] | None:
        now = time.time()

        def stale(sym: str, tf: str, age: float) -> bool:
            c = self.cache.get((sym, tf))
            return c is None or now - c["ts"] > age

        tfs = sorted({tf for _, tf, _ in HORIZONS.values()})  # one forecast per token: 15m
        for sym in list(self.focus):
            for tf in tfs:
                if stale(sym, tf, MAX_AGE[tf] * min(self.factor, 2.0)) and self.feed.store.count(sym, tf) >= MIN_BARS[tf]:
                    return sym, tf
        for sym in list(self.priority):
            # open positions: the 4h forecast for the exits, on the same 5-minute cycle as the scan
            if stale(sym, "15m", settings.position_refresh_s) and self.feed.store.count(sym, "15m") >= MIN_BARS["15m"]:
                return sym, "15m:pos"
        # the same forecast serves the token list, the Checker and the Executor: scan the stalest token
        for tf in tfs:
            job = self._scan(tf, settings.scan_age_s, "board", stale)
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
                base_tf = tf.split(":")[0]
                await self.feed.refresh_recent(sym, base_tf)  # last closed candles from the exchange, not from ticks
                if tf == "15m:pos":
                    # open position: the decision model when loaded, the usual number of samples
                    engine = self.decider if self.decider is not None and self.decider.ready else None
                    res = await asyncio.to_thread(self._run_tf, sym, base_tf, engine)
                else:
                    res = await asyncio.to_thread(self._run_tf, sym, tf)
                    if res and res["source"] == "kronos":
                        lat = res["latency"]
                        self.scan_latency = lat if self.scan_latency is None else 0.8 * self.scan_latency + 0.2 * lat
                if res:
                    self.cache[(sym, base_tf)] = res
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
        now = time.time()
        for label, (secs, tf, step) in HORIZONS.items():
            c = self.cache.get((sym, tf))
            i = horizon_index(c["bar_t"], TIMEFRAMES[tf], step, c["paths"].shape[1], now) if c else None
            if not c or not last or i is None:
                out[label] = None
                continue
            # the close `step` bars ahead of now (an older forecast is read further along), vs the live price
            st = horizon_stats(c["paths"], last, i + 1)
            st.update({
                "tf": tf, "step": step, "path_step": i + 1, "seconds": secs, "source": c["source"], "age": now - c["ts"],
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
            i = horizon_index(c["bar_t"], TIMEFRAMES[tf], step, c["paths"].shape[1], now) if c else None
            if i is None:
                continue
            closes = c["paths"][:, i, 3]
            live = self.feed.store.last_price(sym) or c["last"]
            p_up = float((closes > live).mean())
            rows[sym] = {
                "p_up": p_up, "dir": "up" if p_up >= 0.5 else "down", "conf": max(p_up, 1 - p_up),
                "exp_ret": float((closes / live - 1).mean()), "age": now - c["ts"], "source": c["source"],
            }
        return {"h": label, "tf": tf, "rows": rows}

    def snapshot(self, sym: str) -> dict:
        res = {"symbol": sym, "horizons": self.horizons(sym), "fans": {}, "trade": None}
        for tf in sorted({tf for _, tf, _ in HORIZONS.values()}):
            c = self.cache.get((sym, tf))
            if c:
                # the cone goes exactly to 4 hours from now (the farthest horizon); the 2 slack steps are hidden
                L = c["paths"].shape[1]
                i4 = horizon_index(c["bar_t"], TIMEFRAMES[tf], HORIZONS["4h"][2], L, time.time())
                end = L if i4 is None else i4 + 1
                fan_ = {k: v[:end] for k, v in c["fan"].items()}
                res["fans"][tf] = {"t": c["future_t"][:end], **fan_, "anchor": c["last"], "bar_t": c["bar_t"],
                                   "ts": c["ts"], "tf_seconds": TIMEFRAMES[tf], "source": c["source"],
                                   "age": time.time() - c["ts"]}
                if tf == "15m":
                    res["trade"] = c["trade"]
        return res

    def status(self) -> dict:
        return {
            "model": self.engine.model_name, "model_key": self.model_key, "switching": self.switching,
            "models": list(MODELS), "ready": self.engine.ready, "error": self.engine.error,
            "decision_model": settings.decision_model, "decision_ready": bool(self.decider and self.decider.ready),
            "decision_error": self.decider.error if self.decider else None,
            "device": self.engine.device, "latency": self.engine.last_latency_s, "busy": self.busy_with,
            "inferences": self.inferences, "samples": settings.n_samples,
            "lookback": settings.base_lookback if self.model_key == "base" else settings.lookback,
            "scanned": len({s for (s, tf) in self.cache if tf == "15m"} & set(self.active_symbols())),
            "active": len(self.active_symbols()), "scan_latency": self.scan_latency,
            "tokens_auto": self.model_key == "base" and not MODEL_UNIVERSE.get("base"),
        }


class Calibration:
    """Scores every prediction once its horizon has elapsed (live hit rate)."""

    def __init__(self) -> None:
        self.pending: list[tuple[float, str, str, float, float, float, float]] = []
        self.stats: dict[str, dict] = defaultdict(lambda: {"n": 0, "hits": 0, "brier": 0.0, "covered": 0})

    def register(self, res: dict) -> None:
        sec = TIMEFRAMES[res["tf"]]
        for label, (secs, tf, step) in HORIZONS.items():
            if tf != res["tf"]:
                continue
            st = horizon_stats(res["paths"], res["last"], step)
            # path index step-1 is the close of the bar opening `step` bars after the forming one: it closes at
            # bar_t + (step + 1) bars, which is when this forecast is scored (before: forecast time + horizon,
            # up to one bar later than the step that was read)
            due = res.get("bar_t", res["ts"] - sec) + (step + 1) * sec
            self.pending.append((due, label, res["symbol"], res["last"], st["p_up"], st["q10"], st["q90"]))
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
            done.append({"label": label, "symbol": sym, "p_up": p_up, "ret": r, "covered": int(q10 <= r <= q90)})
        self.pending = keep
        return done

    def summary(self) -> dict:
        out = {}
        for label, s in self.stats.items():
            n = s["n"]
            if n:
                out[label] = {"n": n, "hit_rate": s["hits"] / n, "brier": s["brier"] / n, "coverage": s["covered"] / n}
        return out
