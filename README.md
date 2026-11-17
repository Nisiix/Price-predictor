# Predictor: Kronos su Raydium Perps

Dashboard in tempo reale che usa [Kronos](https://github.com/shiyu-coder/Kronos) (modello open source per candele finanziarie, licenza MIT) per prevedere il prezzo delle top 100 crypto che hanno un mercato perpetuo su **Raydium Perps** (order book di Orderly Network). Quattro agenti lavorano sopra le previsioni:

| Agente | Cosa fa |
|---|---|
| **Checker** | Per ogni token stima con Kronos la probabilità di toccare il take profit (+5% con leva) prima dello stop loss (−2%) o della liquidazione, per long e short da 10x a 20x. Aggiunge indicatori (EMA 9/21/50, RSI, MACD, Bollinger, ATR, volumi) e una ricerca dei 25 pattern storici più simili sullo stesso token e sugli altri, da cui ricava un **rischio aggiuntivo** mostrato accanto alla probabilità di Kronos. |
| **Analyst** | Legge notizie crypto e macro (CoinDesk, Cointelegraph, Decrypt, Google News) e le confronta con un archivio di eventi storici (Mt. Gox, FTX, Terra, COVID, SVB, dazi 2025, guerre…) che hanno mosso BTC e altcoin. Con shock di mercato e Fear & Greed calcola un rischio da 0 a 100: sopra 55 sospende le nuove aperture, sopra 70 (o con una notizia critica, o con BTC/ETH/SOL −3% in 5 minuti) ordina all'Executor di chiudere tutto. |
| **Executor** | Strategia a 4 ore: apre le posizioni segnalate dal Checker con probabilità Kronos a 4 ore oltre il 70%, a prescindere dal rischio (leva da 10x a 20x in base al rischio), solo quando il prezzo è vicino al minimo delle ultime 4 ore (long) o al massimo (short). Apre solo long; tra i token idonei dà precedenza a quelli con un calo oltre il 5% nelle ultime 4, 8 o 12 ore. Chiude una posizione in profitto quando la previsione Kronos a 4 ore si inverte rispetto all'apertura (scarto: la direzione opposta deve superare il 60%, con due previsioni consecutive) o su ordine dell'Analyst; le posizioni in perdita non vengono mai chiuse dagli agenti (`PP_HOLD_LOSERS=0` per cambiarlo), resta solo la liquidazione, tenuta lontana dal dimensionamento. Non c'è uno stop in perdita: dopo 2 ore una posizione in perdita viene chiusa solo se la proiezione Kronos a 4 ore dà meno del 50% di probabilità di tornare in pari (`PP_RECOVERY_HOURS`, `PP_RECOVERY_MIN_PROB`; `PP_USE_SL=1` ripristina lo stop fisso −2% con leva). Dopo un guadagno di +5% con leva viene messo uno stop a mercato a +2% di profitto. Il take profit fisso +5% è disattivato (`PP_USE_TP=1` per riattivarlo). Invia ogni risultato al Controller. |
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
| `PP_MARGIN_USDC` | 100 | margine massimo per operazione |
| `PP_LIQ_DISTANCE` | 0.5 | long: l'importo è calcolato (margine incrociato) perché la liquidazione di ogni posizione resti almeno al 50% dall'entrata; si aprono long finché la regola lo permette. Short: margine fisso se c'è collaterale libero |
| `PP_MAX_POSITIONS` | 0 | limite fisso di posizioni (0 = nessuno, decide la regola di liquidazione) |
| `PP_TP_PCT` / `PP_SL_PCT` | 5 / 2 | take profit e stop loss in % con leva |
| `PP_MIN_CONF` | 0.70 | l'Executor apre solo se la probabilità a 4 ore supera il 70%, a prescindere dal rischio |
| `PP_MIN_SCORE` / `PP_MAX_RISK` | 0 / 1 (spenti) | filtri opzionali su probabilità × (1 − rischio) e sul rischio aggiuntivo; il rischio oggi decide solo la leva |
| `PP_ENTRY_ZONE` | 0.20 | fascia di ingresso: ultimo 20% del range delle 4 ore verso il minimo (long) o il massimo (short) |
| `PP_MIN_CONF_8H` | 0.50 | la previsione a 8 ore serve solo a confermare il trend al rialzo (oltre il 50%); la soglia del 70% è sulle 4 ore |
| `PP_ENTRY_TOL` | 0.002 | ingresso quando il prezzo è entro lo 0,2% dal punto più basso previsto da Kronos nelle prossime 4 ore; prima di entrare la previsione viene ricalcolata |
| `PP_MAX_FORECAST_AGE` | 900 | età massima (s) della previsione a 4 ore per aprire |
| `PP_EXIT_MARGIN` / `PP_EXIT_CONFIRM` | 0.10 / 2 | scarto e conferme per chiudere sull'inversione della previsione |
| `PP_USE_TP` | 0 | 1 riattiva il take profit fisso +5% con leva |
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

## Limiti noti

- Registrazione del conto, chiave delegata, ordini e TP/SL su Raydium Perps sono scritti seguendo l'SDK ufficiale di Orderly ma **non sono ancora stati eseguiti contro il mercato reale**. Primo passo consigliato: un ordine manuale della dimensione minima, controllando su perps.raydium.io che TP e SL compaiano, prima di accendere il trading automatico reale.
- Con commissioni taker dello 0,06% per lato più slippage, a 20x un'operazione chiusa in take profit rende circa +2,6% del margine e una chiusa in stop loss circa −4,9%: servono più vincite che perdite per andare in pari. Il Controller mostra i numeri reali.
- La libreria dei pattern storici copre le ultime 1000 candele da 1 minuto per token (circa 16 ore).
- Posizioni simulate aperte e saldo simulato si azzerano al riavvio del backend; lo storico delle operazioni chiuse resta in `data/predictor.sqlite3`.
