import type { HeatmapTier } from "./api";
import { fadeMultiplier } from "./heatmapDecay";

/** Suma el notional de todos los niveles de apalancamiento VISIBLES que
 * caen en el mismo precio. Los tramos comparten la misma rejilla de bins
 * (mismo reference_price/bin_width_pct en la respuesta de la API), asi que
 * sumar por `price` es seguro: no hace falta re-binear en el cliente.
 * Usado por VolumeProfile (las barras del perfil, un snapshot sin tiempo). */
export function mergeTiers(tiers: HeatmapTier[], visible: Set<number>): Map<number, number> {
  const merged = new Map<number, number>();
  for (const tier of tiers) {
    if (!visible.has(tier.leverage)) continue;
    for (const lvl of tier.levels) {
      merged.set(lvl.price, (merged.get(lvl.price) ?? 0) + lvl.notional_usd);
    }
  }
  return merged;
}

/** Como `mergeTiers`, pero sobre la cuadricula precio x tiempo (`cells`) en
 * vez del snapshot: usado por HeatmapOverlay para dibujar celdas, no
 * bandas de ancho completo. La clave combina tiempo y precio porque el
 * mismo precio existe en muchos buckets distintos.
 *
 * `nowMs` (pedido del usuario, 2026-09-25, "limpieza de ruido"): cada
 * celda se pesa por `fadeMultiplier(tier.leverage, nowMs - cell.time_ms)`
 * ANTES de sumarla -- ver heatmapDecay.ts para los numeros (medidos, no
 * inventados) y el porque de que sea por apalancamiento. Una celda que ya
 * paso su corte duro se descarta entera (ni siquiera aporta al merge), no
 * solo se pinta mas transparente -- si dos apalancamientos distintos
 * comparten el mismo bin de precio/tiempo, cada uno decae con SU PROPIA
 * curva antes de sumarse, asi que el orden de suma no importa (la
 * decision de incluir/pesar ya esta tomada por celda individual, no
 * despues del merge).*/
export function mergeCells(
  tiers: HeatmapTier[],
  visible: Set<number>,
  nowMs: number,
): Map<string, { timeMs: number; price: number; notional: number; bucketMs: number }> {
  const merged = new Map<string, { timeMs: number; price: number; notional: number; bucketMs: number }>();
  for (const tier of tiers) {
    if (!visible.has(tier.leverage)) continue;
    for (const cell of tier.cells) {
      const weight = fadeMultiplier(tier.leverage, nowMs - cell.time_ms);
      if (weight === null) continue;
      const weightedNotional = cell.notional_usd * weight;
      const key = `${cell.time_ms}:${cell.price}`;
      const existing = merged.get(key);
      if (existing) {
        existing.notional += weightedNotional;
      } else {
        merged.set(key, {
          timeMs: cell.time_ms, price: cell.price, notional: weightedNotional, bucketMs: cell.bucket_ms,
        });
      }
    }
  }
  return merged;
}
