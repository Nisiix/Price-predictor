import asyncio
import time

import numpy as np
import pytest

from app.agents.executor import Executor
from app.agents.indicators import indicator_view, pattern_analogs, rsi
from app.config import settings
from app.market.feed import Market
from app.predict.probability import first_touch, trade_levels


def path(closes, spread=0.0):
    c = np.array(closes, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    return np.stack([o, np.maximum(o, c) * (1 + spread), np.minimum(o, c) * (1 - spread), c], axis=-1)


def test_levels_10x():
    lv = trade_levels(100.0, 1, 10, 5.0, 2.0, 0.025)
    assert lv["tp"] == pytest.approx(100.5)
    assert lv["sl"] == pytest.approx(99.8)
    assert lv["liq"] == pytest.approx(92.5)
    s = trade_levels(100.0, -1, 20, 5.0, 2.0, 0.025)
    assert s["tp"] == pytest.approx(99.75)
    assert s["sl"] == pytest.approx(100.1)


def test_first_touch_counts_sl_when_bar_hits_both():
    up = path([100.2, 100.6, 100.7])  # hits TP (100.5) on bar 2
    down = path([99.9, 99.7, 99.5])  # hits SL (99.8) on bar 2
    both = np.array([[[100, 100.6, 99.7, 100]] * 3])  # one bar touches both
    paths = np.concatenate([up[None], down[None], both])
    r = first_touch(paths, 100.0, 1, 10, 5.0, 2.0, 0.025, 0.0)
    assert r["p_tp"] == pytest.approx(1 / 3)
    assert r["p_sl"] == pytest.approx(2 / 3)
    assert r["p_liq"] == 0
    r_short = first_touch(paths, 100.0, -1, 10, 5.0, 2.0, 0.025, 0.0)
    assert r_short["p_tp"] == pytest.approx(1 / 3)  # the down path


def test_first_touch_liquidation_on_gap():
    gap = path([100, 90, 89])
    r = first_touch(gap[None], 100.0, 1, 10, 5.0, 2.0, 0.025, 0.0)
    assert r["p_liq"] == 1.0 and r["ev"] == pytest.approx(-1.0)


def test_indicators_and_analogs():
    rng = np.random.default_rng(0)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.002, 400)))
    bars = np.column_stack([np.arange(400) * 60, c, c * 1.001, c * 0.999, c, np.ones(400)])
    v = indicator_view(bars)
    assert 0 <= v["rsi"] <= 100 and -1 <= v["score"] <= 1
    assert rsi(np.linspace(1, 2, 50)) == 100.0
    a = pattern_analogs(c[-60:], {"A": c, "B": c[::-1].copy()}, self_symbol="A")
    assert a["n"] > 0 and 0 <= a["long_win"] <= 1


# ------------------------------------------------------------------ executor
class FakeStore:
    def last_price(self, s):
        return None


class FakeFeed:
    def __init__(self, price):
        self.store = FakeStore()
        self.markets = {"PERP_SOL_USDC": Market("PERP_SOL_USDC", "SOL", "Solana", 7, 20, 0.05, 0.025, 0.01, 0.001, 0.01, 10,
                                                mark_price=price)}

    def set(self, p):
        self.markets["PERP_SOL_USDC"].mark_price = p


class FakeChecker:
    paused = False
    macro_risk = 0.0
    excluded: set = set()

    def signals(self):
        return []


class FakeController:
    def __init__(self):
        self.trades = []

    def record_trade(self, p):
        self.trades.append(p)


class FakeBus:
    def publish(self, *a, **k):
        pass

    def agent_log(self, *a, **k):
        pass


def make(price=100.0):
    feed = FakeFeed(price)
    ctl = FakeController()
    ex = Executor(feed, FakeChecker(), ctl, FakeBus())
    return feed, ctl, ex


