# Predictor: Kronos su Raydium Perps

Dashboard in tempo reale che usa [Kronos](https://github.com/shiyu-coder/Kronos) (modello open source per candele finanziarie, licenza MIT) per prevedere il prezzo delle top 100 crypto che hanno un mercato perpetuo su **Raydium Perps** (order book di Orderly Network). Quattro agenti lavorano sopra le previsioni:

| Agente | Cosa fa |
|---|---|
| **Checker** | Per ogni token legge la previsione Kronos a 4 ore (candele da 15 minuti) contro il prezzo live: l'**ampiezza prevista** (la media del movimento a 4 ore dei percorsi campionati, in un verso o nell'altro) e la probabilità di rialzo. Calcola il **tetto**, cioè il massimo delle ultime 4 candele da 15 minuti chiuse, e segnala la **rottura** quando il prezzo lo supera. Aggiunge indicatori (EMA 9/21/50, RSI, MACD, Bollinger, ATR) e i 25 pattern storici più simili sullo stesso token e sugli altri, da cui ricava un **rischio aggiuntivo** che decide leva e margine, Registra ogni nuova previsione e la sua prima rottura nel registro dei segnali del Controller. |
| **Analyst** | Legge notizie crypto e macro (CoinDesk, Cointelegraph, Decrypt, Google News) e le confronta con un archivio di eventi storici che hanno mosso BTC e altcoin. Con shock di mercato e Fear & Greed calcola un rischio da 0 a 100 che entra nel rischio aggiuntivo (fascia di margine); non sospende più le aperture (`PP_ANALYST_BLOCKS=1` per farlo sopra 55); sopra 70 (o con una notizia critica, o con BTC/ETH/SOL −3% in 5 minuti) ordina all'Executor di chiudere tutto; per regola le posizioni in perdita restano comunque aperte. |
| **Executor** | Strategia a 4 ore, solo long, **ingresso in rottura**: apre quando Kronos prevede un movimento ampio (ampiezza almeno 1,5%), non è contrario al rialzo (oltre il 50%) e il prezzo supera il tetto dell'ultima ora dello 0,1%, cioè il movimento previsto è cominciato verso l'alto. Se il prezzo scende non apre nulla; oltre l'1% sopra il tetto è troppo tardi e aspetta un nuovo tetto. Prima di entrare **ricalcola la previsione con 96 campioni e decide su quella**. Tra i token idonei ha la precedenza il movimento previsto più ampio. Chiude una posizione quando la previsione a 4 ore si inverte (direzione opposta oltre il 60% in due previsioni consecutive), con l'**uscita in pari netto** (appena la chiusura non è in perdita, commissioni e funding compresi, se la previsione è al ribasso a 15 minuti, 1 ora e 4 ore) o su ordine dell'Analyst, mai in perdita. **Le posizioni in perdita non vengono mai chiuse** (`PP_HOLD_LOSERS=0` per cambiarlo) e non c'è stop in perdita: resta solo la liquidazione, tenuta ad almeno il 50% dall'entrata. Lo **stop di protezione** va dove la chiusura lascia +2% **netto** sul margine (commissioni, slittamento e funding pagati) e si attiva quando il prezzo è 1 ATR a 15 minuti sopra quel livello. Invia ogni risultato al Controller. |
| **Controller** | Salva lo storico in SQLite, calcola win rate e PnL per token, pattern, fascia di probabilità, fascia di rischio, leva e motivo di chiusura, mostra le posizioni aperte con il risultato non realizzato, verifica a scadenza ogni previsione (direzione corretta, Brier, copertura della fascia 10-90%) e tiene il **registro dei segnali a 4 ore**: ampiezza prevista contro movimento reale, e il ritorno a 4 ore di tutte le previsioni, di tutte le rotture del tetto e di quelle idonee. Report scaricabile in Markdown. |

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

## Backtest dell'ingresso in rottura

```bash
cd backend && ..\.venv\Scripts\python -m tools.backtest_4h
```

Scarica lo storico da 15 minuti da Orderly, esegue Kronos a ritroso e scrive in `data/backtest/` un rapporto con: quanto l'ampiezza prevista da Kronos anticipa il movimento reale (contro la stima ingenua "quanto si è mosso nelle 4 ore prima"), la calibrazione della probabilità a 4 ore, le operazioni con le regole dell'Executor, gli stessi ingressi senza l'uscita in pari, tutte le rotture senza filtri Kronos e ingressi casuali, sempre con le stesse uscite. Default: Kronos-base, primi 10 token, 30 giorni, una previsione ogni 2 ore per token (`--days`, `--top`, `--every`, `--model`, `--lev`; `--help` per tutto). Con Kronos-base su CPU richiede molte ore e usa la stessa CPU del server: conviene lanciarlo a server fermo. Le previsioni restano in cache, quindi un'interruzione non fa perdere il lavoro fatto. Il punteggio di rischio del Checker non è replicato (ogni operazione usa una sola leva) e le uscite usano le previsioni programmate invece di una ogni 5 minuti.

## Configurazione

Tutto è modificabile con variabili d'ambiente (vedi `backend/app/config.py`):

