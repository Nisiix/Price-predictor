"""In-memory OHLCV store with live tick aggregation."""
from __future__ import annotations

import threading
from collections import deque

import numpy as np

MAX_BARS = 3000  # 1m: ~2 days, 5m: ~10 days


class CandleStore:
    """bars[(symbol, timeframe)] = deque of [t_open, o, h, l, c, v] (t in seconds)."""

    def __init__(self, timeframes: dict[str, int]) -> None:
        self.timeframes = timeframes
        self._bars: dict[tuple[str, str], deque] = {}
        self._lock = threading.Lock()

    def _dq(self, sym: str, tf: str) -> deque:
        key = (sym, tf)
        if key not in self._bars:
            self._bars[key] = deque(maxlen=MAX_BARS)
        return self._bars[key]

    def set_history(self, sym: str, tf: str, bars: list[list[float]]) -> None:
        """Merge closed historical bars (sorted by time) with what we already built."""
        if not bars:
            return
        with self._lock:
            dq = self._dq(sym, tf)
            existing = {int(b[0]): b for b in dq}
            for b in bars:
                existing[int(b[0])] = list(b)
            merged = [existing[k] for k in sorted(existing)]
            dq.clear()
            dq.extend(merged[-MAX_BARS:])

    def update_tick(self, sym: str, price: float, ts: float) -> None:
        if price <= 0:
            return
        with self._lock:
            for tf, sec in self.timeframes.items():
                dq = self._dq(sym, tf)
                t_open = int(ts // sec * sec)
                if dq and int(dq[-1][0]) == t_open:
                    bar = dq[-1]
                    bar[2] = max(bar[2], price)
                    bar[3] = min(bar[3], price)
                    bar[4] = price
                elif not dq or t_open > int(dq[-1][0]):
                    if dq and tf == "5s":
                        # carry forward so empty seconds don't leave gaps
                        last_t = int(dq[-1][0])
                        last_c = dq[-1][4]
                        gap = (t_open - last_t) // sec
                        for k in range(1, min(gap, 60)):
                            dq.append([last_t + k * sec, last_c, last_c, last_c, last_c, 0.0])
                    dq.append([t_open, price, price, price, price, 0.0])

    def get(self, sym: str, tf: str, n: int | None = None) -> np.ndarray:
        with self._lock:
            dq = self._bars.get((sym, tf))
            if not dq:
                return np.empty((0, 6))
            arr = np.array(dq, dtype=float)
        return arr[-n:] if n else arr

    def get_agg(self, sym: str, tf: str, seconds: int, n: int | None = None) -> np.ndarray:
        """Candles of `seconds` (1 h, 4 h, aligned to UTC) built from the stored `tf` candles: first open, highest
        high, lowest low, last close, summed volume. The last one is still forming while its last `tf` candle is."""
        src = self.get(sym, tf)
        if not len(src):
            return src
        keys = (src[:, 0] // seconds * seconds).astype(np.int64)
        starts = np.flatnonzero(np.r_[True, keys[1:] != keys[:-1]])
        ends = np.r_[starts[1:], len(src)]
        out = np.empty((len(starts), src.shape[1]))
        out[:, 0] = keys[starts]
        out[:, 1] = src[starts, 1]
        out[:, 2] = np.maximum.reduceat(src[:, 2], starts)
        out[:, 3] = np.minimum.reduceat(src[:, 3], starts)
        out[:, 4] = src[ends - 1, 4]
        out[:, 5] = np.add.reduceat(src[:, 5], starts)
        return out[-n:] if n else out

    def last_price(self, sym: str) -> float | None:
        for tf in ("5s", "1m", "5m"):
            arr = self.get(sym, tf, 1)
            if len(arr):
                return float(arr[-1, 4])
        return None

    def count(self, sym: str, tf: str) -> int:
        with self._lock:
            dq = self._bars.get((sym, tf))
            return len(dq) if dq else 0
