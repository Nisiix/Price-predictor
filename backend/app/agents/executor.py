"""Executor agent: opens positions on the Checker's signals and closes them.

Close rules (from the spec):
  1. liquidation;
  2. take profit when the leveraged move reaches +5% (e.g. +0.5% on the
     token at 10x); a stop loss is set automatically at -2% leveraged from the
     entry;
  3. on order of the Analyst, whatever the current PnL.
Every closed trade is sent to the Controller.

Modes
  * paper (no Phantom wallet): positions are simulated on live mark prices,
    with taker fees, slippage and funding, nothing is sent to any venue.
  * real (wallet + delegated Orderly key + auto-trading switched on in the
    dashboard): the Executor emits order intents; the browser signs them with
    the delegated key and sends them to Raydium Perps (Orderly), and reports
    fills back. TP/SL are also placed on the venue so they work even if the
    dashboard is closed.
"""
from __future__ import annotations

import asyncio
import itertools

import numpy as np
import logging
import time
from dataclasses import asdict, dataclass, field

from ..config import settings
from ..predict.probability import trade_levels

log = logging.getLogger("executor")
_ids = itertools.count(1)


@dataclass
class Position:
    id: str
    symbol: str
    base: str
    side: int  # +1 long, -1 short
    leverage: float
    margin: float
    entry: float
    qty: float
    notional: float
    tp: float
    sl: float
    liq: float
    mode: str  # paper | real
    origin: str  # executor | manual
    opened_ts: float = field(default_factory=time.time)
    signal: dict = field(default_factory=dict)
    fees: float = 0.0
    funding: float = 0.0
    last_price: float = 0.0
    status: str = "open"  # open | closing | closed
    exit: float | None = None
    exit_ts: float | None = None
    reason: str = ""
    pnl: float = 0.0
    roe: float = 0.0  # net return on margin
    lev_move: float = 0.0  # gross leveraged price move (what TP/SL watch), %
    venue_order_id: str | None = None
    p_dir_now: float | None = None  # latest Kronos 4h probability of the position's direction
    locked: bool = False  # stop moved into profit after the +5% leveraged gain
    recovery_prob: float | None = None  # Kronos 4h chance of getting back to break-even (checked after N hours)
    peak_lev_move: float = 0.0
    last_pred_ts: float = 0.0
    reverse_count: int = 0

    def view(self) -> dict:
        d = asdict(self)
        d["side"] = "long" if self.side > 0 else "short"
        return d


@dataclass
class PendingOrder:
    id: str
    symbol: str
    side: int
    leverage: float
    margin: float
    limit: float
    tp: float | None
    sl: float | None
    created: float = field(default_factory=time.time)

    def view(self) -> dict:
        d = asdict(self)
        d["side"] = "long" if self.side > 0 else "short"
        return d


