/** "Limpieza de ruido" del heatmap por antiguedad (pedido del usuario,
 * 2026-09-25) -- SOLO visual (opacidad/peso al mezclar celdas), nunca toca
 * `notional_usd` como lo devuelve la API: mismo criterio que ya usa
 * `HeatmapOverlay.tsx` para el gamma/percentil de color (presentacion, no
 * un supuesto nuevo metido en `heatmap_model.py`).
 *
 * Los dos parametros (media vida y corte duro) NO son un numero inventado:
 * salen de correr el modelo real (`compute_levels_from_trades` +
 * la logica de cruce de `heatmap_model.py`) contra ~150,000 big trades
 * reales de BTCUSDT (junio-julio 2026, la unica ventana con velas Y big
 * trades completos en la DB a la vez) y medir cuanto tiempo sobrevive de
 * verdad cada nivel modelado antes de que el precio lo cruce, por
 * apalancamiento. Mediana medida (horas): 100x=3.3, 50x=21, 25x=78.8,
 * 15x=178.4, 10x=222.2 -- la media vida de cada tramo es esa mediana. El
 * corte duro es el p95 medido, redondeado: 100x=5d, 50x=12d, 25x=20d,
 * 15x=22d, 10x=25d ("mas alla de esto, el 95% de las veces ya se cruzo de
 * verdad" en vez de un numero arbitrario). Un solo apalancamiento no
 * medido (fuera de la cesta por defecto) usa el tramo mas cercano.
 */

const HOUR_MS = 3_600_000;
const DAY_MS = 86_400_000;

export interface LeverageDecay {
  halfLifeMs: number;
  cutoffMs: number;
}

export const LEVERAGE_DECAY: Record<number, LeverageDecay> = {
  100: { halfLifeMs: 3.3 * HOUR_MS, cutoffMs: 5 * DAY_MS },
  50: { halfLifeMs: 21 * HOUR_MS, cutoffMs: 12 * DAY_MS },
  25: { halfLifeMs: 78.8 * HOUR_MS, cutoffMs: 20 * DAY_MS },
  15: { halfLifeMs: 178.4 * HOUR_MS, cutoffMs: 22 * DAY_MS },
  10: { halfLifeMs: 222.2 * HOUR_MS, cutoffMs: 25 * DAY_MS },
};

// Fallback para un apalancamiento fuera de la cesta medida (ver
// docstring): el tramo mas conservador (el que menos rapido decae), para
// no desaparecer de mas algo que no se midio.
const FALLBACK_DECAY: LeverageDecay = LEVERAGE_DECAY[10];

// Piso de opacidad: un nivel que todavia no llego al corte duro pero ya
// esta muy decaido no debe llegar a 0 (eso lo hace `cutoffMs`, de golpe,
// a proposito -- una desaparicion clara en vez de un desvanecido infinito
// que nunca termina de irse del todo). 0.08 es visualmente "casi nada"
// sin ser exactamente cero antes de tiempo.
const OPACITY_FLOOR = 0.08;

function decayFor(leverage: number): LeverageDecay {
  return LEVERAGE_DECAY[leverage] ?? FALLBACK_DECAY;
}

/** `null` si el nivel ya paso su corte duro (debe descartarse entero, no
 * solo atenuarse) -- si no, el multiplicador [OPACITY_FLOOR, 1] a aplicar
 * sobre `notional_usd` antes de sumar/pintar. `ageMs` negativo (reloj
 * desincronizado, o `nowMs` un poco atras del `time_ms` de la celda) se
 * trata como edad 0, nunca como decaimiento "hacia el futuro". */
export function fadeMultiplier(leverage: number, ageMs: number): number | null {
  const { halfLifeMs, cutoffMs } = decayFor(leverage);
  if (ageMs >= cutoffMs) return null;
  const clampedAge = Math.max(0, ageMs);
  return Math.max(OPACITY_FLOOR, Math.pow(0.5, clampedAge / halfLifeMs));
}
