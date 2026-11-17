import { useMemo, useState } from "react";
import type { Board, CheckerRow, HorizonPick, ExecutorView, HorizonStat, Market, Position, Snapshot } from "../api";
import { pct, postJSON, price, signed, usd } from "../api";

const HORIZON_ORDER = ["15m", "30m", "1h", "4h"];
export const HORIZON_CHOICES: [string, string][] = [["15m", "15 minuti"], ["30m", "30 minuti"], ["1h", "1 ora"], ["4h", "4 ore"]];
export const horizonName = (h: string) => HORIZON_CHOICES.find(([k]) => k === h)?.[1] ?? h;

// ------------------------------------------------------------------ watchlist (Checker ranking)
/* The list no longer depends on the chart's horizon: each token is shown with its own best horizon
   (best probability x (1 - risk) among 15 min, 30 min, 1 h, 4 h). With "show downward" off, the best
   upward horizon is used, since the Executor opens longs only. Filters and sorting use that pick. */
function pickFor(r: CheckerRow | undefined, showDown: boolean): HorizonPick | null {
  if (!r?.horizons?.length) return null;
  const cands = showDown ? r.horizons : r.horizons.filter((h) => h.side === "long");
  if (!cands.length) return null;
  return cands.reduce((a, b) => (b.score > a.score ? b : a));
}