def open_long(ex, lev=10):
    sig = {"symbol": "PERP_SOL_USDC", "base": "SOL", "side": "long", "leverage": lev, "kronos_p_tp": 0.6, "extra_risk": 0.2,
           "conf_4h": 0.7, "p_up_4h": 0.7, "analog_dir": 0.6,
           "indicators": {"regime": "laterale", "rsi": 55}, "analogs": {"long_win": 0.5}}
    asyncio.run(ex.open_from_signal(sig))
    return next(iter(ex.positions.values()))


def step(ex, feed, price):
    feed.set(price)
    p = ex.open_positions()[0]
    reason = ex._check(p, price, 0.5)
    if reason:
        asyncio.run(ex.close(p.id, reason, ex._exit_level(p, reason, price)))
    return reason


def test_take_profit_at_plus_5_leveraged(monkeypatch):
    monkeypatch.setattr(settings, "use_take_profit", True)
    feed, ctl, ex = make()
    p = open_long(ex)
    assert p.margin == settings.margin_usdc and p.notional == settings.margin_usdc * 10
    assert step(ex, feed, p.entry * 1.004) is None  # +4% leveraged: keep open
    r = step(ex, feed, p.entry * 1.0051)
    assert r.startswith("take profit")
    t = ctl.trades[-1]
    assert t.lev_move == pytest.approx(5.0 - settings.slippage * 10 * 100, abs=0.01)  # TP triggers a market close: exit slippage
    assert t.pnl < settings.margin_usdc * 0.05  # fees are paid


def test_stop_loss_at_minus_2_leveraged(monkeypatch):
    monkeypatch.setattr(settings, "use_initial_stop", True)
    feed, ctl, ex = make()
    p = open_long(ex)
    r = step(ex, feed, p.entry * 0.9979)
    assert r.startswith("stop loss")
    assert ctl.trades[-1].pnl < 0


def test_liquidation_is_cross_margin_and_far():
    feed, ctl, ex = make()
    p = open_long(ex, lev=20)
    assert p.liq <= p.entry * 0.5 + 1e-9  # liquidation at least 50% below the entry
    r = step(ex, feed, p.liq * 0.99)
    assert r == "liquidazione"
    assert ctl.trades[-1].pnl < -p.margin  # cross margin: the account absorbs the loss


def test_analyst_close_all_and_balance_accounting(monkeypatch):
    monkeypatch.setattr(settings, "hold_losers", False)  # flat trade is slightly negative after fees
    feed, ctl, ex = make()
    start = ex.balance
    p = open_long(ex)
    feed.set(p.entry)  # flat
    asyncio.run(ex.close_all("Analyst: test"))
    assert not ex.open_positions()
    t = ctl.trades[-1]
    assert t.reason.startswith("Analyst")
    assert ex.balance == pytest.approx(start + t.pnl)


def test_position_cap_is_optional(monkeypatch):
    feed, ctl, ex = make()
    open_long(ex)
    ex.positions["X"] = ex.positions[next(iter(ex.positions))].__class__(**{**ex.open_positions()[0].__dict__, "id": "X"})
    assert settings.max_positions == 0 and ex.can_open()[0]  # default: no fixed cap
    monkeypatch.setattr(settings, "max_positions", 2)
    assert not ex.can_open()[0]


def test_opens_until_the_liquidation_rule_would_break():
    feed, ctl, ex = make()
    margins = []
    for i in range(30):
        sym = f"PERP_T{i}_USDC"
        feed.markets[sym] = Market(sym, f"T{i}", f"T{i}", 3, 20, 0.05, 0.025, 0.01, 0.01, 0.001, 10, mark_price=10.0)
        sig = {"symbol": sym, "base": f"T{i}", "side": "long", "leverage": 10, "kronos_p_tp": 0.7, "extra_risk": 0.3,
               "conf_4h": 0.75, "indicators": {"regime": "laterale", "rsi": 50}, "analogs": {}}
        n = len(ex.open_positions())
        asyncio.run(ex.open_from_signal(sig))
        if len(ex.open_positions()) == n:
            break
        margins.append(ex.open_positions()[-1].margin)
    assert 1 < len(margins) < 30  # several positions, then it stops by itself
    for q in ex.open_positions():
        assert abs(ex.cross_liq(q) / q.entry - 1) >= 0.5 - 1e-6


