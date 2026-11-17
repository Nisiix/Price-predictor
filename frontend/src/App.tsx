import { useEffect, useMemo, useState } from "react";
import { api, pct, price, signed, useLive } from "./api";
import type { Snapshot } from "./api";
import { Chart, fmtDur } from "./components/Chart";
import { AgentsPage } from "./components/AgentsPage";
import { HORIZON_CHOICES, HorizonLadder, Positions, Watchlist, horizonName } from "./components/Panels";
import { Ticket, WalletPanel } from "./components/Ticket";
import { useRealTrading } from "./wallet/useRealTrading";

const TF_SECONDS: Record<string, number> = { "5s": 5, "1m": 60, "5m": 300, "15m": 900 };
// horizon -> [candle timeframe, steps on that timeframe] (mirrors backend/app/config.py HORIZONS)
const HORIZON_TF: Record<string, ["5s" | "1m" | "5m" | "15m", number]> = {
  "5s": ["5s", 1], "10s": ["5s", 2], "30s": ["5s", 6], "1m": ["1m", 1], "15m": ["1m", 15], "30m": ["5m", 6], "1h": ["5m", 12], "4h": ["15m", 16],
};
const TF_NAME: Record<string, string> = { "5s": "candele da 5 secondi", "1m": "candele da 1 minuto", "5m": "candele da 5 minuti", "15m": "candele da 15 minuti" };

