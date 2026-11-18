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

from ..config import HORIZONS, TIMEFRAMES, settings
from ..predict.probability import horizon_index, predicted_dip
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
        # user rule: tokens with an open position are not analysed until it closes (set by the Executor);
        # Kronos still forecasts them because the Executor's exits need it
        self.excluded: set[str] = set()
        self._library15: dict[str, np.ndarray] = {}
        self._library15_ts = 0.0
        self.controller = None  # set by main: every new 4h forecast is logged with its outcome (signal register)
        self._logged: dict[str, float] = {}  # symbol -> ts of the last forecast logged
        self.btc = {"ok": True, "chg_4h": None, "chg_24h": None}

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

    def btc_trend(self) -> dict:
        """Market filter: BTC's change over the last 4 and 24 hours (15m candles, live price).
        Longs are allowed only while neither is below -threshold (default 0: any fall blocks)."""
        sym = "PERP_BTC_USDC"
        bars = self.feed.store.get(sym, "15m", 100)
        price = self.feed.store.last_price(sym)
        out = {"ok": True, "chg_4h": None, "chg_24h": None}
        if not price or len(bars) < 98:
            return out  # no BTC data yet: the filter cannot judge, it does not block
        closes = bars[:, 4]
        if bars[-1, 0] + 900 > time.time():
            closes = closes[:-1]  # forming candle
        out["chg_4h"] = float(price / closes[-16] - 1)
        out["chg_24h"] = float(price / closes[-96] - 1)
        out["ok"] = out["chg_4h"] >= -settings.btc_filter_4h and out["chg_24h"] >= -settings.btc_filter_24h
        return out

    def evaluate(self, sym: str, decision: bool = False, forecast: dict | None = None) -> dict | None:
        """4-hour plan for one token: direction and probability from Kronos (15m candles, 16 steps),
        extra risk from indicators, historical analogs, volatility and macro, and the entry zone
        (position of the price inside the last 4 hours' range)."""
        m = self.feed.markets.get(sym)
        if forecast is not None:
            c4 = forecast  # the forecast just computed for this decision (Executor, right before an entry)
        else:
            c4 = self.predictor.decision_cache(sym) if decision else self.predictor.cache.get((sym, "15m"))
        bars15 = self.feed.store.get(sym, "15m")
        bars5 = self.feed.store.get(sym, "5m", 48)  # last 4 hours
        if not m or not c4 or len(bars15) < 64 or len(bars5) < 12:
            return None
        step = HORIZONS["4h"][2]
        now = time.time()
        L = c4["paths"].shape[1]
        i4 = horizon_index(c4["bar_t"], 900, step, L, now)  # 4 hours from now, even if the forecast is a few bars old
        if i4 is None:
            return None
        elapsed = i4 - (step - 1)  # 15m bars closed since the forecast was computed
        closes = c4["paths"][:, i4, 3]
        # Probabilities are measured against the LIVE price, not the price at the time Kronos ran:
        # if the market already moved toward the forecast, the remaining upside is what matters.
        price = self.feed.store.last_price(sym) or c4["last"]
        p_up = float((closes > price).mean())
        side = 1 if p_up >= 0.5 else -1
        conf = max(p_up, 1 - p_up)
        exp_ret = float((closes / price - 1).mean())
        median_4h = float(np.median(closes))
        drift = price / c4["last"] - 1  # how far the price moved since the forecast was computed
        # predicted entry point: from the lowest price of each sampled path before the 4h horizon
        dip_price, dip_step = predicted_dip(c4["paths"][:, elapsed:], step, settings.dip_quantile)  # bars still ahead
        entry_ok = price <= dip_price * (1 + settings.entry_tolerance)
        ind = indicator_view(bars15)

        cached = self._analog_cache.get((sym, "4h"))
        if cached and time.time() - cached[0] < 300:
            ana = cached[1]
        else:
            ana = pattern_analogs(bars15[:, 4], self._library15, window=16, horizon=16, self_symbol=sym)
            self._analog_cache[(sym, "4h")] = (time.time(), ana)
        analog_dir = (ana["fwd_up"] if side > 0 else 1 - ana["fwd_up"]) if ana.get("n") else None

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
            reasons.append(f"rischio aggiuntivo oltre il {settings.max_risk * 100:.0f}%")
        if score < settings.min_signal_score:
            reasons.append("punteggio sotto soglia")
        zone_bypass = False  # the 4h-range zone is replaced by the predicted entry point
        if side > 0 and not entry_ok:
            reasons.append(f"in attesa del punto d'ingresso previsto ({dip_price:.6g}, tra circa {(dip_step + 1) * 15} min)")
        costs = 2 * (settings.taker_fee + settings.slippage)
        if side > 0 and median_4h <= price * (1 + costs):
            reasons.append("prezzo attuale già sopra la mediana prevista a 4 ore")
        if side < 0 and median_4h >= price * (1 - costs):
            reasons.append("prezzo attuale già sotto la mediana prevista a 4 ore")
        if time.time() - c4["ts"] > settings.max_forecast_age_s:
            reasons.append("previsione a 4 ore troppo vecchia")
        btc = self.btc
        if side > 0 and not btc["ok"]:
            reasons.append(f"BTC in calo (4 ore {btc['chg_4h'] * 100:+.1f}%, 24 ore {btc['chg_24h'] * 100:+.1f}%)")
        if settings.longs_only and side < 0:
            reasons.insert(0, "previsione al ribasso: l'Executor apre solo long")
        eligible = not reasons
        if forecast is None and not decision:
            self._log_signal(sym, c4, price, p_up, entry_ok, eligible and not self.paused, reasons, extra_risk)

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
        for h in HORIZONS:  # 15m, 1h, 4h
            _, tf, st = HORIZONS[h]
            ch = self.predictor.cache.get((sym, tf))
            ih = horizon_index(ch["bar_t"], TIMEFRAMES[tf], st, ch["paths"].shape[1], time.time()) if ch else None
            if ih is None:
                continue
            cl = ch["paths"][:, ih, 3]
            pu = float((cl > price).mean())
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

        # TP/SL analysis over the next 4 hours (same 15m forecast), kept for reference
        c1 = c4
        best = c1["trade"]["best"] if c1 and c1.get("trade") and c1["trade"]["best"] else None
        return {
            "symbol": sym, "base": m.base, "name": m.name, "rank": m.rank, "price": price,
            "change_24h": m.change_24h, "max_leverage": m.max_leverage,
            "side": "long" if side > 0 else "short", "leverage": lev,
            "p_up_4h": p_up, "conf_4h": conf, "exp_ret_4h": exp_ret, "median_4h": median_4h,
            "forecast_price": c4["last"], "drift": drift,
            "range_pos": range_pos, "range_hi": hi4, "range_lo": lo4, "in_zone": in_zone, "zone_bypass": zone_bypass,
            "dip_price": dip_price, "dip_minutes": (dip_step + 1) * 15, "entry_ok": entry_ok,
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
            "model": c4.get("model", ""), "samples": int(c4["paths"].shape[0]), "forecast_ts": c4["ts"],
            "btc_chg_4h": btc["chg_4h"], "btc_chg_24h": btc["chg_24h"],
        }

    def _log_signal(self, sym, c4, price, p_up, entry_ok, eligible, reasons, extra_risk) -> None:
        """Signal register: the first evaluation of every new 4h forecast, executed or not, with what the Checker
        decided; the Controller fills in the real 4h return when it is due."""
        if self.controller is None or self._logged.get(sym) == c4["ts"]:
            return
        self._logged[sym] = c4["ts"]
        try:
            self.controller.record_signal({
                "ts": c4["ts"], "due": c4["bar_t"] + (HORIZONS["4h"][2] + 1) * 900, "symbol": sym, "price": price,
                "p_up_4h": p_up, "entry_ok": entry_ok, "eligible": eligible,
                "extra_risk": extra_risk, "reasons": reasons, "model": c4.get("model", ""),
                "samples": int(c4["paths"].shape[0]),
            })
        except Exception:
            log.exception("signal register failed")

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._refresh_library15)
                self.btc = self.btc_trend()
                rows = []
                for sym in self.predictor.active_symbols():  # mini: top 12, base: all
                    if sym in self.excluded:
                        continue  # open position: no analysis until it is closed
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
                                  "drops", "drop_priority", "drop_window", "dip_price", "dip_minutes",
                                  "entry_ok")} | {
            "rsi": ind["rsi"], "regime": ind["regime"], "flags": ind["flags"], "ind_score": ind["score"],
            "analog_win": r["analog_dir"], "analog_n": r["analogs"].get("n", 0),
        }

    def signals(self) -> list[dict]:
        """Eligible tokens in opening order: those after a sharp recent drop first (largest drop first),
        then by score. The drop never makes a token eligible, it only moves it up the queue."""
        sig = [r for r in self.ranking if r["eligible"]]
        return sorted(sig, key=lambda r: (r.get("drop_priority", 0) > 0, r.get("drop_priority", 0), r["score"]), reverse=True)
