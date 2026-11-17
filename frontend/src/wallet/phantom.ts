/** Minimal Phantom (Solana) provider access, no private keys ever touched. */
import type { PublicKey } from "@solana/web3.js";

export interface PhantomProvider {
  isPhantom?: boolean;
  publicKey: PublicKey | null;
  isConnected: boolean;
  connect(opts?: { onlyIfTrusted?: boolean }): Promise<{ publicKey: PublicKey }>;
  disconnect(): Promise<void>;
  signMessage(message: Uint8Array, display?: "utf8" | "hex"): Promise<{ signature: Uint8Array; publicKey: PublicKey }>;
  signTransaction<T>(tx: T): Promise<T>;
  on(event: "connect" | "disconnect" | "accountChanged", cb: (arg: any) => void): void;
  removeListener?(event: string, cb: (arg: any) => void): void;
}

export function getPhantom(): PhantomProvider | null {
  const w = window as any;
  const p = w.phantom?.solana ?? w.solana;
  return p?.isPhantom ? (p as PhantomProvider) : null;
}

export async function solBalance(address: string): Promise<number | null> {
  try {
    const r = await fetch("https://api.mainnet-beta.solana.com", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "getBalance", params: [address] }),
    });
    const j = await r.json();
    return j.result?.value != null ? j.result.value / 1e9 : null;
  } catch {
    return null;
  }
}
