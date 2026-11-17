/**
 * Raydium Perps (Orderly Network, broker "raydium") client for the browser.
 *
 * Security model
 *  - The Phantom wallet never leaves Phantom: we only ask it to sign two
 *    messages (account registration, once; and adding a delegated key).
 *  - The delegated Orderly key is an ed25519 key pair generated here, with
 *    scope "read,trading" and a 30-day expiry. It can place and cancel orders
 *    but cannot withdraw funds. Its secret is kept in this browser's
 *    localStorage only and is never sent to our backend.
 *  - Message formats for Solana come from Orderly's own
 *    @orderly.network/default-solana-adapter (their public docs only cover EVM).
 */
// The Orderly adapter pulls heavy Solana/LayerZero deps: load it only when the user enables real trading.
import { WalletAdapterNetwork } from "@solana/wallet-adapter-base";
import * as ed from "@noble/ed25519";
import bs58 from "bs58";
import type { PhantomProvider } from "./phantom";

export const ORDERLY_API = "https://api.orderly.org";
export const BROKER_ID = "raydium";
export const SOLANA_MAINNET_CHAIN_ID = 900900900;
const KEY_STORE = "pp.orderlyKey.v1";
const KEY_DAYS = 30;

export type StoredKey = { address: string; accountId: string; publicKey: string; secret: string; expires: number };

function loadKey(address: string): StoredKey | null {
  try {
    const raw = localStorage.getItem(KEY_STORE);
    if (!raw) return null;
    const k = JSON.parse(raw) as StoredKey;
    if (k.address !== address || k.expires < Date.now() + 3600_000) return null;
    return k;
  } catch {
    return null;
  }
}

function saveKey(k: StoredKey | null) {
  try {
    if (k) localStorage.setItem(KEY_STORE, JSON.stringify(k));
    else localStorage.removeItem(KEY_STORE);
  } catch {
    /* storage blocked: key lives for this session only */
  }
}

async function pub<T = any>(path: string): Promise<T> {
  const r = await fetch(ORDERLY_API + path);
  const j = await r.json();
  if (!j.success) throw new Error(j.message || `Orderly ${path} failed`);
  return j.data as T;
}

