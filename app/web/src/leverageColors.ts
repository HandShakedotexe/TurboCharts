/** Un color fijo por nivel de apalancamiento, mas "caliente" cuanto mas alto
 * -- mas apalancamiento liquida mas cerca del precio actual, asi que el
 * mapeo de color coincide con la intuicion habitual de un heatmap. */
export const LEVERAGE_COLORS: Record<number, string> = {
  10: "#3b82f6",
  15: "#22c55e",
  25: "#eab308",
  50: "#f97316",
  100: "#ef4444",
};

export function colorForLeverage(leverage: number): string {
  return LEVERAGE_COLORS[leverage] ?? "#a855f7";
}
