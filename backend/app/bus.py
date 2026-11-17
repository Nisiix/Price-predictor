"""Tiny pub/sub hub between agents and WebSocket clients."""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque


class Bus:
    def __init__(self) -> None:
        self.clients: set[asyncio.Queue] = set()
        self.log: deque = deque(maxlen=400)  # agent activity feed
        self.latest: dict[str, dict] = {}

    def publish(self, kind: str, data: dict) -> None:
        msg = {"type": kind, "ts": time.time(), "data": data}
        self.latest[kind] = msg
        raw = json.dumps(msg, default=_default)
        for q in list(self.clients):
            if q.qsize() > 200:
                continue  # slow client, drop
            q.put_nowait(raw)

    def agent_log(self, agent: str, text: str, level: str = "info", **extra) -> None:
        entry = {"agent": agent, "text": text, "level": level, "ts": time.time(), **extra}
        self.log.append(entry)
        self.publish("log", entry)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)


def _default(o):
    try:
        import numpy as np

        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
    except Exception:
        pass
    return str(o)
