"""Technical indicators, short moving averages and historical pattern analogs."""
from __future__ import annotations

import numpy as np


def ema(x: np.ndarray, n: int) -> np.ndarray:
    a = 2 / (n + 1)
    out = np.empty_like(x, dtype=float)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def rsi(close: np.ndarray, n: int = 14) -> float:
    d = np.diff(close)
    if len(d) < n:
        return 50.0
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au, ad = up[:n].mean(), dn[:n].mean()
    for i in range(n, len(d)):  # Wilder smoothing
        au = (au * (n - 1) + up[i]) / n
        ad = (ad * (n - 1) + dn[i]) / n
    if ad == 0:
        return 100.0 if au > 0 else 50.0
    return float(100 - 100 / (1 + au / ad))


def macd(close: np.ndarray) -> tuple[float, float, float]:
    m = ema(close, 12) - ema(close, 26)
    s = ema(m, 9)
    return float(m[-1]), float(s[-1]), float(m[-1] - s[-1])


def bollinger_pct_b(close: np.ndarray, n: int = 20, k: float = 2.0) -> float:
    w = close[-n:]
    mu, sd = w.mean(), w.std()
    if sd == 0:
        return 0.5
    return float((close[-1] - (mu - k * sd)) / (2 * k * sd))


def atr_pct(bars: np.ndarray, n: int = 14) -> float:
    """Average true range as a fraction of price. bars: (T, >=5) [t,o,h,l,c]."""
    h, l, c = bars[:, 2], bars[:, 3], bars[:, 4]
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    return float(tr[-n:].mean() / c[-1]) if c[-1] else 0.0


def vol5_breakout(bars1: np.ndarray, price: float, now: float, window: int = 60, top: float = 0.10,
                  trigger: float = 0.5, min_bars: int = 300) -> dict:
    """5-minute volatility breakout (backtest of 18 Nov 2026, "seguire il movimento").

    width  = expected 5-minute range: std of the 1m log returns over the last `window` closed minutes x sqrt(5)
             x 2.563 (the 10-90% band of a normal), i.e. what Kronos' 5-minute forecast width tracked (corr 0.81);
    hot    = that width is in the top `top` share of the token's own widths over the 1m history in memory;
    pushed = the live price is at least `trigger` x width above the lowest close of the last 5 closed minutes,
             i.e. the move has started upwards by half of what is expected.
    """
    out = {"ready": False, "hot": False, "pushed": False, "ok": False,
           "width": 0.0, "threshold": 0.0, "rank": 0.0, "push": 0.0, "need": 0.0}
    if bars1 is None or len(bars1) == 0 or price <= 0:
        return out
    closed = bars1[:-1] if bars1[-1, 0] + 60 > now else bars1
    if len(closed) < min_bars + window + 1:
        return out
    c = np.maximum(closed[:, 4].astype(float), 1e-12)
    lr = np.diff(np.log(c))
    cs = np.concatenate([[0.0], np.cumsum(lr)])
    cs2 = np.concatenate([[0.0], np.cumsum(lr * lr)])
    m1 = (cs[window:] - cs[:-window]) / window
    m2 = (cs2[window:] - cs2[:-window]) / window
    widths = np.sqrt(np.maximum(m2 - m1 * m1, 0.0)) * np.sqrt(5.0) * 2.563
    w = float(widths[-1])
    thr = float(np.quantile(widths, 1.0 - top))
    push = float(price / c[-5:].min() - 1.0)
    rank = float((widths < w).mean())  # share of the token's own widths below the current one
    out.update(ready=True, width=w, threshold=thr, rank=rank, push=push, need=trigger * w,
               hot=rank >= 1.0 - top and w > 0, pushed=push >= trigger * w)
    out["ok"] = out["hot"] and out["pushed"]
    return out


