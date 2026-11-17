import { type ReactElement, useEffect, useMemo, useRef, useState } from "react";
import type { Fan } from "../api";
import { price } from "../api";

type Bar = number[]; // [t, o, h, l, c, v]
type Level = { y: number; label: string; color: string };

const H = 360;
const PAD = { l: 8, r: 72, t: 12, b: 24 };

export function Chart({ bars, fan, levels, tfSeconds }: { bars: Bar[]; fan: Fan | null; levels: Level[]; tfSeconds: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(800);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(320, e.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const view = useMemo(() => {
    const shown = bars.slice(-Math.max(40, Math.min(110, Math.floor(w / 8))));
    const futN = fan ? fan.q50.length : 0;
    const total = shown.length + futN + 1;
    const plotW = w - PAD.l - PAD.r;
    const step = plotW / Math.max(total, 1);
    let lo = Infinity, hi = -Infinity;
    for (const b of shown) { lo = Math.min(lo, b[3]); hi = Math.max(hi, b[2]); }
    if (fan) for (let i = 0; i < futN; i++) { lo = Math.min(lo, fan.q10[i]); hi = Math.max(hi, fan.q90[i]); }
    for (const l of levels) {
      // only stretch the scale for levels reasonably close to price
      if (Number.isFinite(lo) && Math.abs(l.y / ((lo + hi) / 2) - 1) < 0.04) { lo = Math.min(lo, l.y); hi = Math.max(hi, l.y); }
    }
    if (!Number.isFinite(lo)) { lo = 0; hi = 1; }
    const padY = (hi - lo) * 0.08 || hi * 0.001 || 1;
    lo -= padY; hi += padY;
    const y = (v: number) => PAD.t + (1 - (v - lo) / (hi - lo)) * (H - PAD.t - PAD.b);
    const x = (i: number) => PAD.l + (i + 0.5) * step;
    return { shown, futN, step, lo, hi, y, x };
  }, [bars, fan, levels, w]);

  const { shown, futN, step, lo, hi, y, x } = view;
  const ticks = 5;
  const last = shown.length ? shown[shown.length - 1] : null;
  const nowX = shown.length ? x(shown.length - 1) : PAD.l;

  // forecast cone: starts at the last close and widens to the right
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
      </g>
    );
  }

  const fmtTime = (t: number) => {
    const d = new Date(t * 1000);
    return tfSeconds < 60
      ? d.toLocaleTimeString("it-IT", { hour: "2-digit", minute: "2-digit", second: "2-digit" })
      : d.toLocaleTimeString("it-IT", { hour: "2-digit", minute: "2-digit" });
  };

  return (
    <div ref={ref} className="chartwrap">
      <svg className="chart" width={w} height={H} role="img" aria-label="Grafico a candele con il cono di previsione di Kronos">
        {Array.from({ length: ticks }, (_, i) => {
          const v = lo + ((i + 0.5) / ticks) * (hi - lo);
          return (
            <g key={i}>
              <line className="grid" x1={PAD.l} x2={w - PAD.r} y1={y(v)} y2={y(v)} />
              <text x={w - PAD.r + 6} y={y(v) + 4}>{price(v)}</text>
            </g>
          );
        })}
        {/* future zone */}
        {futN > 0 && <rect x={nowX + step / 2} y={PAD.t} width={w - PAD.r - nowX - step / 2} height={H - PAD.t - PAD.b} fill="var(--kronos)" opacity={0.035} />}
        {shown.map((b, i) => {
          const up = b[4] >= b[1];
          const c = up ? "var(--long)" : "var(--short)";
          const bw = Math.max(1.5, step * 0.62);
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
        {shown.length > 0 && [0, Math.floor(shown.length / 2)].map((i) => (
          <text key={i} x={x(i)} y={H - 6}>{fmtTime(shown[i][0])}</text>
        ))}
        {fan && futN > 0 && (
          <text x={x(shown.length + futN - 1) - 30} y={H - 6} style={{ fill: "var(--kronos)" }}>+{fmtDur(futN * tfSeconds)}</text>
        )}
      </svg>
    </div>
  );
}

export function fmtDur(s: number) {
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(s % 3600 ? 1 : 0)} h`;
}
