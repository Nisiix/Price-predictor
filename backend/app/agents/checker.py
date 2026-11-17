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

from ..config import settings
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

    def evaluate(self, sym: str) -> dict | None:
        c = self.predictor.cache.get((sym, "1m"))
        m = self.feed.markets.get(sym)
        if not c or not m or not c.get("trade") or not c["trade"]["best"]:
            return None
        bars = self.feed.store.get(sym, "1m")
        if len(bars) < 60:
            return None
        best = c["trade"]["best"]
        side = 1 if best["side"] == "long" else -1
        lev = best["leverage"]
        ind = indicator_view(bars)

        key = (sym, best["side"], lev)
        cached = self._analog_cache.get(key)
        if cached and time.time() - cached[0] < 60:
            ana = cached[1]
        else:
            tp_move = settings.take_profit_pct / 100 / lev
            sl_move = settings.stop_loss_pct / 100 / lev
            ana = pattern_analogs(bars[:, 4], self._library, tp_move=tp_move, sl_move=sl_move, self_symbol=sym)
            self._analog_cache[key] = (time.time(), ana)

        p_tp = best["p_tp"]
        p_adverse = best["p_sl"] + best["p_liq"]
        analog_win = (ana.get("long_win") if side > 0 else ana.get("short_win")) if ana.get("n") else None
        ind_agree = (1 + side * ind["score"]) / 2  # 1 = all indicators agree with the side
        sl_move = settings.stop_loss_pct / 100 / lev
        noise = ind["atr_pct"] / sl_move if sl_move else 0  # 1m ATR relative to the stop distance

        risk_parts = {
            "kronos_avverso": p_adverse,
            "indicatori_contrari": 1 - ind_agree,
            "pattern_storici": (1 - analog_win) if analog_win is not None else 0.5,
            "volatilita": float(np.clip((noise - 0.5) / 2.0, 0, 1)),
            "macro_analyst": self.macro_risk / 100,
        }
        weights = {"kronos_avverso": 0.30, "indicatori_contrari": 0.20, "pattern_storici": 0.20,
                   "volatilita": 0.15, "macro_analyst": 0.15}
        extra_risk = float(sum(risk_parts[k] * w for k, w in weights.items()))
        combined = 0.6 * p_tp + 0.25 * (analog_win if analog_win is not None else p_tp) + 0.15 * ind_agree
        score = combined * (1 - 0.5 * extra_risk)
        eligible = (best["ev"] > 0 and score >= settings.min_signal_score and extra_risk <= 0.6
                    and p_tp > p_adverse)
        reasons = []
        if best["ev"] <= 0:
            reasons.append("valore atteso negativo dopo commissioni")
        if score < settings.min_signal_score:
            reasons.append("punteggio sotto soglia")
        if extra_risk > 0.6:
            reasons.append("rischio aggiuntivo alto")
        if p_tp <= p_adverse:
            reasons.append("Kronos vede più probabile lo stop")

        return {
            "symbol": sym, "base": m.base, "name": m.name, "rank": m.rank, "price": c["last"],
            "change_24h": m.change_24h, "max_leverage": m.max_leverage,
            "side": best["side"], "leverage": lev, "kronos_p_tp": p_tp, "kronos_p_sl": best["p_sl"],
            "kronos_p_liq": best["p_liq"], "ev": best["ev"], "levels": best["levels"],
            "combined_prob": combined, "extra_risk": extra_risk, "risk_parts": risk_parts, "score": score,
            "indicators": ind, "analogs": ana, "eligible": eligible and not self.paused, "reasons": reasons,
            "source": c["source"], "age": time.time() - c["ts"], "options": c["trade"]["options"][:4],
        }

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._refresh_library)
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
                                  "extra_risk", "score", "eligible", "reasons", "source", "max_leverage")} | {
            "rsi": ind["rsi"], "regime": ind["regime"], "flags": ind["flags"], "ind_score": ind["score"],
            "analog_win": (r["analogs"].get("long_win") if r["side"] == "long" else r["analogs"].get("short_win")),
            "analog_n": r["analogs"].get("n", 0),
        }

    def signals(self) -> list[dict]:
        return [r for r in self.ranking if r["eligible"]]
