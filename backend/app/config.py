"""Runtime settings. Every value can be overridden with an environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("PP_DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass
class Settings:
    # --- endpoints -----------------------------------------------------------
    orderly_api: str = os.getenv("PP_ORDERLY_API", "https://api.orderly.org")
    raydium_perps_api: str = "https://api-perps-v1.raydium.io"
    coingecko_api: str = "https://api.coingecko.com/api/v3"
    broker_id: str = "raydium"

    # --- universe ------------------------------------------------------------
    top_n: int = _i("PP_TOP_N", 100)  # only the top-N crypto by market cap
    min_max_leverage: int = 10  # a market must allow at least 10x

    # --- Kronos --------------------------------------------------------------
    kronos_model: str = os.getenv("PP_KRONOS_MODEL", "NeoQuasar/Kronos-base")  # user: always the largest open model
    kronos_tokenizer: str = os.getenv("PP_KRONOS_TOKENIZER", "NeoQuasar/Kronos-Tokenizer-base")
    # largest open Kronos model, used where accuracy matters more than speed: the 4h forecast of open
    # positions and the re-prediction right before an entry (Kronos-mini keeps scanning the universe)
    decision_model: str = os.getenv("PP_DECISION_MODEL", "")  # empty: decisions use the main model (already base)
    decision_tokenizer: str = os.getenv("PP_DECISION_TOKENIZER", "NeoQuasar/Kronos-Tokenizer-base")
    kronos_max_context: int = _i("PP_KRONOS_MAX_CONTEXT", 512)
    lookback: int = _i("PP_LOOKBACK", 128)  # bars fed to the model (CPU budget)
    # Kronos-base context: 128 15m candles (32 h). Measured on the user's CPU with 32 steps: 64 bars ~21 s, 128 bars
    # ~44 s; the forecast is now 18 steps (4 h + slack), so 128 bars should cost about what 64 did before
    base_lookback: int = _i("PP_BASE_LOOKBACK", 128)
    # tokens analysed with Kronos-base, by market cap. 0 = automatic: as many as one scan cycle (scan_age_s) fits at
    # the measured time per forecast, minus the open positions' forecasts
    base_tokens: int = _i("PP_BASE_TOKENS", 0)
    base_tokens_min: int = _i("PP_BASE_TOKENS_MIN", 6)
    scan_age_s: float = _f("PP_SCAN_AGE", 300.0)  # a token is re-forecast once its forecast is this old
    n_samples: int = _i("PP_SAMPLES", 24)  # sampled futures per prediction
    # the forecast re-run right before an entry: more samples, so its probabilities are measured to about +/-5 points
    # instead of +/-9 (24 samples); it costs ~4x one scan forecast, once per entry
    decision_samples: int = _i("PP_DECISION_SAMPLES", 96)
    position_refresh_s: float = _f("PP_POSITION_REFRESH", 300.0)  # 4h forecast of open positions, for the exits
    temperature: float = _f("PP_TEMPERATURE", 1.0)
    top_p: float = _f("PP_TOP_P", 0.9)

    # --- market feed ---------------------------------------------------------
    tick_interval_s: float = _f("PP_TICK_S", 1.0)
    fast_bar_s: int = 5  # bar size for the 10s / 30s horizons (built from live ticks)

    # --- trading rules (from the spec) ----------------------------------------
    leverage_choices: tuple[int, ...] = (10, 12, 15, 20)
    take_profit_pct: float = _f("PP_TP_PCT", 5.0)  # leveraged move, % of margin
    stop_loss_pct: float = _f("PP_SL_PCT", 2.0)  # leveraged move, % of margin
    margin_usdc: float = _f("PP_MARGIN_USDC", 100.0)  # default margin for manual tickets only
    # user rule: margin per trade as a share of total capital, by extra risk (risk <= threshold -> share);
    # above the last threshold the smallest share is used. The liquidation sizing can still lower it.
    risk_tiers: tuple[tuple[float, float], ...] = ((0.25, 0.10), (0.30, 0.07), (0.35, 0.05), (0.40, 0.03))
    # above the last tier: the smallest share (user rule, 18 Nov: the risk sizes the trade, it never blocks it)
    risk_tier_above: float = _f("PP_RISK_ABOVE_40", 0.03)
    # cross-margin sizing (user rule): every position's liquidation price must stay at least this far from its
    # entry (0.5 = a long liquidates only below half its entry price, a short only above 1.5x)
    liq_min_distance: float = _f("PP_LIQ_DISTANCE", 0.5)
    min_margin_usdc: float = _f("PP_MIN_MARGIN", 10.0)
    # exposure caps (notional / total capital): leverage only lowers the margin locked, it no longer multiplies
    # the exposure of a "low risk" trade up to ~190% of the capital on a single token
    max_token_exposure: float = _f("PP_MAX_TOKEN_EXPOSURE", 0.50)
    max_total_exposure: float = _f("PP_MAX_TOTAL_EXPOSURE", 1.50)
    max_positions: int = _i("PP_MAX_POSITIONS", 0)  # 0 = no fixed cap: the liquidation-distance rule is the limit
    paper_start_balance: float = _f("PP_PAPER_BALANCE", 1000.0)
    taker_fee: float = _f("PP_TAKER_FEE", 0.0006)  # per side, on notional
    slippage: float = _f("PP_SLIPPAGE", 0.0002)  # per side, on price

    # --- checker / analyst ------------------------------------------------------
    # 4-hour strategy (Checker selection + Executor entry/exit)
    min_signal_score: float = _f("PP_MIN_SCORE", 0.0)  # probability x (1 - risk); 0 = off (user: ignore risk for entries)
    # user rule (breakout entry): Kronos must not be against the long, its 4h probability ABOVE this (50%); the
    # direction comes from the breakout itself, Kronos is asked for the size of the move
    min_confidence: float = _f("PP_MIN_CONF", 0.50)
    # user rule (18 Nov): "the Kronos forecasts count" - the extra risk only decides the margin tier, it does not block
    # an opening any more; 0 = no ceiling (PP_MAX_RISK=0.40 restores the old block above 40%)
    max_risk: float = _f("PP_MAX_RISK", 0.0)
    # user rule (18 Nov): no extra risk for a Kronos probability between 50% and this value; below it the Kronos part of
    # the extra risk is the one at this value, so a 55% breakout is not sized or blocked differently from a 70% one
    risk_conf_floor: float = _f("PP_RISK_CONF_FLOOR", 0.70)
    # 15m ATR against the old -2% leveraged stop in the extra risk (1 = on). Off: there is no stop loss any more, and a
    # move of at least 1.5% in 4 h implies an ATR that alone added ~9 risk points and blocked neutral breakouts
    risk_volatility: bool = os.getenv("PP_RISK_VOLATILITY", "0") == "1"
    max_forecast_age_s: float = _f("PP_MAX_FORECAST_AGE", 2700.0)  # screening age limit; the entry itself re-predicts
    longs_only: bool = os.getenv("PP_LONGS_ONLY", "1") == "1"  # user rule: the Executor opens long positions only
    # breakout entry (user rule): Kronos predicts a large move over the next 4 h, the long opens only once the price
    # breaks the last hour's high ("the roof"), i.e. once the predicted move has started upwards; nothing if it falls.
    #   amplitude = mean over the sampled paths of |close 4 h ahead / price when Kronos ran - 1|
    #   roof      = highest high of the last `roof_bars` CLOSED 15m candles (4 = the last hour)
    #   entry     = live price above roof x (1 + buffer) and not more than roof x (1 + max_chase) (later = too late,
    #               wait for a new roof)
    min_move: float = _f("PP_MIN_MOVE", 0.015)
    roof_bars: int = _i("PP_ROOF_BARS", 4)
    breakout_buffer: float = _f("PP_BREAKOUT_BUFFER", 0.001)
    breakout_max_chase: float = _f("PP_BREAKOUT_MAX_CHASE", 0.01)  # user: 1% (0.5% was often passed during the confirmation)
    # user choice (18 Nov, option C of the 5-minute backtest): the 4h breakout long also needs the token's 5-minute
    # volatility in its own top `vol5_top` share AND a push of at least `vol5_trigger` x the expected 5-minute range
    # over the last 5 minutes (agents/indicators.vol5_breakout). PP_VOL5_FILTER=0 turns it off.
    vol5_filter: bool = os.getenv("PP_VOL5_FILTER", "1") == "1"
    vol5_top: float = _f("PP_VOL5_TOP", 0.10)
    vol5_trigger: float = _f("PP_VOL5_TRIGGER", 0.5)
    vol5_window: int = _i("PP_VOL5_WINDOW", 60)  # minutes of 1m returns behind the expected range
    # market filter on BTC: removed by the user (18 Nov). PP_BTC_FILTER=1 restores it: no long while BTC is falling
    # over 4 h or 24 h by more than the thresholds below (0 = any fall blocks)
    btc_filter: bool = os.getenv("PP_BTC_FILTER", "0") == "1"
    btc_filter_4h: float = _f("PP_BTC_FILTER_4H", 0.0)
    btc_filter_24h: float = _f("PP_BTC_FILTER_24H", 0.0)
    exit_margin: float = _f("PP_EXIT_MARGIN", 0.10)  # reversal only if the opposite direction reaches 50% + margin
    exit_confirmations: int = _i("PP_EXIT_CONFIRM", 2)  # consecutive 4h forecasts confirming the reversal
    use_take_profit: bool = os.getenv("PP_USE_TP", "0") == "1"  # fixed +5% take profit (off: exit on reversal)
    # no stop in loss (user rule): after `recovery_hours` a losing position is closed only if the
    # Kronos 4h projection gives less than `recovery_min_prob` of getting back to break-even
    hold_losers: bool = os.getenv("PP_HOLD_LOSERS", "1") == "1"  # user rule: never close a position at a loss
    use_initial_stop: bool = os.getenv("PP_USE_SL", "0") == "1"  # 1 restores the fixed -2% leveraged stop
    recovery_hours: float = _f("PP_RECOVERY_HOURS", 2.0)
    recovery_min_prob: float = _f("PP_RECOVERY_MIN_PROB", 0.5)
    # protective stop: placed where the position would close at +2% NET profit on the margin (fees, slippage and
    # funding paid), armed once the price is one 15m ATR above that level
    lock_profit_pct: float = _f("PP_LOCK_PROFIT", 2.0)
    lock_atr_mult: float = _f("PP_LOCK_ATR", 1.0)
    # break-even exit (user request): an open position is closed as soon as it is at least at NET break-even (fees,
    # slippage and funding paid) while the latest forecast is bearish on all three horizons (15 min, 1 h and 4 h:
    # long probability below `be_exit_max_up`). Never a close at a loss.
    be_exit: bool = os.getenv("PP_BE_EXIT", "1") == "1"
    be_exit_max_up: float = _f("PP_BE_EXIT_MAX_UP", 0.5)
    analyst_close_threshold: float = _f("PP_ANALYST_CLOSE", 70.0)
    analyst_pause_threshold: float = _f("PP_ANALYST_PAUSE", 55.0)
    # user rule (18 Nov): the Analyst's pause no longer blocks new openings (its macro risk still lowers the margin
    # tier); PP_ANALYST_BLOCKS=1 restores the pause
    analyst_blocks: bool = os.getenv("PP_ANALYST_BLOCKS", "0") == "1"
    news_poll_s: int = _i("PP_NEWS_POLL_S", 120)

    db_path: Path = field(default_factory=lambda: DATA_DIR / "predictor.sqlite3")
    # simulated capital, open simulated positions and limit orders, kept across restarts (data/ is not in git)
    paper_state_path: Path = field(default_factory=lambda: Path(os.getenv("PP_PAPER_STATE", DATA_DIR / "paper_state.json")))


settings = Settings()


# Horizons shown on the dashboard: label -> (seconds, timeframe used, steps on that timeframe)
TIMEFRAMES = {"5s": 5, "1m": 60, "5m": 300, "15m": 900}
HORIZONS: dict[str, tuple[int, str, int]] = {
    # user rule: only 15 min, 1 h and 4 h, all read from ONE 15m forecast per token (Kronos-base at <= 20 s per token)
    "15m": (900, "15m", 1),
    "1h": (3600, "15m", 4),
    "4h": (14400, "15m", 16),
}
# steps sampled per timeframe. 15m: 18 steps = 4 h (the farthest horizon, no 8 h any more) + 2 bars of slack, so a
# forecast up to two 15m candles old can still be read exactly 4 hours ahead of now
PRED_LEN = {"5s": 12, "1m": 30, "5m": 24, "15m": 18}
EXPERIMENTAL_HORIZONS: set[str] = set()  # sub-minute horizons were removed
