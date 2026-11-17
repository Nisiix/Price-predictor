import { useEffect, useMemo, useState } from "react";
import { api, pct, price, signed, useLive } from "./api";
import type { Snapshot } from "./api";
import { Chart, fmtDur } from "./components/Chart";
import { ActivityLog, AnalystPanel, ControllerPanel, HorizonLadder, Positions, Verdict, Watchlist } from "./components/Panels";
import { Ticket, WalletPanel } from "./components/Ticket";
import { useRealTrading } from "./wallet/useRealTrading";

const TF_SECONDS: Record<string, number> = { "5s": 5, "1m": 60, "5m": 300 };
const TF_LABEL: Record<string, string> = { "5s": "5 s · 10s-30s", "1m": "1 min · 1-15 min", "5m": "5 min · 30 min-1 h" };

export default function App() {
  const [focus, setFocus] = useState<string | null>(() => {
    try { return localStorage.getItem("pp.focus"); } catch { return null; }
  });
  const live = useLive(focus);
  const real = useRealTrading(live.executor, live.markets);
  const [tf, setTf] = useState<"5s" | "1m" | "5m">("1m");
  const [bars, setBars] = useState<number[][]>([]);
  const [detail, setDetail] = useState<Snapshot | undefined>();
  const [toast, setToast] = useState<string | null>(null);

  // default focus: best Checker token, else SOL
  useEffect(() => {
    if (focus || !live.markets.length) return;
    const pick = live.ranking[0]?.symbol ?? live.markets.find((m) => m.base === "SOL")?.symbol ?? live.markets[0].symbol;
    setFocus(pick);
  }, [live.markets, live.ranking, focus]);
  useEffect(() => {
    if (focus) try { localStorage.setItem("pp.focus", focus); } catch { /* ignore */ }
  }, [focus]);

  // candles: refresh every second on 5s bars, every 5s otherwise
  useEffect(() => {
    if (!focus) return;
    let alive = true;
    const load = () => api(`/api/candles/${focus}?tf=${tf}&n=200`).then((d) => alive && setBars(d.bars)).catch(() => {});
    load();
    const id = window.setInterval(load, tf === "5s" ? 1000 : 5000);
    return () => { alive = false; window.clearInterval(id); };
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
      ls.push({ y: p.entry, label: `Entrata ${p.side}`, color: "var(--text)" }, { y: p.tp, label: "TP", color: "var(--long)" }, { y: p.sl, label: "SL", color: "var(--short)" });
    }
    return ls;
  }, [live.executor, focus]);

  const sys = live.system;
  const feedOk = sys && Date.now() / 1000 - sys.last_tick < 5;
  const best = detail?.checker;
  const horizonForTf = tf === "5s" ? "30s" : tf === "1m" ? "15m" : "1h";
  const hstat = snap?.horizons?.[horizonForTf];

  const realClose = async (p: { id: string; symbol: string }) => {
    await real.closeReal(p.symbol);
  };

  return (
    <div className="shell">
      <header className="top">
        <div className="brand">
          <h1>Predictor</h1>
          <span>Kronos su Raydium Perps · top 100 crypto</span>
        </div>
        <div className="status" aria-live="polite">
          <span className="chip"><span className={`dot ${live.connected && feedOk ? "ok" : live.connected ? "warn" : "bad"}`} />Prezzi {live.connected ? (feedOk ? "in tempo reale" : "in ritardo") : "disconnesso"}</span>
          <span className="chip"><span className={`dot ${sys?.kronos.ready ? "ok" : sys?.kronos.error ? "bad" : "warn"}`} />
            {sys?.kronos.ready ? <>Kronos <b>{sys.kronos.model.split("/")[1]}</b> · {sys.kronos.scanned}/{sys.markets} token</> : sys?.kronos.error ? "Kronos non disponibile, stima statistica" : "Kronos in caricamento"}
          </span>
          <span className="chip"><span className={`dot ${(sys?.analyst.risk ?? 0) >= 70 ? "bad" : (sys?.analyst.risk ?? 0) >= 55 ? "warn" : "ok"}`} />Rischio macro <b>{(sys?.analyst.risk ?? 0).toFixed(0)}</b></span>
          <span className="chip"><span className={`dot ${live.executor?.mode === "real" ? "warn" : "ok"}`} />{live.executor?.mode === "real" ? <b>Trading reale attivo</b> : "Simulazione"}</span>
        </div>
      </header>

      <main className="desk">
        <aside className="watch">
          <Watchlist markets={live.markets} ranking={live.ranking} prices={live.prices} focus={focus} onFocus={setFocus} paused={live.paused} pauseReason={live.pauseReason} />
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
                    <div className="chg muted">mediana Kronos tra {horizonForTf} · {pct(hstat.p_up)} in rialzo</div>
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
              <div className="tfs" role="group" aria-label="Intervallo candele">
                {(["5s", "1m", "5m"] as const).map((t) => <button key={t} aria-pressed={tf === t} onClick={() => setTf(t)}>{TF_LABEL[t]}</button>)}
              </div>
              {fan ? <span className="note">Cono Kronos su {fmtDur(fan.q50.length * TF_SECONDS[tf])}, aggiornato {Math.round(fan.age)}s fa{fan.source !== "kronos" ? " (stima statistica)" : ""}</span>
                : <span className="note">{tf === "5s" ? "Le candele da 5 secondi si formano dai tick live: servono circa 4 minuti dopo l'avvio." : "Previsione in calcolo per questo token."}</span>}
            </div>
            <Chart bars={liveBars} fan={fan} levels={levels} tfSeconds={TF_SECONDS[tf]} />
            <div className="legend">
              <span><i style={{ background: "var(--kronos)", opacity: 0.35 }} />80% dei futuri campionati</span>
              <span><i style={{ background: "var(--kronos)", opacity: 0.6 }} />50% centrale</span>
              <span><i style={{ background: "none", borderTop: "2px dashed var(--kronos)", height: 0 }} />mediana</span>
            </div>
          </div>
          <Verdict snap={snap} />
        </section>

        <aside className="side">
          <HorizonLadder snap={snap} />
          <Ticket m={m} snap={snap} ex={live.executor} real={real} onToast={setToast} />
          <WalletPanel real={real} ex={live.executor} />
        </aside>

        <section className="deck">
          <div style={{ display: "grid", gap: 14 }}>
            <Positions ex={live.executor} onToast={setToast} realClose={realClose} />
            <ActivityLog log={live.log} />
          </div>
          <AnalystPanel a={live.analyst} />
          <ControllerPanel r={live.report} />
        </section>
      </main>
      {toast && <div className="toast" role="status">{toast}</div>}
    </div>
  );
}
