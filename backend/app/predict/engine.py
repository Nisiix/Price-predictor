"""Kronos inference wrapper that returns every sampled future path.

Kronos (https://github.com/shiyu-coder/Kronos) is an autoregressive foundation
model for OHLCV candlesticks. Its public ``KronosPredictor`` averages the
sampled futures into one path; for probabilities we need the whole sample
cloud, so we call the (patched) ``auto_regressive_inference`` directly with
``return_samples=True``.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

log = logging.getLogger("kronos")

PRICE_COLS = 4  # open, high, low, close


@dataclass
class SeriesInput:
    """One token history on one timeframe."""

    ohlc: np.ndarray  # shape (T, 4): open, high, low, close
    timestamps: pd.DatetimeIndex  # len T
    future_timestamps: pd.DatetimeIndex  # len pred_len


class KronosEngine:
    """Loads a Kronos model + tokenizer once and samples future paths in batches."""

    def __init__(
        self,
        model_name: str = "NeoQuasar/Kronos-small",
        tokenizer_name: str = "NeoQuasar/Kronos-Tokenizer-base",
        max_context: int = 512,
        device: str | None = None,
        clip: float = 5.0,
    ) -> None:
        self.model_name = model_name
        self.tokenizer_name = tokenizer_name
        self.max_context = max_context
        self.clip = clip
        self.device = device
        self.model = None
        self.tokenizer = None
        self.ready = False
        self.error: str | None = None
        self.last_latency_s: float = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ load
    def load(self) -> None:
        try:
            import torch

            from .kronos_model import Kronos, KronosTokenizer

            torch.set_num_threads(max(1, (os.cpu_count() or 2) - 1))
            if self.device is None:
                self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
            t0 = time.time()
            self.tokenizer = KronosTokenizer.from_pretrained(self.tokenizer_name).to(self.device).eval()
            self.model = Kronos.from_pretrained(self.model_name).to(self.device).eval()
            self.ready = True
            log.info("Kronos %s loaded on %s in %.1fs", self.model_name, self.device, time.time() - t0)
        except Exception as exc:  # pragma: no cover - depends on network / torch
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("Kronos failed to load, the Monte-Carlo fallback will be used")

    # ---------------------------------------------------------------- sample
    def sample_paths(
        self,
        series: list[SeriesInput],
        pred_len: int,
        n_samples: int,
        temperature: float = 1.0,
        top_p: float = 0.9,
    ) -> list[np.ndarray | None]:
        """Return, per series, an array (n_samples, pred_len, 4) of OHLC paths.

        All series must share the same length (callers trim to a common
        lookback). A series whose window is flat (std ~ 0) returns ``None``:
        z-scoring it would blow up and the model has nothing to read.
        """
        import torch

        from .kronos_model import auto_regressive_inference, calc_time_stamps

        if not self.ready or not series:
            return [None] * len(series)

        xs, xst, yst, stats, keep = [], [], [], [], []
        for i, s in enumerate(series):
            x = np.zeros((len(s.ohlc), 6), dtype=np.float32)  # o,h,l,c,volume,amount
            x[:, :PRICE_COLS] = s.ohlc
            mean = x.mean(axis=0)
            std = x.std(axis=0)
            if std[3] <= 1e-9 * max(abs(mean[3]), 1e-12):
                continue  # flat window
            xn = np.clip((x - mean) / (std + 1e-5), -self.clip, self.clip)
            xs.append(xn)
            xst.append(calc_time_stamps(pd.Series(s.timestamps)).values.astype(np.float32))
            yst.append(calc_time_stamps(pd.Series(s.future_timestamps)).values.astype(np.float32))
            stats.append((mean, std))
            keep.append(i)

        out: list[np.ndarray | None] = [None] * len(series)
        if not keep:
            return out

        t0 = time.time()
        with self._lock, torch.no_grad():
            x_t = torch.from_numpy(np.stack(xs)).to(self.device)
            xs_t = torch.from_numpy(np.stack(xst)).to(self.device)
            ys_t = torch.from_numpy(np.stack(yst)).to(self.device)
            z = auto_regressive_inference(
                self.tokenizer,
                self.model,
                x_t,
                xs_t,
                ys_t,
                self.max_context,
                pred_len,
                self.clip,
                temperature,
                0,
                top_p,
                n_samples,
                False,
                return_samples=True,
            )  # (B, S, L, 6)
        self.last_latency_s = time.time() - t0

        z = z[:, :, -pred_len:, :]
        for j, idx in enumerate(keep):
            mean, std = stats[j]
            paths = z[j] * (std + 1e-5) + mean
            ohlc = paths[:, :, :PRICE_COLS]
            # Keep candles internally consistent (the decoder is free-form).
            hi = np.maximum.reduce([ohlc[..., 0], ohlc[..., 1], ohlc[..., 3]])
            lo = np.minimum.reduce([ohlc[..., 0], ohlc[..., 2], ohlc[..., 3]])
            ohlc[..., 1], ohlc[..., 2] = hi, lo
            out[idx] = np.maximum(ohlc, 1e-12)
        return out


def monte_carlo_paths(ohlc: np.ndarray, pred_len: int, n_samples: int, rng: np.random.Generator | None = None) -> np.ndarray:
    """Fallback when Kronos is unavailable: bootstrap of historical log-returns.

    Not a forecast model, just an honest volatility envelope so the dashboard
    still works; the UI flags predictions coming from this path.
    """
    rng = rng or np.random.default_rng()
    close = ohlc[:, 3]
    rets = np.diff(np.log(np.maximum(close, 1e-12)))
    if len(rets) < 5 or np.allclose(rets, 0):
        rets = np.array([0.0])
    draws = rng.choice(rets, size=(n_samples, pred_len), replace=True)
    paths_close = close[-1] * np.exp(np.cumsum(draws, axis=1))
    prev = np.concatenate([np.full((n_samples, 1), close[-1]), paths_close[:, :-1]], axis=1)
    rng_hl = np.abs(rng.choice(rets, size=(n_samples, pred_len))) * paths_close * 0.5
    hi = np.maximum(prev, paths_close) + rng_hl
    lo = np.maximum(np.minimum(prev, paths_close) - rng_hl, 1e-12)
    return np.stack([prev, hi, lo, paths_close], axis=-1)
