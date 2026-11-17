import { useEffect, useState } from "react";
import type { ExecutorView, Market, Snapshot } from "../api";
import { postJSON, price, signed } from "../api";
import type { useRealTrading } from "../wallet/useRealTrading";

type Real = ReturnType<typeof useRealTrading>;

export function Ticket({ m, snap, ex, real, onToast }: { m: Market | undefined; snap: Snapshot | undefined; ex: ExecutorView | null; real: Real; onToast: (s: string) => void }) {
  const [side, setSide] = useState<"long" | "short">("long");
  const [lev, setLev] = useState(10);
  const [margin, setMargin] = useState(100);
  const [type, setType] = useState<"market" | "limit">("market");
  const [limit, setLimit] = useState("");
  const [tp, setTp] = useState("");
  const [sl, setSl] = useState("");
  const [sending, setSending] = useState(false);
  const live = real.ready; // real orders when the delegated key is active

  // follow the Checker's suggestion when switching token
  useEffect(() => {
    const best = snap?.checker;
    if (best) {
      setSide(best.side);
      setLev(best.leverage);
    }
    setLimit("");
    setTp("");
    setSl("");
  }, [m?.symbol]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!m) return null;
  const maxLev = Math.min(20, m.max_leverage);
  const ref = type === "limit" && Number(limit) > 0 ? Number(limit) : m.mark_price;
  const dir = side === "long" ? 1 : -1;
  const defTp = ref * (1 + (dir * (ex?.tp_pct ?? 5)) / 100 / lev);
  const defSl = ref * (1 - (dir * (ex?.sl_pct ?? 2)) / 100 / lev);
  const liq = ref * (1 - dir * Math.max(1 / lev - m.mmr, 1e-6));
  const tpV = Number(tp) > 0 ? Number(tp) : defTp;
  const slV = Number(sl) > 0 ? Number(sl) : defSl;
  const option = snap?.trade?.options.find((o) => o.side === side && o.leverage === lev);

  const submit = async () => {
    setSending(true);
    try {
      if (live) {
        const ok = window.confirm(
          `Ordine REALE su Raydium Perps\n\n${side === "long" ? "LONG" : "SHORT"} ${m.base} ${lev}x, margine ${margin} USDC (esposizione ${margin * lev} USDC)\n` +
            `${type === "market" ? "a mercato" : `limite ${price(Number(limit))}`} · TP ${price(tpV)} · SL ${price(slV)}\n\nConfermi?`,
        );
        if (!ok) return;
        await real.openReal({ symbol: m.symbol, side, leverage: lev, margin, type: type === "market" ? "MARKET" : "LIMIT", limit: Number(limit) || undefined, tp: tpV, sl: slV });
        onToast(type === "market" ? `Posizione reale aperta su ${m.base}` : `Ordine limite reale inviato su ${m.base}: TP e SL vengono piazzati appena viene eseguito`);
      } else {
        const r = await postJSON("/api/order", { symbol: m.symbol, side, leverage: lev, margin, order_type: type, limit: Number(limit) || null, tp: tpV, sl: slV });
        onToast(r.ok ? (type === "market" ? `Posizione simulata aperta su ${m.base}` : `Ordine limite simulato inserito su ${m.base}`) : r.error);
      }
    } catch (e: any) {
      onToast(`Ordine non inviato: ${e?.message ?? e}`);
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="panel">
      <h2>Ordine su {m.base} <small>{live ? "reale su Raydium Perps" : "attiva il trading reale per inviarlo"}</small></h2>
      <div className="ticket">
        <div className="seg">
          <button className="l" aria-pressed={side === "long"} onClick={() => setSide("long")}>Long</button>
          <button className="s" aria-pressed={side === "short"} onClick={() => setSide("short")}>Short</button>
        </div>
        <label className="field">
          Leva {lev}x {option && <span className="kronos">· Kronos TP {Math.round(option.p_tp * 100)}%, SL {Math.round(option.p_sl * 100)}%, EV {signed(option.ev * 100, 1)}%</span>}
          <input type="range" min={10} max={maxLev} step={1} value={Math.min(lev, maxLev)} onChange={(e) => setLev(Number(e.target.value))} />
        </label>
        <div className="seg">
          <button className="t" aria-pressed={type === "market"} onClick={() => setType("market")}>A mercato</button>
          <button className="t" aria-pressed={type === "limit"} onClick={() => setType("limit")}>Limite</button>
        </div>
        <div className="row2">
          <label className="field">Margine (USDC)<input type="number" min={10} step={10} value={margin} onChange={(e) => setMargin(Number(e.target.value))} /></label>
          <label className="field">Prezzo limite<input type="number" step="any" disabled={type === "market"} placeholder={price(m.mark_price)} value={limit} onChange={(e) => setLimit(e.target.value)} /></label>
        </div>
        <div className="row2">
          <label className="field">Take profit<input type="number" step="any" placeholder={price(defTp)} value={tp} onChange={(e) => setTp(e.target.value)} /></label>
          <label className="field">Stop loss<input type="number" step="any" placeholder={price(defSl)} value={sl} onChange={(e) => setSl(e.target.value)} /></label>
        </div>
        <p className="note">
          Esposizione {(margin * lev).toLocaleString("it-IT")} USDC · liquidazione stimata {price(liq)}. Senza valori, TP e SL seguono le regole dell'Executor (+{ex?.tp_pct ?? 5}% / −{ex?.sl_pct ?? 2}% con leva).
        </p>
        {live ? (
          <button className={`primary ${side === "long" ? "l" : "s"}`} disabled={sending || (type === "limit" && !(Number(limit) > 0))} onClick={submit}>
            {sending ? "Invio…" : `${side === "long" ? "Apri long" : "Apri short"} ${type === "market" ? "a mercato" : "limite"}`}
          </button>
        ) : (
          <button className="walletbtn" disabled={real.busy} onClick={real.enable}>{real.busy ? "Firma in Phantom…" : "Attiva trading reale"}</button>
        )}
      </div>
    </div>
  );
}

export function WalletPanel({ real, ex }: { real: Real; ex: ExecutorView | null }) {
  const toggleAuto = async (on: boolean) => {
    if (on && !window.confirm(
      `Attivi il trading automatico REALE?\n\nL'Executor aprirà fino a ${ex?.max_positions ?? 2} posizioni da ${ex?.margin_usdc ?? 100} USDC con leva 10x-20x sui segnali del Checker, firmandole con la chiave delegata, e le chiuderà secondo le regole. Puoi spegnerlo in qualsiasi momento.`,
    )) return;
    await postJSON("/api/executor", { real_auto: on });
  };
  const v = real.venue;
  return (
    <div className="panel">
      <h2>Wallet <small>{real.address ? "Phantom collegato" : "nessun wallet: modalità simulata"}</small></h2>
      {!real.address ? (
        <>
          <p className="note">Collega Phantom per vedere il tuo conto su Raydium Perps e operare davvero. Senza wallet l'Executor lavora solo in simulazione.</p>
          <button className="walletbtn" onClick={real.connect}>{real.hasPhantom ? "Collega Phantom" : "Installa Phantom"}</button>
        </>
      ) : (
        <div style={{ display: "grid", gap: 10 }}>
          <div className="wallet">
            <span className="addr">{real.address.slice(0, 4)}…{real.address.slice(-4)}{real.sol != null ? ` · ${real.sol.toFixed(3)} SOL` : ""}</span>
            <button className="ghost" onClick={real.disconnect} style={{ marginLeft: "auto" }}>Scollega</button>
          </div>
          {!real.ready ? (
            <>
              <p className="note">
                Per operare su Raydium Perps firma in Phantom: la registrazione del conto (solo la prima volta) e una chiave di trading delegata valida 30 giorni.
                La chiave può aprire e chiudere posizioni ma non può prelevare fondi, e resta solo in questo browser.
              </p>
              <button className="walletbtn" disabled={real.busy} onClick={real.enable}>{real.busy ? "Firma in Phantom…" : "Attiva trading reale"}</button>
            </>
          ) : (
            <>
              <div className="balance" style={{ marginBottom: 0 }}>
                <div><span>Collaterale</span><b>{v ? `${v.total_collateral_value.toFixed(2)}` : "…"}</b></div>
                <div><span>Disponibile</span><b>{v ? v.free_collateral.toFixed(2) : "…"}</b></div>
                <div><span>Posizioni</span><b>{v ? v.rows.filter((r) => r.position_qty !== 0).length : "…"}</b></div>
              </div>
              {v && v.total_collateral_value < (ex?.margin_usdc ?? 100) && (
                <p className="note warn">
                  Collaterale insufficiente per un'operazione da {ex?.margin_usdc ?? 100} USDC. Deposita USDC su{" "}
                  <a href="https://perps.raydium.io" target="_blank" rel="noreferrer">Raydium Perps</a> con questo stesso wallet: il conto è lo stesso.
                </p>
              )}
              <label className="switch">
                <input type="checkbox" checked={!!ex?.real_auto} onChange={(e) => toggleAuto(e.target.checked)} />
                Trading automatico reale dell'Executor
              </label>
              {ex?.real_auto && <p className="note warn">Attivo: tieni aperta questa pagina, gli ordini partono da qui. TP e SL restano comunque sul mercato.</p>}
              {v && v.rows.filter((r) => r.position_qty !== 0).length > 0 && (
                <table className="t">
                  <thead><tr><th>Mercato</th><th className="n">Qty</th><th className="n">Entrata</th><th className="n">PnL</th><th /></tr></thead>
                  <tbody>
                    {v.rows.filter((r) => r.position_qty !== 0).map((r) => (
                      <tr key={r.symbol}>
                        <td className={r.position_qty > 0 ? "long" : "short"}>{r.symbol.split("_")[1]}</td>
                        <td className="n">{r.position_qty}</td>
                        <td className="n">{price(r.average_open_price)}</td>
                        <td className={`n ${r.unsettled_pnl >= 0 ? "long" : "short"}`}>{signed(r.unsettled_pnl ?? 0)}</td>
                        <td className="n"><button className="ghost danger" onClick={() => window.confirm(`Chiudere a mercato la posizione reale su ${r.symbol.split("_")[1]}?`) && real.closeReal(r.symbol)}>Chiudi</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {real.orders.length > 0 && (
                <table className="t">
                  <thead><tr><th>Ordine aperto</th><th className="n">Qty</th><th className="n">Prezzo</th><th /></tr></thead>
                  <tbody>
                    {real.orders.map((o) => (
                      <tr key={o.order_id}>
                        <td className={o.side === "BUY" ? "long" : "short"}>{o.symbol.split("_")[1]} {o.side === "BUY" ? "long" : "short"} {o.type === "LIMIT" ? "limite" : o.type}</td>
                        <td className="n">{o.quantity}</td>
                        <td className="n">{price(o.price)}</td>
                        <td className="n"><button className="ghost" onClick={() => real.cancelOrder(o.symbol, o.order_id)}>Annulla</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <button className="ghost" onClick={real.revoke}>Rimuovi la chiave da questo browser</button>
            </>
          )}
          {real.error && <p className="note warn">{real.error}</p>}
        </div>
      )}
    </div>
  );
}
