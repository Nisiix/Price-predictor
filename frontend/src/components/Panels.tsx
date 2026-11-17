import { useMemo, useState } from "react";
import type { AnalystView, Board, CheckerRow, ExecutorView, HorizonStat, LogEntry, Market, Position, Report, Snapshot } from "../api";
import { ago, pct, postJSON, price, signed, usd } from "../api";

const HORIZON_ORDER = ["5s", "10s", "30s", "1m", "5m", "15m", "30m", "1h"];
export const HORIZON_CHOICES: [string, string][] = [
  ["5s", "5 secondi"], ["10s", "10 secondi"], ["30s", "30 secondi"], ["1m", "1 minuto"],
  ["15m", "15 minuti"], ["30m", "30 minuti"], ["1h", "1 ora"],
];
export const horizonName = (h: string) => HORIZON_CHOICES.find(([k]) => k === h)?.[1] ?? h;

// ------------------------------------------------------------------ watchlist (Checker ranking)
export function Watchlist({ markets, ranking, prices, focus, onFocus, paused, pauseReason, board, horizon }: {
  markets: Market[]; ranking: CheckerRow[]; prices: Record<string, { p: number; c: number }>; focus: string | null;
  onFocus: (s: string) => void; paused: boolean; pauseReason: string; board: Board | null; horizon: string;
}) {
  const [q, setQ] = useState("");
  const [sortBy, setSortBy] = useState<SortMode>(() => {
    try {
      const v = localStorage.getItem("pp.sort") as SortMode | null;
      return v && SORTS.some(([k]) => k === v) ? v : "prob";
    } catch {
      return "prob";
    }
  });
  const changeSort = (v: SortMode) => {
    setSortBy(v);
    try { localStorage.setItem("pp.sort", v); } catch { /* ignore */ }
  };
  const byRank = useMemo(() => new Map(ranking.map((r) => [r.symbol, r])), [ranking]);
  const probs = board && board.h === horizon ? board.rows : {};
  const ready = Object.keys(probs).length;
  const rows = useMemo(() => {
    const ql = q.trim().toLowerCase();
    return markets
      .map((m) => ({ s: m.symbol, m, r: byRank.get(m.symbol), b: probs[m.symbol] }))
      .filter((x) => !ql || x.m.base.toLowerCase().includes(ql) || x.m.name.toLowerCase().includes(ql))
      // tokens without the needed numbers go last, by market cap
      .sort((a, b) => sortKey(b, sortBy) - sortKey(a, sortBy) || a.m.rank - b.m.rank);
  }, [markets, byRank, probs, q, sortBy]);

  return (
    <div className="panel">
      <h2>
        Token <small>{ready ? `probabilità a ${horizonName(horizon)}` : `calcolo le probabilità a ${horizonName(horizon)}…`}</small>
      </h2>
      <div className="tabs" role="group" aria-label="Ordina i token" style={{ padding: "0 8px" }}>
        {SORTS.map(([k, l]) => <button key={k} aria-pressed={sortBy === k} onClick={() => changeSort(k)}>{l}</button>)}
      </div>
      {ready > 0 && ready < markets.length && <p className="note" style={{ padding: "0 8px", margin: "0 0 6px" }}>{ready} di {markets.length} token già calcolati, gli altri arrivano a breve.</p>}
      {paused && <p className="note warn" style={{ padding: "0 8px" }}>Nuove aperture sospese: {pauseReason}</p>}
      <input className="search" placeholder="Cerca token" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Cerca token" />
      <ul className="wl">
        {rows.map(({ s, m, r, b }) => {
          const p = prices[s]?.p ?? m.mark_price;
          return (
            <li key={s}>
              <button aria-current={focus === s} onClick={() => onFocus(s)}>
                <span className="name">
                  {m.base}
                  {r?.eligible && <span className="go" title={`Segnale valido per l'Executor: ${r.side} ${r.leverage}x`} />}
                </span>
                <span className={`prob ${b ? (b.dir === "up" ? "long" : "short") : "faint"}`}>
                  {b ? <>{b.dir === "up" ? "▲" : "▼"} {pct(b.conf)}</> : "–"}
                </span>
                <span className="meta">
                  {price(p)}
                  {r && <span className={`side-tag ${r.side}`} title={`Checker: TP ${pct(r.kronos_p_tp)}, rischio ${pct(r.extra_risk)}`}>{r.side === "long" ? "Long" : "Short"} {r.leverage}x</span>}
                </span>
                <span className="score" title="Rischio aggiuntivo stimato dal Checker">
                  {r ? <span style={{ color: riskColor(r.extra_risk) }}>rischio {pct(r.extra_risk)}</span> : b ? "rischio in calcolo" : "in calcolo"}
                </span>
              </button>
            </li>
          );
        })}
        {!rows.length && <li className="empty" style={{ padding: 8 }}>Nessun token corrisponde alla ricerca.</li>}
      </ul>
    </div>
  );
}

