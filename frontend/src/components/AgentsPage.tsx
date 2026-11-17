import { useMemo, useState } from "react";
import type { AnalystView, Headline, LiveState, LogEntry, Position, Report, Snapshot } from "../api";
import { ago, pct, signed } from "../api";
import { CheckerTable, HORIZON_CHOICES, Positions, Verdict } from "./Panels";
import "./agents.css";

/*
  Agenti page. The four agents work as a chain (Analyst -> Checker -> Executor -> Controller):
  the strip at the top follows that chain and links to each section, the sections below are
  ordered by what needs attention first: open positions, then candidates, then context and results.
*/

type Props = {
  live: LiveState;
  focus: string | null;
  onFocus: (s: string) => void;
  snap: Snapshot | undefined;
  onToast: (s: string) => void;
  realClose: (p: Position) => Promise<void>;
};

export function AgentsPage({ live, focus, onFocus, snap, onToast, realClose }: Props) {
  return (
    <main className="ag">
      <Flow live={live} />
      <div className="ag-row ag-now">
        <section id="ag-executor" className="ag-sec">
          <Positions ex={live.executor} onToast={onToast} realClose={realClose} />
        </section>
        <section className="ag-sec">
          <Activity log={live.log} />
        </section>
      </div>
      <section id="ag-checker" className="ag-sec ag-stack">
        <CheckerTable ranking={live.ranking} focus={focus} onFocus={onFocus} paused={live.paused} pauseReason={live.pauseReason} />
        <Verdict snap={snap} />
      </section>
      <div className="ag-row ag-context">
        <section id="ag-analyst" className="ag-sec"><Analyst a={live.analyst} /></section>
        <section id="ag-controller" className="ag-sec"><Controller r={live.report} /></section>
      </div>
    </main>
  );
}

// ------------------------------------------------------------------ chain of agents
function Flow({ live }: { live: LiveState }) {
  const a = live.analyst;
  const ex = live.executor;
  const r = live.report?.summary;
  const ready = live.ranking.filter((x) => x.eligible).length;
  const openPnl = ex?.positions.reduce((s, p) => s + p.pnl, 0) ?? 0;
  const risk = a?.risk ?? 0;

  const steps: { id: string; name: string; tone: string; main: string; sub: string }[] = [
    {
      id: "ag-analyst", name: "Analyst",
      tone: !a ? "idle" : risk >= (a.close_threshold ?? 100) ? "bad" : risk >= (a.pause_threshold ?? 100) ? "warn" : "ok",
      main: a ? `Rischio macro ${risk.toFixed(0)} su 100` : "Raccolgo le notizie",
      sub: live.paused ? "Aperture sospese" : a ? "Aperture consentite" : "",
    },
    {
      id: "ag-checker", name: "Checker",
      tone: !live.ranking.length ? "idle" : ready ? "ok" : "warn",
      main: live.ranking.length ? `${ready} ${ready === 1 ? "segnale pronto" : "segnali pronti"}` : "Attendo Kronos",
      sub: live.ranking.length ? `su ${live.ranking.length} token valutati` : "",
    },
    {
      id: "ag-executor", name: "Executor",
      tone: !ex ? "idle" : ex.positions.length ? (openPnl >= 0 ? "ok" : "neg") : "idle",
      main: ex ? `${ex.positions.length} di ${ex.max_positions} posizioni` : "Connessione…",
      sub: ex ? (ex.positions.length ? `${signed(openPnl)} USDC aperti` : ex.auto ? "in attesa di segnali" : "apertura automatica spenta") : "",
    },
    {
      id: "ag-controller", name: "Controller",
      tone: !r || !r.n ? "idle" : r.pnl >= 0 ? "ok" : "neg",
      main: r && r.n ? `${r.n} operazioni chiuse` : "Nessuna operazione chiusa",
      sub: r && r.n ? `${pct(r.win_rate)} in profitto · ${signed(r.pnl)} USDC` : "",
    },
  ];

  const go = (id: string) => document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });

  return (
    <nav className="flow" aria-label="Catena degli agenti">
      <ol>
        {steps.map((s) => (
          <li key={s.id}>
            <button className={`station ${s.tone}`} onClick={() => go(s.id)}>
              <span className="nm"><span className={`dot ${s.tone === "neg" ? "bad" : s.tone}`} />{s.name}</span>
              <span className="mn">{s.main}</span>
              <span className="sb">{s.sub}</span>
            </button>
          </li>
        ))}
      </ol>
    </nav>
  );
}

