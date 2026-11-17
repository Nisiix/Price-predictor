"""Market universe + real-time feed.

* Universe: top-N crypto by market cap (CoinGecko) that have an ACTIVE perp
  market on Raydium Perps (Orderly Network order book) allowing >= 10x.
* Live prices: Orderly public futures snapshot (mark price for every market
  in one call) polled every second and aggregated into 5s / 1m / 5m bars.
* History: Orderly TradingView kline endpoint (1000 bars of 1m and 5m).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass

import httpx

from ..config import TIMEFRAMES, settings
from .candles import CandleStore

log = logging.getLogger("feed")

STABLES = {
    "USDT", "USDC", "USDS", "USDE", "DAI", "USD1", "RLUSD", "USDG", "USDY", "PYUSD", "USDF", "FDUSD",
    "TUSD", "USDD", "BFUSD", "USDGO", "OUSD", "GHO", "USTB", "USD0", "EURC", "EURSAFO", "USYC", "BUIDL",
    "JAAA", "USDTB", "SUSDE", "FRAX", "LUSD", "CRVUSD", "U",
}
# Tokens that appear in the top-100 but are wrapped/pegged copies of others.
WRAPPED = {"WBTC", "WETH", "STETH", "WSTETH", "WEETH", "CBBTC", "WBT", "XAUT", "PAXG", "FIGR_HELOC"}
# CoinGecko symbol -> Orderly base when the perp is quoted per 1000 units.
THOUSANDS = {"PEPE", "BONK", "SHIB", "FLOKI", "SATS", "CAT", "X"}
FALLBACK_TOP = [
    "BTC", "ETH", "BNB", "XRP", "SOL", "TRX", "HYPE", "ZEC", "XMR", "DOGE", "LINK", "ADA", "XLM", "NEAR",
    "BCH", "LTC", "AVAX", "UNI", "SUI", "HBAR", "CRO", "SHIB", "TAO", "AAVE", "PEPE", "ENA", "ONDO",
    "WLFI", "ICP", "MNT", "PUMP", "DOT", "WLD", "ETC", "ARB", "POL", "JUP", "RENDER", "INJ", "ALGO",
    "ATOM", "ZRO", "CAKE", "FIL", "STX", "BONK", "WIF", "RAY", "OP", "APT", "TIA", "SEI",
]


@dataclass
class Market:
    symbol: str  # Orderly symbol, e.g. PERP_SOL_USDC
    base: str  # display base, e.g. SOL or 1000PEPE
    name: str
    rank: int
    max_leverage: int
    imr: float
    mmr: float
    base_tick: float
    quote_tick: float
    base_min: float
    min_notional: float
    mark_price: float = 0.0
    index_price: float = 0.0
    change_24h: float = 0.0
    volume_24h: float = 0.0
    funding_rate: float = 0.0  # estimated, per funding period
    funding_period_h: float = 8.0
    logo: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class MarketFeed:
    def __init__(self) -> None:
        self.store = CandleStore(TIMEFRAMES)
        self.markets: dict[str, Market] = {}
        self.last_tick_ts: float = 0.0
        self.status = "starting"
        self.errors = 0
        self._client = httpx.AsyncClient(timeout=15, headers={"User-Agent": "price-predictor/1.0"})
        self._history_ready: set[str] = set()

    # ---------------------------------------------------------------- helpers
    async def _get(self, url: str, **params) -> dict:
        r = await self._client.get(url, params=params or None)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _max_lev(imr: float) -> int:
        return int(round(1 / imr)) if imr > 0 else 0

    # --------------------------------------------------------------- universe
    async def build_universe(self) -> None:
        top: list[dict] = []
        try:
            top = await self._get(
                f"{settings.coingecko_api}/coins/markets",
                vs_currency="usd", order="market_cap_desc", per_page=settings.top_n, page=1,
            )
        except Exception as exc:
            log.warning("CoinGecko unavailable (%s), using built-in top list", exc)
        if top:
            ranked = [(i + 1, c["symbol"].upper(), c.get("name", ""), c.get("image", "")) for i, c in enumerate(top)]
        else:
            ranked = [(i + 1, s, s, "") for i, s in enumerate(FALLBACK_TOP)]

        info = (await self._get(f"{settings.orderly_api}/v1/public/info"))["data"]["rows"]
        by_base: dict[str, dict] = {}
        for row in info:
            sym = row["symbol"]
            parts = sym.split("_")
            if len(parts) != 3 or row.get("status") != "ACTIVE":
                continue  # broker-specific markets carry a suffix, e.g. PERP_X_USDC_mythos
            by_base[parts[1]] = row

        markets: dict[str, Market] = {}
        for rank, sym, name, logo in ranked:
            if sym in STABLES or sym in WRAPPED:
                continue
            base = f"1000{sym}" if sym in THOUSANDS and f"1000{sym}" in by_base else sym
            row = by_base.get(base)
            if not row:
                continue
            imr = float(row.get("base_imr") or 0.1)
            lev = self._max_lev(imr)
            if lev < settings.min_max_leverage:
                continue
            markets[row["symbol"]] = Market(
                symbol=row["symbol"], base=base, name=name, rank=rank, max_leverage=lev, imr=imr,
                mmr=float(row.get("base_mmr") or 0.05), base_tick=float(row.get("base_tick") or 0),
                quote_tick=float(row.get("quote_tick") or 0), base_min=float(row.get("base_min") or 0),
                min_notional=float(row.get("min_notional") or 0), funding_period_h=float(row.get("funding_period") or 8),
                logo=logo,
            )
        self.markets = markets
        log.info("Universe: %d perp markets among the top %d crypto", len(markets), settings.top_n)

    # ------------------------------------------------------------------ ticks
    async def poll_ticks(self) -> None:
        data = (await self._get(f"{settings.orderly_api}/v1/public/futures"))["data"]["rows"]
        now = time.time()
        for row in data:
            m = self.markets.get(row["symbol"])
            if not m:
                continue
            price = float(row.get("mark_price") or 0)
            m.mark_price = price
            m.index_price = float(row.get("index_price") or 0)
            o24 = float(row.get("24h_open") or 0)
            m.change_24h = (price / o24 - 1) * 100 if o24 else 0.0
            m.volume_24h = float(row.get("24h_amount") or 0)
            m.funding_rate = float(row.get("est_funding_rate") or 0)
            self.store.update_tick(m.symbol, price, now)
        self.last_tick_ts = now

    async def load_history(self, symbol: str, tf: str) -> None:
        res = {"1m": "1m", "5m": "5m"}[tf]
        now = int(time.time())
        span = TIMEFRAMES[tf] * 1000
        d = await self._get(
            f"{settings.orderly_api}/v1/tv/kline_history", symbol=symbol, resolution=res, **{"from": now - span, "to": now}
        )
        if d.get("s") != "ok":
            return
        bars = [[t, o, h, l, c, v] for t, o, h, l, c, v in zip(d["t"], d["o"], d["h"], d["l"], d["c"], d["v"])]
        self.store.set_history(symbol, tf, bars)
        self._history_ready.add(f"{symbol}:{tf}")

    def history_ready(self, symbol: str) -> bool:
        return f"{symbol}:1m" in self._history_ready and f"{symbol}:5m" in self._history_ready

    # ------------------------------------------------------------------ loops
    async def run(self) -> None:
        while True:
            try:
                await self.build_universe()
                break
            except Exception as exc:
                self.status = f"universe error: {exc}"
                log.warning("universe build failed: %s", exc)
                await asyncio.sleep(10)
        self.status = "live"
        asyncio.create_task(self._history_loop())
        asyncio.create_task(self._universe_loop())
        while True:
            t0 = time.time()
            try:
                await self.poll_ticks()
                self.status = "live"
            except Exception as exc:
                self.errors += 1
                self.status = f"tick error: {type(exc).__name__}"
            await asyncio.sleep(max(0.05, settings.tick_interval_s - (time.time() - t0)))

    async def _history_loop(self) -> None:
        """Backfill every market, then refresh closed bars (with volume) every few minutes."""
        while True:
            for sym in list(self.markets):
                for tf in ("1m", "5m"):
                    try:
                        await self.load_history(sym, tf)
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code == 429:
                            await asyncio.sleep(5)  # rate limited: back off
                    except Exception as exc:
                        log.debug("history %s %s failed: %s", sym, tf, exc)
                    await asyncio.sleep(0.7)  # stay inside Orderly's public rate limit
            await asyncio.sleep(180)

    async def _universe_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                old = self.markets
                await self.build_universe()
                for k, m in old.items():  # keep live fields
                    if k in self.markets:
                        self.markets[k].mark_price = m.mark_price
            except Exception as exc:
                log.warning("universe refresh failed: %s", exc)
