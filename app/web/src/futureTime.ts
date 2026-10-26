import type { IChartApi, UTCTimestamp } from "lightweight-charts";

/**
 * Conversion tiempo <-> pixel que SI funciona en el hueco vacio que reserva
 * `rightOffset` despues de la ultima vela (`PriceChart.tsx`). Verificado con
 * el chart real (2026-09-21): `timeToCoordinate` y
 * `coordinateToTime` devuelven `null` para cualquier tiempo/pixel que no sea
 * el de una vela ya existente, incluso dentro de ese hueco -- asi que se
 * extrapola a mano con el espaciado de barra actual. Compartido por
 * `DrawingToolsOverlay` y `FixedRangeVolumeProfile` para no tener dos copias
 * de la misma extrapolacion.
 */
export interface FutureAnchor {
  lastTime: number;
  lastX: number;
  intervalSec: number;
}

/** `barTimes` ya ordenado ascendente. `null` si no hay forma de anclar (menos
 * de dos velas, o la ultima esta fuera del eje). */
export function futureAnchor(chart: IChartApi, barTimes: number[]): FutureAnchor | null {
  if (barTimes.length < 2) return null;
  const lastTime = barTimes[barTimes.length - 1];
  const lastX = chart.timeScale().timeToCoordinate(lastTime as UTCTimestamp);
  const intervalSec = lastTime - barTimes[barTimes.length - 2];
  if (lastX === null || intervalSec <= 0) return null;
  return { lastTime, lastX, intervalSec };
}

/** Tiempo (segundos) -> pixel. Directo si la libreria lo resuelve; si no y el
 * tiempo cae despues de la ultima vela, extrapola. `null` en cualquier otro
 * caso (tiempo viejo que no existe en el eje: eso lo resuelve cada llamador
 * con `barTimeForEvent`, que es otra cosa). */
export function timeSecToX(chart: IChartApi, barTimes: number[], timeSec: number): number | null {
  const direct = chart.timeScale().timeToCoordinate(timeSec as UTCTimestamp);
  if (direct !== null) return direct;
  const anchor = futureAnchor(chart, barTimes);
  if (anchor && timeSec > anchor.lastTime) {
    const barSpacing = chart.timeScale().options().barSpacing;
    return anchor.lastX + ((timeSec - anchor.lastTime) / anchor.intervalSec) * barSpacing;
  }
  return null;
}

/** Pixel -> tiempo (segundos). Directo si la libreria lo resuelve; si no y el
 * pixel cae a la derecha de la ultima vela, extrapola. `isFuture` avisa cuando
 * el resultado salio de la extrapolacion (zona futura). */
export function xToTimeSec(
  chart: IChartApi, barTimes: number[], x: number,
): { timeSec: number; isFuture: boolean } | null {
  const time = chart.timeScale().coordinateToTime(x);
  if (time !== null) return { timeSec: Number(time), isFuture: false };
  const anchor = futureAnchor(chart, barTimes);
  const barSpacing = chart.timeScale().options().barSpacing;
  if (!anchor || barSpacing <= 0 || x <= anchor.lastX) return null;
  return { timeSec: anchor.lastTime + ((x - anchor.lastX) / barSpacing) * anchor.intervalSec, isFuture: true };
}
