# Predictor: Kronos su Raydium Perps

Dashboard in tempo reale che usa [Kronos](https://github.com/shiyu-coder/Kronos) (modello open source per candele finanziarie, licenza MIT) per prevedere il prezzo delle top 100 crypto che hanno un mercato perpetuo su **Raydium Perps** (order book di Orderly Network). Quattro agenti lavorano sopra le previsioni:

| Agente | Cosa fa |
|---|---|
| **Checker** | Per ogni token legge la previsione Kronos a 4 ore (candele da 15 minuti) contro il prezzo live. Aggiunge indicatori (EMA 9/21/50, RSI, MACD, Bollinger, ATR) e i 25 pattern storici più simili sullo stesso token e sugli altri, da cui ricava un **rischio aggiuntivo** che decide leva e margine. Calcola il punto d'ingresso previsto (la mediana dei minimi dei percorsi campionati entro 4 ore) e il filtro di mercato su BTC. Registra ogni nuova previsione nel registro dei segnali del Controller. |
| **Analyst** | Legge notizie crypto e macro (CoinDesk, Cointelegraph, Decrypt, Google News) e le confronta con un archivio di eventi storici che hanno mosso BTC e altcoin. Con shock di mercato e Fear & Greed calcola un rischio da 0 a 100: sopra 55 sospende le nuove aperture, sopra 70 (o con una notizia critica, o con BTC/ETH/SOL −3% in 5 minuti) ordina all'Executor di chiudere tutto; per regola le posizioni in perdita restano comunque aperte. |
| **Executor** | Strategia a 4 ore, solo long: apre quando la probabilità di rialzo a 4 ore supera il 70%, la mediana prevista supera il prezzo più i costi, BTC non è in calo a 4 e 24 ore e il prezzo arriva al punto d'ingresso previsto. Prima di entrare **ricalcola la previsione con 96 campioni e decide su quella** (se vede un ulteriore ribasso aspetta il nuovo punto). Tra i token idonei dà precedenza a quelli scesi oltre il 5% in 4, 8 o 12 ore. Chiude una posizione in profitto quando la previsione a 4 ore si inverte (direzione opposta oltre il 60% in due previsioni consecutive) o su ordine dell'Analyst. **Le posizioni in perdita non vengono mai chiuse** (`PP_HOLD_LOSERS=0` per cambiarlo) e non c'è stop in perdita: resta solo la liquidazione, tenuta ad almeno il 50% dall'entrata. Lo **stop di protezione** va dove la chiusura lascia +2% **netto** sul margine (commissioni, slittamento e funding pagati) e si attiva quando il prezzo è 1 ATR a 15 minuti sopra quel livello. Invia ogni risultato al Controller. |
| **Controller** | Salva lo storico in SQLite, calcola win rate e PnL per token, pattern, fascia di probabilità, fascia di rischio, leva e motivo di chiusura, mostra le posizioni aperte con il risultato non realizzato, verifica a scadenza ogni previsione (direzione corretta, Brier, copertura della fascia 10-90%) e tiene il **registro dei segnali a 4 ore** per fascia di probabilità: tutte le previsioni, quelle al punto d'ingresso e quelle idonee. Report scaricabile in Markdown. |

## Simulazione e trading reale

- **Senza wallet** l'Executor opera in simulazione sui prezzi reali di mercato, con commissioni taker, slippage e funding. Nessun ordine viene inviato. Saldo, posizioni e ordini simulati restano salvati in `data/paper_state.json` anche dopo un riavvio.
- **Con Phantom collegato** premi "Attiva trading reale": firmi in Phantom la registrazione del conto Raydium Perps (solo la prima volta) e una **chiave di trading delegata** valida 30 giorni. La chiave può aprire e chiudere posizioni ma **non può prelevare fondi** e resta solo nel tuo browser: il backend non la vede mai.
- Il collaterale USDC si deposita su [perps.raydium.io](https://perps.raydium.io) con lo stesso wallet: il conto è lo stesso.
- Per regola non viene messo nessuno stop loss sul mercato. Lo stop di protezione in profitto viene inviato al mercato quando si attiva. Aperture e chiusure automatiche reali partono dalla pagina, che deve restare aperta: a dashboard chiusa resta solo la liquidazione.

> Il trading a leva comporta il rischio di perdere l'intero margine, e con le posizioni in perdita mai chiuse anche di più del margine di una singola posizione. Le probabilità sono stime di un modello, non garanzie: controlla il registro dei segnali e il backtest prima di usare soldi veri.

## Orizzonti di previsione

Una sola previsione Kronos-base per token, su candele da 15 minuti, 18 passi (4 ore più due passi di margine per leggere esattamente "4 ore da adesso" anche su una previsione di mezz'ora fa):

| Orizzonte | Passo letto | Grafico |
|---|---|---|
| 15 minuti | 1 | candele da 15 minuti |
| 1 ora | 4 | candele da 1 ora, costruite dalle 15 minuti |
| 4 ore | 16 | candele da 4 ore, costruite dalle 15 minuti |

Ogni previsione nasce da 24 futuri campionati (96 nella conferma prima di un ingresso): la percentuale è la quota di campioni sopra il prezzo attuale, il cono sul grafico mostra il 50% e l'80% centrali e parte dal momento e dal prezzo in cui la previsione è stata calcolata. Kronos riceve aperture, massimi, minimi, chiusure, volume e importo delle candele della borsa; se l'ultima candela chiusa è ancora quella costruita dai tick, prima della previsione viene riletta dalla borsa.

## Avvio

Requisiti: Python 3.11+, Node 20+.

```bash
python -m venv .venv
.venv\Scripts\pip install -r backend/requirements.txt
cd frontend && npm install && npm run build && cd ..
cd backend && ..\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Apri http://127.0.0.1:8000. Al primo avvio vengono scaricati da Hugging Face i pesi di Kronos-base.

Per sviluppare il frontend con ricarica automatica: `cd frontend && npm run dev` e apri http://127.0.0.1:5173 (le chiamate vanno al backend sulla porta 8000).

Test: `cd backend && ..\.venv\Scripts\python -m pytest -q`.

## Backtest delle regole a 4 ore

```bash
cd backend && ..\.venv\Scripts\python -m tools.backtest_4h
```

Scarica lo storico da 15 minuti da Orderly, esegue Kronos a ritroso e scrive in `data/backtest/` un rapporto con la calibrazione della probabilità a 4 ore, le operazioni con le regole dell'Executor e le stesse uscite dopo ingressi casuali. Default: Kronos-base, primi 10 token, 30 giorni, una previsione ogni 4 ore per token (`--days`, `--top`, `--every`, `--model`, `--lev`; `--help` per tutto). Con Kronos-base su CPU richiede molte ore e usa la stessa CPU del server: conviene lanciarlo a server fermo. Le previsioni restano in cache, quindi un'interruzione non fa perdere il lavoro fatto. Il punteggio di rischio del Checker non è replicato: ogni operazione usa una sola leva.

## Configurazione

Tutto è modificabile con variabili d'ambiente (vedi `backend/app/config.py`):

| Variabile | Default | Significato |
|---|---|---|
| `PP_BASE_TOKENS` | 0 (automatico) | token analizzati con Kronos-base, per capitalizzazione; 0 = quanti ne entrano in un ciclo di scansione al tempo misurato per previsione, meno le posizioni aperte (minimo `PP_BASE_TOKENS_MIN`, 6) |
| `PP_BASE_LOOKBACK` | 128 | candele da 15 minuti di contesto date a Kronos-base (32 ore) |
| `PP_SCAN_AGE` / `PP_POSITION_REFRESH` | 300 / 300 | ogni quanti secondi un token e una posizione aperta vengono riprevisti |
| `PP_SAMPLES` / `PP_DECISION_SAMPLES` | 24 / 96 | futuri campionati per previsione: scansione e conferma prima dell'ingresso |
| `PP_DECISION_MODEL` | vuoto | modello diverso per la conferma; vuoto = lo stesso Kronos-base |
| (fasce di rischio) | 25% → 10%, 30% → 7%, 35% → 5%, 40% → 3% | margine per operazione come quota del capitale secondo il rischio aggiuntivo; oltre il 40% l'Executor non apre (`PP_RISK_ABOVE_40`, `PP_MAX_RISK`) |
| `PP_MAX_TOKEN_EXPOSURE` / `PP_MAX_TOTAL_EXPOSURE` | 0.5 / 1.5 | esposizione massima (nozionale sul capitale) per token e in totale: la leva riduce il margine bloccato, non moltiplica l'esposizione |
| `PP_LIQ_DISTANCE` | 0.5 | long: l'importo è calcolato (margine incrociato) perché la liquidazione di ogni posizione resti almeno al 50% dall'entrata |
| `PP_MAX_POSITIONS` | 0 | limite fisso di posizioni (0 = nessuno) |
| `PP_MIN_CONF` | 0.70 | probabilità a 4 ore minima per aprire, sulla previsione di scansione e su quella di conferma |
| `PP_ENTRY_TOL` / `PP_DIP_QUANTILE` | 0.002 / 0.5 | ingresso entro lo 0,2% dal punto previsto, che è il quantile 50% dei minimi dei percorsi entro 4 ore |
| `PP_BTC_FILTER_4H` / `PP_BTC_FILTER_24H` | 0 / 0 | niente long se BTC è sceso più di questa quota in 4 / 24 ore (0 = qualsiasi calo blocca; per esempio 0.005 lascia passare cali fino allo 0,5%) |
| `PP_MAX_FORECAST_AGE` | 2700 | età massima (s) della previsione di scansione per avviare la conferma |
| `PP_LOCK_PROFIT` / `PP_LOCK_ATR` | 2 / 1 | stop di protezione: profitto netto sul margine (%) e distanza in ATR a 15 minuti che lo attiva |
| `PP_EXIT_MARGIN` / `PP_EXIT_CONFIRM` | 0.10 / 2 | scarto e conferme per chiudere sull'inversione della previsione |
| `PP_HOLD_LOSERS` | 1 | 1 = le posizioni in perdita non vengono mai chiuse dagli agenti |
| `PP_TP_PCT` / `PP_SL_PCT`, `PP_USE_TP`, `PP_USE_SL` | 5 / 2, 0, 0 | take profit e stop loss fissi in % con leva, disattivati |
| `PP_ANALYST_PAUSE` / `PP_ANALYST_CLOSE` | 55 / 70 | soglie di rischio dell'Analyst |
| `PP_TAKER_FEE` / `PP_SLIPPAGE` | 0.0006 / 0.0002 | costi usati nella simulazione e nel backtest |
| `PP_TOP_P` | 0.9 | campionamento di Kronos; se la copertura 10-90% nel report resta sotto l'80%, il cono è troppo stretto e conviene alzarlo |

## Struttura

```
backend/app/
  market/feed.py        universo (CoinGecko top 100 ∩ Raydium Perps), prezzi ogni secondo, storico candele
  predict/engine.py     Kronos con tutti i percorsi campionati (kronos_model/ è il codice originale, MIT)
  predict/probability.py  probabilità per orizzonte e TP/SL/liquidazione
  predict/service.py    pianificazione delle inferenze e verifica a scadenza
backend/tools/backtest_4h.py  backtest delle regole a 4 ore
  agents/               checker, indicators, analyst, history_events, executor, controller
  main.py               API REST + WebSocket
frontend/src/
  App.tsx, components/  dashboard
  wallet/               Phantom, client Raydium Perps (Orderly) con chiave delegata
```

## Limiti noti

- Registrazione del conto, chiave delegata e ordini su Raydium Perps sono scritti seguendo l'SDK ufficiale di Orderly ma **non sono ancora stati eseguiti contro il mercato reale**. Primo passo consigliato: un ordine manuale della dimensione minima prima di accendere il trading automatico reale.
- Con commissioni taker dello 0,06% per lato più slippage, un'operazione costa lo 0,16% del nozionale andata e ritorno: il 3,2% del margine a 20x.
- Il punteggio di rischio del Checker e le regole a 4 ore non sono ancora stati verificati su dati storici: il registro dei segnali e il backtest servono a questo.
- La libreria dei pattern storici copre le candele da 15 minuti in memoria (fino a circa un mese).