// ------------------------------------------------------------------ activity
const AGENTS = ["Executor", "Checker", "Analyst", "Controller", "Sistema"];
function Activity({ log }: { log: LogEntry[] }) {
  const [who, setWho] = useState<string>("Tutti");
  const [all, setAll] = useState(false);
  const rows = who === "Tutti" ? log : log.filter((l) => l.agent === who);
  const shown = all ? rows : rows.slice(0, 8);
  const present = AGENTS.filter((a) => log.some((l) => l.agent === a));
  return (
    <div className="panel">
      <h2>Attività recente</h2>
      {present.length > 1 && (
        <div className="tabs" role="group" aria-label="Filtra per agente">
          {["Tutti", ...present].map((a) => <button key={a} aria-pressed={who === a} onClick={() => setWho(a)}>{a}</button>)}
        </div>
      )}
      <ul className="feed">
        {shown.length === 0 && <li className="empty">Nessuna attività ancora.</li>}
        {shown.map((l, i) => (
          <li key={`${l.ts}-${i}`} className={l.level}>
            <time>{new Date(l.ts * 1000).toLocaleTimeString("it-IT", { hour: "2-digit", minute: "2-digit" })}</time>
            <span>
              {who === "Tutti" && <b>{l.agent}</b>}
              {l.url ? <a href={l.url} target="_blank" rel="noreferrer">{l.text}</a> : l.text}
            </span>
          </li>
        ))}
      </ul>
      {rows.length > 8 && <button className="more" onClick={() => setAll(!all)}>{all ? "Mostra solo le più recenti" : `Mostra tutte (${rows.length})`}</button>}
    </div>
  );
}