def test_no_take_profit_by_default_and_reversal_exit():
    feed, ctl, ex = make()
    p = open_long(ex)
    assert p.tp == 0.0  # exit on reversal, not on +5%
    assert step(ex, feed, p.entry * 1.006) is None  # +6% leveraged: still open

    class Pred:
        cache = {}

    ex.predictor = Pred()

    def forecast(p_up, ts):
        n = 24
        ups = int(round(p_up * n))
        last = 100.0
        closes = np.array([last * 1.01] * ups + [last * 0.99] * (n - ups))
        paths = np.repeat(closes[:, None, None], 16, axis=1).repeat(4, axis=2)
        Pred.cache[("PERP_SOL_USDC", "15m")] = {"ts": ts, "last": last, "paths": paths}

    t0 = p.opened_ts + 1
    forecast(0.45, t0)  # opposite only 55%: inside the margin, no exit
    assert ex._reversal(p) is None and p.reverse_count == 0
    forecast(0.30, t0 + 1)  # opposite 70%: first confirmation
    assert ex._reversal(p) is None and p.reverse_count == 1
    assert ex._reversal(p) is None  # same forecast again does not count twice
    forecast(0.25, t0 + 2)  # second confirmation -> close
    r = ex._reversal(p)
    assert r and r.startswith("inversione")


def test_profit_lock_moves_stop_to_plus_2_after_plus_5():
    feed, ctl, ex = make()
    p = open_long(ex, lev=10)
    sl0 = p.sl
    assert step(ex, feed, p.entry * 1.0051) is None  # +5.1% leveraged: lock, no exit
    assert p.locked and p.sl > sl0
    assert p.sl == pytest.approx(p.entry * 1.002)  # +2% leveraged at 10x = +0.2% price
    r = step(ex, feed, p.entry * 1.0019)  # back below the lock level
    assert r.startswith("stop di protezione")
    assert ctl.trades[-1].lev_move > 0  # closed in profit (gross)


def test_no_loss_stop_by_default_recovery_check_after_hours(monkeypatch):
    monkeypatch.setattr(settings, "hold_losers", False)
    feed, ctl, ex = make()
    p = open_long(ex)
    assert p.sl == 0.0
    assert step(ex, feed, p.entry * 0.99) is None  # -10% leveraged: no stop in loss

    def cache(high):
        n, L = 24, 16
        paths = np.full((n, L, 4), p.entry * 0.99)
        paths[:, :, 1] = high
        return {"ts": 0, "last": p.entry * 0.99, "paths": paths}

    p.opened_ts -= 1.0 * 3600  # 1 h: too early to judge
    assert ex._recovery(p, cache(p.entry * 0.995)) is None
    p.opened_ts -= 1.5 * 3600  # 2.5 h, projection never reaches break-even -> close
    r = ex._recovery(p, cache(p.entry * 0.995))
    assert r and r.startswith("nessun recupero") and p.recovery_prob == 0
    assert ex._recovery(p, cache(p.entry * 1.01)) is None  # projection recovers -> keep


def test_sizing_keeps_every_liquidation_half_the_entry_away():
    feed, ctl, ex = make()
    p1 = open_long(ex, lev=20)
    assert p1.margin < 0.10 * 1000  # 10% (risk 20%) at 20x would put liquidation too close on 1000 USDC
    # second position on another market
    feed.markets["PERP_ETH_USDC"] = Market("PERP_ETH_USDC", "ETH", "Ethereum", 2, 20, 0.05, 0.025, 0.01, 0.01, 0.001, 10,
                                           mark_price=2000.0)
    sig = {"symbol": "PERP_ETH_USDC", "base": "ETH", "side": "short", "leverage": 20, "kronos_p_tp": 0.7, "extra_risk": 0.3,
           "conf_4h": 0.75, "indicators": {"regime": "laterale", "rsi": 50}, "analogs": {}}
    asyncio.run(ex.open_from_signal(sig))
    ps = ex.open_positions()
    short = [q for q in ps if q.side < 0][0]
    assert short.margin == pytest.approx(0.07 * 1000, rel=0.02)  # risk 30% -> 7% of capital, no liquidation sizing
    for q in ps:
        q.liq = ex.cross_liq(q)
    assert p1.liq <= p1.entry * 0.5 * 1.05  # the long stays far from liquidation (within the short's extra load)


