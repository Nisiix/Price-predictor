import { useEffect, useRef, useState } from "react";

export type Market = {
  symbol: string; base: string; name: string; rank: number; max_leverage: number; imr: number; mmr: number;
  base_tick: number; quote_tick: number; base_min: number; min_notional: number; mark_price: number;
  change_24h: number; volume_24h: number; funding_rate: number; logo: string;
};
export type HorizonStat = {
  p_up: number; exp_ret: number; q10: number; q50: number; q90: number; price_q10: number; price_q50: number;
  price_q90: number; tf: string; step: number; path_step?: number; seconds: number; source: string; age: number; experimental: boolean;
  accuracy?: { n: number; hit_rate: number; brier: number; coverage: number } | null;
};
export type Fan = { t: number[]; q10: number[]; q25: number[]; q50: number[]; q75: number[]; q90: number[]; anchor: number; bar_t: number; ts: number; tf_seconds: number; source: string; age: number };
export type TradeOption = { side: "long" | "short"; leverage: number; p_tp: number; p_sl: number; p_liq: number; p_open: number; ev: number; levels: { tp: number; sl: number; liq: number } };
export type Snapshot = {
  symbol: string; horizons: Record<string, HorizonStat | null>; fans: Record<string, Fan>;
  trade: { best: TradeOption; options: TradeOption[] } | null; checker?: CheckerRow & { indicators: any; analogs: any; risk_parts: Record<string, number>; options: TradeOption[] };
};
export type HorizonPick = { h: string; side: "long" | "short"; conf: number; risk: number; score: number; age: number };
export type CheckerRow = {
  symbol: string; base: string; name: string; rank: number; price: number; change_24h: number; side: "long" | "short";
  leverage: number; kronos_p_tp: number; kronos_p_sl: number; kronos_p_liq: number; ev: number; combined_prob: number;
  extra_risk: number; score: number; eligible: boolean; reasons: string[]; source: string; max_leverage: number;
  rsi: number; regime: string; flags: string[]; ind_score: number; analog_win: number | null; analog_n: number;
  p_up_4h: number; conf_4h: number; range_pos: number;
  horizons: HorizonPick[]; best_h: HorizonPick | null;
  amplitude: number; roof: number; breakout_price: number; chase_price: number; breakout: boolean;
};
export type Position = {
  id: string; symbol: string; base: string; side: "long" | "short"; leverage: number; margin: number; entry: number;
  qty: number; notional: number; tp: number; sl: number; liq: number; mode: string; origin: string; opened_ts: number;
  fees: number; funding: number; last_price: number; status: string; pnl: number; roe: number; lev_move: number; signal: any;
  p_dir_now: number | null; reverse_count: number; locked: boolean; recovery_prob: number | null;
  p_up_h: Record<string, number>; bearish_all: boolean;
};
export type PaperCapital = {
  start: number; equity: number; pnl: number; pnl_pct: number; realized: number; unrealized: number; exposure: number;
  margin_used: number; open: number; since: number; min_liq_distance: number | null; history: [number, number][];
};
export type ExecutorView = {
  mode: "paper" | "real"; auto: boolean; wallet: string | null; real_ready: boolean; real_auto: boolean; balance: number;
  equity: number; positions: Position[]; pending: any[]; intents: Intent[]; max_positions: number; margin_usdc: number; risk_tiers: [number, number][]; risk_tier_above: number;
  tp_pct: number; sl_pct: number; use_tp: boolean; exit_margin: number; exit_confirmations: number;
  min_conf: number; min_move: number; roof_bars: number; breakout_buffer: number; breakout_max_chase: number;
  be_exit: boolean; be_exit_max_up: number;
  lock_atr_mult: number; lock_profit_pct: number; max_token_exposure: number; max_total_exposure: number;
  use_initial_stop: boolean; recovery_hours: number; recovery_min_prob: number; liq_min_distance: number; equity_now: number; hold_losers: boolean; longs_only: boolean;
  paper: PaperCapital;
};
export type Intent = { id: string; ts: number; action: "open" | "close" | "update_sl"; symbol: string; base?: string; side: "long" | "short"; leverage?: number; margin?: number; qty: number; price?: number; tp?: number | null; sl?: number | null; base_tick?: number; quote_tick?: number; position_id?: string; reason?: string };
export type Headline = { id: string; title: string; source: string; url: string; ts: number; categories: string[]; calming: boolean; severity: number; analog: { date: string; title: string; btc_move: number } | null };
export type AnalystView = { risk: number; components: Record<string, number>; fear_greed: { value: number; label: string } | null; status: string; close_threshold: number; pause_threshold: number; headlines: Headline[] };
export type Group = { key: string; n: number; wins: number; win_rate: number; pnl: number; avg_roe: number; profit_factor: number | null };
export type Report = { summary: Group; groups: Record<string, Group[]>; insights: string[]; calibration: { label: string; n: number; hit_rate: number; brier: number }[]; recent: any[] };
export type LogEntry = { agent: string; text: string; level: string; ts: number; url?: string };
export type System = {
  feed: string; last_tick: number; markets: number; uptime: number; active?: string[];
  kronos: { model: string; ready: boolean; error: string | null; device: string; latency: number; busy: string | null; inferences: number; samples: number; scanned: number; decision_model?: string; decision_ready?: boolean; model_key?: string; switching?: string | null; models?: string[] };
  checker: { paused: boolean; reason: string; ranked: number; signals: number }; analyst: { risk: number; status: string }; executor: { mode: string };
};

