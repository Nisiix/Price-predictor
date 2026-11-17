import { type ReactElement, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Fan } from "../api";
import { price } from "../api";

type Bar = number[]; // [t, o, h, l, c, v]
type Level = { y: number; label: string; color: string };

const H = 380;
const PAD = { l: 8, r: 72, t: 12, b: 24 };
const MIN_VISIBLE = 15;
const MAX_VISIBLE = 3000; // the whole stored history (1m: ~2 days, 5m: ~10 days)
const MAX_CANDLE = 16; // px: few bars stay thin instead of stretching into one giant candle

/**
 * Candles + Kronos cone. Zoom like an exchange chart: mouse wheel or the
 * +/− buttons change how many candles are visible, dragging pans back in time.
 * The cone is drawn only while the latest candle is in view.
 */
export function Chart({ bars, fan, levels, tfSeconds, resetKey, markStep, markLabel }: {
  bars: Bar[]; fan: Fan | null; levels: Level[]; tfSeconds: number; resetKey: string; markStep?: number; markLabel?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(800);
  const defaultVisible = useCallback((width: number) => Math.max(40, Math.min(140, Math.floor(width / 9))), []);
  const [visible, setVisible] = useState(() => defaultVisible(800));
  const [offset, setOffset] = useState(0); // candles hidden on the right (0 = live)
  const [custom, setCustom] = useState(false); // user zoomed or panned
  const drag = useRef<{ x: number; offset: number } | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(300, e.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // new token or timeframe: back to the default live view
  useEffect(() => {
    setVisible(defaultVisible(ref.current?.clientWidth ?? 800));
    setOffset(0);
    setCustom(false);
  }, [resetKey, defaultVisible]);

  const maxOffset = Math.max(0, bars.length - MIN_VISIBLE);
  const zoom = useCallback((factor: number) => {
    setCustom(true);
    setVisible((v) => Math.round(Math.min(MAX_VISIBLE, Math.max(MIN_VISIBLE, v * factor))));
  }, []);

  // wheel zoom needs a non-passive listener to stop the page from scrolling
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        e.preventDefault();
        setCustom(true);
        setOffset((o) => Math.max(0, Math.min(maxOffset, o + Math.round(e.deltaX / 20))));
        return;
      }
      e.preventDefault();
      zoom(e.deltaY > 0 ? 1.15 : 1 / 1.15);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [zoom, maxOffset]);

  const view = useMemo(() => {
    const end = bars.length - Math.min(offset, maxOffset);
    const shown = bars.slice(Math.max(0, end - visible), end);
    const live = offset === 0;
    const futN = live && fan ? fan.q50.length : 0;
    // slots are fixed by the zoom level, not by how many candles exist
    const slots = Math.max(visible, shown.length) + futN + 1;
    const plotW = w - PAD.l - PAD.r;
    const step = plotW / slots;
    const firstSlot = slots - futN - 1 - shown.length; // right-align candles next to the cone
    let lo = Infinity, hi = -Infinity;
    for (const b of shown) { lo = Math.min(lo, b[3]); hi = Math.max(hi, b[2]); }
    if (fan && futN) for (let i = 0; i < futN; i++) { lo = Math.min(lo, fan.q10[i]); hi = Math.max(hi, fan.q90[i]); }
    for (const l of levels) {
      if (Number.isFinite(lo) && Math.abs(l.y / ((lo + hi) / 2) - 1) < 0.04) { lo = Math.min(lo, l.y); hi = Math.max(hi, l.y); }
    }
    if (!Number.isFinite(lo)) { lo = 0; hi = 1; }
    const padY = (hi - lo) * 0.08 || hi * 0.001 || 1;
    lo -= padY; hi += padY;
    const y = (v: number) => PAD.t + (1 - (v - lo) / (hi - lo)) * (H - PAD.t - PAD.b);
    const x = (i: number) => PAD.l + (firstSlot + i + 0.5) * step;
    return { shown, futN, step, lo, hi, y, x, live };
  }, [bars, fan, levels, w, visible, offset, maxOffset]);

  const { shown, futN, step, lo, hi, y, x, live } = view;
  const last = shown.length ? shown[shown.length - 1] : null;
  const nowX = shown.length ? x(shown.length - 1) : PAD.l;
  const bw = Math.max(1.5, Math.min(MAX_CANDLE, step * 0.66));

  let cone: ReactElement | null = null;
  if (fan && last && futN) {
    const xs = [nowX, ...fan.q50.map((_, i) => x(shown.length + i))];
    const band = (a: number[], b: number[]) => {
      const top = [last[4], ...b].map((v, i) => `${xs[i]},${y(v)}`);
      const bot = [last[4], ...a].map((v, i) => `${xs[i]},${y(v)}`).reverse();
      return [...top, ...bot].join(" ");
    };
    const med = [last[4], ...fan.q50].map((v, i) => `${xs[i]},${y(v)}`).join(" ");
    cone = (
      <g>
        <polygon points={band(fan.q10, fan.q90)} fill="var(--kronos)" opacity={0.13} />
        <polygon points={band(fan.q25, fan.q75)} fill="var(--kronos)" opacity={0.22} />
        <polyline points={med} fill="none" stroke="var(--kronos)" strokeWidth={1.8} strokeDasharray="5 4" />
        <circle cx={xs[xs.length - 1]} cy={y(fan.q50[futN - 1])} r={3.5} fill="var(--kronos)" />
        {markStep && markStep <= futN && (
          <g>
            <line x1={xs[markStep]} x2={xs[markStep]} y1={PAD.t} y2={H - PAD.b} stroke="var(--kronos)" strokeWidth={1} strokeDasharray="3 3" opacity={0.8} />
            <circle cx={xs[markStep]} cy={y(fan.q50[markStep - 1])} r={5} fill="var(--ink)" stroke="var(--kronos)" strokeWidth={2} />
            <text x={xs[markStep] + 6} y={PAD.t + 12} style={{ fill: "var(--kronos)", fontWeight: 600 }}>{markLabel}: {price(fan.q50[markStep - 1])}</text>
          </g>
        )}
      </g>
    );
  }

  const fmtTime = (t: number) => {
    const d = new Date(t * 1000);
    const opts: Intl.DateTimeFormatOptions = tfSeconds < 60 ? { hour: "2-digit", minute: "2-digit", second: "2-digit" } : { hour: "2-digit", minute: "2-digit" };
    const sameDay = new Date().toDateString() === d.toDateString();
    return (sameDay ? "" : d.toLocaleDateString("it-IT", { day: "2-digit", month: "2-digit" }) + " ") + d.toLocaleTimeString("it-IT", opts);
  };
  const timeTicks = shown.length > 1 ? [0, Math.floor(shown.length / 3), Math.floor((2 * shown.length) / 3)] : shown.length ? [0] : [];

  const onPointerDown = (e: React.PointerEvent) => {
    drag.current = { x: e.clientX, offset };
    (e.target as Element).setPointerCapture?.(e.pointerId);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (!drag.current) return;
    const dBars = Math.round((e.clientX - drag.current.x) / Math.max(step, 1));
    if (dBars) setCustom(true);
    setOffset(Math.max(0, Math.min(maxOffset, drag.current.offset + dBars)));
  };
  const onPointerUp = () => { drag.current = null; };

  return (
    <div ref={ref} className="chartwrap">
      <div className="zoombar" role="group" aria-label="Zoom del grafico">
        <button className="ghost" onClick={() => zoom(1 / 1.4)} aria-label="Ingrandisci" title="Ingrandisci (rotella in su)">+</button>
        <button className="ghost" onClick={() => zoom(1.4)} aria-label="Riduci" title="Riduci (rotella in giù)">−</button>
        {custom && (
          <button className="ghost" onClick={() => { setOffset(0); setVisible(defaultVisible(w)); setCustom(false); }}>{live ? "Vista predefinita" : "Torna al prezzo attuale"}</button>
        )}
      </div>
      <svg
        className="chart" width={w} height={H} role="img" aria-label="Grafico a candele con il cono di previsione di Kronos"
        onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerLeave={onPointerUp}
        style={{ cursor: drag.current ? "grabbing" : "grab", touchAction: "pan-y" }}
      >
        {Array.from({ length: 5 }, (_, i) => {
          const v = lo + ((i + 0.5) / 5) * (hi - lo);
          return (
            <g key={i}>
              <line className="grid" x1={PAD.l} x2={w - PAD.r} y1={y(v)} y2={y(v)} />
              <text x={w - PAD.r + 6} y={y(v) + 4}>{price(v)}</text>
            </g>
          );
        })}
        {futN > 0 && <rect x={nowX + step / 2} y={PAD.t} width={Math.max(0, w - PAD.r - nowX - step / 2)} height={H - PAD.t - PAD.b} fill="var(--kronos)" opacity={0.035} />}
        {shown.map((b, i) => {
          const up = b[4] >= b[1];
          const c = up ? "var(--long)" : "var(--short)";
          const top = y(Math.max(b[1], b[4]));
          const h = Math.max(1, Math.abs(y(b[1]) - y(b[4])));
          return (
            <g key={b[0]}>
              <line x1={x(i)} x2={x(i)} y1={y(b[2])} y2={y(b[3])} stroke={c} strokeWidth={1} />
              <rect x={x(i) - bw / 2} y={top} width={bw} height={h} fill={c} opacity={up ? 0.9 : 0.85} rx={1} />
            </g>
          );
        })}
        {cone}
        {levels.map((l, i) =>
          l.y > lo && l.y < hi ? (
            <g key={i}>
              <line x1={PAD.l} x2={w - PAD.r} y1={y(l.y)} y2={y(l.y)} stroke={l.color} strokeDasharray="2 4" strokeWidth={1.2} />
              <text x={PAD.l + 4} y={y(l.y) - 4} style={{ fill: l.color }}>{l.label} {price(l.y)}</text>
            </g>
          ) : null,
        )}
        {last && (
          <g>
            <line x1={PAD.l} x2={w - PAD.r} y1={y(last[4])} y2={y(last[4])} stroke="var(--text)" strokeWidth={0.6} opacity={0.5} />
            <rect x={w - PAD.r + 2} y={y(last[4]) - 9} width={PAD.r - 4} height={18} rx={4} fill="var(--text)" />
            <text x={w - PAD.r + 6} y={y(last[4]) + 4} style={{ fill: "var(--ink)", fontWeight: 600 }}>{price(last[4])}</text>
          </g>
        )}
        {timeTicks.map((i) => <text key={i} x={x(i) - 14} y={H - 6}>{fmtTime(shown[i][0])}</text>)}
        {fan && futN > 0 && (
          <text x={x(shown.length + futN - 1) - 30} y={H - 6} style={{ fill: "var(--kronos)" }}>+{fmtDur(futN * tfSeconds)}</text>
        )}
        {shown.length < 3 && (
          <text x={PAD.l + 12} y={PAD.t + 24} style={{ fill: "var(--muted)", fontSize: 13 }}>Storico delle candele in caricamento…</text>
        )}
      </svg>
      <div className="note" style={{ marginTop: 4 }}>
        {shown.length} candele visibili · rotella o +/− per lo zoom, trascina per scorrere nel tempo
      </div>
    </div>
  );
}

export function fmtDur(s: number) {
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(s % 3600 ? 1 : 0)} h`;
}
