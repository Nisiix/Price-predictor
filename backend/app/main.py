"""FastAPI app: wires the feed, Kronos and the four agents, streams everything over WebSocket."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agents.analyst import Analyst
from .agents.checker import Checker
from .agents.controller import Controller
from .agents.executor import Executor
from .bus import Bus
from .config import HORIZONS, ROOT, settings
from .market.feed import MarketFeed
from .predict.service import PredictionService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("app")

bus = Bus()
feed = MarketFeed()
predictor = PredictionService(feed)
controller = Controller(bus)
checker = Checker(feed, predictor, bus)
executor = Executor(feed, checker, controller, bus)
analyst = Analyst(feed, checker, executor, bus)
executor.predictor = predictor
controller.predictor = predictor
_focus: dict[str, float] = {}
_client_horizon: dict[int, str] = {}  # each open dashboard sorts by its own horizon


def _sync_horizons() -> None:
    predictor.board_horizons = set(_client_horizon.values()) or {"15m"}
STARTED = time.time()


async def _ticker() -> None:
    """1 Hz snapshot: prices of the universe + forecast of the focused tokens."""
    while True:
        try:
            now = time.time()
            for s, t in list(_focus.items()):
                if now - t > 90:
                    _focus.pop(s, None)
            predictor.focus = set(_focus)
            bus.publish("tick", {
                "prices": {s: {"p": m.mark_price, "c": m.change_24h} for s, m in feed.markets.items()},
                "focus": {s: predictor.snapshot(s) for s in _focus},
                "boards": {h: predictor.board(h) for h in predictor.board_horizons},
                "system": system_status(),
            })
        except Exception:
            log.exception("ticker failed")
        await asyncio.sleep(1)


def _supervise(coro_fn, name: str):
    async def runner():
        while True:
            try:
                await coro_fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s crashed, restarting in 5s", name)
                await asyncio.sleep(5)
    return asyncio.create_task(runner(), name=name)


@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = [
        _supervise(feed.run, "feed"),
        _supervise(predictor.run, "predictor"),
        _supervise(checker.run, "checker"),
        _supervise(analyst.run, "analyst"),
        _supervise(executor.run, "executor"),
        _supervise(controller.run, "controller"),
        _supervise(_ticker, "ticker"),
    ]
    bus.agent_log("Sistema", "Motore avviato: feed Raydium Perps, Kronos e agenti in esecuzione", "info")
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Price predictor", lifespan=lifespan)
ALLOWED_ORIGINS = {"http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:5173", "http://localhost:5173"}
app.add_middleware(CORSMiddleware, allow_origins=sorted(ALLOWED_ORIGINS), allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def same_origin_only(request, call_next):
    """Other websites open in the browser must not be able to drive the Executor."""
    origin = request.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        return PlainTextResponse("origin not allowed", status_code=403)
    return await call_next(request)


def system_status() -> dict:
    return {
        "feed": feed.status, "last_tick": feed.last_tick_ts, "markets": len(feed.markets),
        "kronos": predictor.status(), "checker": {"paused": checker.paused, "reason": checker.pause_reason if checker.paused else "",
                                                  "ranked": len(checker.ranking), "signals": len(checker.signals())},
        "analyst": {"risk": analyst.risk, "status": analyst.status}, "executor": {"mode": executor.mode},
        "uptime": time.time() - STARTED,
    }


# ------------------------------------------------------------------ REST
@app.get("/api/state")
def state():
    return {
        "system": system_status(),
        "markets": [m.to_dict() for m in sorted(feed.markets.values(), key=lambda m: m.rank)],
        "horizons": {k: {"seconds": v[0], "tf": v[1]} for k, v in HORIZONS.items()},
        "executor": executor.view(), "analyst": analyst.view(),
        "ranking": [Checker.slim(r) for r in checker.ranking[:40]],
        "log": list(bus.log)[-150:],
        "settings": {"margin_usdc": settings.margin_usdc, "max_positions": settings.max_positions,
                     "tp_pct": settings.take_profit_pct, "sl_pct": settings.stop_loss_pct,
                     "leverage_choices": settings.leverage_choices, "broker_id": settings.broker_id,
                     "orderly_api": settings.orderly_api, "taker_fee": settings.taker_fee},
    }


@app.get("/api/candles/{symbol}")
def candles(symbol: str, tf: str = "1m", n: int = 300):
    if tf not in ("5s", "1m", "5m"):
        raise HTTPException(400, "tf must be 5s, 1m or 5m")
    bars = feed.store.get(symbol, tf, n)
    return {"symbol": symbol, "tf": tf, "bars": bars.tolist()}


@app.get("/api/prediction/{symbol}")
def prediction(symbol: str):
    _focus[symbol] = time.time()
    snap = predictor.snapshot(symbol)
    detail = next((r for r in checker.ranking if r["symbol"] == symbol), None)
    if detail:
        snap["checker"] = {k: v for k, v in detail.items() if k not in ("levels",)} | {"levels": detail["levels"]}
    return snap


class Focus(BaseModel):
    symbol: str


@app.post("/api/focus")
def focus(f: Focus):
    if f.symbol not in feed.markets:
        raise HTTPException(404, "unknown market")
    _focus.clear()
    _focus[f.symbol] = time.time()
    predictor.focus = set(_focus)
    return {"ok": True}


class Order(BaseModel):
    symbol: str
    side: str  # long | short
    leverage: float
    margin: float = settings.margin_usdc
    order_type: str = "market"  # market | limit
    limit: float | None = None
    tp: float | None = None
    sl: float | None = None


@app.post("/api/order")
async def order(o: Order):
    if o.side not in ("long", "short") or not (1 <= o.leverage <= 50):
        raise HTTPException(400, "invalid order")
    return await executor.manual_order(o.symbol, o.side, o.leverage, o.margin, o.order_type, o.limit, o.tp, o.sl)


@app.delete("/api/order/{oid}")
def cancel(oid: str):
    return {"ok": executor.cancel_order(oid)}


@app.post("/api/position/{pid}/close")
async def close_position(pid: str):
    await executor.close(pid, "chiusura manuale dalla dashboard")
    return {"ok": True}


class ExecSettings(BaseModel):
    auto: bool | None = None
    real_auto: bool | None = None


@app.post("/api/executor")
def exec_settings(s: ExecSettings):
    if s.auto is not None:
        executor.auto = s.auto
        bus.agent_log("Executor", f"Trading automatico {'attivato' if s.auto else 'sospeso'}", "info")
    if s.real_auto is not None:
        executor.real_auto = s.real_auto and bool(executor.wallet) and executor.real_ready
        bus.agent_log("Executor", f"Trading automatico REALE {'attivato' if executor.real_auto else 'disattivato'}", "warn")
    return executor.view()


class Wallet(BaseModel):
    address: str | None = None
    real_ready: bool = False


@app.post("/api/wallet")
def wallet(w: Wallet):
    changed = w.address != executor.wallet
    executor.wallet = w.address
    executor.real_ready = bool(w.address) and w.real_ready
    if not executor.real_ready:
        executor.real_auto = False
    if changed:
        bus.agent_log("Sistema", f"Wallet Phantom {'collegato: ' + w.address[:4] + 'â€¦' + w.address[-4:] if w.address else 'scollegato'}", "info")
    return executor.view()


class Ack(BaseModel):
    intent_id: str
    ok: bool
    fill_price: float | None = None
    qty: float | None = None
    order_id: str | None = None
    error: str | None = None


class Claim(BaseModel):
    intent_id: str


@app.post("/api/real/claim")
def real_claim(c: Claim):
    return {"ok": executor.claim(c.intent_id)}


@app.post("/api/real/ack")
def real_ack(a: Ack):
    return executor.ack(a.intent_id, a.ok, a.fill_price, a.qty, a.order_id, a.error)


class VenuePositions(BaseModel):
    rows: list[dict]


@app.post("/api/real/positions")
def real_positions(v: VenuePositions):
    executor.reconcile_real(v.rows)
    return {"ok": True}


@app.get("/api/board")
def board(h: str = "15m"):
    if h not in HORIZONS:
        raise HTTPException(400, f"h must be one of {list(HORIZONS)}")
    return predictor.board(h)


@app.get("/api/report")
def report():
    return controller.report()


@app.get("/api/report.md", response_class=PlainTextResponse)
def report_md():
    return controller.markdown()


# ------------------------------------------------------------------ WebSocket
@app.websocket("/ws")
async def ws(socket: WebSocket):
    origin = socket.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        await socket.close(code=1008)
        return
    await socket.accept()
    q = bus.subscribe()
    try:
        import json

        for kind in ("ranking", "analyst", "executor", "report"):
            if kind in bus.latest:
                await socket.send_text(json.dumps(bus.latest[kind], default=str))
        sender = asyncio.create_task(_pump(socket, q))
        while True:
            msg = await socket.receive_json()
            if msg.get("type") == "focus" and msg.get("symbol") in feed.markets:
                _focus.clear()
                _focus[msg["symbol"]] = time.time()
                predictor.focus = set(_focus)
                if not feed.history_ready(msg["symbol"]):
                    asyncio.create_task(feed.ensure_history(msg["symbol"]))
            elif msg.get("type") == "horizon" and msg.get("h") in HORIZONS:
                _client_horizon[id(socket)] = msg["h"]
                _sync_horizons()
            elif msg.get("type") == "ping" and msg.get("symbol") in feed.markets:
                _focus[msg["symbol"]] = time.time()
    except WebSocketDisconnect:
        pass
    finally:
        _client_horizon.pop(id(socket), None)
        _sync_horizons()
        bus.unsubscribe(q)
        try:
            sender.cancel()
        except Exception:
            pass


async def _pump(socket: WebSocket, q: asyncio.Queue):
    while True:
        raw = await q.get()
        await socket.send_text(raw)


# ------------------------------------------------------------------ frontend
DIST = ROOT / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        f = DIST / path
        if path and f.is_file():
            return FileResponse(f)
        return FileResponse(DIST / "index.html")