type SortMode = "prob" | "risk" | "combo";
const SORTS: [SortMode, string][] = [["prob", "Probabilità"], ["risk", "Rischio più basso"], ["combo", "Probabilità e rischio"]];
type WlRow = { m: Market; r?: CheckerRow; b?: { conf: number } };
function sortKey(x: WlRow, mode: SortMode): number {
  const risk = x.r?.extra_risk;
  if (mode === "risk") return risk == null ? -1 : 1 - risk;
  if (mode === "combo") return x.b && risk != null ? x.b.conf * (1 - risk) : -1;
  return x.b?.conf ?? -1;
}
const riskColor = (r: number) => (r > 0.5 ? "var(--short)" : r > 0.35 ? "var(--warn)" : "var(--long)");

// ------------------------------------------------------------------ horizon ladder (signature)
export function HorizonLadder({ snap, selected, onSelect }: { snap: Snapshot | undefined; selected: string; onSelect: (h: string) => void }) {
  return (
    <div className="panel">
      <h2>Previsione Kronos <small>probabilità che il prezzo sia più alto</small></h2>
      <div className="ladder">
        {HORIZON_ORDER.map((h) => {
          const s: HorizonStat | null | undefined = snap?.horizons?.[h];
          if (!s) {
            return (
              <div key={h} className={`rung pending ${h === selected ? "sel" : ""}`}>
                <span className="h">{h}</span>
                <div className="bar" style={{ background: "var(--line-soft)" }} />
                <span className="p">–</span>
                <span className="sub"><span>{h.endsWith("s") ? "raccolgo i tick live" : "calcolo in corso"}</span></span>
              </div>
            );
          }
          const up = s.p_up;
          return (
            <div key={h} className={`rung ${h === selected ? "sel" : ""}`} title={`Kronos: ${s.source}. Aggiornata ${Math.round(s.age)}s fa`}>
              {HORIZON_CHOICES.some(([k]) => k === h)
                ? <button className="h hbtn" onClick={() => onSelect(h)} aria-pressed={h === selected} title={`Ordina i token e mostra il grafico a ${horizonName(h)}`}>{h}</button>
                : <span className="h">{h}</span>}
              <div className="bar" role="img" aria-label={`${pct(up)} rialzo, ${pct(1 - up)} ribasso`}>
                <div className="up" style={{ width: `${up * 100}%` }} />
                <div className="mid" />
              </div>
              <span className={`p ${up >= 0.5 ? "long" : "short"}`}>{pct(up >= 0.5 ? up : 1 - up)}</span>
              <span className="sub">
                <span>
                  {up >= 0.5 ? "su" : "giù"} · mediana <span className="kronos">{price(s.price_q50)}</span> · {signed(s.q10 * 100, 2)}% / {signed(s.q90 * 100, 2)}%
                  {s.experimental && <span className="tag-exp"> · sperimentale</span>}
                  {s.source !== "kronos" && <span className="tag-exp"> · {s.source === "flat" ? "mercato fermo" : "stima statistica"}</span>}
                </span>
                <span>{s.accuracy ? `verificate ${pct(s.accuracy.hit_rate)} su ${s.accuracy.n}` : ""}</span>
              </span>
            </div>
          );
        })}
      </div>
      <p className="note" style={{ marginTop: 8 }}>
        Ogni riga è calcolata da {snap?.fans?.["1m"] ? "24" : "più"} futuri campionati da Kronos. A fianco, la quota di previsioni
        verificate a scadenza in cui la direzione era corretta.
      </p>
    </div>
  );
}

