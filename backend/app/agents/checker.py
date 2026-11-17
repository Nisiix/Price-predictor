"""Checker agent: ranks the universe for long / short entries at 10x-20x.

For every token it combines
  1. Kronos: probability that TP (+5% leveraged) is hit before SL (-2%) or
     liquidation, from the sampled 30-minute paths on 1m bars;
  2. indicators: EMA 9/21/50, RSI, MACD, Bollinger %B, ATR, volume;
  3. historical analogs: the 25 most similar 30-minute shapes in the
     history of the same token and of every other token in the universe,
     replayed forward with the same TP/SL rule;
and derives an extra risk percentage shown next to the Kronos probability.
"""
from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from ..config import HORIZONS, settings
from .indicators import indicator_view, pattern_analogs

log = logging.getLogger("checker")


class Checker:
    def __init__(self, feed, predictor, bus) -> None:
        self.feed = feed
        self.predictor = predictor
        self.bus = bus
        self.ranking: list[dict] = []
        self.paused_until = 0.0
        self.pause_reason = ""
        self._library: dict[str, np.ndarray] = {}
        self._library_ts = 0.0
        self._analog_cache: dict[tuple[str, str, int], tuple[float, dict]] = {}
        self.macro_risk = 0.0  # set by the Analyst, 0-100
        self._library15: dict[str, np.ndarray] = {}
        self._library15_ts = 0.0

    def pause(self, seconds: float, reason: str) -> None:
        self.paused_until = max(self.paused_until, time.time() + seconds)
        self.pause_reason = reason

    @property
    def paused(self) -> bool:
        return time.time() < self.paused_until

    def _refresh_library(self) -> None:
        if time.time() - self._library_ts < 60:
            return
        lib = {}
        for sym in self.feed.markets:
            bars = self.feed.store.get(sym, "1m")
            if len(bars) > 200:
                lib[sym] = bars[:-1, 4]  # closed bars only
        self._library = lib
        self._library_ts = time.time()

    def _refresh_library15(self) -> None:
        if time.time() - self._library15_ts < 300:
            return
        lib = {}
        for sym in self.feed.markets:
            bars = self.feed.store.get(sym, "15m")
            if len(bars) > 100:
                lib[sym] = bars[:-1, 4]
        self._library15 = lib
        self._library15_ts = time.time()

    @staticmethod
    def _leverage(risk: float, max_lev: int) -> int:
        lev = 20 if risk < 0.25 else 15 if risk < 0.35 else 12 if risk < 0.45 else 10
        return min(lev, max_lev)

    def evaluate(self, sym: str) -> dict | None:
        """4-hour plan for one token: direction and probability from Kronos (15m candles, 16 steps),
        extra risk from indicators, historical analogs, volatility and macro, and the entry zone
        (position of the price inside the last 4 hours' range)."""
        m = self.feed.markets.get(sym)
        c4 = self.predictor.cache.get((sym, "15m"))
        bars15 = self.feed.store.get(sym, "15m")
        bars5 = self.feed.store.get(sym, "5m", 48)  # last 4 hours
        if not m or not c4 or len(bars15) < 64 or len(bars5) < 12:
            return None
        step = HORIZONS["4h"][2]
        closes = c4["paths"][:, step - 1, 3]
        p_up = float((closes > c4["last"]).mean())
        side = 1 if p_up >= 0.5 else -1
        conf = max(p_up, 1 - p_up)
        exp_ret = float((closes / c4["last"] - 1).mean())
        ind = indicator_view(bars15)

        cached = self._analog_cache.get((sym, "4h"))
        if cached and time.time() - cached[0] < 300:
            ana = cached[1]
        else:
            ana = pattern_analogs(bars15[:, 4], self._library15, window=16, horizon=16, self_symbol=sym)
            self._analog_cache[(sym, "4h")] = (time.time(), ana)
        analog_dir = (ana["fwd_up"] if side > 0 else 1 - ana["fwd_up"]) if ana.get("n") else None

        price = self.feed.store.last_price(sym) or c4["last"]
        hi4, lo4 = float(bars5[:, 2].max()), float(bars5[:, 3].min())
        range_pos = float((price - lo4) / (hi4 - lo4)) if hi4 > lo4 else 0.5
        in_zone = range_pos <= settings.entry_zone if side > 0 else range_pos >= 1 - settings.entry_zone

        ind_agree = (1 + side * ind["score"]) / 2
        sl_move10 = settings.stop_loss_pct / 100 / 10
        noise = ind["atr_pct"] / sl_move10 if sl_move10 else 0  # 15m ATR vs the stop distance at 10x
        risk_parts = {
            "kronos_avverso": 1 - conf,
            "indicatori_contrari": 1 - ind_agree,
            "pattern_storici": (1 - analog_dir) if analog_dir is not None else 0.5,
            "volatilita": float(np.clip((noise - 1) / 3.0, 0, 1)),
            "macro_analyst": self.macro_risk / 100,
        }
        weights = {"kronos_avverso": 0.30, "indicatori_contrari": 0.20, "pattern_storici": 0.20,
                   "volatilita": 0.15, "macro_analyst": 0.15}
        extra_risk = float(sum(risk_parts[k] * w for k, w in weights.items()))
        score = conf * (1 - extra_risk)
        lev = self._leverage(extra_risk, m.max_leverage)

        reasons = []
        if conf <= settings.min_confidence:
            reasons.append(f"probabilità a 4 ore non oltre il {settings.min_confidence * 100:.0f}%")
        if extra_risk > settings.max_risk:
            reasons.append("rischio aggiuntivo alto")
        if score < settings.min_signal_score:
            reasons.append("punteggio sotto soglia")
        if not in_zone:
            reasons.append("in attesa: prezzo lontano dal " + ("minimo" if side > 0 else "massimo") + " delle 4 ore")
        if settings.longs_only and side < 0:
            reasons.insert(0, "previsione al ribasso: l'Executor apre solo long")
        eligible = not reasons

        # recent sharp drops (> drop_priority_pct over 4/8/12 h): only an ordering priority among eligible tokens
        closes15 = bars15[:, 4]
        drops = {}
        for label, n in (("4h", 16), ("8h", 32), ("12h", 48)):
            if len(closes15) > n and closes15[-n - 1] > 0:
                drops[label] = float(price / closes15[-n - 1] - 1)
        worst = min(drops.items(), key=lambda kv: kv[1]) if drops else None
        drop_priority = -worst[1] if worst and worst[1] <= -settings.drop_priority_pct / 100 else 0.0

        # same risk model on every dashboard horizon: which one has the best probability with the lowest risk
        per_h = []
        for h in ("15m", "30m", "1h", "4h"):
            _, tf, st = HORIZONS[h]
            ch = self.predictor.cache.get((sym, tf))
            if not ch:
                continue
            cl = ch["paths"][:, st - 1, 3]
            pu = float((cl > ch["last"]).mean())
            sd = 1 if pu >= 0.5 else -1
            cf = max(pu, 1 - pu)
            pat = (ana["fwd_up"] if sd > 0 else 1 - ana["fwd_up"]) if ana.get("n") else None
            parts = {
                "kronos_avverso": 1 - cf,
                "indicatori_contrari": 1 - (1 + sd * ind["score"]) / 2,
                "pattern_storici": (1 - pat) if pat is not None else 0.5,
                "volatilita": risk_parts["volatilita"],
                "macro_analyst": risk_parts["macro_analyst"],
            }
            rk = float(sum(parts[k] * w for k, w in weights.items()))
            per_h.append({"h": h, "side": "long" if sd > 0 else "short", "conf": cf, "risk": rk,
                          "score": cf * (1 - rk), "age": time.time() - ch["ts"]})
        best_h = max(per_h, key=lambda x: x["score"]) if per_h else None

        # 30-minute TP/SL analysis on 1m candles, kept for reference
        c1 = self.predictor.cache.get((sym, "1m"))
        best = c1["trade"]["best"] if c1 and c1.get("trade") and c1["trade"]["best"] else None
        return {
            "symbol": sym, "base": m.base, "name": m.name, "rank": m.rank, "price": price,
            "change_24h": m.change_24h, "max_leverage": m.max_leverage,
            "side": "long" if side > 0 else "short", "leverage": lev,
            "p_up_4h": p_up, "conf_4h": conf, "exp_ret_4h": exp_ret,
            "range_pos": range_pos, "range_hi": hi4, "range_lo": lo4, "in_zone": in_zone,
            "kronos_p_tp": conf,  # probability used by the Controller buckets
            "kronos_p_sl": 1 - conf, "kronos_p_liq": best["p_liq"] if best else 0.0,
            "ev": exp_ret * side * lev,
            "levels": best["levels"] if best else None, "short_term": best,
            "combined_prob": conf, "extra_risk": extra_risk, "risk_parts": risk_parts, "score": score,
            "indicators": ind, "analogs": ana, "analog_dir": analog_dir,
            "eligible": eligible and not self.paused, "reasons": reasons,
            "source": c4["source"], "age": time.time() - c4["ts"], "options": c1["trade"]["options"][:4] if best else [],
            "horizons": per_h, "best_h": best_h,
            "drops": drops, "drop_priority": drop_priority, "drop_window": worst[0] if drop_priority else None,
        }

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._refresh_library15)
                rows = []
                for sym in list(self.feed.markets):
                    r = await asyncio.to_thread(self.evaluate, sym)
                    if r:
                        rows.append(r)
                rows.sort(key=lambda r: r["score"], reverse=True)
                self.ranking = rows
                self.bus.publish("ranking", {"rows": [self.slim(r) for r in rows[:40]], "paused": self.paused,
                                             "pause_reason": self.pause_reason if self.paused else ""})
            except Exception:
                log.exception("checker cycle failed")
            await asyncio.sleep(3)

    @staticmethod
    def slim(r: dict) -> dict:
        ind = r["indicators"]
        return {k: r[k] for k in ("symbol", "base", "name", "rank", "price", "change_24h", "side", "leverage",
                                  "kronos_p_tp", "kronos_p_sl", "kronos_p_liq", "ev", "combined_prob",
                                  "extra_risk", "score", "eligible", "reasons", "source", "max_leverage",
                                  "p_up_4h", "conf_4h", "range_pos", "in_zone", "horizons", "best_h",
                                  "drops", "drop_priority", "drop_window")} | {
            "rsi": ind["rsi"], "regime": ind["regime"], "flags": ind["flags"], "ind_score": ind["score"],
            "analog_win": r["analog_dir"], "analog_n": r["analogs"].get("n", 0),
        }

    def signals(self) -> list[dict]:
        """Eligible tokens in opening order: those after a sharp recent drop first (largest drop first),
        then by score. The drop never makes a token eligible, it only moves it up the queue."""
        sig = [r for r in self.ranking if r["eligible"]]
        return sorted(sig, key=lambda r: (r.get("drop_priority", 0) > 0, r.get("drop_priority", 0), r["score"]), reverse=True)
