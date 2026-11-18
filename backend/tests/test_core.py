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
    assert p.notional == pytest.approx(500.0)  # 10% of 1000 at 10x would be 1000: capped at 50% of the capital
    assert step(ex, feed, p.entry * 1.004) is None  # +4% leveraged: keep open
    r = step(ex, feed, p.entry * 1.0051)
    assert r.startswith("take profit")
    t = ctl.trades[-1]
    assert t.lev_move == pytest.approx(5.0 - settings.slippage * 10 * 100, abs=0.01)  # TP triggers a market close: exit slippage
    assert t.pnl < t.margin * 0.05  # fees are paid


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


@pytest.mark.parametrize("lev", [10, 15, 20])
def test_protective_stop_closes_at_plus_2_net(lev):
    feed, ctl, ex = make()
    p = open_long(ex, lev=lev)
    level, trigger = ex.lock_levels(p)
    assert trigger == pytest.approx(level * 1.002)  # no 15m candles in the test: 0.2% minimum distance
    assert step(ex, feed, trigger * 0.9999) is None and not p.locked  # not armed yet
    assert step(ex, feed, trigger * 1.0001) is None and p.locked  # armed, stop at the +2% net level
    assert p.sl == pytest.approx(level)
    r = step(ex, feed, level * (1 - 1e-12))  # back to the stop (no gap through it)
    assert r.startswith("stop di protezione")
    t = ctl.trades[-1]
    assert t.roe == pytest.approx(2.0, abs=0.01)  # +2% of the margin after fees and slippage, at any leverage


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
    assert p1.margin == pytest.approx(25.0)  # 10% (risk 20%) at 20x = 200% exposure: capped at 50% of the capital
    # second position on another market
    feed.markets["PERP_ETH_USDC"] = Market("PERP_ETH_USDC", "ETH", "Ethereum", 2, 20, 0.05, 0.025, 0.01, 0.01, 0.001, 10,
                                           mark_price=2000.0)
    sig = {"symbol": "PERP_ETH_USDC", "base": "ETH", "side": "short", "leverage": 20, "kronos_p_tp": 0.7, "extra_risk": 0.3,
           "conf_4h": 0.75, "indicators": {"regime": "laterale", "rsi": 50}, "analogs": {}}
    asyncio.run(ex.open_from_signal(sig))
    ps = ex.open_positions()
    short = [q for q in ps if q.side < 0][0]
    assert short.margin == pytest.approx(25.0, rel=0.02)  # risk 30% -> 7% of capital, but at 20x the token cap binds
    for q in ps:
        q.liq = ex.cross_liq(q)
    assert p1.liq <= p1.entry * 0.5 * 1.05  # the long stays far from liquidation (within the short's extra load)


