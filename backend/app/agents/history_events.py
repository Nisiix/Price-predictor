"""Historical market-moving events used by the Analyst to weight live news.

Moves are approximate peak-to-trough BTC changes in the days after the event
(rounded, from public price history); `alt_mult` is how much harder altcoins
were typically hit. They calibrate how dangerous a *category* of headline is,
not to predict exact moves.
"""
from __future__ import annotations

EVENTS = [
    {"date": "2014-02-24", "title": "Mt. Gox sospende i prelievi e fallisce", "category": "exchange_collapse", "btc_move": -0.36, "alt_mult": 1.3},
    {"date": "2016-08-02", "title": "Hack di Bitfinex (120k BTC)", "category": "hack", "btc_move": -0.20, "alt_mult": 1.4},
    {"date": "2017-09-04", "title": "La Cina vieta le ICO e chiude gli exchange", "category": "regulation_ban", "btc_move": -0.30, "alt_mult": 1.5},
    {"date": "2020-03-12", "title": "COVID: Black Thursday, crollo dei mercati globali", "category": "macro_crash", "btc_move": -0.50, "alt_mult": 1.3},
    {"date": "2021-05-19", "title": "La Cina vieta servizi crypto alle istituzioni finanziarie e il mining", "category": "regulation_ban", "btc_move": -0.30, "alt_mult": 1.6},
    {"date": "2022-02-24", "title": "La Russia invade l'Ucraina", "category": "war", "btc_move": -0.08, "alt_mult": 1.3},
    {"date": "2022-05-09", "title": "Collasso di Terra/LUNA e depeg di UST", "category": "stablecoin_depeg", "btc_move": -0.25, "alt_mult": 1.8},
    {"date": "2022-06-13", "title": "Celsius blocca i prelievi, crisi 3AC", "category": "lender_insolvency", "btc_move": -0.37, "alt_mult": 1.4},
    {"date": "2022-06-15", "title": "Fed: rialzo dei tassi di 75 punti base", "category": "fed_hawkish", "btc_move": -0.07, "alt_mult": 1.3},
    {"date": "2022-11-08", "title": "Collasso di FTX (SOL -50%)", "category": "exchange_collapse", "btc_move": -0.25, "alt_mult": 2.2},
    {"date": "2023-03-10", "title": "Fallimento di Silicon Valley Bank, depeg di USDC", "category": "banking_crisis", "btc_move": -0.10, "alt_mult": 1.4},
    {"date": "2023-06-05", "title": "La SEC fa causa a Binance e Coinbase", "category": "regulation_enforcement", "btc_move": -0.06, "alt_mult": 2.5},
    {"date": "2024-01-11", "title": "Approvazione ETF spot su BTC (sell the news)", "category": "etf", "btc_move": -0.15, "alt_mult": 1.3},
    {"date": "2024-04-13", "title": "L'Iran attacca Israele con droni e missili", "category": "war", "btc_move": -0.08, "alt_mult": 1.6},
    {"date": "2024-08-05", "title": "Unwind del carry trade sullo yen, crollo del Nikkei", "category": "macro_crash", "btc_move": -0.15, "alt_mult": 1.4},
    {"date": "2025-02-21", "title": "Hack di Bybit (1,5 mld $ in ETH)", "category": "hack", "btc_move": -0.06, "alt_mult": 1.8},
    {"date": "2025-04-02", "title": "Dazi USA del 'Liberation Day'", "category": "trade_war", "btc_move": -0.10, "alt_mult": 1.6},
    {"date": "2025-10-10", "title": "Minaccia di dazi al 100% sulla Cina: liquidazioni record", "category": "trade_war", "btc_move": -0.14, "alt_mult": 2.3},
]

# Category -> keywords (lower-case, matched as substrings on headline + summary).
CATEGORIES = {
    "war": ["war ", "invasion", "invade", "missile", "airstrike", "military strike", "attack on", "nuclear", "troops", "guerra", "attacco"],
    "macro_crash": ["market crash", "stocks plunge", "selloff", "sell-off", "recession", "circuit breaker", "black monday", "carry trade", "crollo"],
    "banking_crisis": ["bank collapse", "bank failure", "bank run", "fdic", "bailout", "insolvent bank", "crisi bancaria", "banking crisis"],
    "fed_hawkish": ["rate hike", "hawkish", "raises rates", "higher for longer", "inflation surge", "hot cpi", "rialzo dei tassi"],
    "trade_war": ["tariff", "trade war", "export ban", "sanction", "dazi"],
    "regulation_ban": ["ban crypto", "crypto ban", "bans bitcoin", "outlaw", "prohibit", "vieta", "divieto"],
    "regulation_enforcement": ["sec sues", "lawsuit", "charged", "enforcement", "crackdown", "indictment", "subpoena", "cftc", "doj", "arrest"],
    "exchange_collapse": ["halts withdrawals", "suspends withdrawals", "bankruptcy", "insolvency", "chapter 11", "exchange collapse", "freezes withdrawals"],
    "hack": ["hack", "exploit", "drained", "stolen", "breach", "attacker"],
    "stablecoin_depeg": ["depeg", "de-peg", "loses peg", "lost its peg", "stablecoin collapse"],
    "lender_insolvency": ["liquidity crisis", "insolvent", "margin call", "contagion"],
    "etf": ["etf outflow", "etf rejected", "etf denied", "rejects etf", "etf delay"],
}
# Words that make a headline about a category positive / calming instead.
CALMING = ["ceasefire", "rate cut", "cuts rates", "approved", "approval", "dovish", "deal reached", "truce", "recovers", "rebound", "surge", "rally", "tregua"]


def category_severity() -> dict[str, float]:
    """Average |BTC move| x altcoin multiplier per category (0..1)."""
    acc: dict[str, list[float]] = {}
    for e in EVENTS:
        acc.setdefault(e["category"], []).append(abs(e["btc_move"]) * e["alt_mult"])
    return {k: min(1.0, sum(v) / len(v)) for k, v in acc.items()}


SEVERITY = category_severity()
