"""Analyst agent: watches micro/macro news and market shocks.

Headlines from crypto and macro feeds are matched to categories of past
events (wars, bank crises, regulation, hacks, exchange collapses...). Each
category carries a severity calibrated on how hard BTC and altcoins fell
after the historical events of that type. Recent, severe, negative headlines
plus sudden BTC moves and extreme sentiment form a 0-100 risk score.

Above `analyst_pause_threshold` the Checker stops opening new trades only if PP_ANALYST_BLOCKS=1 (off: user rule); above
`analyst_close_threshold` (or on a critical headline / BTC shock) the Analyst
orders the Executor to close every open position that is not at a loss (losers are kept, user rule).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from email.utils import parsedate_to_datetime

import feedparser
import httpx

from ..config import settings
from .history_events import CALMING, CATEGORIES, EVENTS, SEVERITY

log = logging.getLogger("analyst")

FEEDS = {
    "Cointelegraph": "https://cointelegraph.com/rss",
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Decrypt": "https://decrypt.co/feed",
    "Google News – macro": "https://news.google.com/rss/search?q=(war+OR+sanctions+OR+tariffs+OR+%22bank+collapse%22+OR+%22Federal+Reserve%22+OR+recession)&hl=en-US&gl=US&ceid=US:en",
    "Google News – crypto regulation": "https://news.google.com/rss/search?q=crypto+(SEC+OR+regulation+OR+ban+OR+hack+OR+exploit)&hl=en-US&gl=US&ceid=US:en",
}
HALF_LIFE_S = 3 * 3600  # headline weight halves every 3 hours
WINDOW_S = 12 * 3600


MARKET_WORDS = ["crypto", "bitcoin", "btc", "ether", "solana", "stablecoin", "exchange", "market", "stocks", "fed ",
                 "federal reserve", "treasury", "dollar", "liquidat", "binance", "coinbase", "defi", "token"]
STRONG_WORDS = ["crash", "collapse", "plunge", "emergency", "declares war", "invades", "invasion", "bankrupt", "halts",
                "record liquidations", "freezes", "nuclear", "default", "panic", "meltdown", "suspends"]


def classify(text: str) -> tuple[list[str], bool]:
    t = text.lower()
    cats = [c for c, kws in CATEGORIES.items() if any(k in t for k in kws)]
    calming = any(k in t for k in CALMING)
    return cats, calming


def impact(text: str, severity: float, calming: bool) -> float:
    """How much one headline should weigh (0..~0.5): category severity from history,
    scaled down for ordinary coverage, up for crash vocabulary, and by market relevance."""
    t = text.lower()
    intensity = 0.875 if any(k in t for k in STRONG_WORDS) else 0.35
    relevance = 1.0 if any(k in t for k in MARKET_WORDS) else 0.4
    return severity * intensity * relevance * (0.3 if calming else 1.0)


class Analyst:
    def __init__(self, feed, checker, executor, bus) -> None:
        self.feed = feed
        self.checker = checker
        self.executor = executor
        self.bus = bus
        self.headlines: dict[str, dict] = {}
        self.risk = 0.0
        self.components: dict[str, float] = {}
        self.fear_greed: dict | None = None
        self.last_close_order = 0.0
        self._critical: dict | None = None
        self.status = "starting"
        self._client = httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": "price-predictor/1.0"})

    # ----------------------------------------------------------------- news
    async def fetch_news(self) -> None:
        ok = 0
        for source, url in FEEDS.items():
            try:
                r = await self._client.get(url)
                parsed = feedparser.parse(r.text)
                ok += 1
            except Exception as exc:
                log.debug("feed %s failed: %s", source, exc)
                continue
            for e in parsed.entries[:40]:
                title = re.sub(r"\s+", " ", e.get("title", "")).strip()
                if not title:
                    continue
                hid = hashlib.sha1(title.lower().encode()).hexdigest()[:16]
                if hid in self.headlines:
                    continue
                try:
                    ts = parsedate_to_datetime(e.get("published")).timestamp() if e.get("published") else time.time()
                except Exception:
                    ts = time.time()
                if time.time() - ts > WINDOW_S:
                    continue
                summary = re.sub(r"<[^>]+>", " ", e.get("summary", ""))[:400]
                cats, calming = classify(f"{title} {summary}")
                sev = impact(f"{title} {summary}", max((SEVERITY.get(c, 0.0) for c in cats), default=0.0), calming)
                h = {
                    "id": hid, "title": title, "source": source, "url": e.get("link", ""), "ts": ts,
                    "categories": cats, "calming": calming, "severity": sev,
                    "analog": self._analog(cats),
                }
                self.headlines[hid] = h
                if sev >= 0.15 and time.time() - ts < 1800:
                    self.bus.agent_log("Analyst", f"Notizia ad alto impatto ({', '.join(cats)}): {title}", "warn", url=h["url"])
        # forget old headlines
        cutoff = time.time() - WINDOW_S
        self.headlines = {k: v for k, v in self.headlines.items() if v["ts"] >= cutoff}
        self.status = f"{ok}/{len(FEEDS)} fonti attive"

    @staticmethod
    def _analog(cats: list[str]) -> dict | None:
        """Closest historical precedent for a headline's category."""
        for c in cats:
            past = [e for e in EVENTS if e["category"] == c]
            if past:
                worst = min(past, key=lambda e: e["btc_move"])
                return {"date": worst["date"], "title": worst["title"], "btc_move": worst["btc_move"]}
        return None

    async def fetch_sentiment(self) -> None:
        try:
            r = await self._client.get("https://api.alternative.me/fng/?limit=1")
            d = r.json()["data"][0]
            self.fear_greed = {"value": int(d["value"]), "label": d["value_classification"]}
        except Exception:
            pass

    # ----------------------------------------------------------------- risk
    def _btc_shock(self) -> float:
        """Largest absolute 5-minute move among BTC / ETH / SOL (fraction)."""
        worst = 0.0
        for sym in ("PERP_BTC_USDC", "PERP_ETH_USDC", "PERP_SOL_USDC"):
            bars = self.feed.store.get(sym, "1m", 6)
            if len(bars) >= 6 and bars[0, 4] > 0:
                mv = bars[-1, 4] / bars[0, 4] - 1
                if abs(mv) > abs(worst):
                    worst = mv
        return worst

    def compute_risk(self) -> float:
        now = time.time()
        critical = None
        weighted = []
        for h in self.headlines.values():
            if h["severity"] <= 0:
                continue
            w = 0.5 ** (max(0.0, now - h["ts"]) / HALF_LIFE_S)
            weighted.append(h["severity"] * w)
            if h["severity"] >= 0.18 and now - h["ts"] < 1800 and not h["calming"]:
                critical = h
        # the three heaviest stories drive the score: many mild headlines must not add up to a crisis
        news = min(60.0, 100 * sum(sorted(weighted, reverse=True)[:3]))
        shock = self._btc_shock()
        shock_risk = min(40.0, abs(shock) / 0.03 * 40.0) if abs(shock) >= 0.01 else 0.0
        if shock > 0:
            shock_risk *= 0.5  # violent pumps also break the model, but less often against us
        senti = 0.0
        if self.fear_greed:
            v = self.fear_greed["value"]
            senti = 10.0 if v <= 20 else 5.0 if v >= 80 else 0.0
        self.components = {"notizie": news, "shock_mercato": shock_risk, "sentiment": senti, "btc_5m": shock * 100}
        self.risk = min(100.0, news + shock_risk + senti)
        self._critical = critical
        return self.risk

    async def act(self) -> None:
        risk = self.compute_risk()
        self.checker.macro_risk = risk
        shock = self.components.get("btc_5m", 0) / 100
        reason = None
        if risk >= settings.analyst_close_threshold:
            reason = f"rischio macro {risk:.0f}/100"
        elif self._critical:
            reason = f"notizia critica: {self._critical['title']}"
        elif shock <= -0.03:
            reason = f"shock di mercato: BTC/ETH/SOL {shock * 100:.1f}% in 5 minuti"
        if reason:
            self.checker.pause(1800, reason)
            if self.executor.open_positions() and time.time() - self.last_close_order > 60:
                self.last_close_order = time.time()
                self.bus.agent_log("Analyst", f"Ordino all'Executor di chiudere tutte le posizioni: {reason}", "danger")
                await self.executor.close_all(f"Analyst: {reason}")
        elif risk >= settings.analyst_pause_threshold:
            self.checker.pause(300, f"rischio macro elevato ({risk:.0f}/100)")

    async def run(self) -> None:
        last_news = 0.0
        while True:
            try:
                if time.time() - last_news > settings.news_poll_s:
                    last_news = time.time()
                    await self.fetch_news()
                    await self.fetch_sentiment()
                await self.act()
                self.bus.publish("analyst", self.view())
            except Exception:
                log.exception("analyst cycle failed")
            await asyncio.sleep(5)

    def view(self) -> dict:
        items = sorted(self.headlines.values(), key=lambda h: h["ts"], reverse=True)[:30]
        return {
            "risk": self.risk, "components": self.components, "fear_greed": self.fear_greed, "status": self.status,
            "close_threshold": settings.analyst_close_threshold, "pause_threshold": settings.analyst_pause_threshold,
            "headlines": items,
        }