def indicator_view(bars: np.ndarray) -> dict:
    """Snapshot of indicators on 1m bars plus a directional vote in [-1, 1]."""
    close = bars[:, 4]
    vol = bars[:, 5]
    e9, e21, e50 = ema(close, 9), ema(close, 21), ema(close, 50)
    r = rsi(close)
    m, s, hist = macd(close)
    pb = bollinger_pct_b(close)
    a = atr_pct(bars)
    vol_ratio = float(vol[-10:].mean() / vol[-60:].mean()) if len(vol) >= 60 and vol[-60:].mean() > 0 else 1.0

    votes = {
        "ema_9_21": 1 if e9[-1] > e21[-1] else -1,
        "ema_slope": 1 if e9[-1] > e9[-4] else -1,
        "price_vs_ema50": 1 if close[-1] > e50[-1] else -1,
        "macd_hist": 1 if hist > 0 else -1,
        "rsi": (1 if 50 < r < 70 else -1 if 30 < r < 50 else 0),  # momentum zone; extremes are neutral
    }
    score = float(np.mean(list(votes.values())))
    flags = []
    if r >= 75:
        flags.append("RSI ipercomprato")
    if r <= 25:
        flags.append("RSI ipervenduto")
    if pb > 1:
        flags.append("sopra banda Bollinger")
    if pb < 0:
        flags.append("sotto banda Bollinger")
    cross = (e9[-1] - e21[-1]) * (e9[-2] - e21[-2]) < 0
    if cross:
        flags.append("incrocio EMA 9/21")
    return {
        "ema9": float(e9[-1]), "ema21": float(e21[-1]), "ema50": float(e50[-1]), "rsi": r,
        "macd": m, "macd_signal": s, "macd_hist": hist, "bb_pct_b": pb, "atr_pct": a, "vol_ratio": vol_ratio,
        "votes": votes, "score": score, "flags": flags,
        "regime": _regime(score, a),
    }


def _regime(score: float, atr: float) -> str:
    trend = "trend rialzista" if score >= 0.4 else "trend ribassista" if score <= -0.4 else "laterale"
    vol = "alta volatilità" if atr > 0.004 else "bassa volatilità" if atr < 0.0012 else "volatilità media"
    return f"{trend}, {vol}"


# --------------------------------------------------------------- pattern analogs
def _shape(close: np.ndarray) -> np.ndarray:
    r = np.diff(np.log(np.maximum(close, 1e-12)))
    c = np.cumsum(r)
    sd = c.std()
    return (c - c.mean()) / sd if sd > 0 else c * 0


def pattern_analogs(
    target: np.ndarray,
    library: dict[str, np.ndarray],
    window: int = 30,
    horizon: int = 30,
    k: int = 25,
    tp_move: float = 0.005,
    sl_move: float = 0.002,
    self_symbol: str | None = None,
) -> dict:
    """k-nearest historical windows (same token and others) and what followed.

    target: recent closes of the token. library: symbol -> closes (1m).
    For each analog we replay the next `horizon` bars and check which of
    +tp_move / -sl_move (long) or the mirror (short) was hit first.
    """
    if len(target) < window + 1:
        return {"n": 0}
    from numpy.lib.stride_tricks import sliding_window_view

    q = _shape(target[-(window + 1):])
    cands: list[tuple[float, str, np.ndarray]] = []
    for sym, closes in library.items():
        n = len(closes)
        end_limit = n - horizon - 1
        if sym == self_symbol:
            end_limit = min(end_limit, n - window - horizon - 1)  # no overlap with the live window
        if end_limit <= window + 1:
            continue
        logc = np.log(np.maximum(closes[:end_limit], 1e-12))
        segs = sliding_window_view(logc, window + 1)  # (M, window+1), segment m ends at index m+window
        c = np.cumsum(np.diff(segs, axis=1), axis=1)
        sd = c.std(axis=1, keepdims=True)
        sd[sd == 0] = 1
        shapes = (c - c.mean(axis=1, keepdims=True)) / sd
        d = np.mean((shapes - q) ** 2, axis=1)
        top = np.argsort(d)[: k * 3]
        last_end = -10 ** 9
        for m in sorted(top, key=lambda i: d[i]):
            end = m + window + 1
            if abs(end - last_end) < window // 2:
                continue  # avoid near-duplicate overlapping analogs
            last_end = end
            cands.append((float(d[m]), sym, closes[end - 1:end + horizon]))
    if not cands:
        return {"n": 0}
    cands.sort(key=lambda x: x[0])
    best = cands[:k]
    long_w = short_w = 0
    fwd = []
    for _, _, fut in best:
        p0 = fut[0]
        path = fut[1:] / p0 - 1
        fwd.append(path[-1])
        long_w += _first(path, tp_move, -sl_move)
        short_w += _first(-path, tp_move, -sl_move)
    sims = [1 / (1 + c[0]) for c in best]
    return {
        "n": len(best),
        "similarity": float(np.mean(sims)),
        "long_win": long_w / len(best),
        "short_win": short_w / len(best),
        "fwd_mean": float(np.mean(fwd)),
        "fwd_up": float(np.mean(np.array(fwd) > 0)),
        "sources": sorted({c[1] for c in best}),
    }


def _first(path: np.ndarray, up: float, down: float) -> int:
    for x in path:
        if x <= down:
            return 0
        if x >= up:
            return 1
    return 0