// ------------------------------------------------------------------ analyst
const CATS: Record<string, string> = {
  war: "guerra", macro_crash: "crollo macro", banking_crisis: "crisi bancaria", fed_hawkish: "Fed restrittiva", trade_war: "dazi/sanzioni",
  regulation_ban: "divieti", regulation_enforcement: "azioni legali", exchange_collapse: "crisi exchange", hack: "hack",
  stablecoin_depeg: "depeg stablecoin", lender_insolvency: "insolvenza", etf: "ETF",
};
// the same story often arrives from two feeds, once with " - Source" appended to the title
const storyKey = (t: string) => t.replace(/\s+-\s+[^-]+$/, "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

function Analyst({ a }: { a: AnalystView | null }) {
  const [all, setAll] = useState(false);
  const news = useMemo(() => {
    const seen = new Set<string>();
    const out: Headline[] = [];
    for (const h of a?.headlines ?? []) {
      const k = storyKey(h.title);
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(h);
    }
    return out;
  }, [a]);
  if (!a) return <div className="panel"><h2>Analyst</h2><p className="empty">Raccolta notizie in corso…</p></div>;
  const relevant = news.filter((h) => h.severity > 0);
  const shown = all ? news : relevant.slice(0, 5);
  const comp = a.components;
  return (
    <div className="panel">
      <h2>Analyst <small>{a.status}</small></h2>
      <div className="gauge">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <span className="big">{a.risk.toFixed(0)}<span className="muted" style={{ fontSize: 14 }}> / 100</span></span>
          {a.fear_greed && <span className="muted" style={{ fontSize: 12.5 }}>Fear &amp; Greed {a.fear_greed.value} ({a.fear_greed.label})</span>}
        </div>
        <div className="scale">
          <div className="needle" style={{ left: `${a.risk}%` }} />
          <span className="th" style={{ left: `${a.pause_threshold}%` }}>pausa</span>
          <span className="th" style={{ left: `${a.close_threshold}%` }}>chiude tutto</span>
        </div>
      </div>
      <dl className="parts">
        <div><dt>Notizie</dt><dd>{comp.notizie?.toFixed(0) ?? 0}</dd></div>
        <div><dt>Shock di mercato</dt><dd>{comp.shock_mercato?.toFixed(0) ?? 0}</dd></div>
        <div><dt>Sentiment</dt><dd>{comp.sentiment?.toFixed(0) ?? 0}</dd></div>
        {comp.btc_5m != null && <div><dt>BTC/ETH/SOL 5 min</dt><dd>{signed(comp.btc_5m, 2)}%</dd></div>}
      </dl>
      <h3 className="sub">{all ? `Tutte le notizie delle ultime 12 ore` : relevant.length ? "Notizie che pesano sul rischio" : "Nessuna notizia pesa sul rischio adesso"}</h3>
      <ul className="news">
        {shown.map((h) => (
          <li key={h.id} title={h.analog && h.severity > 0 ? `Precedente: ${h.analog.title} (${h.analog.date.slice(0, 4)}, BTC ${signed(h.analog.btc_move * 100, 0)}%)` : undefined}>
            <span className="sev" style={{ background: h.severity >= 0.18 ? "var(--danger)" : h.severity >= 0.08 ? "var(--warn)" : h.severity > 0 ? "var(--muted)" : "var(--line)" }} />
            <a href={h.url} target="_blank" rel="noreferrer">{h.title}</a>
            <div className="meta">
              <span>{h.source}</span><span>{ago(h.ts)}</span>
              {h.categories.length > 0 && <span>{h.categories.map((c) => CATS[c] ?? c).join(", ")}{h.calming ? " (distensiva)" : ""}</span>}
            </div>
          </li>
        ))}
      </ul>
      {news.length > shown.length || all ? (
        <button className="more" onClick={() => setAll(!all)}>{all ? "Mostra solo quelle che pesano" : `Mostra tutte le notizie (${news.length})`}</button>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------ controller
const GROUPS: [string, string][] = [["per_token", "Token"], ["per_pattern", "Pattern"], ["per_prob_kronos", "Prob. Kronos"], ["per_rischio", "Rischio"], ["per_motivo_chiusura", "Chiusure"], ["per_leva", "Leva"]];
const LIVE_HORIZONS = new Set(HORIZON_CHOICES.map(([k]) => k));

function Controller({ r }: { r: Report | null }) {
  const [tab, setTab] = useState("per_token");
  if (!r || !r.summary.n) {
    return <div className="panel"><h2>Controller</h2><p className="empty">Il bilancio compare dopo la prima operazione chiusa.</p></div>;
  }
  const s = r.summary;
  const g = r.groups[tab] ?? [];
  // only the horizons Kronos still forecasts; older sub-minute ones are history
  const cal = r.calibration.filter((c) => LIVE_HORIZONS.has(c.label));
  return (
    <div className="panel">
      <h2>Controller <small><a href="/api/report.md" download="report-controller.md">Scarica il report</a></small></h2>
      <div className="stats">
        <div><span>Operazioni</span><b>{s.n}</b></div>
        <div><span>In profitto</span><b>{pct(s.win_rate)}</b></div>
        <div><span>PnL</span><b className={s.pnl >= 0 ? "long" : "short"}>{signed(s.pnl)}</b></div>
        <div><span>ROE medio</span><b>{signed(s.avg_roe, 1)}%</b></div>
      </div>
      {r.insights.length > 0 && <ul className="insights">{r.insights.map((i) => <li key={i}>{i}</li>)}</ul>}
      <h3 className="sub">Risultati per</h3>
      <div className="tabs" role="group" aria-label="Raggruppa i risultati">{GROUPS.map(([k, l]) => <button key={k} aria-pressed={tab === k} onClick={() => setTab(k)}>{l}</button>)}</div>
      {g.length === 0 ? <p className="empty">Nessun dato per questo raggruppamento.</p> : (
        <table className="t">
          <thead><tr><th>Gruppo</th><th className="n">Op.</th><th className="n">In profitto</th><th className="n">PnL</th></tr></thead>
          <tbody>{g.slice(0, 6).map((x) => (
            <tr key={x.key}><td>{x.key}</td><td className="n">{x.n}</td><td className="n">{pct(x.win_rate)}</td><td className={`n ${x.pnl >= 0 ? "long" : "short"}`}>{signed(x.pnl)}</td></tr>
          ))}</tbody>
        </table>
      )}
      {cal.length > 0 && (
        <>
          <h3 className="sub">Affidabilità di Kronos <span className="muted">direzione corretta a scadenza</span></h3>
          <table className="t"><tbody>
            {cal.map((c) => (
              <tr key={c.label}><td>{c.label}</td><td className="n">{c.n} previsioni</td><td className={`n ${c.hit_rate >= 0.5 ? "long" : "short"}`}>{pct(c.hit_rate, 1)}</td></tr>
            ))}
          </tbody></table>
        </>
      )}
    </div>
  );
}