export default function App() {
  const [focus, setFocus] = useState<string | null>(() => {
    try { return localStorage.getItem("pp.focus"); } catch { return null; }
  });
  const [horizon, setHorizon] = useState<string>(() => {
    try {
      const h = localStorage.getItem("pp.horizon");
      return h && HORIZON_CHOICES.some(([k]) => k === h) ? h : "15m";
    } catch {
      return "15m";
    }
  });
  useEffect(() => {
    try { localStorage.setItem("pp.horizon", horizon); } catch { /* ignore */ }
  }, [horizon]);
  const live = useLive(focus, horizon);
  const real = useRealTrading(live.executor, live.markets);
  const [tf, steps] = HORIZON_TF[horizon];
  const [bars, setBars] = useState<number[][]>([]);
  const [detail, setDetail] = useState<Snapshot | undefined>();
  const [toast, setToast] = useState<string | null>(null);
  const [page, setPage] = useState<"market" | "agents">(() => {
    try { return localStorage.getItem("pp.page") === "agents" ? "agents" : "market"; } catch { return "market"; }
  });
  useEffect(() => {
    try { localStorage.setItem("pp.page", page); } catch { /* ignore */ }
  }, [page]);

  // default focus: best Checker token, else SOL
  useEffect(() => {
    if (focus || !live.markets.length) return;
    const pick = live.markets.find((m) => m.base === "SOL")?.symbol ?? live.markets[0].symbol;
    setFocus(pick);
  }, [live.markets, live.ranking, focus]);
  useEffect(() => {
    if (focus) try { localStorage.setItem("pp.focus", focus); } catch { /* ignore */ }
  }, [focus]);

  // candles: full history once (up to 3000 bars), then only the newest bars merged in
  useEffect(() => {
    if (!focus) return;
    let alive = true;
    let full: number[][] = [];
    let fullLoads = 0;
    const loadFull = () => api(`/api/candles/${focus}?tf=${tf}&n=3000`).then((d) => {
      if (!alive) return;
      full = d.bars;
      fullLoads += 1;
      setBars(full);
    }).catch(() => {});
    const loadNew = () => {
      if (!full.length) return loadFull();
      const since = full[full.length - 1][0];
      return api(`/api/candles/${focus}?tf=${tf}&n=50&since=${since}`).then((d) => {
        if (!alive || !d.bars.length) return;
        const head = full.filter((b) => b[0] < d.bars[0][0]);
        full = [...head, ...d.bars];
        setBars(full);
      }).catch(() => {});
    };
    loadFull();
    const id = window.setInterval(loadNew, tf === "5s" ? 1000 : 3000);
    // older pages arrive in the background during the first minutes: re-read the full series a few times
    const deep = window.setInterval(() => { if (fullLoads < 6) loadFull(); }, 30_000);
    return () => { alive = false; window.clearInterval(id); window.clearInterval(deep); };
  }, [focus, tf]);

  // checker detail for the focused token
  useEffect(() => {
    if (!focus) return;
    let alive = true;
    const load = () => api<Snapshot>(`/api/prediction/${focus}`).then((d) => alive && setDetail(d)).catch(() => {});
    load();
    const id = window.setInterval(load, 4000);
    return () => { alive = false; window.clearInterval(id); };
  }, [focus]);

  useEffect(() => {
    if (!toast) return;
    const id = window.setTimeout(() => setToast(null), 4500);
    return () => window.clearTimeout(id);
  }, [toast]);

  const m = live.markets.find((x) => x.symbol === focus);
  const snap: Snapshot | undefined = focus ? { ...(detail ?? ({} as Snapshot)), ...(live.focus[focus] ?? {}), checker: detail?.checker } : undefined;
  // the whole sampled projection; the selected horizon is marked on it
  const fan = snap?.fans?.[tf] ?? null;
  const px = focus ? live.prices[focus]?.p ?? m?.mark_price : undefined;
  const chg = focus ? live.prices[focus]?.c ?? m?.change_24h ?? 0 : 0;

  // live candle: patch the last bar with the latest tick so the chart moves every second
  const liveBars = useMemo(() => {
    if (!bars.length || !px) return bars;
    const out = bars.slice();
    const last = out[out.length - 1].slice();
    last[2] = Math.max(last[2], px);
    last[3] = Math.min(last[3], px);
    last[4] = px;
    out[out.length - 1] = last;
    return out;
  }, [bars, px]);

  const levels = useMemo(() => {
    const ls: { y: number; label: string; color: string }[] = [];
    for (const p of live.executor?.positions ?? []) {
      if (p.symbol !== focus) continue;
      ls.push({ y: p.entry, label: `Entrata ${p.side}`, color: "var(--text)" }, { y: p.liq, label: "Liquidazione", color: "var(--danger)" });
      if (p.sl > 0) ls.push({ y: p.sl, label: p.locked ? "Stop protetto" : "SL", color: p.locked ? "var(--long)" : "var(--short)" });
      if (p.tp > 0) ls.push({ y: p.tp, label: "TP", color: "var(--long)" });
    }
    return ls;
  }, [live.executor, focus]);

  const sys = live.system;
  const feedOk = sys && Date.now() / 1000 - sys.last_tick < 5;
  const best = detail?.checker;
  const hstat = snap?.horizons?.[horizon];

  const realClose = async (p: { id: string; symbol: string }) => {
    await real.closeReal(p.symbol);
  };

  const walletChip = real.address ? (
    <button className={`walletchip ${real.ready ? "ok" : ""}`} onClick={() => setPage("market")} title={real.ready ? "Trading reale attivo con la chiave delegata" : "Wallet collegato: attiva il trading reale dal pannello Wallet"}>
      <span className={`dot ${real.ready ? "ok" : "warn"}`} />
      {real.address.slice(0, 4)}…{real.address.slice(-4)}
    </button>
  ) : (
    <button className="walletbtn" onClick={real.connect}>{real.hasPhantom ? "Collega Phantom" : "Installa Phantom"}</button>
  );

  return (
    <div className="shell">
      <header className="top">
        <div className="brand">
          <h1>Predictor</h1>
        </div>
        <div className="status" aria-live="polite">
          <span className="chip"><span className={`dot ${live.connected && feedOk ? "ok" : live.connected ? "warn" : "bad"}`} />Prezzi {live.connected ? (feedOk ? "in tempo reale" : "in ritardo") : "disconnesso"}</span>
          <span className="chip"><span className={`dot ${sys?.kronos.ready ? "ok" : sys?.kronos.error ? "bad" : "warn"}`} />
            {sys?.kronos.ready ? <>Kronos <b>{sys.kronos.model.split("/")[1]}</b>{sys.kronos.decision_model ? <> + <b title="Usato per le decisioni dell'Executor: ricalcolo prima dell'ingresso e uscite">{sys.kronos.decision_model.split("/")[1]}</b>{sys.kronos.decision_ready ? "" : " (in caricamento)"}</> : null} · {sys.kronos.scanned}/{sys.markets} token</> : sys?.kronos.error ? "Kronos non disponibile, stima statistica" : "Kronos in caricamento"}
          </span>
          <span className="chip"><span className={`dot ${(sys?.analyst.risk ?? 0) >= 70 ? "bad" : (sys?.analyst.risk ?? 0) >= 55 ? "warn" : "ok"}`} />Rischio macro <b>{(sys?.analyst.risk ?? 0).toFixed(0)}</b></span>
          <span className="chip"><span className={`dot ${live.executor?.mode === "real" ? "warn" : "ok"}`} />{live.executor?.mode === "real" ? <b>Trading reale attivo</b> : "Executor in simulazione"}</span>
        </div>
        <nav className="pages" role="tablist" aria-label="Sezioni">
          <button role="tab" aria-selected={page === "market"} onClick={() => setPage("market")}>Mercato</button>
          <button role="tab" aria-selected={page === "agents"} onClick={() => setPage("agents")}>
            Agenti{(live.executor?.positions.length ?? 0) > 0 && <span className="badge">{live.executor!.positions.length}</span>}
          </button>
        </nav>
        {walletChip}
      </header>

      {page === "market" ? (
        <main className="desk">
          <aside className="watch">
            <Watchlist markets={live.markets} ranking={live.ranking} prices={live.prices} focus={focus} onFocus={setFocus} paused={live.paused} pauseReason={live.pauseReason} board={live.boards[horizon] ?? null} horizon={horizon} />
          </aside>

          <section className="main">
            <div className="panel">
              {m ? (
                <div className="tokenhead">
                  <div>
                    <div className="sym">{m.base}</div>
                    <div className="full">{m.name} · #{m.rank} per capitalizzazione</div>
                  </div>
                  <div>
                    <div className="last">{price(px)}</div>
                    <div className={`chg ${chg >= 0 ? "long" : "short"}`}>{signed(chg, 2)}% in 24 h</div>
                  </div>
                  {hstat && (
                    <div>
                      <div className="last kronos">{price(hstat.price_q50)}</div>
                      <div className="chg muted">mediana Kronos tra {horizonName(horizon)} · {pct(hstat.p_up)} in rialzo</div>
                    </div>
                  )}
                  <div className="facts">
                    <span>Leva max <b>{m.max_leverage}x</b></span>
                    <span>Funding <b>{(m.funding_rate * 100).toFixed(4)}%</b></span>
                    {best && <span>Checker <b className={best.side}>{best.side} {best.leverage}x</b></span>}
                  </div>
                </div>
              ) : (
                <p className="empty">Caricamento dei mercati di Raydium Perps…</p>
              )}
              <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 12, flexWrap: "wrap" }}>
                <label className="hsel">
                  <span>Orizzonte</span>
                  <select value={horizon} onChange={(e) => setHorizon(e.target.value)} aria-label="Orizzonte di previsione">
                    {HORIZON_CHOICES.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
                  </select>
                </label>
                {fan ? <span className="note">Proiezione Kronos fino a {fmtDur(fan.q50.length * TF_SECONDS[tf])} su {TF_NAME[tf]}, orizzonte {horizonName(horizon)} segnato sul grafico, aggiornata {Math.round(fan.age)}s fa{fan.source !== "kronos" ? " (stima statistica)" : ""}</span>
                  : <span className="note">{tf === "5s" ? "Le candele da 5 secondi si formano dai tick live: servono circa 4 minuti dopo l'avvio." : "Previsione in calcolo per questo token."}</span>}
              </div>
              <Chart bars={liveBars} fan={fan} levels={levels} tfSeconds={TF_SECONDS[tf]} resetKey={`${focus}:${tf}`} markStep={steps} markLabel={horizonName(horizon)} />
              <div className="legend">
                <span><i style={{ background: "var(--kronos)", opacity: 0.35 }} />80% dei futuri campionati</span>
                <span><i style={{ background: "var(--kronos)", opacity: 0.6 }} />50% centrale</span>
                <span><i style={{ background: "none", borderTop: "2px dashed var(--kronos)", height: 0 }} />mediana</span>
              </div>
            </div>
            {/* Executor positions right under the chart (also on the Agenti page) */}
            <Positions ex={live.executor} onToast={setToast} realClose={realClose} />
          </section>

          <aside className="side">
            <HorizonLadder snap={snap} selected={horizon} onSelect={setHorizon} />
            {real.address ? (
              <>
                <Ticket m={m} snap={snap} ex={live.executor} real={real} onToast={setToast} />
                <WalletPanel real={real} ex={live.executor} />
              </>
            ) : (
              <div className="panel">
                <h2>Ordini</h2>
                <p className="note">
                  Senza wallet l'Executor apre e chiude da solo le posizioni simulate: le trovi sotto il grafico.
                  Per operare davvero su Raydium Perps collega Phantom in alto a destra.
                </p>
              </div>
            )}
          </aside>
        </main>
      ) : (
        <AgentsPage live={live} focus={focus} onFocus={setFocus} snap={snap} onToast={setToast} realClose={realClose} />
      )}
      {toast && <div className="toast" role="status">{toast}</div>}
    </div>
  );
}