def test_signals_prioritise_recent_drops_without_changing_eligibility():
    from app.agents.checker import Checker

    ch = Checker.__new__(Checker)
    ch.ranking = [
        {"symbol": "A", "eligible": True, "score": 0.6, "drop_priority": 0.0},
        {"symbol": "B", "eligible": True, "score": 0.4, "drop_priority": 0.08},
        {"symbol": "C", "eligible": False, "score": 0.9, "drop_priority": 0.20},  # big drop but not eligible
        {"symbol": "D", "eligible": True, "score": 0.5, "drop_priority": 0.06},
    ]
    assert [r["symbol"] for r in ch.signals()] == ["B", "D", "A"]


def test_losing_positions_are_never_closed_by_agents():
    feed, ctl, ex = make()
    p = open_long(ex)
    feed.set(p.entry * 0.99)
    ex._mark(p, p.entry * 0.99)
    asyncio.run(ex.close_all("Analyst: test"))
    assert p.status == "open"  # Analyst cannot close a loser
    ex._reversal = lambda q: "inversione della previsione 4h"
    assert ex._check(p, p.entry * 0.99, 0.5) is None  # reversal ignored while at a loss
    assert ex._check(p, p.entry * 1.003, 0.5).startswith("inversione")  # in profit: closes


def test_entry_rejected_when_price_already_above_the_forecast():
    feed, ctl, ex = make(price=100.0)

    class Pred:
        cache = {}

        def decision_cache(self, sym):
            return self.cache.get((sym, "15m"))

    ex.predictor = Pred()
    n, L = 24, 16
    paths = np.full((n, L, 4), 101.0)  # forecast made at 99.0: 4h median 101
    Pred.cache[("PERP_SOL_USDC", "15m")] = {"ts": time.time(), "last": 99.0, "paths": paths}
    ok, why = ex.forecast_still_valid("PERP_SOL_USDC", 1, 100.0)
    assert ok  # 101 > 100 + costs, all samples above
    ok, why = ex.forecast_still_valid("PERP_SOL_USDC", 1, 101.5)  # price already above the forecast
    assert not ok and "probabilit" in why or "mediana" in why
    Pred.cache[("PERP_SOL_USDC", "15m")]["ts"] = time.time() - 3600
    assert not ex.forecast_still_valid("PERP_SOL_USDC", 1, 100.0)[0]  # stale forecast


def test_predicted_dip_finds_the_low_before_the_rise():
    from app.predict.probability import predicted_dip

    lows = np.array([99.5, 99.0, 98.6, 98.9, 99.8, 100.5])
    paths = np.zeros((10, 6, 4))
    paths[:, :, 2] = lows
    price, i = predicted_dip(paths, 6)
    assert price == pytest.approx(98.6) and i == 2


def test_risk_tiers_share_of_capital():
    from app.agents.executor import Executor as E

    assert E.risk_share(0.20) == 0.10 and E.risk_share(0.25) == 0.10
    assert E.risk_share(0.28) == 0.07 and E.risk_share(0.33) == 0.05
    assert E.risk_share(0.40) == 0.03 and E.risk_share(0.55) == 0.0  # above 40%: no opening
    feed, ctl, ex = make()
    p = open_long(ex, lev=10)  # risk 0.2 -> 10% of 1000 = 100 USDC, fits the liquidation rule at 10x
    assert p.margin == pytest.approx(100.0, rel=0.01)
