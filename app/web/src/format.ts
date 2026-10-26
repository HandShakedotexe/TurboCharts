/** Formato compacto de un notional en dolares -- 18490000 -> "18.49M", 950 ->
 * "950" -- el mismo formato que usa el tooltip de un heatmap publicado
 * (CoinGlass: "Liquidation Leverage 18.49M"). */
export function formatNotional(usd: number): string {
  if (usd >= 1_000_000) return `${(usd / 1_000_000).toFixed(2)}M`;
  if (usd >= 1_000) return `${(usd / 1_000).toFixed(2)}K`;
  return usd.toFixed(0);
}

/** Fecha/hora en una zona horaria FIJA (UTC), no la del navegador de quien
 * mire la pantalla -- a peticion del usuario para el trade journal
 * (2026-09-03): dos personas en zonas distintas viendo la misma corrida
 * deben ver la MISMA hora, no cada una la suya via `toLocaleString()`. */
export function formatUtc(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
}
