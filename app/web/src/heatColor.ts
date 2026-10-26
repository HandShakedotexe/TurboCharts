/** Gradiente de "calor" azul -> cian -> amarillo -> rojo, el mismo lenguaje
 * visual que usan los heatmaps de liquidaciones publicados (CoinGlass,
 * BitcoinCounterFlow): el color representa DENSIDAD de notional en ese
 * precio, no el apalancamiento que lo genero. */
const STOPS: [number, number, number][] = [
  [59, 130, 246], // azul: zona debil
  [34, 211, 238], // cian
  [250, 204, 21], // amarillo
  [239, 68, 68], // rojo: zona densa
];

export function heatColor(intensity: number, alpha: number): string {
  const t = Math.min(1, Math.max(0, intensity)) * (STOPS.length - 1);
  const i = Math.floor(t);
  const frac = t - i;
  const a = STOPS[i];
  const b = STOPS[Math.min(i + 1, STOPS.length - 1)];
  const r = Math.round(a[0] + (b[0] - a[0]) * frac);
  const g = Math.round(a[1] + (b[1] - a[1]) * frac);
  const bl = Math.round(a[2] + (b[2] - a[2]) * frac);
  return `rgba(${r},${g},${bl},${alpha})`;
}
