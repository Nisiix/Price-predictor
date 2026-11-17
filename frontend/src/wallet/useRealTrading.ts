import { useCallback, useEffect, useRef, useState } from "react";
import type { ExecutorView, Intent, Market } from "../api";
import { postJSON } from "../api";
import { OrderlyClient, roundPrice, roundTo } from "./orderly";
import { getPhantom, solBalance } from "./phantom";

export type Venue = { rows: any[]; free_collateral: number; total_collateral_value: number; margin_ratio: number } | null;

// TP/SL for real limit orders, attached on the venue once the order fills
const PENDING_KEY = "pp.pendingTpSl.v1";
type PendingTpSl = Record<string, { long: boolean; tp?: number; sl?: number; ts: number }>;
function readPending(): PendingTpSl {
  try { return JSON.parse(localStorage.getItem(PENDING_KEY) || "{}"); } catch { return {}; }
}
function writePending(p: PendingTpSl) {
  try { localStorage.setItem(PENDING_KEY, JSON.stringify(p)); } catch { /* ignore */ }
}

/**
 * Wallet + delegated-key lifecycle and execution of the Executor's real intents.
 * Real orders are only sent when the user switched on automatic real trading
 * (executor.real_auto) or presses a button in the order ticket.
 */
export function useRealTrading(executor: ExecutorView | null, markets: Market[]) {
  const phantom = getPhantom();
  const [address, setAddress] = useState<string | null>(null);
  const [client, setClient] = useState<OrderlyClient | null>(null);
  const [venue, setVenue] = useState<Venue>(null);
  const [orders, setOrders] = useState<any[]>([]);
  const [sol, setSol] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const handled = useRef(new Set<string>());
  const marketsRef = useRef(markets);
  marketsRef.current = markets;

  const report = useCallback((addr: string | null, ready: boolean) => postJSON("/api/wallet", { address: addr, real_ready: ready }).catch(() => {}), []);

  const attach = useCallback(
    (addr: string | null) => {
      setAddress(addr);
      const c = addr ? new OrderlyClient(addr) : null;
      setClient(c);
      setVenue(null);
      report(addr, !!c?.ready);
      if (addr) solBalance(addr).then(setSol);
    },
    [report],
  );

  useEffect(() => {
    if (!phantom) return;
    phantom.connect({ onlyIfTrusted: true }).then((r) => attach(r.publicKey.toBase58())).catch(() => {});
    const onAcc = (pk: any) => attach(pk ? pk.toBase58() : null);
    const onDisc = () => attach(null);
    phantom.on("accountChanged", onAcc);
    phantom.on("disconnect", onDisc);
    return () => {
      phantom.removeListener?.("accountChanged", onAcc);
      phantom.removeListener?.("disconnect", onDisc);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const connect = async () => {
    setError(null);
    if (!phantom) {
      window.open("https://phantom.com/download", "_blank", "noopener");
      return;
    }
    try {
      const r = await phantom.connect();
      attach(r.publicKey.toBase58());
    } catch (e: any) {
      setError(e?.message ?? "Connessione rifiutata in Phantom");
    }
  };

  const disconnect = async () => {
    await postJSON("/api/executor", { real_auto: false }).catch(() => {});
    await phantom?.disconnect().catch(() => {});
    attach(null);
  };

  const enable = async () => {
    if (!phantom || !client) return;
    setBusy(true);
    setError(null);
    try {
      await client.enable(phantom);
      setClient(Object.assign(Object.create(Object.getPrototypeOf(client)), client));
      await report(client.address, true);
    } catch (e: any) {
      setError(`Attivazione non riuscita: ${e?.message ?? e}`);
    } finally {
      setBusy(false);
    }
  };

  const revoke = async () => {
    client?.forget();
    await postJSON("/api/executor", { real_auto: false }).catch(() => {});
    if (client) await report(client.address, false);
    setClient(client ? new OrderlyClient(client.address) : null);
    setVenue(null);
  };

  // venue snapshot every 8s, mirrored to the Executor for reconciliation
  useEffect(() => {
    if (!client?.ready) return;
    let alive = true;
    const tick = async () => {
      try {
        const p = await client.positions();
        if (!alive) return;
        setVenue(p);
        postJSON("/api/real/positions", { rows: p.rows }).catch(() => {});
        // attach TP/SL to limit orders that have filled
        const pend = readPending();
        let changed = false;
        for (const [sym, t] of Object.entries(pend)) {
          const row = p.rows.find((r) => r.symbol === sym && Math.abs(r.position_qty) > 0);
          if (row) {
            try {
              await client.positionTpSl(sym, row.position_qty > 0, t.tp, t.sl);
            } catch (e: any) {
              setError(`TP/SL non impostati su ${sym.split("_")[1]}: ${e?.message ?? e}`);
            }
            delete pend[sym];
            changed = true;
          } else if (Date.now() - t.ts > 7 * 86400_000) {
            delete pend[sym];
            changed = true;
          }
        }
        if (changed) writePending(pend);
        const o = await client.openOrders();
        if (alive) setOrders(o.rows ?? []);
      } catch (e: any) {
        if (alive) setError(`Raydium Perps: ${e?.message ?? e}`);
      }
    };
    tick();
    const id = window.setInterval(tick, 8000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [client]);

  // ---------------------------------------------------------------- execution
  const openReal = useCallback(
    async (o: { symbol: string; side: "long" | "short"; leverage: number; margin: number; type: "MARKET" | "LIMIT"; limit?: number; tp?: number; sl?: number }) => {
      if (!client?.ready) throw new Error("trading reale non attivato");
      const m = marketsRef.current.find((x) => x.symbol === o.symbol);
      if (!m) throw new Error("mercato sconosciuto");
      const ref = o.type === "LIMIT" && o.limit ? o.limit : m.mark_price;
      const qty = roundTo((o.margin * o.leverage) / ref, m.base_tick);
      if (qty < m.base_min || qty * ref < m.min_notional) throw new Error(`quantità sotto il minimo del mercato (${m.min_notional} USDC)`);
      await client.setLeverage(o.leverage);
      const res = await client.order({
        symbol: o.symbol, side: o.side === "long" ? "BUY" : "SELL", type: o.type, qty,
        price: o.type === "LIMIT" && o.limit ? roundPrice(o.limit, m.quote_tick) : undefined,
      });
      // From here the order exists on the venue: never throw, so callers always track the position.
      const tp = o.tp ? roundPrice(o.tp, m.quote_tick) : undefined;
      const sl = o.sl ? roundPrice(o.sl, m.quote_tick) : undefined;
      let tpslError: string | null = null;
      if (o.type === "MARKET" && (tp || sl)) {
        await new Promise((r) => setTimeout(r, 1200));
        try {
          await client.positionTpSl(o.symbol, o.side === "long", tp, sl);
        } catch (e: any) {
          tpslError = e?.message ?? String(e);
          setError(`Posizione aperta ma TP/SL non impostati sul mercato (${tpslError}): l'Executor la sorveglia comunque, verifica su perps.raydium.io`);
        }
      } else if (o.type === "LIMIT" && (tp || sl)) {
        writePending({ ...readPending(), [o.symbol]: { long: o.side === "long", tp, sl, ts: Date.now() } });
      }
      let fill = ref;
      try {
        const p = await client.positions();
        const row = p.rows.find((r) => r.symbol === o.symbol);
        if (row?.average_open_price) fill = row.average_open_price;
        setVenue(p);
      } catch {
        /* keep reference price */
      }
      return { orderId: String(res.order_id), qty, fill, tpslError };
    },
    [client],
  );

  const closeReal = useCallback(
    async (symbol: string) => {
      if (!client?.ready) throw new Error("trading reale non attivato");
      const m = marketsRef.current.find((x) => x.symbol === symbol);
      await client.closePosition(symbol);
      return m?.mark_price;
    },
    [client],
  );

  useEffect(() => {
    if (!executor?.real_auto || !client?.ready) return;
    for (const it of executor.intents as Intent[]) {
      if (handled.current.has(it.id) || (it as any).claimed) continue;
      handled.current.add(it.id);
      (async () => {
        // only one dashboard tab may execute an intent
        const claim = await postJSON<{ ok: boolean }>("/api/real/claim", { intent_id: it.id }).catch(() => ({ ok: false }));
        if (!claim.ok) return;
        let sent = false;
        try {
          if (it.action === "open") {
            const r = await openReal({ symbol: it.symbol, side: it.side, leverage: it.leverage!, margin: it.margin!, type: "MARKET", tp: it.tp, sl: it.sl });
            sent = true;
            await postJSON("/api/real/ack", { intent_id: it.id, ok: true, fill_price: r.fill, qty: r.qty, order_id: r.orderId });
          } else {
            const px = await closeReal(it.symbol);
            await postJSON("/api/real/ack", { intent_id: it.id, ok: true, fill_price: px });
          }
        } catch (e: any) {
          if (sent) return; // the order went through; only the ack failed, the venue snapshot reconciles it
          await postJSON("/api/real/ack", { intent_id: it.id, ok: false, error: e?.message ?? String(e) }).catch(() => {});
        }
      })();
    }
  }, [executor?.intents, executor?.real_auto, client, openReal, closeReal]);

  return {
    hasPhantom: !!phantom, address, sol, ready: !!client?.ready, venue, orders, busy, error, setError,
    connect, disconnect, enable, revoke, openReal, closeReal,
    cancelOrder: async (symbol: string, id: number) => {
      await client?.cancelOrder(symbol, id);
      setOrders((os) => os.filter((o) => o.order_id !== id));
      const pend = readPending();
      if (pend[symbol]) { delete pend[symbol]; writePending(pend); }
    },
  };
}
