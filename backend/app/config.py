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
    kronos_model: str = os.getenv("PP_KRONOS_MODEL", "NeoQuasar/Kronos-mini")
    kronos_tokenizer: str = os.getenv("PP_KRONOS_TOKENIZER", "NeoQuasar/Kronos-Tokenizer-2k")
    kronos_max_context: int = _i("PP_KRONOS_MAX_CONTEXT", 2048)
    lookback: int = _i("PP_LOOKBACK", 128)  # bars fed to the model (CPU budget)
    n_samples: int = _i("PP_SAMPLES", 24)  # sampled futures per prediction
    temperature: float = _f("PP_TEMPERATURE", 1.0)
    top_p: float = _f("PP_TOP_P", 0.9)

    # --- market feed ---------------------------------------------------------
    tick_interval_s: float = _f("PP_TICK_S", 1.0)
    fast_bar_s: int = 5  # bar size for the 10s / 30s horizons (built from live ticks)

    # --- trading rules (from the spec) ----------------------------------------
    leverage_choices: tuple[int, ...] = (10, 12, 15, 20)
    take_profit_pct: float = _f("PP_TP_PCT", 5.0)  # leveraged move, % of margin
    stop_loss_pct: float = _f("PP_SL_PCT", 2.0)  # leveraged move, % of margin
    margin_usdc: float = _f("PP_MARGIN_USDC", 100.0)
    max_positions: int = _i("PP_MAX_POSITIONS", 2)
    paper_start_balance: float = _f("PP_PAPER_BALANCE", 1000.0)
    taker_fee: float = _f("PP_TAKER_FEE", 0.0006)  # per side, on notional
    slippage: float = _f("PP_SLIPPAGE", 0.0002)  # per side, on price

    # --- checker / analyst ------------------------------------------------------
    # 4-hour strategy (Checker selection + Executor entry/exit)
    min_signal_score: float = _f("PP_MIN_SCORE", 0.35)  # probability x (1 - risk) on the 4h horizon
    min_confidence: float = _f("PP_MIN_CONF", 0.60)  # Kronos probability of the predicted 4h direction
    max_risk: float = _f("PP_MAX_RISK", 0.55)  # extra risk ceiling
    entry_zone: float = _f("PP_ENTRY_ZONE", 0.20)  # long in the lowest 20% of the 4h range, short in the highest 20%
    exit_margin: float = _f("PP_EXIT_MARGIN", 0.10)  # reversal only if the opposite direction reaches 50% + margin
    exit_confirmations: int = _i("PP_EXIT_CONFIRM", 2)  # consecutive 4h forecasts confirming the reversal
    use_take_profit: bool = os.getenv("PP_USE_TP", "0") == "1"  # fixed +5% take profit (off: exit on reversal)
    analyst_close_threshold: float = _f("PP_ANALYST_CLOSE", 70.0)
    analyst_pause_threshold: float = _f("PP_ANALYST_PAUSE", 55.0)
    news_poll_s: int = _i("PP_NEWS_POLL_S", 120)

    db_path: Path = field(default_factory=lambda: DATA_DIR / "predictor.sqlite3")


settings = Settings()

# Horizons shown on the dashboard: label -> (seconds, timeframe used, steps on that timeframe)
TIMEFRAMES = {"5s": 5, "1m": 60, "5m": 300, "15m": 900}
HORIZONS: dict[str, tuple[int, str, int]] = {
    "5s": (5, "5s", 1),
    "10s": (10, "5s", 2),
    "30s": (30, "5s", 6),
    "1m": (60, "1m", 1),
    "5m": (300, "1m", 5),
    "15m": (900, "1m", 15),
    "30m": (1800, "5m", 6),
    "1h": (3600, "5m", 12),
    "4h": (14400, "15m", 16),
}
# steps sampled per timeframe: the cone on the chart goes this far (1m: 30 min, 5m: 2 h, 15m: 8 h)
PRED_LEN = {"5s": 12, "1m": 30, "5m": 24, "15m": 32}
EXPERIMENTAL_HORIZONS = {"5s", "10s", "30s"}  # sub-minute: outside Kronos' training resolution
