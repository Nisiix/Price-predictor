"""Checker agent: ranks the universe for breakout longs at 10x-20x.

For every token it combines
  1. Kronos (15m candles, 4 hours ahead): the predicted SIZE of the move (amplitude, mean of |4h return| over the
     sampled paths) and its direction (probability of a long, which must not be against it);
  2. the breakout: the live price above the highest high of the last hour's closed candles (the roof), i.e. the
     predicted move has started and it is going up; with PP_VOL5_FILTER (on by default, user choice 18 Nov) the
     token's 5-minute volatility must also be in its own top 10% and the price must have covered half of the
     expected 5-minute range over the last 5 minutes (indicators.vol5_breakout);
  3. indicators (EMA 9/21/50, RSI, MACD, Bollinger %B, ATR, volume) and historical analogs (the 25 most similar
     4-hour shapes in the history of every token), which with volatility and the Analyst's macro risk give the
     extra risk shown next to the Kronos probability (it decides the margin tier).
"""
from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from ..config import HORIZONS, TIMEFRAMES, settings
from ..predict.probability import horizon_index
from .indicators import indicator_view, pattern_analogs, vol5_breakout

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
        self._broke: dict[str, int] = {}  # symbol -> breakout already logged for that forecast (1) / eligible (2)
        self.btc = {"ok": True, "chg_4h": None, "chg_24h": None}

    def pause(self, seconds: float, reason: str) -> None:
        self.paused_until = max(self.paused_until, time.time() + seconds)
        self.pause_reason = reason

    @property
    def paused(self) -> bool:
        return settings.analyst_blocks and time.time() < self.paused_until

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
        """4-hour breakout plan for one token: predicted amplitude and direction from Kronos (15m candles, 4 h
        ahead), the roof (last hour's high) the price must break, and the extra risk from indicators, historical
        analogs, volatility and macro."""
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
        # predicted amplitude: how much the paths move in 4 hours, either way, from the price Kronos started from
        amplitude = float(np.abs(closes / c4["last"] - 1).mean())
        # the roof: highest high of the last hour's CLOSED 15m candles (the forming one is the breakout itself)
        closed15 = bars15[:-1] if bars15[-1, 0] + 900 > now else bars15
        roof = float(closed15[-settings.roof_bars:, 2].max())
        trigger = roof * (1 + settings.breakout_buffer)
        chase = roof * (1 + settings.breakout_max_chase)
        breakout = trigger < price <= chase
        ind = indicator_view(bars15)

        cached = self._analog_cache.get((sym, "4h"))
        if cached and time.time() - cached[0] < 300:
            ana = cached[1]
        else:
            ana = pattern_analogs(bars15[:, 4], self._library15, window=16, horizon=16, self_symbol=sym)
            self._analog_cache[(sym, "4h")] = (time.time(), ana)
        analog_dir = (ana["fwd_up"] if side > 0 else 1 - ana["fwd_up"]) if ana.get("n") else None

        hi4, lo4 = float(bars5[:, 2].max()), float(bars5[:, 3].min())
        range_pos = float((price - lo4) / (hi4 - lo4)) if hi4 > lo4 else 0.5  # shown only, not an entry rule

        ind_agree = (1 + side * ind["score"]) / 2
        sl_move10 = settings.stop_loss_pct / 100 / 10
        noise = ind["atr_pct"] / sl_move10 if sl_move10 else 0  # 15m ATR vs the stop distance at 10x
        risk_parts = {
            # user rule: a Kronos probability between 50% and 70% weighs on the risk as 70% does (the breakout gives
            # the direction, Kronos only has to be above 50%); only above 70% does a higher probability lower it
            "kronos_avverso": min(1 - conf, 1 - settings.risk_conf_floor),
            "indicatori_contrari": 1 - ind_agree,
            "pattern_storici": (1 - analog_dir) if analog_dir is not None else 0.5,
            "volatilita": float(np.clip((noise - 1) / 3.0, 0, 1)),
            "macro_analyst": self.macro_risk / 100,
        }
        weights = {"kronos_avverso": 0.30, "indicatori_contrari": 0.20, "pattern_storici": 0.20,
                   # volatility is measured against the old -2% leveraged stop, which no longer exists: it would penalise
                   # exactly the tokens with the large predicted move the breakout needs, so it is off by default
                   "volatilita": 0.15 if settings.risk_volatility else 0.0, "macro_analyst": 0.15}
        extra_risk = float(sum(risk_parts[k] * w for k, w in weights.items()))
        score = conf * (1 - extra_risk)
        lev = self._leverage(extra_risk, m.max_leverage)

        reasons = []
        if side > 0 and p_up <= settings.min_confidence:
            reasons.append(f"Kronos non favorevole al rialzo a 4 ore ({p_up * 100:.0f}%, serve oltre il {settings.min_confidence * 100:.0f}%)")
        if amplitude < settings.min_move:
            reasons.append(f"movimento previsto a 4 ore {amplitude * 100:.1f}%, sotto la soglia di {settings.min_move * 100:g}%")
        if settings.max_risk and extra_risk > settings.max_risk:
            reasons.append(f"rischio aggiuntivo oltre il {settings.max_risk * 100:.0f}%")
        if score < settings.min_signal_score:
            reasons.append("punteggio sotto soglia")
        if price <= trigger:
            reasons.append(f"in attesa della rottura del tetto dell'ultima ora ({roof:.6g}, ingresso sopra {trigger:.6g})")
        elif price > chase:
            reasons.append(f"prezzo già {(price / roof - 1) * 100:.1f}% sopra il tetto {roof:.6g}: troppo tardi, attendo un nuovo tetto")
        if time.time() - c4["ts"] > settings.max_forecast_age_s:
            reasons.append("previsione a 4 ore troppo vecchia")
        btc = self.btc
        if settings.btc_filter and side > 0 and not btc["ok"]:
            reasons.append(f"BTC in calo (4 ore {btc['chg_4h'] * 100:+.1f}%, 24 ore {btc['chg_24h'] * 100:+.1f}%)")
        v5 = vol5_breakout(self.feed.store.get(sym, "1m"), price, now, settings.vol5_window, settings.vol5_top,
                           settings.vol5_trigger)
        if settings.vol5_filter:
            if not v5["ready"]:
                reasons.append("storico a 1 minuto insufficiente per misurare la volatilità a 5 minuti")
            elif not v5["hot"]:
                reasons.append(f"volatilità a 5 minuti al {v5['rank'] * 100:.0f}° percentile, serve il "
                               f"{settings.vol5_top * 100:.0f}% più alto")
            elif not v5["pushed"]:
                reasons.append(f"spinta degli ultimi 5 minuti {v5['push'] * 100:+.2f}%, serve almeno "
                               f"{v5['need'] * 100:.2f}% (metà del movimento atteso)")
        if settings.longs_only and side < 0:
            reasons.insert(0, "previsione al ribasso: l'Executor apre solo long")
        eligible = not reasons
        if forecast is None and not decision:
            self._log_signal(sym, c4, price, p_up, amplitude, breakout, eligible and not self.paused, reasons, extra_risk)

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
                "kronos_avverso": min(1 - cf, 1 - settings.risk_conf_floor),
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
            "range_pos": range_pos, "range_hi": hi4, "range_lo": lo4,
            "amplitude": amplitude, "roof": roof, "breakout_price": trigger, "chase_price": chase, "breakout": breakout,
            "vol5_rank": v5["rank"], "vol5_push": v5["push"], "vol5_need": v5["need"], "vol5_ok": v5["ok"],
            "kronos_p_tp": conf,  # probability used by the Controller buckets
            "kronos_p_sl": 1 - conf, "kronos_p_liq": best["p_liq"] if best else 0.0,
            "ev": exp_ret * side * lev,
            "levels": best["levels"] if best else None, "short_term": best,
            "combined_prob": conf, "extra_risk": extra_risk, "risk_parts": risk_parts, "score": score,
            "indicators": ind, "analogs": ana, "analog_dir": analog_dir,
            "eligible": eligible and not self.paused, "reasons": reasons,
            "source": c4["source"], "age": time.time() - c4["ts"], "options": c1["trade"]["options"][:4] if best else [],
            "horizons": per_h, "best_h": best_h,
            "model": c4.get("model", ""), "samples": int(c4["paths"].shape[0]), "forecast_ts": c4["ts"],
            "btc_chg_4h": btc["chg_4h"], "btc_chg_24h": btc["chg_24h"],
        }

    def _log_signal(self, sym, c4, price, p_up, amplitude, breakout, eligible, reasons, extra_risk) -> None:
        """Signal register: one row per new 4h forecast, executed or not (price, probability and predicted amplitude
        at its first evaluation), then the first breakout of the roof seen while that forecast is in use, and whether
        it was eligible. The Controller fills in the real 4h returns, from the forecast and from the breakout."""
        if self.controller is None:
            return
        try:
            if self._logged.get(sym) != c4["ts"]:
                self._logged[sym] = c4["ts"]
                self._broke.pop(sym, None)
                self.controller.record_signal({
                    "ts": c4["ts"], "due": c4["bar_t"] + (HORIZONS["4h"][2] + 1) * 900, "symbol": sym, "price": price,
                    "p_up_4h": p_up, "amplitude": amplitude, "breakout": False, "eligible": False,
                    "extra_risk": extra_risk, "reasons": reasons, "model": c4.get("model", ""),
                    "samples": int(c4["paths"].shape[0]),
                })
            state = self._broke.get(sym, 0)  # 0 none yet, 1 breakout logged, 2 eligible breakout logged
            if breakout and (state == 0 or (eligible and state == 1)):
                self._broke[sym] = 2 if eligible else 1
                self.controller.record_breakout(sym, c4["ts"], price, time.time() + HORIZONS["4h"][0], eligible)
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
                rows.sort(key=lambda r: (r["amplitude"], r["score"]), reverse=True)  # same order as the openings
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
                                  "p_up_4h", "conf_4h", "range_pos", "horizons", "best_h",
                                  "amplitude", "roof", "breakout_price", "chase_price", "breakout",
                                  "vol5_rank", "vol5_push", "vol5_need", "vol5_ok")} | {
            "rsi": ind["rsi"], "regime": ind["regime"], "flags": ind["flags"], "ind_score": ind["score"],
            "analog_win": r["analog_dir"], "analog_n": r["analogs"].get("n", 0),
        }

    def signals(self) -> list[dict]:
        """Eligible tokens (breaking their roof now) in opening order: largest predicted 4h move first (user rule),
        then by score."""
        sig = [r for r in self.ranking if r["eligible"]]
        return sorted(sig, key=lambda r: (r.get("amplitude", 0.0), r["score"]), reverse=True)