// ------------------------------------------------------------------ checker ranking (Agenti page)
export function CheckerTable({ ranking, focus, onFocus, paused, pauseReason }: {
  ranking: CheckerRow[]; focus: string | null; onFocus: (s: string) => void; paused: boolean; pauseReason: string;
}) {
  return (
    <div className="panel">
      <h2>Checker <small>{ranking.length ? `${ranking.length} token valutati per long e short da 10x a 20x` : "in attesa delle prime previsioni"}</small></h2>
      {paused && <p className="note warn">Nuove aperture sospese dall'Analyst: {pauseReason}</p>}
      {ranking.length === 0 ? <p className="empty">Il Checker pubblica la classifica dopo la prima scansione di Kronos.</p> : (
        <div className="scroll" style={{ maxHeight: 360, overflowY: "auto" }}>
          <table className="t">
            <thead>
              <tr><th>Token</th><th>Setup</th><th className="n">Kronos TP</th><th className="n">Pattern</th><th className="n">Rischio</th><th className="n">Punteggio</th><th>Esito</th></tr>
            </thead>
            <tbody>
              {ranking.map((r) => (
                <tr key={r.symbol} className={`clickable ${focus === r.symbol ? "cur" : ""}`} onClick={() => onFocus(r.symbol)}>
                  <td><b>{r.base}</b></td>
                  <td className={r.side}>{r.side === "long" ? "Long" : "Short"} {r.leverage}x</td>
                  <td className="n">{pct(r.kronos_p_tp)}</td>
                  <td className="n">{r.analog_win != null ? pct(r.analog_win) : "–"}</td>
                  <td className="n" style={{ color: r.extra_risk > 0.5 ? "var(--short)" : r.extra_risk > 0.35 ? "var(--warn)" : "var(--long)" }}>{pct(r.extra_risk)}</td>
                  <td className="n">{r.score.toFixed(2)}</td>
                  <td className="faint" style={{ fontSize: 12 }}>{r.eligible ? <span className="kronos">segnale all'Executor</span> : r.reasons[0] ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="note" style={{ marginTop: 8 }}>Seleziona una riga per vedere sotto l'analisi completa del token.</p>
    </div>
  );
}

// ------------------------------------------------------------------ checker verdict for focus token
export function Verdict({ snap }: { snap: Snapshot | undefined }) {
  const c = snap?.checker;
  if (!c) {
    return (
      <div className="panel">
        <h2>Analisi del Checker {snap?.symbol ? <small>{snap.symbol.split("_")[1]}</small> : null}</h2>
        <p className="empty">Il Checker non ha ancora valutato questo token: serve almeno un'ora di candele da 1 minuto e una previsione Kronos.</p>
      </div>
    );
  }
  const ind = c.indicators;
  const an = c.analogs;
  const win = c.side === "long" ? an.long_win : an.short_win;
  const labels: Record<string, string> = {
    kronos_avverso: "Kronos: SL o liquidazione", indicatori_contrari: "Indicatori contrari", pattern_storici: "Pattern storici perdenti",
    volatilita: "Volatilità vs stop", macro_analyst: "Rischio macro (Analyst)",
  };
  return (
    <div className="panel">
      <h2>
        Analisi del Checker: {c.base}
        <small>
          miglior setup: <span className={c.side}>{c.side === "long" ? "long" : "short"} {c.leverage}x</span>
          {c.eligible ? " · segnale valido" : c.reasons.length ? ` · ${c.reasons.join(", ")}` : ""}
        </small>
      </h2>
      <div className="verdict">
        <section>
          <h3>Kronos</h3>
          <div className="big kronos">{pct(c.kronos_p_tp)}</div>
          <div className="note">di toccare il take profit prima dello stop</div>
          <dl className="kv">
            <dt>Stop loss prima</dt><dd>{pct(c.kronos_p_sl)}</dd>
            <dt>Liquidazione</dt><dd>{pct(c.kronos_p_liq, 1)}</dd>
            <dt>Valore atteso</dt><dd className={c.ev >= 0 ? "long" : "short"}>{signed(c.ev * 100, 2)}%</dd>
          </dl>
        </section>
        <section>
          <h3>Indicatori e medie</h3>
          <div className="big">{ind.score >= 0 ? "+" : ""}{ind.score.toFixed(2)}</div>
          <div className="note">{ind.regime}</div>
          <dl className="kv">
            <dt>RSI 14</dt><dd>{ind.rsi.toFixed(0)}</dd>
            <dt>EMA 9 / 21</dt><dd className={ind.ema9 > ind.ema21 ? "long" : "short"}>{ind.ema9 > ind.ema21 ? "sopra" : "sotto"}</dd>
            <dt>MACD hist</dt><dd className={ind.macd_hist > 0 ? "long" : "short"}>{ind.macd_hist > 0 ? "positivo" : "negativo"}</dd>
            <dt>Bollinger %B</dt><dd>{ind.bb_pct_b.toFixed(2)}</dd>
            <dt>ATR 1m</dt><dd>{(ind.atr_pct * 100).toFixed(2)}%</dd>
          </dl>
          {ind.flags.length > 0 && <div className="flags">{ind.flags.map((f: string) => <span key={f}>{f}</span>)}</div>}
        </section>
        <section>
          <h3>Pattern storici</h3>
          <div className="big">{win != null ? pct(win) : "–"}</div>
          <div className="note">{an.n ? `dei ${an.n} casi più simili ha raggiunto il TP` : "storico insufficiente"}</div>
          {an.n > 0 && (
            <dl className="kv">
              <dt>Somiglianza media</dt><dd>{pct(an.similarity)}</dd>
              <dt>Rendimento dopo 30 min</dt><dd>{signed(an.fwd_mean * 100, 2)}%</dd>
              <dt>Fonti</dt><dd title={an.sources.join(", ")}>{an.sources.slice(0, 4).map((s: string) => s.split("_")[1]).join(", ")}{an.sources.length > 4 ? "…" : ""}</dd>
            </dl>
          )}
        </section>
        <section>
          <h3>Rischio aggiuntivo</h3>
          <div className="big" style={{ color: c.extra_risk > 0.5 ? "var(--short)" : c.extra_risk > 0.35 ? "var(--warn)" : "var(--long)" }}>{pct(c.extra_risk)}</div>
          <div className="note">probabilità combinata {pct(c.combined_prob)}</div>
          <div style={{ marginTop: 8 }}>
            {Object.entries(c.risk_parts).map(([k, v]) => (
              <div key={k} style={{ fontSize: 12 }}>
                <div style={{ display: "flex", justifyContent: "space-between" }}><span className="muted">{labels[k] ?? k}</span><span>{pct(v)}</span></div>
                <div className="riskbar"><span style={{ width: `${Math.min(100, v * 100)}%` }} /></div>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ positions (Executor)
export function Positions({ ex, onToast, realClose }: { ex: ExecutorView | null; onToast: (s: string) => void; realClose: (p: Position) => Promise<void> }) {
  if (!ex) return <div className="panel"><h2>Executor</h2><p className="empty">Connessione al motore…</p></div>;
  const toggleAuto = async () => {
    await postJSON("/api/executor", { auto: !ex.auto });
  };
  const close = async (p: Position) => {
    if (p.mode === "real") await realClose(p);
    else await postJSON(`/api/position/${p.id}/close`, {});
    onToast(`Chiusura di ${p.base} richiesta`);
  };
  return (
    <div className="panel">
      <h2>
        Executor <small>{ex.mode === "real" ? "operazioni reali su Raydium Perps" : "operazioni simulate su prezzi reali"}</small>
      </h2>
      <div className="balance">
        {ex.mode === "paper" && <div><span>Saldo simulato</span><b>{usd(ex.equity)}</b></div>}
        <div><span>Posizioni</span><b>{ex.positions.length} / {ex.max_positions}</b></div>
        <div><span>Per operazione</span><b>{ex.margin_usdc} USDC</b></div>
        <div><span>Regole</span><b style={{ fontSize: 14 }}>TP +{ex.tp_pct}% · SL −{ex.sl_pct}%</b></div>
        <label className="switch" style={{ marginLeft: "auto" }}>
          <input type="checkbox" checked={ex.auto} onChange={toggleAuto} /> Apertura automatica
        </label>
      </div>
      {ex.positions.length === 0 ? (
        <p className="empty">Nessuna posizione aperta. L'Executor apre quando il Checker segnala un token valido{ex.auto ? "" : " (apertura automatica spenta)"}.</p>
      ) : (
        <div className="scroll">
          <table className="t">
            <thead><tr><th>Token</th><th>Lato</th><th className="n">Entrata</th><th className="n">Prezzo</th><th>Verso TP / SL</th><th className="n">PnL</th><th /></tr></thead>
            <tbody>
              {ex.positions.map((p) => {
                // map lev_move from [-SL, +TP] onto the track
                const span = ex.tp_pct + ex.sl_pct;
                const zero = (ex.sl_pct / span) * 100;
                const at = Math.max(0, Math.min(100, ((p.lev_move + ex.sl_pct) / span) * 100));
                return (
                  <tr key={p.id}>
                    <td><b>{p.base}</b> <span className="faint">{p.mode === "real" ? "reale" : "sim."}</span></td>
                    <td className={p.side}>{p.side === "long" ? "Long" : "Short"} {p.leverage}x</td>
                    <td className="n">{price(p.entry)}</td>
                    <td className="n">{price(p.last_price)}</td>
                    <td>
                      <div className="track" title={`${signed(p.lev_move, 2)}% con leva`}>
                        <div className="z" style={{ left: `${zero}%` }} />
                        <div className="m" style={{ left: `${Math.min(zero, at)}%`, width: `${Math.abs(at - zero)}%`, background: p.lev_move >= 0 ? "var(--long)" : "var(--short)" }} />
                      </div>
                      <span className="faint" style={{ fontSize: 11 }}>{signed(p.lev_move, 2)}% leva · liq {price(p.liq)}</span>
                    </td>
                    <td className={`n ${p.pnl >= 0 ? "long" : "short"}`}>{signed(p.pnl)}<br /><span style={{ fontSize: 11 }}>{signed(p.roe, 1)}%</span></td>
                    <td className="n"><button className="ghost danger" onClick={() => close(p)} disabled={p.status !== "open"}>{p.status === "closing" ? "In chiusura" : "Chiudi"}</button></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {ex.pending.length > 0 && (
        <>
          <h2 style={{ marginTop: 12 }}>Ordini limite simulati</h2>
          <table className="t"><tbody>
            {ex.pending.map((o) => (
              <tr key={o.id}>
                <td>{o.symbol.split("_")[1]}</td><td className={o.side}>{o.side} {o.leverage}x</td><td className="n">{price(o.limit)}</td>
                <td className="n"><button className="ghost" onClick={() => fetch(`/api/order/${o.id}`, { method: "DELETE" })}>Annulla</button></td>
              </tr>
            ))}
          </tbody></table>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ analyst
export function AnalystPanel({ a }: { a: AnalystView | null }) {
  if (!a) return <div className="panel"><h2>Analyst</h2><p className="empty">Raccolta notizie in corso…</p></div>;
  const comp = a.components;
  return (
    <div className="panel">
      <h2>Analyst <small>{a.status}</small></h2>
      <div className="gauge">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <span className="big">{a.risk.toFixed(0)}<span className="muted" style={{ fontSize: 14 }}> / 100</span></span>
          <span className="muted" style={{ fontSize: 12.5 }}>
            {a.fear_greed ? `Fear & Greed ${a.fear_greed.value} (${a.fear_greed.label})` : ""}
          </span>
        </div>
        <div className="scale">
          <div className="needle" style={{ left: `${a.risk}%` }} />
          <span className="th" style={{ left: `${a.pause_threshold}%` }}>pausa</span>
          <span className="th" style={{ left: `${a.close_threshold}%` }}>chiude tutto</span>
        </div>
        <div className="note" style={{ marginTop: 14 }}>
          Notizie {comp.notizie?.toFixed(0) ?? 0} · shock di mercato {comp.shock_mercato?.toFixed(0) ?? 0} · sentiment {comp.sentiment?.toFixed(0) ?? 0}
          {comp.btc_5m != null && ` · BTC/ETH/SOL 5 min ${signed(comp.btc_5m, 2)}%`}
        </div>
      </div>
      <ul className="news">
        {a.headlines.length === 0 && <li className="empty">Nessuna notizia nelle ultime 12 ore.</li>}
        {a.headlines.map((h) => (
          <li key={h.id}>
            <span className="sev" style={{ background: h.severity >= 0.18 ? "var(--danger)" : h.severity >= 0.08 ? "var(--warn)" : h.severity > 0 ? "var(--muted)" : "var(--line)" }} />
            <a href={h.url} target="_blank" rel="noreferrer">{h.title}</a>
            <div className="meta">
              <span>{h.source}</span><span>{ago(h.ts)}</span>
              {h.categories.length > 0 && <span>{h.categories.map(catLabel).join(", ")}{h.calming ? " (distensiva)" : ""}</span>}
              {h.analog && h.severity > 0 && <span>precedente: {h.analog.title} ({h.analog.date.slice(0, 4)}, BTC {signed(h.analog.btc_move * 100, 0)}%)</span>}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

const CATS: Record<string, string> = {
  war: "guerra", macro_crash: "crollo macro", banking_crisis: "crisi bancaria", fed_hawkish: "Fed restrittiva", trade_war: "dazi/sanzioni",
  regulation_ban: "divieti", regulation_enforcement: "azioni legali", exchange_collapse: "crisi exchange", hack: "hack",
  stablecoin_depeg: "depeg stablecoin", lender_insolvency: "insolvenza", etf: "ETF",
};
const catLabel = (c: string) => CATS[c] ?? c;

// ------------------------------------------------------------------ controller
export function ControllerPanel({ r }: { r: Report | null }) {
  const [tab, setTab] = useState("per_token");
  if (!r) return <div className="panel"><h2>Controller</h2><p className="empty">Il report compare dopo la prima operazione chiusa.</p></div>;
  const s = r.summary;
  const tabs: [string, string][] = [["per_token", "Token"], ["per_pattern", "Pattern"], ["per_prob_kronos", "Prob. Kronos"], ["per_rischio", "Rischio"], ["per_motivo_chiusura", "Chiusure"], ["per_leva", "Leva"]];
  const g = r.groups[tab] ?? [];
  return (
    <div className="panel">
      <h2>Controller <small><a href="/api/report.md" download="report-controller.md">Scarica il report</a></small></h2>
      <div className="stats">
        <div><span>Operazioni</span><b>{s.n}</b></div>
        <div><span>Win rate</span><b>{pct(s.win_rate)}</b></div>
        <div><span>PnL</span><b className={s.pnl >= 0 ? "long" : "short"}>{signed(s.pnl)}</b></div>
        <div><span>ROE medio</span><b>{signed(s.avg_roe, 1)}%</b></div>
      </div>
      {r.insights.length > 0 && <ul className="insights">{r.insights.map((i) => <li key={i}>{i}</li>)}</ul>}
      <div className="tabs">{tabs.map(([k, l]) => <button key={k} aria-pressed={tab === k} onClick={() => setTab(k)}>{l}</button>)}</div>
      {g.length === 0 ? <p className="empty">Ancora nessuna operazione chiusa.</p> : (
        <table className="t">
          <thead><tr><th>Gruppo</th><th className="n">Op.</th><th className="n">Win</th><th className="n">PnL</th></tr></thead>
          <tbody>{g.slice(0, 8).map((x) => (
            <tr key={x.key}><td>{x.key}</td><td className="n">{x.n}</td><td className="n">{pct(x.win_rate)}</td><td className={`n ${x.pnl >= 0 ? "long" : "short"}`}>{signed(x.pnl)}</td></tr>
          ))}</tbody>
        </table>
      )}
      {r.calibration.length > 0 && (
        <>
          <h2 style={{ marginTop: 14 }}>Affidabilità di Kronos <small>direzione corretta a scadenza</small></h2>
          <table className="t"><tbody>
            {r.calibration.map((c) => (
              <tr key={c.label}><td>{c.label}</td><td className="n">{c.n} previsioni</td><td className={`n ${c.hit_rate >= 0.5 ? "long" : "short"}`}>{pct(c.hit_rate, 1)}</td></tr>
            ))}
          </tbody></table>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ activity log
export function ActivityLog({ log }: { log: LogEntry[] }) {
  return (
    <div className="panel">
      <h2>Attività degli agenti</h2>
      <ul className="log">
        {log.length === 0 && <li className="empty">Nessuna attività ancora.</li>}
        {log.map((l, i) => (
          <li key={`${l.ts}-${i}`}>
            <span className="who">{l.agent}<br /><span className="faint">{new Date(l.ts * 1000).toLocaleTimeString("it-IT")}</span></span>
            <span className={l.level}>{l.url ? <a href={l.url} target="_blank" rel="noreferrer" style={{ color: "inherit" }}>{l.text}</a> : l.text}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
