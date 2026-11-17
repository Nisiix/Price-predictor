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
    sig = {"symbol": "PERP_SOL_USDC", "side": "long", "leverage": lev, "kronos_p_tp": 0.6, "extra_risk": 0.2,
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


def test_take_profit_at_plus_5_leveraged():
    feed, ctl, ex = make()
    p = open_long(ex)
    assert p.margin == settings.margin_usdc and p.notional == settings.margin_usdc * 10
    assert step(ex, feed, p.entry * 1.004) is None  # +4% leveraged: keep open
    r = step(ex, feed, p.entry * 1.0051)
    assert r.startswith("take profit")
    t = ctl.trades[-1]
    assert t.lev_move == pytest.approx(5.0 - settings.slippage * 10 * 100, abs=0.01)  # TP triggers a market close: exit slippage
    assert t.pnl < settings.margin_usdc * 0.05  # fees are paid


def test_stop_loss_at_minus_2_leveraged():
    feed, ctl, ex = make()
    p = open_long(ex)
    r = step(ex, feed, p.entry * 0.9979)
    assert r.startswith("stop loss")
    assert ctl.trades[-1].pnl < 0


def test_liquidation_on_gap():
    feed, ctl, ex = make()
    p = open_long(ex, lev=20)
    r = step(ex, feed, p.liq * 0.99)
    assert r == "liquidazione"
    assert ctl.trades[-1].pnl == pytest.approx(-p.margin - p.fees)


def test_analyst_close_all_and_balance_accounting():
    feed, ctl, ex = make()
    start = ex.balance
    p = open_long(ex)
    feed.set(p.entry)  # flat
    asyncio.run(ex.close_all("Analyst: test"))
    assert not ex.open_positions()
    t = ctl.trades[-1]
    assert t.reason.startswith("Analyst")
    assert ex.balance == pytest.approx(start + t.pnl)


def test_max_two_positions():
    feed, ctl, ex = make()
    open_long(ex)
    ex.positions["X"] = ex.positions[next(iter(ex.positions))].__class__(**{**ex.open_positions()[0].__dict__, "id": "X"})
    ok, why = ex.can_open()
    assert settings.max_positions == 2 and not ok