| Variabile | Default | Significato |
|---|---|---|
| `PP_BASE_TOKENS` | 0 (automatico) | token analizzati con Kronos-base, per capitalizzazione; 0 = quanti ne entrano in un ciclo di scansione al tempo misurato per previsione, meno le posizioni aperte (minimo `PP_BASE_TOKENS_MIN`, 6) |
| `PP_BASE_LOOKBACK` | 128 | candele da 15 minuti di contesto date a Kronos-base (32 ore) |
| `PP_SCAN_AGE` / `PP_POSITION_REFRESH` | 300 / 300 | ogni quanti secondi un token e una posizione aperta vengono riprevisti |
| `PP_SAMPLES` / `PP_DECISION_SAMPLES` | 24 / 96 | futuri campionati per previsione: scansione e conferma prima dell'ingresso |
| `PP_DECISION_MODEL` | vuoto | modello diverso per la conferma; vuoto = lo stesso Kronos-base |
| (fasce di rischio) | 25% → 10%, 30% → 7%, 35% → 5%, 40% → 3% | margine per operazione come quota del capitale secondo il rischio aggiuntivo; oltre il 40% il 3%: il rischio non blocca mai un'apertura (`PP_RISK_ABOVE_40`; `PP_MAX_RISK=0.40` ripristina il blocco) |
| `PP_RISK_CONF_FLOOR` | 0.70 | tra il 50% e il 70% di probabilità Kronos pesa sul rischio aggiuntivo come al 70%: un ingresso in rottura al 55% non viene bloccato né ridotto per la probabilità; sopra il 70% una probabilità più alta abbassa ancora il rischio |
| `PP_RISK_VOLATILITY` | 0 | 1 = la volatilità a 15 minuti torna nel rischio aggiuntivo; era misurata contro lo stop a −2% che non esiste più e penalizzava proprio i token con movimento previsto ampio |
| `PP_MAX_TOKEN_EXPOSURE` / `PP_MAX_TOTAL_EXPOSURE` | 0.5 / 1.5 | esposizione massima (nozionale sul capitale) per token e in totale: la leva riduce il margine bloccato, non moltiplica l'esposizione |
| `PP_LIQ_DISTANCE` | 0.5 | long: l'importo è calcolato (margine incrociato) perché la liquidazione di ogni posizione resti almeno al 50% dall'entrata |
| `PP_MAX_POSITIONS` | 0 | limite fisso di posizioni (0 = nessuno) |
| `PP_MIN_CONF` | 0.50 | probabilità di rialzo a 4 ore che Kronos deve superare (non contrario al long), sulla previsione di scansione e su quella di conferma |
| `PP_MIN_MOVE` | 0.015 | ampiezza prevista minima: media del movimento a 4 ore dei percorsi, in valore assoluto, dal prezzo della previsione |
| `PP_ROOF_BARS` / `PP_BREAKOUT_BUFFER` / `PP_BREAKOUT_MAX_CHASE` | 4 / 0.001 / 0.01 | tetto = massimo delle ultime 4 candele da 15 minuti chiuse; ingresso sopra il tetto + 0,1%, non oltre il tetto + 1% |
| `PP_VOL5_FILTER` / `PP_VOL5_TOP` / `PP_VOL5_TRIGGER` / `PP_VOL5_WINDOW` | 1 / 0.10 / 0.5 / 60 | condizione in più per l'ingresso (scelta del 18 novembre): la volatilità a 5 minuti del token (deviazione dei rendimenti a 1 minuto degli ultimi 60 minuti) deve essere nel suo 10% più alto, e negli ultimi 5 minuti il prezzo deve aver percorso almeno metà del movimento atteso a 5 minuti (0 la disattiva) |
| `PP_BE_EXIT` / `PP_BE_EXIT_MAX_UP` | 1 / 0.5 | uscita in pari netto quando la previsione dà il rialzo sotto il 50% a 15 minuti, 1 ora e 4 ore (0 la disattiva) |
| `PP_BTC_FILTER` | 0 | filtro BTC tolto; 1 = niente long se BTC è sceso a 4 o 24 ore oltre `PP_BTC_FILTER_4H` / `PP_BTC_FILTER_24H` (0 / 0) |
| `PP_ANALYST_BLOCKS` | 0 | la pausa dell'Analyst non blocca le aperture (il suo rischio macro abbassa solo la fascia di margine); 1 la ripristina |
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
backend/tools/backtest_4h.py  backtest dell'ingresso in rottura e delle uscite
  agents/               checker, indicators, analyst, history_events, executor, controller
  main.py               API REST + WebSocket
frontend/src/
  App.tsx, components/  dashboard
  wallet/               Phantom, client Raydium Perps (Orderly) con chiave delegata
```

## Limiti noti

- Registrazione del conto, chiave delegata e ordini su Raydium Perps sono scritti seguendo l'SDK ufficiale di Orderly ma **non sono ancora stati eseguiti contro il mercato reale**. Primo passo consigliato: un ordine manuale della dimensione minima prima di accendere il trading automatico reale.
- Con commissioni taker dello 0,06% per lato più slippage, un'operazione costa lo 0,16% del nozionale andata e ritorno: il 3,2% del margine a 20x.
- L'ingresso in rottura, l'uscita in pari, il punteggio di rischio del Checker e le altre regole a 4 ore non sono ancora stati verificati su dati storici: il registro dei segnali e il backtest servono a questo.
- La libreria dei pattern storici copre le candele da 15 minuti in memoria (fino a circa un mese).
