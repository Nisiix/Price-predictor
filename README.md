# Predictor: Kronos su Raydium Perps

Dashboard in tempo reale che usa [Kronos](https://github.com/shiyu-coder/Kronos) (modello open source per candele finanziarie, licenza MIT) per prevedere il prezzo delle top 100 crypto che hanno un mercato perpetuo su **Raydium Perps** (order book di Orderly Network). Quattro agenti lavorano sopra le previsioni:

| Agente | Cosa fa |
|---|---|
| **Checker** | Per ogni token stima con Kronos la probabilità di toccare il take profit (+5% con leva) prima dello stop loss (−2%) o della liquidazione, per long e short da 10x a 20x. Aggiunge indicatori (EMA 9/21/50, RSI, MACD, Bollinger, ATR, volumi) e una ricerca dei 25 pattern storici più simili sullo stesso token e sugli altri, da cui ricava un **rischio aggiuntivo** mostrato accanto alla probabilità di Kronos. |
| **Analyst** | Legge notizie crypto e macro (CoinDesk, Cointelegraph, Decrypt, Google News) e le confronta con un archivio di eventi storici (Mt. Gox, FTX, Terra, COVID, SVB, dazi 2025, guerre…) che hanno mosso BTC e altcoin. Con shock di mercato e Fear & Greed calcola un rischio da 0 a 100: sopra 55 sospende le nuove aperture, sopra 70 (o con una notizia critica, o con BTC/ETH/SOL −3% in 5 minuti) ordina all'Executor di chiudere tutto. |
| **Executor** | Apre le posizioni segnalate dal Checker (100 USDC di margine, massimo 2 aperte) e le chiude per liquidazione, take profit +5% con leva, stop loss −2% con leva o ordine dell'Analyst. Invia ogni risultato al Controller. |
| **Controller** | Salva lo storico in SQLite, calcola win rate e PnL per token, pattern, fascia di probabilità, rischio, leva e motivo di chiusura, e verifica a scadenza ogni previsione di Kronos (quota di direzioni corrette per orizzonte). Report scaricabile in Markdown. |

## Simulazione e trading reale

- **Senza wallet** l'Executor opera in simulazione sui prezzi reali di mercato, con commissioni taker, slippage e funding. Nessun ordine viene inviato.
- **Con Phantom collegato** premi "Attiva trading reale": firmi in Phantom la registrazione del conto Raydium Perps (solo la prima volta) e una **chiave di trading delegata** valida 30 giorni. La chiave può aprire e chiudere posizioni ma **non può prelevare fondi** e resta solo nel tuo browser: il backend non la vede mai. Da quel momento il pannello ordini invia ordini reali (a mercato o limite, con TP/SL) e puoi accendere il trading automatico reale dell'Executor.
- Il collaterale USDC si deposita su [perps.raydium.io](https://perps.raydium.io) con lo stesso wallet: il conto è lo stesso.
- TP e SL vengono piazzati anche sul mercato come ordini di posizione, quindi restano attivi anche se la dashboard è chiusa. L'apertura automatica reale invece parte dalla pagina, che deve restare aperta.

> Il trading a leva comporta il rischio di perdere l'intero margine. Le probabilità sono stime di un modello, non garanzie: controlla nel pannello del Controller quanto spesso Kronos ha avuto ragione prima di usare soldi veri.

## Orizzonti di previsione

| Orizzonte | Candele usate | Note |
|---|---|---|
| 10 s, 30 s | 5 secondi, costruite dai tick live | Sperimentale: Kronos è addestrato su candele da 1 minuto in su. Servono circa 4 minuti dall'avvio. |
| 1, 5, 15 min | 1 minuto | Usato anche dal Checker (percorsi di 30 minuti). |
| 30 min, 1 h | 5 minuti | |

Ogni previsione nasce da 24 futuri campionati da Kronos: la percentuale è la quota di campioni sopra il prezzo attuale, il cono sul grafico mostra il 50% e l'80% centrali.

## Avvio

Requisiti: Python 3.11+, Node 20+.

```bash
python -m venv .venv
.venv\Scripts\pip install -r backend/requirements.txt
cd frontend && npm install && npm run build && cd ..
cd backend && ..\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Apri http://127.0.0.1:8000. Al primo avvio vengono scaricati da Hugging Face i pesi di Kronos-mini (circa 20 MB).

Per sviluppare il frontend con ricarica automatica: `cd frontend && npm run dev` e apri http://127.0.0.1:5173 (le chiamate vanno al backend sulla porta 8000).

Test: `cd backend && ..\.venv\Scripts\python -m pytest -q`.

## Configurazione

Tutto è modificabile con variabili d'ambiente (vedi `backend/app/config.py`):

| Variabile | Default | Significato |
|---|---|---|
| `PP_KRONOS_MODEL` / `PP_KRONOS_TOKENIZER` | `NeoQuasar/Kronos-mini` / `NeoQuasar/Kronos-Tokenizer-2k` | `NeoQuasar/Kronos-small` con `Kronos-Tokenizer-base` è più accurato ma circa 3,5 volte più lento su CPU |
| `PP_SAMPLES` | 24 | futuri campionati per previsione |
| `PP_LOOKBACK` | 128 | candele passate date al modello |
| `PP_MARGIN_USDC` | 100 | margine per operazione |
| `PP_MAX_POSITIONS` | 2 | posizioni aperte al massimo |
| `PP_TP_PCT` / `PP_SL_PCT` | 5 / 2 | take profit e stop loss in % con leva |
| `PP_MIN_SCORE` | 0.40 | punteggio minimo del Checker per un segnale |
| `PP_ANALYST_PAUSE` / `PP_ANALYST_CLOSE` | 55 / 70 | soglie di rischio dell'Analyst |
| `PP_TAKER_FEE` / `PP_SLIPPAGE` | 0.0006 / 0.0002 | costi usati nella simulazione |

## Struttura

```
backend/app/
  market/feed.py        universo (CoinGecko top 100 ∩ Raydium Perps), prezzi ogni secondo, storico candele
  predict/engine.py     Kronos con tutti i percorsi campionati (kronos_model/ è il codice originale, MIT)
  predict/probability.py  probabilità per orizzonte e TP/SL/liquidazione
  predict/service.py    pianificazione delle inferenze e verifica a scadenza
  agents/               checker, indicators, analyst, history_events, executor, controller
  main.py               API REST + WebSocket
frontend/src/
  App.tsx, components/  dashboard
  wallet/               Phantom, client Raydium Perps (Orderly) con chiave delegata
```