class Executor:
    def __init__(self, feed, checker, controller, bus) -> None:
        self.feed = feed
        self.checker = checker
        self.controller = controller
        self.bus = bus
        self.positions: dict[str, Position] = {}
        self.pending: dict[str, PendingOrder] = {}
        self.balance = settings.paper_start_balance
        self.auto = True  # automatic trading on Checker signals
        self.wallet: str | None = None  # Phantom public key reported by the browser
        self.real_ready = False  # browser has a delegated Orderly key
        self.real_auto = False  # user switched on automatic real trading
        self.intents: dict[str, dict] = {}
        self.cooldown: dict[str, float] = {}
        self.predictor = None  # set by main, to prioritise open positions
        self.venue_equity: float | None = None  # real account collateral (reported by the browser)
        self.paper_started = time.time()
        self._confirming: set[str] = set()
        self.equity_hist: list[tuple[float, float]] = []  # (ts, paper equity) every 30 s, last 12 h
        self._last_hist = 0.0
        self.venue_rows: list[dict] = []

    # ------------------------------------------------------------- helpers
    @property
    def mode(self) -> str:
        return "real" if (self.wallet and self.real_ready and self.real_auto) else "paper"

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.status in ("open", "closing")]

    def _price(self, sym: str) -> float | None:
        m = self.feed.markets.get(sym)
        return m.mark_price if m and m.mark_price else self.feed.store.last_price(sym)

    def _mark(self, p: Position, price: float) -> None:
        p.last_price = price
        gross = p.side * (price / p.entry - 1)
        p.lev_move = gross * p.leverage * 100
        exit_fee = p.notional * (settings.taker_fee + settings.slippage)
        p.pnl = gross * p.notional - p.fees - exit_fee - p.funding
        p.roe = p.pnl / p.margin * 100

    # ------------------------------------------------------------- cross margin
    def equity(self) -> float:
        """Account value backing every position (cross margin)."""
        if self.mode == "real" and self.venue_equity is not None:
            return self.venue_equity
        return self.balance + sum(p.margin + p.pnl + p.notional * settings.taker_fee
                                  for p in self.open_positions() if p.mode == "paper")

    def paper_equity(self) -> float:
        return self.balance + sum(p.margin + p.pnl + p.notional * settings.taker_fee
                                  for p in self.open_positions() if p.mode == "paper")

    def paper_view(self) -> dict:
        """Test capital: realized result of closed trades + open (unrealized) result, since the engine started."""
        paper = [p for p in self.open_positions() if p.mode == "paper"]
        start = settings.paper_start_balance
        equity = self.paper_equity()
        realized = self.balance + sum(p.margin + p.notional * settings.taker_fee for p in paper) - start
        unrealized = sum(p.pnl for p in paper)
        liq_dist = [abs(p.liq / p.entry - 1) for p in paper if p.liq and p.entry]
        return {
            "start": start, "equity": equity, "pnl": equity - start, "pnl_pct": (equity / start - 1) * 100 if start else 0.0,
            "realized": realized, "unrealized": unrealized, "exposure": sum(p.qty * (p.last_price or p.entry) for p in paper),
            "margin_used": sum(p.margin for p in paper), "open": len(paper), "since": self.paper_started,
            "min_liq_distance": min(liq_dist) if liq_dist else None,
            "history": [[round(t), round(e, 4)] for t, e in self.equity_hist[::4]] + ([[round(time.time()), round(equity, 4)]]),
        }

    def _exposures(self) -> list[tuple[float, float]]:
        """(notional, maintenance rate) of every open position, from the venue in real mode."""
        if self.mode == "real" and self.venue_equity is not None:
            out = []
            for r in self.venue_rows:
                q = abs(float(r.get("position_qty") or 0))
                if q:
                    m = self.feed.markets.get(r.get("symbol"))
                    out.append((q * float(r.get("mark_price") or 0), m.mmr if m else 0.05))
            return out
        out = []
        for p in self.open_positions():
            m = self.feed.markets.get(p.symbol)
            out.append((p.qty * (p.last_price or p.entry), m.mmr if m else 0.05))
        return out

    def forecast_still_valid(self, sym: str, side: int, price: float) -> tuple[bool, str]:
        """Right before entering: the latest 4h forecast must still point our way from the CURRENT price
        (probability above the threshold and median above the live price plus costs, mirrored for shorts)."""
        if self.predictor is None:
            return True, ""
        from ..config import HORIZONS

        c = self.predictor.cache.get((sym, "15m"))
        if not c:
            return False, "nessuna previsione a 4 ore disponibile"
        if time.time() - c["ts"] > settings.max_forecast_age_s:
            return False, "previsione a 4 ore troppo vecchia"
        closes = c["paths"][:, HORIZONS["4h"][2] - 1, 3]
        p_dir = float((closes > price).mean()) if side > 0 else float((closes < price).mean())
        median = float(np.median(closes))
        costs = 2 * (settings.taker_fee + settings.slippage)
        if p_dir <= settings.min_confidence:
            return False, f"dal prezzo attuale {price:.6g} la probabilità a 4 ore è scesa al {p_dir * 100:.0f}%"
        if (side > 0 and median <= price * (1 + costs)) or (side < 0 and median >= price * (1 - costs)):
            return False, f"il prezzo attuale {price:.6g} ha già raggiunto la mediana prevista {median:.6g}"
        return True, ""

    def size_margin(self, sym: str, leverage: float, side: int = 1) -> tuple[float, str]:
        """Largest margin (<= margin_usdc) such that, even if every open position and the new one moved
        `liq_min_distance` against us at the same time, the account would still be above maintenance:
        sum(N_j * (d + mmr_j)) + N_new * (d + mmr_new) <= equity. That keeps each liquidation price at
        least d (50%) away from its entry."""
        if side < 0:
            # user rule: the liquidation-distance sizing applies to longs only; shorts use the fixed margin
            # as long as the account has that much free collateral left
            used = sum(p.margin for p in self.open_positions())
            free = self.equity() - used
            if free < settings.margin_usdc:
                return 0.0, f"collaterale libero insufficiente ({free:.2f} USDC) per uno short da {settings.margin_usdc:g} USDC"
            return settings.margin_usdc, ""
        d = settings.liq_min_distance
        c = 2 * (settings.taker_fee + settings.slippage) + 0.001  # entry/exit costs and a small buffer
        mmr = self.feed.markets[sym].mmr
        room = self.equity() - sum(n * (d + r + c) for n, r in self._exposures())
        notional = max(0.0, room / (d + mmr + c))
        margin = min(settings.margin_usdc, notional / leverage)
        if margin < settings.min_margin_usdc:
            return 0.0, f"capitale insufficiente per tenere la liquidazione al {d * 100:.0f}% dall'entrata"
        return margin, ""

    def cross_liq(self, p: Position) -> float:
        """Liquidation price of `p` with the other positions at their current price (cross margin)."""
        if not p.qty:
            return 0.0
        price = p.last_price or p.entry
        m = self.feed.markets.get(p.symbol)
        mmr = m.mmr if m else 0.05
        own = p.qty * price * mmr
        mm_others = sum(n * r for n, r in self._exposures()) - own
        # solve equity(P) = maintenance(P): E + s*q*(P - Pn) = mm_others + mmr*q*P
        free = self.equity() - mm_others
        liq = (p.side * p.qty * price - free) / (p.qty * (p.side - mmr))
        return max(liq, 0.0)

    # ------------------------------------------------------------- opening
    def _make_position(self, sym, side, lev, margin, price, origin, signal=None, tp=None, sl=None, mode="paper") -> Position:
        m = self.feed.markets[sym]
        entry = price * (1 + side * settings.slippage)
        notional = margin * lev
        lv = trade_levels(entry, side, lev, settings.take_profit_pct, settings.stop_loss_pct, m.mmr)
        p = Position(
            id=f"P{next(_ids)}", symbol=sym, base=m.base, side=side, leverage=lev, margin=margin, entry=entry,
            qty=notional / entry, notional=notional, tp=tp or lv["tp"],
            sl=sl or (lv["sl"] if settings.use_initial_stop else 0.0), liq=lv["liq"],
            mode=mode, origin=origin, signal=signal or {}, fees=notional * settings.taker_fee, last_price=price,
        )
        self._mark(p, price)
        return p

    def can_open(self) -> tuple[bool, str]:
        opening = sum(1 for i in self.intents.values() if i["action"] == "open")
        if settings.max_positions and len(self.open_positions()) + opening >= settings.max_positions:
            return False, f"già {settings.max_positions} posizioni aperte"
        if self.mode == "paper" and self.balance < settings.min_margin_usdc:
            return False, "saldo simulato insufficiente"
        return True, ""

    async def open_from_signal(self, sig: dict) -> None:
        sym = sig["symbol"]
        price = self._price(sym)
        if not price:
            return
        side = 1 if sig["side"] == "long" else -1
        snapshot = {k: sig.get(k) for k in ("kronos_p_tp", "kronos_p_sl", "combined_prob", "extra_risk", "score", "ev",
                                            "source", "p_up_4h", "conf_4h", "range_pos", "p_up_8h", "dip_price")}
        snapshot["regime"] = sig["indicators"]["regime"]
        snapshot["rsi"] = sig["indicators"]["rsi"]
        snapshot["analog_win"] = sig.get("analog_dir")
        snapshot["macro_risk"] = self.checker.macro_risk
        ok, why = self.forecast_still_valid(sym, side, price)
        if not ok:
            self.cooldown[sym] = time.time() + 120
            self.bus.agent_log("Executor", f"{sig['base']}: apertura annullata, {why}", "warn")
            return
        margin, why = self.size_margin(sym, sig["leverage"], side)
        if not margin:
            self.cooldown[sym] = time.time() + 600
            self.bus.agent_log("Executor", f"{sig['base']}: apertura saltata, {why}", "warn")
            return
        snapshot["margin_sized"] = margin
        if self.mode == "real":
            await self._real_open(sym, side, sig["leverage"], margin, price, "executor", snapshot)
            return
        p = self._make_position(sym, side, sig["leverage"], margin, price, "executor", snapshot)
        if not settings.use_take_profit:
            p.tp = 0.0  # exit on forecast reversal instead
        p.p_dir_now = sig.get("conf_4h")
        self.balance -= p.margin + p.fees
        self.positions[p.id] = p
        p.liq = self.cross_liq(p)
        where = "minimo" if side > 0 else "massimo"
        self.bus.agent_log(
            "Executor",
            f"Aperta {'LONG' if side > 0 else 'SHORT'} simulata su {p.base} {p.leverage:.0f}x, margine {p.margin:.2f} USDC, a {p.entry:.6g} "
            f"vicino al {where} delle 4 ore (liquidazione {p.liq:.6g}, {abs(p.liq / p.entry - 1) * 100:.0f}% dall'entrata); Kronos 4h {(sig.get('conf_4h') or 0) * 100:.0f}% "
            f"{'in rialzo' if side > 0 else 'in ribasso'}, rischio aggiuntivo {sig['extra_risk'] * 100:.0f}%", "success",
        )

    async def manual_order(self, sym: str, side: str, leverage: float, margin: float, order_type: str,
                           limit: float | None, tp: float | None, sl: float | None) -> dict:
        """Paper-mode order ticket from the dashboard (real orders go browser -> Orderly)."""
        if sym not in self.feed.markets:
            return {"ok": False, "error": "mercato sconosciuto"}
        ok, why = self.can_open()
        if not ok:
            return {"ok": False, "error": why}
        s = 1 if side == "long" else -1
        price = self._price(sym)
        if order_type == "limit" and limit:
            o = PendingOrder(f"O{next(_ids)}", sym, s, leverage, margin, limit, tp, sl)
            self.pending[o.id] = o
            self.bus.agent_log("Executor", f"Ordine limite simulato {side.upper()} {self.feed.markets[sym].base} a {limit:.6g}", "info")
            return {"ok": True, "order": o.view()}
        p = self._make_position(sym, s, leverage, margin, price, "manual", tp=tp, sl=sl)
        self.balance -= p.margin + p.fees
        self.positions[p.id] = p
        self.bus.agent_log("Executor", f"Ordine manuale simulato {side.upper()} {p.base} {leverage:.0f}x a {p.entry:.6g}", "success")
        return {"ok": True, "position": p.view()}

    def cancel_order(self, oid: str) -> bool:
        return self.pending.pop(oid, None) is not None

    # ------------------------------------------------------------- closing
    async def close(self, pid: str, reason: str, price: float | None = None) -> None:
        p = self.positions.get(pid)
        if not p or p.status == "closed":
            return
        if p.mode == "real" and p.status == "open":
            p.status = "closing"
            self._intent({"action": "close", "position_id": p.id, "symbol": p.symbol, "side": "long" if p.side > 0 else "short",
                          "qty": p.qty, "reason": reason})
            p.reason = reason
            return
        price = price or self._price(p.symbol) or p.last_price
        exit_price = price * (1 - p.side * settings.slippage)
        self._finalize(p, exit_price, reason)

    def _finalize(self, p: Position, exit_price: float, reason: str) -> None:
        gross = p.side * (exit_price / p.entry - 1)
        p.fees += p.notional * settings.taker_fee
        p.pnl = gross * p.notional - p.fees - p.funding
        p.lev_move = p.side * (exit_price / p.entry - 1) * p.leverage * 100
        p.roe = p.pnl / p.margin * 100
        p.exit, p.exit_ts, p.reason, p.status = exit_price, time.time(), reason, "closed"
        if p.mode == "paper":
            entry_fee = p.notional * settings.taker_fee  # already taken from the balance at open
            self.balance += p.margin + p.pnl + entry_fee  # cross margin: a loss can exceed the position margin
        self.cooldown[p.symbol] = time.time() + 300
        self.controller.record_trade(p)
        lvl = "success" if p.pnl > 0 else "danger"
        self.bus.agent_log("Executor", f"Chiusa {p.base} ({reason}): PnL {p.pnl:+.2f} USDC ({p.roe:+.1f}%) → inviato al Controller", lvl)

    async def close_all(self, reason: str) -> None:
        kept = []
        for p in list(self.open_positions()):
            if settings.hold_losers and p.pnl < 0:
                kept.append(p.base)  # user rule: losing positions stay open
                continue
            await self.close(p.id, reason)
        if kept:
            self.bus.agent_log("Executor", f"Posizioni in perdita mantenute aperte nonostante l'Analyst: {', '.join(kept)}", "warn")

    # ------------------------------------------------------------- monitoring
    def _check(self, p: Position, price: float, dt: float) -> str | None:
        m = self.feed.markets.get(p.symbol)
        if m and m.funding_rate and p.mode == "paper":
            p.funding += p.side * p.notional * m.funding_rate * dt / (m.funding_period_h * 3600)
        self._mark(p, price)
        if p.mode == "paper":
            p.liq = self.cross_liq(p)
        p.peak_lev_move = max(p.peak_lev_move, p.lev_move)
        if (p.side > 0 and price <= p.liq) or (p.side < 0 and price >= p.liq):
            return "liquidazione"
        if p.sl > 0 and ((p.side > 0 and price <= p.sl) or (p.side < 0 and price >= p.sl)):
            if p.locked:
                return f"stop di protezione +{settings.lock_profit_pct:g}% (leva)"
            return f"stop loss {-settings.stop_loss_pct:g}% (leva)"
        if not p.locked and p.lev_move >= settings.lock_trigger_pct:
            self._lock_profit(p)
        if p.tp > 0 and ((p.side > 0 and price >= p.tp) or (p.side < 0 and price <= p.tp)):
            return f"take profit +{settings.take_profit_pct:.0f}% (leva)"
        reason = self._reversal(p)
        if reason and settings.hold_losers and p.pnl < 0:
            return None  # user rule: a losing position is never closed by the agents (only liquidation)
        return reason

    def _lock_profit(self, p: Position) -> None:
        """After +5% leveraged, move the stop to +2% leveraged profit (never below the current stop)."""
        new_sl = p.entry * (1 + p.side * settings.lock_profit_pct / 100 / p.leverage)
        if p.sl > 0 and ((p.side > 0 and new_sl <= p.sl) or (p.side < 0 and new_sl >= p.sl)):
            return
        p.sl, p.locked = new_sl, True
        self.bus.agent_log(
            "Executor", f"{p.base}: raggiunto +{settings.lock_trigger_pct:g}% con leva, stop spostato a +{settings.lock_profit_pct:g}% di profitto ({new_sl:.6g})",
            "success",
        )
        if p.mode == "real":
            self._intent({"action": "update_sl", "position_id": p.id, "symbol": p.symbol,
                          "side": "long" if p.side > 0 else "short", "qty": p.qty, "sl": new_sl})

    def _reversal(self, p: Position) -> str | None:
        """Close when the 4h forecast flips against the position, with a margin and confirmation
        so small wobbles around 50% do not close a trade early (the stop loss stays active anyway)."""
        if self.predictor is None:
            return None
        c = self.predictor.cache.get((p.symbol, "15m"))
        if not c or c["ts"] <= p.last_pred_ts or c["ts"] < p.opened_ts:
            return None
        p.last_pred_ts = c["ts"]
        from ..config import HORIZONS

        closes = c["paths"][:, HORIZONS["4h"][2] - 1, 3]
        p_up = float((closes > c["last"]).mean())
        p.p_dir_now = p_up if p.side > 0 else 1 - p_up
        if p.p_dir_now < 0.5 - settings.exit_margin:
            p.reverse_count += 1
        else:
            p.reverse_count = 0
        if p.reverse_count >= settings.exit_confirmations:
            opp = 1 - p.p_dir_now
            return f"inversione della previsione 4h ({'ribasso' if p.side > 0 else 'rialzo'} al {opp * 100:.0f}%)"
        return None if settings.hold_losers else self._recovery(p, c)

    def _recovery(self, p: Position, c: dict) -> str | None:
        """No stop in loss: after `recovery_hours`, a losing position is closed only if the 4h projection
        gives little chance (< recovery_min_prob) of touching break-even (entry + round-trip costs)."""
        if p.locked or p.pnl >= 0 or time.time() - p.opened_ts < settings.recovery_hours * 3600:
            p.recovery_prob = None if p.pnl >= 0 else p.recovery_prob
            return None
        from ..config import HORIZONS

        steps = HORIZONS["4h"][2]
        cost = 2 * (settings.taker_fee + settings.slippage)
        be = p.entry * (1 + p.side * cost)
        path = c["paths"][:, :steps, :]
        hit = (path[:, :, 1] >= be).any(axis=1) if p.side > 0 else (path[:, :, 2] <= be).any(axis=1)
        p.recovery_prob = float(hit.mean())
        if p.recovery_prob < settings.recovery_min_prob:
            hours = (time.time() - p.opened_ts) / 3600
            return f"nessun recupero previsto dopo {hours:.1f} h (pareggio entro 4 h al {p.recovery_prob * 100:.0f}%)"
        return None

    async def run(self) -> None:
        last = time.time()
        while True:
            now = time.time()
            dt, last = now - last, now
            try:
                for p in list(self.open_positions()):
                    price = self._price(p.symbol)
                    if not price or p.status != "open":
                        continue
                    reason = self._check(p, price, dt)
                    if reason:
                        await self.close(p.id, reason, self._exit_level(p, reason, price))
                for o in list(self.pending.values()):
                    price = self._price(o.symbol)
                    if price and ((o.side > 0 and price <= o.limit) or (o.side < 0 and price >= o.limit)):
                        self.pending.pop(o.id)
                        if self.can_open()[0]:
                            p = self._make_position(o.symbol, o.side, o.leverage, o.margin, o.limit, "manual", tp=o.tp, sl=o.sl)
                            self.balance -= p.margin + p.fees
                            self.positions[p.id] = p
                            self.bus.agent_log("Executor", f"Ordine limite eseguito su {p.base} a {o.limit:.6g}", "success")
                await self._maybe_open()
                self._expire_intents()
                if time.time() - self._last_hist >= 30:
                    self._last_hist = time.time()
                    self.equity_hist.append((self._last_hist, self.paper_equity()))
                    self.equity_hist = self.equity_hist[-1440:]
                if self.predictor is not None:
                    self.predictor.priority = {p.symbol for p in self.open_positions()}
                self.bus.publish("executor", self.view())
            except Exception:
                log.exception("executor cycle failed")
            await asyncio.sleep(0.5)

    @staticmethod
    def _exit_level(p: Position, reason: str, price: float) -> float:
        """Paper fills at the trigger level (stop orders), or worse if the price gapped through it."""
        if reason.startswith("take"):
            return p.tp
        if reason.startswith("stop"):
            return min(price, p.sl) if p.side > 0 else max(price, p.sl)
        return price

    async def _maybe_open(self) -> None:
        if not self.auto or self.checker.paused:
            return
        ok, _ = self.can_open()
        if not ok:
            return
        if self._confirming:
            return  # one confirmation at a time: sizing must see the previous opening first
        held = {p.symbol for p in self.open_positions()} | {i["symbol"] for i in self.intents.values()}
        for sig in self.checker.signals():
            if sig["symbol"] in held or self.cooldown.get(sig["symbol"], 0) > time.time():
                continue
            if sig["age"] > settings.max_forecast_age_s:
                continue  # stale forecast
            self._confirming.add(sig["symbol"])
            asyncio.create_task(self._confirm_and_open(sig))
            return  # one per cycle

    async def _confirm_and_open(self, sig: dict) -> None:
        """Before entering, run Kronos again on the token and re-evaluate: enter only if the fresh forecast
        still says this is the entry point, not if it now shows a further drop ahead."""
        sym = sig["symbol"]
        try:
            if self.predictor is not None:
                fresh = await asyncio.to_thread(self.predictor._run_tf, sym, "15m")
                if fresh:
                    self.predictor.cache[(sym, "15m")] = fresh
            new = await asyncio.to_thread(self.checker.evaluate, sym)
            if not new or not new["eligible"]:
                why = ", ".join(new["reasons"]) if new else "previsione non disponibile"
                self.cooldown[sym] = time.time() + 180
                self.bus.agent_log("Executor", f"{sig['base']}: previsione ricalcolata prima dell'ingresso, rinvio ({why})", "info")
                return
            self.bus.agent_log(
                "Executor",
                f"{sig['base']}: previsione ricalcolata, punto d'ingresso confermato (4 ore {new['conf_4h'] * 100:.0f}%, "
                f"8 ore {new['p_up_8h'] * 100:.0f}% di rialzo)", "info",
            )
            await self.open_from_signal(new)
        except Exception:
            log.exception("confirmation before entry failed for %s", sym)
        finally:
            self._confirming.discard(sym)

    # ------------------------------------------------------------- real mode
    def _intent(self, payload: dict) -> dict:
        iid = f"I{next(_ids)}"
        intent = {"id": iid, "ts": time.time(), **payload}
        self.intents[iid] = intent
        self.bus.publish("real_intent", intent)
        return intent

    async def _real_open(self, sym, side, lev, margin, price, origin, snapshot) -> None:
        m = self.feed.markets[sym]
        lv = trade_levels(price, side, lev, settings.take_profit_pct, settings.stop_loss_pct, m.mmr)
        qty = margin * lev / price
        self._intent({
            "action": "open", "symbol": sym, "base": m.base, "side": "long" if side > 0 else "short",
            "leverage": lev, "margin": margin, "qty": qty, "price": price,
            "tp": lv["tp"] if settings.use_take_profit else None, "sl": lv["sl"] if settings.use_initial_stop else None,
            "base_tick": m.base_tick, "quote_tick": m.quote_tick, "origin": origin, "signal": snapshot,
        })
        self.bus.agent_log("Executor", f"Richiesta ordine REALE {('LONG' if side > 0 else 'SHORT')} {m.base} {lev}x inviata alla dashboard per la firma", "warn")

    def claim(self, iid: str) -> bool:
        """First browser tab to claim an intent executes it; the others skip it."""
        it = self.intents.get(iid)
        if not it or it.get("claimed"):
            return False
        it["claimed"] = time.time()
        return True

    def ack(self, iid: str, ok: bool, fill_price: float | None = None, qty: float | None = None,
            order_id: str | None = None, error: str | None = None) -> dict:
        intent = self.intents.pop(iid, None)
        if not intent:
            return {"ok": False, "error": "intent sconosciuto o scaduto"}
        if not ok:
            self.bus.agent_log("Executor", f"Ordine reale fallito su {intent['symbol']}: {error}", "danger")
            if intent["action"] == "open":
                # never retry blindly with real money: cool the token down and stop automatic real trading
                self.cooldown[intent["symbol"]] = time.time() + 900
                self.real_auto = False
                self.bus.agent_log("Executor", "Trading automatico reale disattivato dopo un errore: controlla e riattivalo dalla dashboard", "warn")
            if intent["action"] == "close":
                p = self.positions.get(intent["position_id"])
                if p:
                    p.status = "open"
            return {"ok": True}
        if intent["action"] == "update_sl":
            self.bus.agent_log("Executor", f"Stop di protezione aggiornato anche sul mercato per {intent['symbol'].split('_')[1]}", "info")
            return {"ok": True}
        if intent["action"] == "open":
            side = 1 if intent["side"] == "long" else -1
            p = self._make_position(intent["symbol"], side, intent["leverage"], intent["margin"], fill_price or intent["price"],
                                    intent["origin"], intent.get("signal"), mode="real")
            if qty:
                p.qty = qty
            p.venue_order_id = order_id
            if not settings.use_take_profit:
                p.tp = 0.0
            self.positions[p.id] = p
            self.bus.agent_log("Executor", f"Posizione REALE aperta su {p.base} a {p.entry:.6g}", "success")
        else:
            p = self.positions.get(intent["position_id"])
            if p:
                self._finalize(p, fill_price or self._price(p.symbol) or p.last_price, p.reason or intent.get("reason", "chiusura"))
        return {"ok": True}

    def reconcile_real(self, venue_positions: list[dict], equity: float | None = None) -> None:
        """Browser snapshot of Orderly positions: anything we track that vanished was closed by the venue."""
        self.venue_rows = venue_positions
        if equity is not None:
            self.venue_equity = equity
        for row in venue_positions:
            for p in self.open_positions():
                if p.mode == "real" and p.symbol == row.get("symbol") and row.get("est_liq_price"):
                    p.liq = float(row["est_liq_price"])
        live = {row["symbol"] for row in venue_positions if abs(float(row.get("position_qty", 0))) > 0}
        for p in list(self.open_positions()):
            if p.mode != "real" or p.symbol in live or time.time() - p.opened_ts < 15:
                continue
            price = self._price(p.symbol) or p.last_price
            if (p.side > 0 and price <= p.liq * 1.002) or (p.side < 0 and price >= p.liq * 0.998):
                reason = "liquidazione"
            elif (p.side > 0 and price >= p.tp * 0.999) or (p.side < 0 and price <= p.tp * 1.001):
                reason = "take profit (ordine sul mercato)"
            elif (p.side > 0 and price <= p.sl * 1.001) or (p.side < 0 and price >= p.sl * 0.999):
                reason = "stop loss (ordine sul mercato)"
            else:
                reason = "chiusa sul mercato"
            self._finalize(p, price, reason)

    def _expire_intents(self) -> None:
        for iid, it in list(self.intents.items()):
            if time.time() - it["ts"] > (90 if it.get("claimed") else 30):
                self.intents.pop(iid)
                self.bus.agent_log("Executor", f"Richiesta reale scaduta senza firma ({it['action']} {it['symbol']})", "warn")
                if it["action"] == "close":
                    p = self.positions.get(it["position_id"])
                    if p and p.status == "closing":
                        p.status = "open"

    # ------------------------------------------------------------------ view
    def view(self) -> dict:
        open_ = [p.view() for p in self.open_positions()]
        equity = self.balance + sum(p["margin"] + p["pnl"] + p["fees"] for p in open_ if p["mode"] == "paper")
        return {
            "mode": self.mode, "auto": self.auto, "wallet": self.wallet, "real_ready": self.real_ready,
            "real_auto": self.real_auto, "balance": self.balance, "equity": equity, "positions": open_,
            "pending": [o.view() for o in self.pending.values()], "intents": list(self.intents.values()),
            "max_positions": settings.max_positions, "margin_usdc": settings.margin_usdc,
            "tp_pct": settings.take_profit_pct, "sl_pct": settings.stop_loss_pct,
            "use_tp": settings.use_take_profit, "exit_margin": settings.exit_margin,
            "exit_confirmations": settings.exit_confirmations, "entry_zone": settings.entry_zone,
            "lock_trigger_pct": settings.lock_trigger_pct, "lock_profit_pct": settings.lock_profit_pct,
            "liq_min_distance": settings.liq_min_distance, "equity_now": self.equity(), "paper": self.paper_view(),
            "hold_losers": settings.hold_losers, "longs_only": settings.longs_only,
            "use_initial_stop": settings.use_initial_stop, "recovery_hours": settings.recovery_hours,
            "recovery_min_prob": settings.recovery_min_prob,
        }