async function post<T = any>(path: string, body: unknown): Promise<T> {
  const r = await fetch(ORDERLY_API + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const j = await r.json();
  if (!j.success) throw new Error(j.message || `Orderly ${path} failed`);
  return j.data as T;
}

async function makeAdapter(provider: PhantomProvider, address: string) {
  const { DefaultSolanaWalletAdapter } = await import("@orderly.network/default-solana-adapter");
  const adapter = new DefaultSolanaWalletAdapter();
  adapter.active({
    address,
    chain: { id: SOLANA_MAINNET_CHAIN_ID },
    provider: {
      network: WalletAdapterNetwork.Mainnet,
      rpcUrl: "https://api.mainnet-beta.solana.com",
      publicKey: provider.publicKey ?? undefined,
      signMessage: async (msg: Uint8Array) => (await provider.signMessage(msg, "utf8")).signature,
      signTransaction: (tx: any) => provider.signTransaction(tx),
      sendTransaction: (() => {
        throw new Error("transactions are not sent from this dashboard");
      }) as any,
    },
  });
  return adapter;
}

export class OrderlyClient {
  key: StoredKey | null;
  address: string;
  constructor(address: string) {
    this.address = address;
    this.key = loadKey(address);
  }

  get ready() {
    return !!this.key;
  }

  async accountId(): Promise<string | null> {
    try {
      const d = await pub<{ account_id: string }>(
        `/v1/get_account?address=${this.address}&broker_id=${BROKER_ID}&chain_type=SOL`,
      );
      return d.account_id;
    } catch {
      return null;
    }
  }

  /** One Phantom signature to register (if needed) and one to add the delegated key. */
  async enable(provider: PhantomProvider): Promise<StoredKey> {
    const adapter = await makeAdapter(provider, this.address);
    let accountId = await this.accountId();
    if (!accountId) {
      const nonce = await pub<{ registration_nonce: string }>("/v1/registration_nonce");
      const { message, signatured } = await adapter.generateRegisterAccountMessage({
        brokerId: BROKER_ID,
        registrationNonce: Number(nonce.registration_nonce),
        timestamp: Date.now(),
      });
      const d = await post<{ account_id: string }>("/v1/register_account", {
        message,
        signature: signatured,
        userAddress: this.address,
      });
      accountId = d.account_id;
    }
    const secret = ed.utils.randomSecretKey();
    const publicKey = "ed25519:" + bs58.encode(await ed.getPublicKeyAsync(secret));
    const ts = Date.now();
    const { message, signatured } = await adapter.generateAddOrderlyKeyMessage({
      publicKey,
      brokerId: BROKER_ID,
      scope: "read,trading",
      timestamp: ts,
      expiration: KEY_DAYS,
      tag: "price-predictor",
    });
    await post("/v1/orderly_key", { message, signature: signatured, userAddress: this.address });
    const key: StoredKey = {
      address: this.address,
      accountId: accountId!,
      publicKey,
      secret: bs58.encode(secret),
      expires: ts + KEY_DAYS * 86400_000,
    };
    saveKey(key);
    this.key = key;
    return key;
  }

  forget() {
    saveKey(null);
    this.key = null;
  }

  private async signed<T = any>(method: "GET" | "POST" | "PUT" | "DELETE", path: string, body?: unknown): Promise<T> {
    if (!this.key) throw new Error("trading reale non attivato");
    const ts = Date.now().toString();
    const raw = body === undefined ? "" : JSON.stringify(body);
    const sig = await ed.signAsync(new TextEncoder().encode(ts + method + path + raw), bs58.decode(this.key.secret));
    const b64url = btoa(String.fromCharCode(...sig)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    const r = await fetch(ORDERLY_API + path, {
      method,
      headers: {
        "Content-Type": method === "GET" || method === "DELETE" ? "application/x-www-form-urlencoded" : "application/json",
        "orderly-timestamp": ts,
        "orderly-account-id": this.key.accountId,
        "orderly-key": this.key.publicKey,
        "orderly-signature": b64url,
      },
      body: raw || undefined,
    });
    const j = await r.json();
    if (!j.success) throw new Error(j.message || `Orderly ${path}: ${r.status}`);
    return j.data as T;
  }

  // ------------------------------------------------------------- account
  holdings() {
    return this.signed<{ holding: { token: string; holding: number; frozen: number }[] }>("GET", "/v1/client/holding");
  }
  positions() {
    return this.signed<{ rows: any[]; free_collateral: number; total_collateral_value: number; margin_ratio: number }>(
      "GET",
      "/v1/positions",
    );
  }
  openOrders() {
    return this.signed<{ rows: any[] }>("GET", "/v1/orders?status=INCOMPLETE");
  }
  setLeverage(leverage: number) {
    return this.signed("POST", "/v1/client/leverage", { leverage });
  }

  // ------------------------------------------------------------- trading
  order(o: {
    symbol: string;
    side: "BUY" | "SELL";
    type: "MARKET" | "LIMIT";
    qty: number;
    price?: number;
    reduceOnly?: boolean;
  }) {
    const body: Record<string, unknown> = {
      symbol: o.symbol,
      order_type: o.type,
      side: o.side,
      order_quantity: o.qty,
      reduce_only: !!o.reduceOnly,
    };
    if (o.type === "LIMIT") body.order_price = o.price;
    return this.signed<{ order_id: number; order_price?: number; order_quantity?: number }>("POST", "/v1/order", body);
  }

  /** Position-level TP/SL kept on the venue: closes the whole position at mark price. */
  positionTpSl(symbol: string, isLong: boolean, tp?: number, sl?: number) {
    const side = isLong ? "SELL" : "BUY";
    const child = (algo: "TAKE_PROFIT" | "STOP_LOSS", price: number) => ({
      symbol,
      algo_type: algo,
      side,
      type: "CLOSE_POSITION",
      trigger_price_type: "MARK_PRICE",
      trigger_price: price,
      reduce_only: true,
    });
    const children = [] as unknown[];
    if (tp) children.push(child("TAKE_PROFIT", tp));
    if (sl) children.push(child("STOP_LOSS", sl));
    return this.signed("POST", "/v1/algo/order", {
      symbol,
      algo_type: "POSITIONAL_TP_SL",
      trigger_price_type: "MARK_PRICE",
      child_orders: children,
    });
  }

  cancelOrder(symbol: string, orderId: number) {
    return this.signed("DELETE", `/v1/order?order_id=${orderId}&symbol=${symbol}`);
  }

  /** Market reduce-only order on the opposite side for the whole position. */
  async closePosition(symbol: string) {
    const pos = await this.positions();
    const row = pos.rows.find((r) => r.symbol === symbol && Math.abs(r.position_qty) > 0);
    if (!row) return null;
    return this.order({
      symbol,
      side: row.position_qty > 0 ? "SELL" : "BUY",
      type: "MARKET",
      qty: Math.abs(row.position_qty),
      reduceOnly: true,
    });
  }
}

/** Round a quantity down to the market's base tick, a price to its quote tick. */
export function roundTo(x: number, tick: number) {
  if (!tick) return x;
  const decimals = Math.max(0, Math.ceil(-Math.log10(tick)));
  return Number((Math.floor(x / tick + 1e-9) * tick).toFixed(decimals));
}
export function roundPrice(x: number, tick: number) {
  if (!tick) return x;
  const decimals = Math.max(0, Math.ceil(-Math.log10(tick)));
  return Number((Math.round(x / tick) * tick).toFixed(decimals));
}