export type BoardRow = { p_up: number; dir: "up" | "down"; conf: number; exp_ret: number; age: number; source: string };
export type Board = { h: string; tf: string; rows: Record<string, BoardRow> };

export type LiveState = {
  connected: boolean; system: System | null; markets: Market[]; prices: Record<string, { p: number; c: number }>;
  focus: Record<string, Snapshot>; ranking: CheckerRow[]; paused: boolean; pauseReason: string;
  executor: ExecutorView | null; analyst: AnalystView | null; report: Report | null; log: LogEntry[];
  intents: Intent[]; settings: any; boards: Record<string, Board>;
};

const initial: LiveState = {
  connected: false, system: null, markets: [], prices: {}, focus: {}, ranking: [], paused: false, pauseReason: "",
  executor: null, analyst: null, report: null, log: [], intents: [], settings: null, boards: {},
};

export async function api<T = any>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...init });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
export const postJSON = <T = any>(path: string, body: unknown) => api<T>(path, { method: "POST", body: JSON.stringify(body) });

export function useLive(focusSymbol: string | null, horizon: string) {
  const [state, setState] = useState<LiveState>(initial);
  const wsRef = useRef<WebSocket | null>(null);
  const focusRef = useRef(focusSymbol);
  focusRef.current = focusSymbol;
  const horizonRef = useRef(horizon);
  horizonRef.current = horizon;

  useEffect(() => {
    let alive = true;
    let retry: number | undefined;
    api("/api/state").then((s) => {
      if (!alive) return;
      setState((st) => ({
        ...st, system: s.system, markets: s.markets, executor: s.executor, analyst: s.analyst, ranking: s.ranking,
        log: s.log.slice().reverse(), settings: s.settings,
      }));
    }).catch(() => {});
    const refreshMarkets = window.setInterval(() => {
      api("/api/state").then((s) => alive && setState((st) => ({ ...st, markets: s.markets, settings: s.settings }))).catch(() => {});
    }, 60_000);

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${location.host}/ws`);
      wsRef.current = ws;
      ws.onopen = () => {
        setState((s) => ({ ...s, connected: true }));
        if (focusRef.current) ws.send(JSON.stringify({ type: "focus", symbol: focusRef.current }));
        ws.send(JSON.stringify({ type: "horizon", h: horizonRef.current }));
      };
      ws.onclose = () => {
        setState((s) => ({ ...s, connected: false }));
        if (alive) retry = window.setTimeout(connect, 2000);
      };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        setState((s) => reduce(s, msg));
      };
    };
    connect();
    const ping = window.setInterval(() => {
      const ws = wsRef.current;
      if (ws?.readyState === 1 && focusRef.current) ws.send(JSON.stringify({ type: "ping", symbol: focusRef.current }));
    }, 20_000);
    return () => {
      alive = false;
      window.clearTimeout(retry);
      window.clearInterval(ping);
      window.clearInterval(refreshMarkets);
      wsRef.current?.close();
    };
  }, []);

  useEffect(() => {
    const ws = wsRef.current;
    if (focusSymbol && ws?.readyState === 1) ws.send(JSON.stringify({ type: "focus", symbol: focusSymbol }));
  }, [focusSymbol]);

  useEffect(() => {
    const ws = wsRef.current;
    if (ws?.readyState === 1) ws.send(JSON.stringify({ type: "horizon", h: horizon }));
    // the board for a new horizon fills in progressively; show what is already cached now
    api<Board>(`/api/board?h=${horizon}`).then((b) => setState((s) => ({ ...s, boards: { ...s.boards, [b.h]: b } }))).catch(() => {});
  }, [horizon]);

  return state;
}

function reduce(s: LiveState, msg: { type: string; data: any }): LiveState {
  const d = msg.data;
  switch (msg.type) {
    case "tick":
      return { ...s, prices: d.prices, focus: { ...s.focus, ...d.focus }, system: d.system, boards: { ...s.boards, ...(d.boards ?? {}) } };
    case "ranking":
      return { ...s, ranking: d.rows, paused: d.paused, pauseReason: d.pause_reason };
    case "executor":
      return { ...s, executor: d, intents: d.intents };
    case "analyst":
      return { ...s, analyst: d };
    case "report":
      return { ...s, report: d };
    case "log":
      return { ...s, log: [d, ...s.log].slice(0, 200) };
    case "real_intent":
      return { ...s, intents: [...s.intents.filter((i) => i.id !== d.id), d] };
    default:
      return s;
  }
}

// ------------------------------------------------------------------ format
export const pct = (x: number | null | undefined, digits = 0) => (x == null || Number.isNaN(x) ? "–" : `${(x * 100).toFixed(digits)}%`);
export const signed = (x: number, digits = 2) => `${x >= 0 ? "+" : ""}${x.toFixed(digits)}`;
export function price(x: number | null | undefined) {
  if (x == null || !Number.isFinite(x)) return "–";
  const a = Math.abs(x);
  const d = a >= 1000 ? 2 : a >= 10 ? 3 : a >= 1 ? 4 : a >= 0.01 ? 5 : 7;
  return x.toLocaleString("it-IT", { minimumFractionDigits: d, maximumFractionDigits: d });
}
export const usd = (x: number) => x.toLocaleString("it-IT", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " USDC";
export function ago(ts: number) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.round(s)}s fa`;
  if (s < 3600) return `${Math.round(s / 60)} min fa`;
  return `${Math.round(s / 3600)} h fa`;
}