def test_signals_put_the_largest_predicted_move_first():
    from app.agents.checker import Checker

    ch = Checker.__new__(Checker)
    ch.ranking = [
        {"symbol": "A", "eligible": True, "score": 0.6, "amplitude": 0.016},
        {"symbol": "B", "eligible": True, "score": 0.4, "amplitude": 0.030},
        {"symbol": "C", "eligible": False, "score": 0.9, "amplitude": 0.050},  # largest move but not eligible
        {"symbol": "D", "eligible": True, "score": 0.5, "amplitude": 0.020},
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


def test_entry_rejected_when_kronos_is_against_the_long_from_the_live_price():
    feed, ctl, ex = make(price=100.0)

    class Pred:
        cache = {}

        def decision_cache(self, sym):
            return self.cache.get((sym, "15m"))

    ex.predictor = Pred()
    n, L = 24, 16
    paths = np.full((n, L, 4), 101.0)  # forecast made at 99.0: 4h median 101
    bar_t = time.time() // 900 * 900 - 900  # fresh: computed in the 15m bar now forming
    Pred.cache[("PERP_SOL_USDC", "15m")] = {"ts": time.time(), "last": 99.0, "paths": paths, "bar_t": bar_t}
    ok, why = ex.forecast_still_valid("PERP_SOL_USDC", 1, 100.0)
    assert ok  # every sample ends above 100
    ok, why = ex.forecast_still_valid("PERP_SOL_USDC", 1, 101.5)  # price already above every path: 0% up
    assert not ok and "probabilit" in why
    Pred.cache[("PERP_SOL_USDC", "15m")]["ts"] = time.time() - 3600
    assert not ex.forecast_still_valid("PERP_SOL_USDC", 1, 100.0)[0]  # stale forecast


def test_risk_tiers_share_of_capital():
    from app.agents.executor import Executor as E

    assert E.risk_share(0.20) == 0.10 and E.risk_share(0.25) == 0.10
    assert E.risk_share(0.28) == 0.07 and E.risk_share(0.33) == 0.05
    assert E.risk_share(0.40) == 0.03 and E.risk_share(0.55) == 0.03  # above 40%: smallest tier, no block
    feed, ctl, ex = make()
    p = open_long(ex, lev=10)  # risk 0.2 -> 10% of 1000 = 100 USDC, but 1000 notional is above the 50% token cap
    assert p.margin == pytest.approx(50.0, rel=0.01)


def test_simulated_account_survives_a_restart(tmp_path):
    state = tmp_path / "paper_state.json"
    feed = FakeFeed(100.0)
    ex = Executor(feed, FakeChecker(), FakeController(), FakeBus(), state)
    assert not ex.restored  # no file yet: initial balance
    p = open_long(ex)
    ex.equity_hist.append((time.time(), ex.paper_equity()))
    balance = ex.balance
    ex.save_state()

    ex2 = Executor(feed, FakeChecker(), FakeController(), FakeBus(), state)
    assert ex2.restored
    assert ex2.balance == pytest.approx(balance)
    assert ex2.paper_started == pytest.approx(ex.paper_started)
    assert len(ex2.equity_hist) == 1
    q = ex2.positions[p.id]
    assert (q.symbol, q.side, q.entry, q.margin, q.leverage, q.status) == (p.symbol, p.side, p.entry, p.margin, p.leverage, "open")
    # a new position after the restore gets a fresh id
    new = ex2._make_position("PERP_SOL_USDC", 1, 10, 10.0, 100.0, "manual")
    assert new.id != p.id
    # closing a restored position credits the restored balance
    asyncio.run(ex2.close(q.id, "test", 100.0))
    assert not ex2.open_positions()
    assert ex2.balance > balance


def test_unreadable_simulated_account_starts_fresh(tmp_path):
    state = tmp_path / "paper_state.json"
    state.write_text("{not json")
    ex = Executor(FakeFeed(100.0), FakeChecker(), FakeController(), FakeBus(), state)
    assert not ex.restored
    assert ex.balance == settings.paper_start_balance


def test_horizon_index_moves_along_an_older_forecast():
    from app.predict.probability import horizon_index
    bar_t = 1_000_000.0  # last closed 1m bar when Kronos ran; the bar forming then closes at bar_t + 120
    # fresh forecast (inside the bar that was forming): the 1-minute horizon is the first step
    assert horizon_index(bar_t, 60, 1, 30, bar_t + 60 + 10) == 0
    # 6 bars later the same horizon is read 6 steps further along the paths
    assert horizon_index(bar_t, 60, 1, 30, bar_t + 60 + 6 * 60 + 5) == 6
    assert horizon_index(bar_t, 60, 15, 30, bar_t + 60 + 6 * 60 + 5) == 20
    # the forecast does not reach 15 minutes from now any more
    assert horizon_index(bar_t, 60, 15, 30, bar_t + 60 + 16 * 60) is None


# ------------------------------------------------------------------ review fixes (A, B, C, E, G, H, I, 13, 15)
def _service_with_history(n_bars=200):
    from app.config import TIMEFRAMES
    from app.market.candles import CandleStore
    from app.predict.service import PredictionService

    class M:
        rank, mmr, max_leverage, symbol = 1, 0.05, 20, "PERP_X_USDC"

    class Feed:
        def __init__(self):
            self.store = CandleStore(TIMEFRAMES)
            self.markets = {"PERP_X_USDC": M()}
            now = int(time.time() // 900 * 900)
            self.store.set_history("PERP_X_USDC", "15m", [
                [now - 900 * (n_bars - i), 1 + i * 1e-3, 1.01 + i * 1e-3, 0.99 + i * 1e-3, 1 + i * 1e-3, 5.0] for i in range(n_bars)])

        async def refresh_recent(self, *a, **k):
            return None

    return PredictionService(Feed())


def test_decision_forecast_is_kept_and_used_without_a_decision_model():
    svc = _service_with_history()
    assert svc.decider is None  # default configuration: the main model decides
    old = svc._run_tf("PERP_X_USDC", "15m")
    svc.cache[("PERP_X_USDC", "15m")] = old
    fresh = svc.run_decision("PERP_X_USDC")
    assert fresh is not None and fresh["samples"] == settings.decision_samples
    assert svc.decision_cache("PERP_X_USDC") is fresh  # before the fix the old forecast was used
    assert svc.cache[("PERP_X_USDC", "15m")] is fresh


def test_calibration_scores_the_step_it_read():
    from app.predict.service import Calibration

    cal = Calibration()
    bar_t = 1_000_000.0
    paths = np.full((24, 32, 4), 100.0)
    cal.register({"tf": "15m", "ts": bar_t + 900 + 60, "bar_t": bar_t, "symbol": "X", "last": 100.0, "paths": paths})
    due = {label: d for d, label, *_ in cal.pending}
    assert due["4h"] == bar_t + 17 * 900  # close of path index 15 (step 16)
    assert due["15m"] == bar_t + 2 * 900


def test_volumes_fill_tick_built_candles():
    from app.predict.service import _volumes

    v = _volumes(np.array([10.0, 20.0, 30.0, 0.0]))
    assert v[-1] == pytest.approx(20.0) and v[0] == 10.0


def test_total_exposure_cap(monkeypatch):
    monkeypatch.setattr(settings, "max_token_exposure", 1.0)
    monkeypatch.setattr(settings, "max_total_exposure", 0.6)
    monkeypatch.setattr(settings, "liq_min_distance", 0.01)
    feed, ctl, ex = make()
    p = open_long(ex, lev=10)  # 100 USDC margin at 10x = 1000 notional, total cap 600
    assert p.notional == pytest.approx(600.0, rel=0.01)
    room, why = ex.exposure_room("PERP_ETH_USDC")
    assert room == pytest.approx(0.0, abs=1.0) and "totale" in why


def test_btc_filter_blocks_longs_while_btc_falls(monkeypatch):
    from app.agents.checker import Checker
    from app.config import TIMEFRAMES
    from app.market.candles import CandleStore

    ch = Checker.__new__(Checker)

    class Feed:
        store = CandleStore(TIMEFRAMES)

    ch.feed = Feed()
    now = int(time.time() // 900 * 900)
    closes = np.linspace(110, 100, 100)  # falling over the last day
    ch.feed.store.set_history("PERP_BTC_USDC", "15m", [[now - 900 * (100 - i), c, c, c, c, 1.0] for i, c in enumerate(closes)])
    ch.feed.store.update_tick("PERP_BTC_USDC", 100.0, time.time())
    t = ch.btc_trend()
    assert not t["ok"] and t["chg_4h"] < 0 and t["chg_24h"] < 0
    monkeypatch.setattr(settings, "btc_filter_4h", 0.05)
    monkeypatch.setattr(settings, "btc_filter_24h", 0.10)
    assert ch.btc_trend()["ok"]  # a looser threshold lets small falls through


def test_reversal_reads_four_hours_from_now_against_the_live_price():
    feed, ctl, ex = make(price=100.0)
    p = open_long(ex)

    class Pred:
        cache = {}

        def decision_cache(self, sym):
            return self.cache.get((sym, "15m"))

    ex.predictor = Pred()
    bar_t = time.time() // 900 * 900 - 900 * 3  # forecast two bars old
    paths = np.full((24, 32, 4), 104.0)
    paths[:, 17, 3] = 99.0  # 4 hours from NOW on this older forecast (index 15 + 2 elapsed bars): below the price
    Pred.cache[("PERP_SOL_USDC", "15m")] = {"ts": p.opened_ts + 1, "last": 100.0, "paths": paths, "bar_t": bar_t}
    ex._reversal(p)
    assert p.p_dir_now == 0.0  # index 15 (104) would have said 100%


def test_signal_register_and_stats(tmp_path, monkeypatch):
    from app.agents.controller import Controller

    monkeypatch.setattr(settings, "db_path", tmp_path / "t.sqlite3")
    ctl = Controller(FakeBus())
    for i, (p_up, amp) in enumerate([(0.72, 0.022), (0.58, 0.012), (0.45, 0.004)]):
        ctl.record_signal({"ts": 1.0 + i, "due": 2.0, "symbol": f"S{i}", "price": 100.0, "p_up_4h": p_up,
                           "amplitude": amp, "eligible": False, "extra_risk": 0.3, "reasons": []})
    ctl.record_signal({"ts": 1.0, "due": 2.0, "symbol": "S0", "price": 100.0, "p_up_4h": 0.72,
                       "eligible": False})  # same forecast logged twice: ignored
    ctl.record_breakout("S1", 2.0, 100.5, 3.0, False)  # breakout, not eligible (move too small)
    ctl.record_breakout("S0", 1.0, 100.2, 3.0, False)
    ctl.record_breakout("S0", 1.0, 100.4, 3.0, True)  # later eligible breakout of the same forecast replaces it
    ctl.record_breakout("S0", 1.0, 100.9, 3.0, True)  # ... but only once
    assert ctl.resolve_signals(lambda s: 101.0) == 5  # 3 forecasts + 2 breakouts
    st = ctl.signal_stats()
    assert [g["key"] for g in st["eligible"]] == ["70-75%"] and st["eligible"][0]["n"] == 1
    assert st["eligible"][0]["avg_ret"] == pytest.approx(101.0 / 100.4 - 1)
    assert sum(g["n"] for g in st["breakout"]) == 2
    assert {g["key"] for g in st["amplitude"]} == {"<0.5%", "1-1.5%", "2-3%"}
    assert st["all"][0]["win"] == 1.0 and st["pending"] == 0
    md = ctl.markdown()
    assert "Registro segnali" in md and "ampiezza prevista" in md


# ------------------------------------------------------------------ breakout entry
def _breakout_checker(price, highs, p_up=0.75, move=0.03, age_bars=0):
    """A Checker on synthetic 15m candles whose last closed hour peaks at max(highs), with a fresh Kronos forecast."""
    from app.agents.checker import Checker
    from app.config import TIMEFRAMES
    from app.market.candles import CandleStore

    class Feed:
        store = CandleStore(TIMEFRAMES)
        markets = {"PERP_SOL_USDC": FakeFeed(price).markets["PERP_SOL_USDC"]}

    now = time.time()
    cur = int(now // 900 * 900)
    rows = []
    for i in range(100):
        t = cur - 900 * (100 - i)
        h = highs[i - 96] if i >= 96 else 99.5
        rows.append([t, 99.0, h, 98.5, 99.0, 10.0])
    Feed.store.set_history("PERP_SOL_USDC", "15m", rows)
    Feed.store.set_history("PERP_SOL_USDC", "5m", [[cur - 300 * (48 - i), 99.0, 99.5, 98.5, 99.0, 1.0] for i in range(48)])
    Feed.store.update_tick("PERP_SOL_USDC", price, now)

    class Pred:
        cache = {}

        def decision_cache(self, sym):
            return self.cache.get((sym, "15m"))

    n, ups = 96, int(round(p_up * 96))
    closes = np.array([99.0 * (1 + move)] * ups + [99.0 * (1 - move)] * (n - ups))
    paths = np.repeat(closes[:, None, None], 18, axis=1).repeat(4, axis=2)
    Pred.cache[("PERP_SOL_USDC", "15m")] = {"ts": now, "last": 99.0, "paths": paths, "bar_t": cur - 900 * (1 + age_bars),
                                            "source": "kronos", "trade": None, "model": "base"}
    ch = Checker(Feed(), Pred(), FakeBus())
    return ch


def test_breakout_entry_only_above_the_last_hours_roof():
    highs = [99.6, 100.0, 99.8, 99.7]  # roof 100.0
    r = _breakout_checker(99.9, highs).evaluate("PERP_SOL_USDC")
    assert r["roof"] == pytest.approx(100.0) and not r["breakout"] and not r["eligible"]
    assert any("rottura del tetto" in x for x in r["reasons"])
    r = _breakout_checker(100.05, highs).evaluate("PERP_SOL_USDC")  # above the roof but inside the 0.1% buffer
    assert not r["breakout"]
    r = _breakout_checker(100.2, highs).evaluate("PERP_SOL_USDC")
    assert r["breakout"] and r["eligible"], r["reasons"]
    assert r["amplitude"] == pytest.approx(0.03)
    r = _breakout_checker(100.8, highs).evaluate("PERP_SOL_USDC")  # 0.8% above the roof: still inside the 1% band
    assert r["eligible"], r["reasons"]
    r = _breakout_checker(101.2, highs).evaluate("PERP_SOL_USDC")  # 1.2% above the roof: too late
    assert not r["eligible"] and any("troppo tardi" in x for x in r["reasons"])


def test_breakout_needs_a_large_predicted_move_and_kronos_not_against():
    highs = [99.6, 100.0, 99.8, 99.7]
    r = _breakout_checker(100.2, highs, move=0.01).evaluate("PERP_SOL_USDC")
    assert not r["eligible"] and any("movimento previsto" in x for x in r["reasons"])
    r = _breakout_checker(100.2, highs, p_up=0.40).evaluate("PERP_SOL_USDC")
    assert not r["eligible"]
    assert any("ribasso" in x for x in r["reasons"])
    r = _breakout_checker(100.2, highs, p_up=0.55).evaluate("PERP_SOL_USDC")  # above 50%: no 70% rule any more
    assert not any("Kronos" in x for x in r["reasons"])
    r70 = _breakout_checker(100.2, highs, p_up=0.70).evaluate("PERP_SOL_USDC")
    assert r["extra_risk"] == pytest.approx(r70["extra_risk"])  # 50-70%: Kronos weighs on the risk as at 70%
    assert r["eligible"] == r70["eligible"]
    r80 = _breakout_checker(100.2, highs, p_up=0.80).evaluate("PERP_SOL_USDC")
    assert r80["extra_risk"] < r70["extra_risk"]  # above 70% a higher probability still lowers it


def test_order_cancelled_if_the_breakout_faded():
    feed, ctl, ex = make(price=99.9)
    sig = {"symbol": "PERP_SOL_USDC", "base": "SOL", "side": "long", "leverage": 10, "kronos_p_tp": 0.6, "extra_risk": 0.2,
           "conf_4h": 0.7, "p_up_4h": 0.7, "analog_dir": 0.6, "roof": 100.0, "breakout_price": 100.1,
           "chase_price": 100.5, "amplitude": 0.03,
           "indicators": {"regime": "laterale", "rsi": 55}, "analogs": {"long_win": 0.5}}
    asyncio.run(ex.open_from_signal(sig))
    assert not ex.positions  # back under the roof
    feed.set(100.2)
    asyncio.run(ex.open_from_signal(sig))
    assert len(ex.positions) == 1


def test_break_even_exit_when_every_horizon_turns_bearish():
    feed, ctl, ex = make(price=100.0)
    p = open_long(ex)

    class Pred:
        cache = {}

        def decision_cache(self, sym):
            return self.cache.get((sym, "15m"))

    ex.predictor = Pred()
    bar_t = time.time() // 900 * 900 - 900
    paths = np.full((24, 18, 4), 99.0)  # every horizon below the price
    Pred.cache[("PERP_SOL_USDC", "15m")] = {"ts": p.opened_ts + 1, "last": 100.0, "paths": paths, "bar_t": bar_t}
    feed.set(p.entry * 0.999)
    assert ex._check(p, p.entry * 0.999, 0.5) is None  # at a loss: never closed
    assert p.bearish_all and set(p.p_up_h) == {"15m", "1h", "4h"}
    be = p.entry * (1 + 2 * settings.taker_fee + 2 * settings.slippage) * 1.0005
    feed.set(be)
    r = ex._check(p, be, 0.5)
    assert r and r.startswith("uscita in pari netto") and p.pnl >= 0


def test_break_even_exit_needs_all_three_horizons_bearish():
    feed, ctl, ex = make(price=100.0)
    p = open_long(ex)

    class Pred:
        cache = {}

        def decision_cache(self, sym):
            return self.cache.get((sym, "15m"))

    ex.predictor = Pred()
    bar_t = time.time() // 900 * 900 - 900
    paths = np.full((24, 18, 4), 99.0)
    paths[:, 15, 3] = 103.0  # 4 h ahead still up
    Pred.cache[("PERP_SOL_USDC", "15m")] = {"ts": p.opened_ts + 1, "last": 100.0, "paths": paths, "bar_t": bar_t}
    be = p.entry * 1.002
    feed.set(be)
    assert ex._check(p, be, 0.5) is None and not p.bearish_all


def test_volatility_does_not_raise_the_risk_by_default(monkeypatch):
    highs = [99.6, 100.0, 99.8, 99.7]
    r = _breakout_checker(100.2, highs, p_up=0.55).evaluate("PERP_SOL_USDC")
    assert r["risk_parts"]["volatilita"] > 0.5  # measured and shown...
    monkeypatch.setattr(settings, "risk_volatility", True)
    rv = _breakout_checker(100.2, highs, p_up=0.55).evaluate("PERP_SOL_USDC")
    assert rv["extra_risk"] == pytest.approx(r["extra_risk"] + 0.15 * r["risk_parts"]["volatilita"])  # ...counted only if on


def test_risk_btc_and_analyst_never_block_by_default():
    highs = [99.6, 100.0, 99.8, 99.7]
    ch = _breakout_checker(100.2, highs)
    ch.macro_risk = 100.0
    ch.btc = {"ok": False, "chg_4h": -0.03, "chg_24h": -0.05}
    ch.pause(600, "test")
    r = ch.evaluate("PERP_SOL_USDC")
    assert r["eligible"] and not ch.paused, r["reasons"]  # only the Kronos forecast and the breakout decide
