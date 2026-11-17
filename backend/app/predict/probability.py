"""Turn a cloud of sampled OHLC paths into probabilities. Pure functions, unit tested."""
from __future__ import annotations

import numpy as np


def horizon_stats(paths: np.ndarray, last: float, step: int) -> dict:
    """Distribution of the close `step` bars ahead. paths: (S, L, 4)."""
    closes = paths[:, step - 1, 3]
    rets = closes / last - 1.0
    return {
        "p_up": float((rets > 0).mean()),
        "exp_ret": float(rets.mean()),
        "q10": float(np.quantile(rets, 0.10)),
        "q50": float(np.quantile(rets, 0.50)),
        "q90": float(np.quantile(rets, 0.90)),
        "price_q10": float(last * (1 + np.quantile(rets, 0.10))),
        "price_q50": float(last * (1 + np.quantile(rets, 0.50))),
        "price_q90": float(last * (1 + np.quantile(rets, 0.90))),
    }


def fan(paths: np.ndarray) -> dict:
    """Quantile bands of the close along the horizon, for the chart."""
    c = paths[:, :, 3]
    return {
        "q10": np.quantile(c, 0.10, axis=0).tolist(),
        "q25": np.quantile(c, 0.25, axis=0).tolist(),
        "q50": np.quantile(c, 0.50, axis=0).tolist(),
        "q75": np.quantile(c, 0.75, axis=0).tolist(),
        "q90": np.quantile(c, 0.90, axis=0).tolist(),
    }


def trade_levels(entry: float, side: int, leverage: float, tp_pct: float, sl_pct: float, mmr: float) -> dict:
    """Price levels for a leveraged position. side=+1 long, -1 short.

    tp_pct / sl_pct are leveraged moves (percent of margin): with 10x, +5% means
    the underlying moved +0.5%. Liquidation is when the loss eats the margin
    down to the maintenance requirement: move = 1/L - mmr.
    """
    tp_move = tp_pct / 100.0 / leverage
    sl_move = sl_pct / 100.0 / leverage
    liq_move = max(1.0 / leverage - mmr, 1e-6)
    return {
        "tp": entry * (1 + side * tp_move),
        "sl": entry * (1 - side * sl_move),
        "liq": entry * (1 - side * liq_move),
    }


def first_touch(
    paths: np.ndarray,
    entry: float,
    side: int,
    leverage: float,
    tp_pct: float,
    sl_pct: float,
    mmr: float,
    fee_round_trip: float,
) -> dict:
    """Probability that each sampled path hits TP, SL or liquidation first.

    Bars are scanned in order; inside a bar we cannot know the order of high
    and low, so when a bar touches both the adverse level wins (conservative).
    Returns probabilities and the expected return on margin net of fees.
    """
    lv = trade_levels(entry, side, leverage, tp_pct, sl_pct, mmr)
    hi, lo = paths[:, :, 1], paths[:, :, 2]
    S, L = hi.shape
    if side > 0:
        hit_tp = hi >= lv["tp"]
        hit_sl = lo <= lv["sl"]
        hit_liq = lo <= lv["liq"]
    else:
        hit_tp = lo <= lv["tp"]
        hit_sl = hi >= lv["sl"]
        hit_liq = hi >= lv["liq"]

    def first_idx(mask: np.ndarray) -> np.ndarray:
        any_ = mask.any(axis=1)
        idx = np.where(any_, mask.argmax(axis=1), L + 1)
        return idx

    i_tp, i_sl, i_liq = first_idx(hit_tp), first_idx(hit_sl), first_idx(hit_liq)
    adverse = np.minimum(i_sl, i_liq)
    tp_first = i_tp < adverse
    liq_first = (i_liq <= i_sl) & (i_liq <= L) & ~tp_first
    sl_first = (i_sl <= L) & ~tp_first & ~liq_first
    open_end = ~(tp_first | sl_first | liq_first)

    fee_on_margin = fee_round_trip * leverage
    end_ret = side * (paths[:, -1, 3] / entry - 1.0) * leverage
    outcome = np.where(
        tp_first, tp_pct / 100.0, np.where(liq_first, -1.0, np.where(sl_first, -sl_pct / 100.0, end_ret))
    ) - fee_on_margin
    p_tp = float(tp_first.mean())
    p_sl = float(sl_first.mean())
    p_liq = float(liq_first.mean())
    return {
        "side": "long" if side > 0 else "short",
        "leverage": leverage,
        "p_tp": p_tp,
        "p_sl": p_sl,
        "p_liq": p_liq,
        "p_open": float(open_end.mean()),
        "ev": float(outcome.mean()),  # expected return on margin, net of fees
        "levels": lv,
        "bars_to_tp": float(np.median(i_tp[tp_first])) if tp_first.any() else None,
    }


def best_trade(paths: np.ndarray, entry: float, leverages, tp_pct, sl_pct, mmr, fee_rt, max_leverage: int) -> dict:
    """Evaluate long and short at every allowed leverage, return all + the best by EV."""
    options = []
    for side in (1, -1):
        for lev in leverages:
            if lev > max_leverage:
                continue
            options.append(first_touch(paths, entry, side, lev, tp_pct, sl_pct, mmr, fee_rt))
    options.sort(key=lambda o: (o["ev"], o["p_tp"]), reverse=True)
    return {"best": options[0] if options else None, "options": options}