export function Watchlist({ markets, ranking, prices, focus, onFocus, paused, pauseReason }: {
  markets: Market[]; ranking: CheckerRow[]; prices: Record<string, { p: number; c: number }>; focus: string | null;
  onFocus: (s: string) => void; paused: boolean; pauseReason: string; board?: Board | null; horizon?: string;
}) {
  const [q, setQ] = useState("");
  const [onlyStrong, setOnlyStrong] = useState<boolean>(() => {
    try { return localStorage.getItem("pp.only70") !== "0"; } catch { return true; }
  });
  const toggleStrong = (v: boolean) => {
    setOnlyStrong(v);
    try { localStorage.setItem("pp.only70", v ? "1" : "0"); } catch { /* ignore */ }
  };
  // the Executor opens longs only: by default the list shows upward forecasts only
  const [showDown, setShowDown] = useState<boolean>(() => {
    try { return localStorage.getItem("pp.showDown") === "1"; } catch { return false; }
  });
  const toggleDown = (v: boolean) => {
    setShowDown(v);
    try { localStorage.setItem("pp.showDown", v ? "1" : "0"); } catch { /* ignore */ }
  };
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
  const ready = ranking.filter((r) => r.horizons?.length).length;
  const rows = useMemo(() => {
    const ql = q.trim().toLowerCase();
    return markets
      .map((m) => {
        const r = byRank.get(m.symbol);
        return { s: m.symbol, m, r, k: pickFor(r, showDown) };
      })
      .filter((x) => !ql || x.m.base.toLowerCase().includes(ql) || x.m.name.toLowerCase().includes(ql))
      .filter((x) => x.k != null || (showDown && !onlyStrong)) // without a pick: still being computed, or no upward horizon
      .filter((x) => !onlyStrong || (x.k != null && x.k.conf > STRONG_PROB))
      .sort((a, b) => sortKey(b.k, sortBy) - sortKey(a.k, sortBy) || a.m.rank - b.m.rank);
  }, [markets, byRank, q, sortBy, onlyStrong, showDown]);

  return (
    <div className="panel">
      <h2>
        Token <small>{ready ? "ciascuno sul suo orizzonte migliore" : "calcolo le probabilità…"}</small>
      </h2>
      <label className="switch" style={{ padding: "0 8px", marginBottom: 6, fontSize: 12.5 }}>
        <input type="checkbox" checked={onlyStrong} onChange={(e) => toggleStrong(e.target.checked)} />
        Solo probabilità sopra il 70% {ready > 0 && <span className="faint">({rows.length} di {ready})</span>}
      </label>
      <label className="switch" style={{ padding: "0 8px", marginBottom: 6, fontSize: 12.5 }}>
        <input type="checkbox" checked={showDown} onChange={(e) => toggleDown(e.target.checked)} />
        Mostra anche i ribassi <span className="faint">(l'Executor apre solo long)</span>
      </label>
      <div className="tabs" role="group" aria-label="Ordina i token" style={{ padding: "0 8px" }}>
        {SORTS.map(([k, l]) => <button key={k} aria-pressed={sortBy === k} onClick={() => changeSort(k)}>{l}</button>)}
      </div>
      {ready > 0 && ready < markets.length && <p className="note" style={{ padding: "0 8px", margin: "0 0 6px" }}>{ready} di {markets.length} token già calcolati, gli altri arrivano a breve.</p>}
      {paused && <p className="note warn" style={{ padding: "0 8px" }}>Nuove aperture sospese: {pauseReason}</p>}
      <input className="search" placeholder="Cerca token" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Cerca token" />
      <ul className="wl">
        {rows.map(({ s, m, r, k }) => {
          const p = prices[s]?.p ?? m.mark_price;
          return (
            <li key={s}>
              <button aria-current={focus === s} onClick={() => onFocus(s)}>
                <span className="name">
                  {m.base}
                  {r?.eligible && <span className="go" title={`Segnale valido per l'Executor: ${r.side} ${r.leverage}x`} />}
                  {r && r.drop_priority > 0 && <span className="droptag" title="Calo recente oltre il 5%: precedenza in apertura (non cambia la decisione)">↓{(r.drop_priority * 100).toFixed(1)}% {r.drop_window}</span>}
                </span>
                <span className={`prob ${k ? k.side : "faint"}`}>
                  {k ? <>{k.side === "long" ? "▲" : "▼"} {pct(k.conf)}</> : "–"}
                </span>
                <span className="meta">
                  {price(p)}
                  {r && <span className={`side-tag ${r.side}`} title={`Checker, piano a 4 ore: ${pct(r.conf_4h)} di probabilità, rischio ${pct(r.extra_risk)}`}>4 ore: {r.side === "long" ? "Long" : "Short"} {r.leverage}x</span>}
                </span>
                <span className="score" title="Rischio aggiuntivo sull'orizzonte migliore">
                  {k ? <span style={{ color: riskColor(k.risk) }}>rischio {pct(k.risk)}</span> : "in calcolo"}
                </span>
                {k && r && (
                  <span
                    className="besth"
                    title={"Probabilità e rischio per orizzonte:\n" + r.horizons.map((x) => `${horizonName(x.h)}: ${x.side === "long" ? "▲" : "▼"} ${pct(x.conf)}, rischio ${pct(x.risk)}`).join("\n")}
                  >
                    Migliore: <b>{horizonName(k.h)}</b>{" "}
                    <span className={k.side}>{k.side === "long" ? "▲" : "▼"} {pct(k.conf)}</span>
                    {" · "}<span style={{ color: riskColor(k.risk) }}>rischio {pct(k.risk)}</span>
                  </span>
                )}
              </button>
            </li>
          );
        })}
        {!rows.length && (
          <li className="empty" style={{ padding: 8 }}>
            {q ? "Nessun token corrisponde alla ricerca." : `Nessun token ${showDown ? "" : "in rialzo "}${onlyStrong ? "supera il 70% di probabilità " : ""}su nessun orizzonte. Allarga i filtri o attendi i prossimi calcoli.`}
          </li>
        )}
      </ul>
    </div>
  );
}

type SortMode = "prob" | "risk" | "combo";
const STRONG_PROB = 0.7;
const SORTS: [SortMode, string][] = [["prob", "Probabilità"], ["risk", "Rischio più basso"], ["combo", "Probabilità e rischio"]];
function sortKey(k: HorizonPick | null, mode: SortMode): number {
  if (!k) return -1;
  if (mode === "risk") return 1 - k.risk;
  if (mode === "combo") return k.score;
  return k.conf;
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
function RangeBar({ pos, side }: { pos: number; side: "long" | "short" }) {
  const zone = 20;
  return (
    <div className="rangebar" title={`Prezzo al ${Math.round(pos * 100)}% del range delle ultime 4 ore (0% = minimo, 100% = massimo)`}>
      <div className={`zone ${side}`} style={side === "long" ? { left: 0, width: `${zone}%` } : { right: 0, width: `${zone}%` }} />
      <div className="dotp" style={{ left: `${Math.max(0, Math.min(100, pos * 100))}%` }} />
    </div>
  );
}

export function CheckerTable({ ranking, focus, onFocus, paused, pauseReason }: {
  ranking: CheckerRow[]; focus: string | null; onFocus: (s: string) => void; paused: boolean; pauseReason: string;
}) {
  // the Pattern column only earns its space once some token has historical analogs
  const withPattern = ranking.some((r) => r.analog_win != null);
  return (
    <div className="panel">
      <h2>Checker <small>{ranking.length ? `${ranking.length} token, piano a 4 ore ordinato per probabilità × (1 − rischio)` : "in attesa delle previsioni a 4 ore"}</small></h2>
      {paused && <p className="note warn">Nuove aperture sospese dall'Analyst: {pauseReason}</p>}
      {ranking.length === 0 ? <p className="empty">Il Checker pubblica la classifica quando Kronos ha calcolato le 4 ore dei token (pochi minuti dall'avvio).</p> : (
        <div className="scroll" style={{ maxHeight: 380, overflowY: "auto" }}>
          <table className="t">
            <thead>
              <tr><th>Token</th><th>Kronos 4 ore</th><th>8 ore</th><th>Ingresso previsto</th><th>Leva</th>{withPattern && <th className="n">Pattern</th>}<th className="n">Rischio</th><th className="n">Punteggio</th><th>Range 4 ore</th><th>Esito</th></tr>
            </thead>
            <tbody>
              {ranking.map((r) => (
                <tr key={r.symbol} className={`clickable ${focus === r.symbol ? "cur" : ""}`} onClick={() => onFocus(r.symbol)}>
                  <td><b>{r.base}</b>{r.drop_priority > 0 && <span className="droptag" title="Calo recente oltre il 5%: precedenza in apertura">↓{(r.drop_priority * 100).toFixed(1)}% {r.drop_window}</span>}</td>
                  <td className={r.side}>{r.side === "long" ? "▲ rialzo" : "▼ ribasso"} {pct(r.conf_4h)}</td>
                  <td className={r.p_up_8h >= 0.5 ? "long" : "short"}>{r.p_up_8h >= 0.5 ? "▲" : "▼"} {pct(Math.max(r.p_up_8h, 1 - r.p_up_8h))}</td>
                  <td title={`Punto più basso previsto da Kronos nelle prossime 4 ore, tra circa ${r.dip_minutes} min`}>
                    {price(r.dip_price)} {r.entry_ok ? <span className="long">· raggiunto</span> : <span className="faint">· {signed((r.dip_price / r.price - 1) * 100, 2)}%</span>}
                  </td>
                  <td>{r.leverage}x</td>
                  {withPattern && <td className="n">{r.analog_win != null ? pct(r.analog_win) : "–"}</td>}
                  <td className="n" style={{ color: riskColor(r.extra_risk) }}>{pct(r.extra_risk)}</td>
                  <td className="n">{r.score.toFixed(2)}</td>
                  <td><RangeBar pos={r.range_pos} side={r.side} /></td>
                  <td className="faint" style={{ fontSize: 12 }}>{r.eligible ? <span className="kronos">ingresso: segnale all'Executor</span> : r.reasons[0] ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="note" style={{ marginTop: 8 }}>
        L'Executor apre solo long, quando la probabilità di rialzo a 4 ore supera il 70% e il trend a 8 ore è al rialzo (il rischio decide la leva) e il prezzo arriva al punto più basso previsto da Kronos; prima di entrare ricalcola la previsione e rinvia se ne vede un ribasso ulteriore; i token con un calo oltre il 5% in 4, 8 o 12 ore hanno la precedenza:
        vicino al minimo delle ultime 4 ore per un long, vicino al massimo per uno short. Seleziona una riga per l'analisi completa.
      </p>
    </div>
  );
}

// ------------------------------------------------------------------ checker verdict for focus token
export function Verdict({ snap }: { snap: Snapshot | undefined }) {
  const c = snap?.checker as any;
  if (!c) {
    return (
      <div className="panel">
        <h2>Analisi del Checker {snap?.symbol ? <small>{snap.symbol.split("_")[1]}</small> : null}</h2>
        <p className="empty">Il Checker non ha ancora valutato questo token: servono le candele da 15 minuti e una previsione Kronos a 4 ore.</p>
      </div>
    );
  }
  const ind = c.indicators;
  const an = c.analogs;
  const labels: Record<string, string> = {
    kronos_avverso: "Kronos: direzione opposta", indicatori_contrari: "Indicatori contrari", pattern_storici: "Pattern storici contrari",
    volatilita: "Volatilità vs stop", macro_analyst: "Rischio macro (Analyst)",
  };
  return (
    <div className="panel">
      <h2>
        Analisi del Checker: {c.base}
        <small>
          piano a 4 ore: <span className={c.side}>{c.side} {c.leverage}x</span>
          {c.eligible ? " · segnale all'Executor" : c.reasons.length ? ` · ${c.reasons.join(", ")}` : ""}
        </small>
      </h2>
      <div className="verdict">
        <section>
          <h3>Kronos a 4 ore</h3>
          <div className={`big ${c.side}`}>{c.side === "long" ? "▲" : "▼"} {pct(c.conf_4h)}</div>
          <div className="note">probabilità di {c.side === "long" ? "rialzo" : "ribasso"} tra 4 ore</div>
          <dl className="kv">
            <dt>Rendimento medio atteso</dt><dd className={c.exp_ret_4h >= 0 ? "long" : "short"}>{signed(c.exp_ret_4h * 100, 2)}%</dd>
            <dt>Range 4 ore</dt><dd>{price(c.range_lo)} – {price(c.range_hi)}</dd>
            <dt>Posizione nel range</dt><dd>{pct(c.range_pos)}</dd>
          </dl>
          <div style={{ marginTop: 8 }}><RangeBar pos={c.range_pos} side={c.side} /></div>
        </section>
        <section>
          <h3>Indicatori (candele 15 min)</h3>
          <div className="big">{ind.score >= 0 ? "+" : ""}{ind.score.toFixed(2)}</div>
          <div className="note">{ind.regime}</div>
          <dl className="kv">
            <dt>RSI 14</dt><dd>{ind.rsi.toFixed(0)}</dd>
            <dt>EMA 9 / 21</dt><dd className={ind.ema9 > ind.ema21 ? "long" : "short"}>{ind.ema9 > ind.ema21 ? "sopra" : "sotto"}</dd>
            <dt>MACD hist</dt><dd className={ind.macd_hist > 0 ? "long" : "short"}>{ind.macd_hist > 0 ? "positivo" : "negativo"}</dd>
            <dt>Bollinger %B</dt><dd>{ind.bb_pct_b.toFixed(2)}</dd>
            <dt>ATR 15 min</dt><dd>{(ind.atr_pct * 100).toFixed(2)}%</dd>
          </dl>
          {ind.flags.length > 0 && <div className="flags">{ind.flags.map((f: string) => <span key={f}>{f}</span>)}</div>}
        </section>
        <section>
          <h3>Pattern storici</h3>
          <div className="big">{c.analog_dir != null ? pct(c.analog_dir) : "–"}</div>
          <div className="note">{an.n ? `dei ${an.n} casi più simili è andato ${c.side === "long" ? "su" : "giù"} nelle 4 ore dopo` : "storico insufficiente"}</div>
          {an.n > 0 && (
            <dl className="kv">
              <dt>Somiglianza media</dt><dd>{pct(an.similarity)}</dd>
              <dt>Rendimento dopo 4 ore</dt><dd>{signed(an.fwd_mean * 100, 2)}%</dd>
              <dt>Fonti</dt><dd title={an.sources.join(", ")}>{an.sources.slice(0, 4).map((s: string) => s.split("_")[1]).join(", ")}{an.sources.length > 4 ? "…" : ""}</dd>
            </dl>
          )}
        </section>
        <section>
          <h3>Rischio aggiuntivo</h3>
          <div className="big" style={{ color: riskColor(c.extra_risk) }}>{pct(c.extra_risk)}</div>
          <div className="note">punteggio {c.score.toFixed(2)} · leva scelta {c.leverage}x</div>
          <div style={{ marginTop: 8 }}>
            {Object.entries(c.risk_parts as Record<string, number>).map(([k, v]) => (
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
        <div><span>Posizioni</span><b>{ex.max_positions ? `${ex.positions.length} / ${ex.max_positions}` : ex.positions.length}</b></div>
        <div title={(ex.risk_tiers ?? []).map(([r, s]) => `rischio ≤${Math.round(r * 100)}%: ${Math.round(s * 100)}%`).join(" · ") + ` · oltre: ${Math.round((ex.risk_tier_above ?? 0) * 100)}%`}><span>Per operazione</span><b>3–10% del capitale secondo il rischio</b></div>
        <div title="L'importo di ogni apertura è calcolato perché la liquidazione di tutte le posizioni resti almeno a questa distanza dall'entrata"><span>Limite long</span><b>liquidazione a ≥{Math.round(ex.liq_min_distance * 100)}% dall'entrata</b></div>
        <div><span>Chiusura</span><b style={{ fontSize: 14 }}>{ex.use_tp ? `TP +${ex.tp_pct}% · ` : "inversione 4 ore · "}{ex.use_initial_stop ? `SL −${ex.sl_pct}%` : ex.hold_losers ? "le posizioni in perdita non vengono mai chiuse" : `niente stop in perdita, dopo ${ex.recovery_hours} h esce se il recupero è sotto il ${Math.round(ex.recovery_min_prob * 100)}%`} · da +{ex.lock_trigger_pct}% protegge +{ex.lock_profit_pct}% (con leva)</b></div>
        <label className="switch" style={{ marginLeft: "auto" }}>
          <input type="checkbox" checked={ex.auto} onChange={toggleAuto} /> Apertura automatica
        </label>
      </div>
      {ex.positions.length === 0 ? (
        <p className="empty">Nessuna posizione aperta. L'Executor apre quando il Checker segnala un token valido{ex.auto ? "" : " (apertura automatica spenta)"}.</p>
      ) : (
        <div className="scroll">
          <table className="t">
            <thead><tr><th>Token</th><th>Lato</th><th className="n">Entrata</th><th className="n">Prezzo</th><th>Andamento con leva</th><th>Kronos 4 ore ora</th><th className="n">PnL</th><th /></tr></thead>
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
                      <span className="faint" style={{ fontSize: 11 }}>{signed(p.lev_move, 2)}% leva · {p.locked ? <><span className="long">SL protetto +{ex.lock_profit_pct}%</span> {price(p.sl)}</> : p.sl > 0 ? <>SL {price(p.sl)}</> : p.recovery_prob != null ? <span style={{ color: p.recovery_prob >= ex.recovery_min_prob ? "var(--long)" : "var(--warn)" }}>recupero {pct(p.recovery_prob)}</span> : "nessuno stop"} · liq {price(p.liq)}</span>
                    </td>
                    <td style={{ fontSize: 12 }}>
                      {p.p_dir_now != null ? (
                        <span style={{ color: p.p_dir_now >= 0.5 ? "var(--long)" : p.p_dir_now >= 0.5 - ex.exit_margin ? "var(--warn)" : "var(--short)" }}>
                          {pct(p.p_dir_now)} a favore
                        </span>
                      ) : "–"}
                      {p.reverse_count > 0 && <div className="faint">inversione {p.reverse_count}/{ex.exit_confirmations}</div>}
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
