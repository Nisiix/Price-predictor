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

    # ------------------------------------------------------------- opening
    def _make_position(self, sym, side, lev, margin, price, origin, signal=None, tp=None, sl=None, mode="paper") -> Position:
        m = self.feed.markets[sym]
        entry = price * (1 + side * settings.slippage)
        notional = margin * lev
        lv = trade_levels(entry, side, lev, settings.take_profit_pct, settings.stop_loss_pct, m.mmr)
        p = Position(
            id=f"P{next(_ids)}", symbol=sym, base=m.base, side=side, leverage=lev, margin=margin, entry=entry,
            qty=notional / entry, notional=notional, tp=tp or lv["tp"], sl=sl or lv["sl"], liq=lv["liq"],
            mode=mode, origin=origin, signal=signal or {}, fees=notional * settings.taker_fee, last_price=price,
        )
        self._mark(p, price)
        return p

    def can_open(self) -> tuple[bool, str]:
        if len(self.open_positions()) >= settings.max_positions:
            return False, f"già {settings.max_positions} posizioni aperte"
        if self.mode == "paper" and self.balance < settings.margin_usdc:
            return False, "saldo simulato insufficiente"
        return True, ""

    async def open_from_signal(self, sig: dict) -> None:
        sym = sig["symbol"]
        price = self._price(sym)
        if not price:
            return
        side = 1 if sig["side"] == "long" else -1
        snapshot = {k: sig.get(k) for k in ("kronos_p_tp", "kronos_p_sl", "combined_prob", "extra_risk", "score", "ev", "source")}
        snapshot["regime"] = sig["indicators"]["regime"]
        snapshot["rsi"] = sig["indicators"]["rsi"]
        snapshot["analog_win"] = sig["analogs"].get("long_win" if side > 0 else "short_win")
        snapshot["macro_risk"] = self.checker.macro_risk
        if self.mode == "real":
            await self._real_open(sym, side, sig["leverage"], settings.margin_usdc, price, "executor", snapshot)
            return
        p = self._make_position(sym, side, sig["leverage"], settings.margin_usdc, price, "executor", snapshot)
        self.balance -= p.margin + p.fees
        self.positions[p.id] = p
        self.bus.agent_log(
            "Executor",
            f"Aperta {'LONG' if side > 0 else 'SHORT'} simulata su {p.base} {p.leverage:.0f}x a {p.entry:.6g} "
            f"(TP {p.tp:.6g}, SL {p.sl:.6g}, liq {p.liq:.6g}); Kronos TP {sig['kronos_p_tp'] * 100:.0f}%, "
            f"rischio aggiuntivo {sig['extra_risk'] * 100:.0f}%", "success",
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
        if reason.startswith("liquidazione"):
            p.pnl = -p.margin - p.fees
        else:
            gross = p.side * (exit_price / p.entry - 1)
            p.fees += p.notional * settings.taker_fee
            p.pnl = gross * p.notional - p.fees - p.funding
        p.lev_move = p.side * (exit_price / p.entry - 1) * p.leverage * 100
        p.roe = p.pnl / p.margin * 100
        p.exit, p.exit_ts, p.reason, p.status = exit_price, time.time(), reason, "closed"
        if p.mode == "paper":
            entry_fee = p.notional * settings.taker_fee  # already taken from the balance at open
            self.balance += max(0.0, p.margin + p.pnl + entry_fee)
        self.cooldown[p.symbol] = time.time() + 300
        self.controller.record_trade(p)
        lvl = "success" if p.pnl > 0 else "danger"
        self.bus.agent_log("Executor", f"Chiusa {p.base} ({reason}): PnL {p.pnl:+.2f} USDC ({p.roe:+.1f}%) → inviato al Controller", lvl)

    async def close_all(self, reason: str) -> None:
        for p in list(self.open_positions()):
            await self.close(p.id, reason)

    # ------------------------------------------------------------- monitoring
    def _check(self, p: Position, price: float, dt: float) -> str | None:
        m = self.feed.markets.get(p.symbol)
        if m and m.funding_rate and p.mode == "paper":
            p.funding += p.side * p.notional * m.funding_rate * dt / (m.funding_period_h * 3600)
        self._mark(p, price)
        if (p.side > 0 and price <= p.liq) or (p.side < 0 and price >= p.liq):
            return "liquidazione"
        if (p.side > 0 and price <= p.sl) or (p.side < 0 and price >= p.sl):
            return f"stop loss {-settings.stop_loss_pct:.0f}% (leva)"
        if (p.side > 0 and price >= p.tp) or (p.side < 0 and price <= p.tp):
            return f"take profit +{settings.take_profit_pct:.0f}% (leva)"
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
        held = {p.symbol for p in self.open_positions()} | {i["symbol"] for i in self.intents.values()}
        for sig in self.checker.signals():
            if sig["symbol"] in held or self.cooldown.get(sig["symbol"], 0) > time.time():
                continue
            if sig["age"] > 240:
                continue  # stale forecast
            await self.open_from_signal(sig)
            return  # one per cycle

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
            "leverage": lev, "margin": margin, "qty": qty, "price": price, "tp": lv["tp"], "sl": lv["sl"],
            "base_tick": m.base_tick, "quote_tick": m.quote_tick, "origin": origin, "signal": snapshot,
        })
        self.bus.agent_log("Executor", f"Richiesta ordine REALE {('LONG' if side > 0 else 'SHORT')} {m.base} {lev}x inviata alla dashboard per la firma", "warn")

    def ack(self, iid: str, ok: bool, fill_price: float | None = None, qty: float | None = None,
            order_id: str | None = None, error: str | None = None) -> dict:
        intent = self.intents.pop(iid, None)
        if not intent:
            return {"ok": False, "error": "intent sconosciuto o scaduto"}
        if not ok:
            self.bus.agent_log("Executor", f"Ordine reale fallito su {intent['symbol']}: {error}", "danger")
            if intent["action"] == "close":
                p = self.positions.get(intent["position_id"])
                if p:
                    p.status = "open"
            return {"ok": True}
        if intent["action"] == "open":
            side = 1 if intent["side"] == "long" else -1
            p = self._make_position(intent["symbol"], side, intent["leverage"], intent["margin"], fill_price or intent["price"],
                                    intent["origin"], intent.get("signal"), mode="real")
            if qty:
                p.qty = qty
            p.venue_order_id = order_id
            self.positions[p.id] = p
            self.bus.agent_log("Executor", f"Posizione REALE aperta su {p.base} a {p.entry:.6g}", "success")
        else:
            p = self.positions.get(intent["position_id"])
            if p:
                self._finalize(p, fill_price or self._price(p.symbol) or p.last_price, p.reason or intent.get("reason", "chiusura"))
        return {"ok": True}

    def reconcile_real(self, venue_positions: list[dict]) -> None:
        """Browser snapshot of Orderly positions: anything we track that vanished was closed by the venue."""
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
            if time.time() - it["ts"] > 30:
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
        }
