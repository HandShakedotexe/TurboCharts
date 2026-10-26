/**
 * Busqueda binaria del primer tiempo de vela (CIERRE) >= `eventTimeSec`
 * dentro de `sortedCloseTimes`. Extraido de `LiquidationVolumePane.tsx`
 * (2026-09-11) para reusarlo en cualquier overlay/pane que ancle un evento
 * propio (liquidacion, big trade, un extremo de rango FRVP, una posicion
 * Long/Short) a un punto real del eje de tiempo -- NUNCA calcular un bucket
 * propio.
 *
 * Motivo (bug real): `time` de cada vela de
 * `/api/candles` es su CIERRE (`open + interval - 1s`, confirmado con datos
 * reales: en 5m cada valor cae en `... % 300 === 299`, nunca alineado a una
 * apertura), no su apertura. Lightweight Charts reparte el ancho de barra
 * por indice logico COMPARTIDO entre TODAS las series del mismo eje de
 * tiempo -- un tiempo que no existe exactamente en la serie de velas mete
 * un indice extra ahi y descoloca las velas reales alrededor de ese punto.
 * `timeToCoordinate`/`coordinateToTime` tampoco interpolan entre tiempos
 * arbitrarios: solo resuelven tiempos que YA existen en el eje. Por eso
 * cualquier tiempo propio (el de un evento, o el guardado de una posicion
 * colocada en OTRA temporalidad) tiene que buscar el tiempo de vela real
 * mas cercano antes de pedirle una coordenada al chart.
 *
 * Si el evento es mas nuevo que la ultima vela conocida, se lo asigna a
 * esa ultima -- sigue siendo un tiempo real, solo que la vela todavia esta
 * en curso. `null` si `sortedCloseTimes` esta vacio.
 */
export function barTimeForEvent(sortedCloseTimes: number[], eventTimeSec: number): number | null {
  if (sortedCloseTimes.length === 0) return null;
  let lo = 0;
  let hi = sortedCloseTimes.length; // hi = un pasado el final, no el ultimo indice
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (sortedCloseTimes[mid] < eventTimeSec) lo = mid + 1;
    else hi = mid;
  }
  if (lo >= sortedCloseTimes.length) return sortedCloseTimes[sortedCloseTimes.length - 1];
  return sortedCloseTimes[lo];
}
