import { useEffect, useRef, useState } from "react";
import type { IChartApi, ISeriesApi, UTCTimestamp } from "lightweight-charts";
import type { Candle } from "../api";
import { CHART_PANES_CHANGED_EVENT } from "../chartLayoutEvent";
import { barTimeForEvent } from "../barTimeForEvent";
import { plotWidth } from "../chartGeometry";
import { timeSecToX, xToTimeSec } from "../futureTime";

interface Props {
  chart: IChartApi | null;
  series: ISeriesApi<"Candlestick"> | null;
  symbol: string;
  candles: Candle[];
  /** Igual que `placingSide` en TradePositionsOverlay -- controlado por el
   * boton "+ FRVP" del toolbar. Primer clic marca el inicio del rango,
   * segundo clic lo cierra y llama `onPlaced`. */
  placing: boolean;
  onPlaced: () => void;
}

interface FrvpRange {
  id: string;
  startTimeMs: number;
  endTimeMs: number;
}

interface Bucket {
  priceLow: number;
  priceHigh: number;
  volume: number;
  // Porcion de `volume` que fue compra agresiva (taker buy) -- ver
  // `computeProfile`. `volume - buyVolume` es la venta agresiva.
  buyVolume: number;
}

interface Rect {
  left: number;
  top: number;
  width: number;
  height: number;
}

const NUM_BUCKETS = 24;
// El bar mas largo del perfil llega al 70% del ancho del rango -- nunca lo
// desborda, y deja aire para leer las velas de abajo.
const MAX_BAR_WIDTH_FRACTION = 0.7;
// Fraccion de volumen total que define el Area de Valor (VA) -- 70% es el
// estandar de la industria (Market/Volume Profile de Peter Steidlmayer),
// el mismo numero que usan TradingView/Sierra Chart/etc.
const VALUE_AREA_FRACTION = 0.7;
const POC_COLOR = "#f5b942"; // point of control -- el nivel de mayor volumen
const POC_LINE_COLOR = "#f5b942";
// Delta (2026-09-18, pedido del usuario): el LARGO de la barra sigue
// siendo el volumen TOTAL del nivel (asi el POC/VA se siguen leyendo
// igual que antes), pero ahora se pinta en dos tramos -- compra agresiva
// (verde) y venta agresiva (rojo), mismos tonos que ya usa el resto del
// dashboard para compra/venta (BigTradesOverlay.tsx). POC/VA ya no se
// marcan con un color de RELLENO propio (tapaba el delta) -- se marcan
// con un BORDE (dorado grueso en el POC, azul fino en el resto del Area
// de Valor) alrededor de la misma barra compra/venta.
const BUY_BAR_COLOR = "rgba(38, 166, 154, 0.55)";
const SELL_BAR_COLOR = "rgba(239, 83, 80, 0.55)";
const VA_BORDER_COLOR = "rgba(96, 165, 250, 0.85)";
const VA_LINE_COLOR = "rgba(226, 232, 240, 0.85)";
const BOX_BORDER_COLOR = "rgba(148, 163, 184, 0.6)";
const CLOSE_BUTTON_SIZE_PX = 14;

function storageKey(symbol: string): string {
  return `liq-intel:frvp:${symbol}`;
}

function loadRanges(symbol: string): FrvpRange[] {
  try {
    const raw = window.localStorage.getItem(storageKey(symbol));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function saveRanges(symbol: string, ranges: FrvpRange[]): void {
  try {
    window.localStorage.setItem(storageKey(symbol), JSON.stringify(ranges));
  } catch {
    // localStorage lleno o bloqueado -- sigue funcionando en memoria esta sesion.
  }
}

/** Sin datos de trade a trade, el volumen de cada vela se reparte
 * PROPORCIONALMENTE entre los buckets de precio que su rango [low, high]
 * atraviesa -- no todo a un solo precio (el cierre), que exageraria un
 * pico artificial ahi. Mismo criterio que usan la mayoria de los
 * indicadores de "volume profile" publicados cuando no tienen el tape
 * completo. `null` si no hay velas en el rango o el rango de precio es
 * degenerado (una sola vela plana, dato en vivo aun asentandose).
 *
 * `buyVolume` (delta, pedido por el usuario 2026-09-18: "quiero ver el
 * delta del volumen ya que ahora solo se ve volumen total") se reparte
 * con el MISMO `overlap` proporcional que el volumen total -- no hay
 * forma de saber en que nivel de precio exacto de la vela ocurrio la
 * compra vs. la venta agresiva sin el tape completo, asi que asume que
 * el delta de la vela se distribuye igual que el resto de su volumen. Si
 * `taker_buy_volume` no esta disponible (candle historica vieja sin esa
 * columna poblada, ver db/schema.sql) se reparte a mitad compra/mitad
 * venta -- neutro, nunca se inventa un lado que no se sabe. */
function computeProfile(candles: Candle[], startSec: number, endSec: number): Bucket[] | null {
  const inRange = candles.filter((c) => c.time >= startSec && c.time <= endSec);
  if (inRange.length === 0) return null;
  let priceMin = Infinity;
  let priceMax = -Infinity;
  for (const c of inRange) {
    if (c.low < priceMin) priceMin = c.low;
    if (c.high > priceMax) priceMax = c.high;
  }
  if (!(priceMax > priceMin)) return null;

  const bucketSize = (priceMax - priceMin) / NUM_BUCKETS;
  const volumes = new Array(NUM_BUCKETS).fill(0);
  const buyVolumes = new Array(NUM_BUCKETS).fill(0);
  for (const c of inRange) {
    const buyFraction = c.taker_buy_volume !== null && c.volume > 0 ? c.taker_buy_volume / c.volume : 0.5;
    const span = c.high - c.low;
    if (span <= 0) {
      const idx = Math.min(NUM_BUCKETS - 1, Math.max(0, Math.floor((c.close - priceMin) / bucketSize)));
      volumes[idx] += c.volume;
      buyVolumes[idx] += c.volume * buyFraction;
      continue;
    }
    for (let i = 0; i < NUM_BUCKETS; i++) {
      const bucketLow = priceMin + i * bucketSize;
      const bucketHigh = bucketLow + bucketSize;
      const overlap = Math.min(c.high, bucketHigh) - Math.max(c.low, bucketLow);
      if (overlap > 0) {
        const vol = c.volume * (overlap / span);
        volumes[i] += vol;
        buyVolumes[i] += vol * buyFraction;
      }
    }
  }
  return volumes.map((volume, i) => ({
    priceLow: priceMin + i * bucketSize, priceHigh: priceMin + (i + 1) * bucketSize,
    volume, buyVolume: buyVolumes[i],
  }));
}

interface ValueArea {
  pocIdx: number;
  vaLowIdx: number;
  vaHighIdx: number;
}

/** POC (Point of Control) + Area de Valor (VA) -- pedido por el usuario
 * (2026-09-15). Algoritmo estandar de "volume profile": arranca en el
 * bucket de mayor volumen (POC) y va sumando, un paso a la vez, el bucket
 * INMEDIATO de arriba o de abajo -- el que tenga mas volumen de los dos --
 * hasta acumular `VALUE_AREA_FRACTION` (70%) del volumen total del rango.
 * VAH/VAL son los bordes superior/inferior de esa zona. */
function computeValueArea(profile: Bucket[]): ValueArea {
  let pocIdx = 0;
  for (let i = 1; i < profile.length; i++) if (profile[i].volume > profile[pocIdx].volume) pocIdx = i;

  const total = profile.reduce((sum, b) => sum + b.volume, 0);
  const target = total * VALUE_AREA_FRACTION;
  let vaLowIdx = pocIdx;
  let vaHighIdx = pocIdx;
  let accumulated = profile[pocIdx].volume;
  while (accumulated < target && (vaLowIdx > 0 || vaHighIdx < profile.length - 1)) {
    const belowVol = vaLowIdx > 0 ? profile[vaLowIdx - 1].volume : -1;
    const aboveVol = vaHighIdx < profile.length - 1 ? profile[vaHighIdx + 1].volume : -1;
    if (aboveVol >= belowVol) {
      vaHighIdx++;
      accumulated += aboveVol;
    } else {
      vaLowIdx--;
      accumulated += belowVol;
    }
  }
  return { pocIdx, vaLowIdx, vaHighIdx };
}

const inRect = (x: number, y: number, r: Rect) =>
  x >= r.left && x <= r.left + r.width && y >= r.top && y <= r.top + r.height;

/**
 * Perfil de Volumen de Rango Fijo (FRVP) -- pedido por el usuario
 * (2026-09-15): selecciona un rango de tiempo a mano (dos clics sobre el
 * grafico, igual que "+ Long"/"+ Short") y muestra la distribucion de
 * volumen por nivel de precio DENTRO de ese rango: el POC (Point of
 * Control, el nivel de mayor volumen -- "actua como iman para el precio"),
 * el Area de Valor (VA, el rango de precio contiguo al POC que concentra
 * el 70% del volumen total, ver `computeValueArea`) y sus bordes VAH/VAL
 * (soportes/resistencias). Calculado 100% en el cliente a partir de las
 * velas ya cargadas (`candles`, con volumen OHLCV real) -- no hace falta
 * ningun endpoint nuevo.
 *
 * Delta de volumen (2026-09-18): cada barra se pinta en dos tramos
 * (verde = compra agresiva / rojo = venta agresiva, `Candle.taker_buy_volume`)
 * mientras el LARGO total de la barra sigue siendo el volumen TOTAL del
 * nivel -- asi POC/VA se siguen leyendo igual, marcados ahora con un
 * BORDE (dorado en el POC, azul en el resto del Area de Valor) en vez de
 * un relleno propio que tapaba el delta.
 *
 * Mismo patron de snapping a un tiempo de vela REAL que
 * `LiquidationVolumePane`/`BigTradesOverlay` (`barTimeForEvent`): los
 * dos extremos del rango se calculan con `coordinateToTime` en la
 * temporalidad en la que se coloco, y sin snapping dejarian de resolver
 * coordenada al cambiar de temporalidad (mismo bug de fondo que
 * TradePositionsOverlay, ver el comentario ahi).
 */
export function FixedRangeVolumeProfile({ chart, series, symbol, candles, placing, onPlaced }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [ranges, setRanges] = useState<FrvpRange[]>(() => loadRanges(symbol));
  const rangesRef = useRef<FrvpRange[]>(ranges);
  rangesRef.current = ranges;
  const candlesRef = useRef<Candle[]>(candles);
  candlesRef.current = candles;
  const draftRef = useRef<{ startTimeMs: number } | null>(null);
  const draftEndRef = useRef<number | null>(null);

  useEffect(() => {
    setRanges(loadRanges(symbol));
  }, [symbol]);

  useEffect(() => {
    saveRanges(symbol, ranges);
  }, [symbol, ranges]);

  // Destildar "+ FRVP" a medio colocar (ya con el primer clic dado) cancela
  // ese primer clic en vez de dejarlo "colgado".
  useEffect(() => {
    if (!placing) draftRef.current = null;
  }, [placing]);

  useEffect(() => {
    if (!chart || !series || !canvasRef.current) return;
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const chartEl = chart.chartElement();

    const getBarTimes = () => series.data().map((b) => Number(b.time)).sort((a, b) => a - b);

    const snappedX = (timeMs: number, barTimes: number[]): number | null => {
      // Primero la resolucion directa/futura (futureTime.ts): un extremo del
      // rango puede estar en el hueco despues de la ultima vela (2026-10-20,
      // pedido del usuario: "extender a mas de la linea del tiempo").
      // `barTimeForEvent` clampearia ese extremo a la ultima vela.
      const x = timeSecToX(chart, barTimes, timeMs / 1000);
      if (x !== null) return x;
      const snapped = barTimeForEvent(barTimes, timeMs / 1000);
      if (snapped === null) return null;
      return chart.timeScale().timeToCoordinate(snapped as UTCTimestamp);
    };

    const closeButtonRect = (x1: number, yTop: number): Rect => ({
      left: x1 - CLOSE_BUTTON_SIZE_PX - 2, top: yTop + 2,
      width: CLOSE_BUTTON_SIZE_PX, height: CLOSE_BUTTON_SIZE_PX,
    });

    // Devuelve la caja dibujada (para poder ubicar el boton de cerrar) o
    // `null` si el rango no se puede resolver a pantalla todavia (fuera de
    // rango de tiempo/precio visible, o sin velas cargadas ahi).
    const renderRange = (startTimeMs: number, endTimeMs: number, barTimes: number[], draft: boolean) => {
      const lo = Math.min(startTimeMs, endTimeMs);
      const hi = Math.max(startTimeMs, endTimeMs);
      const x0 = snappedX(lo, barTimes);
      const x1 = snappedX(hi, barTimes);
      if (x0 === null || x1 === null) return null;
      const profile = computeProfile(candlesRef.current, lo / 1000, hi / 1000);
      if (!profile) return null;
      let maxVol = 0;
      for (const b of profile) if (b.volume > maxVol) maxVol = b.volume;
      if (maxVol <= 0) return null;
      const { pocIdx, vaLowIdx, vaHighIdx } = computeValueArea(profile);

      const yTop = series.priceToCoordinate(profile[profile.length - 1].priceHigh);
      const yBottom = series.priceToCoordinate(profile[0].priceLow);
      if (yTop === null || yBottom === null) return null;

      ctx.setLineDash(draft ? [4, 3] : []);
      ctx.strokeStyle = BOX_BORDER_COLOR;
      ctx.lineWidth = 1;
      ctx.strokeRect(x0, yTop, x1 - x0, yBottom - yTop);
      ctx.setLineDash([]);
      if (!draft) {
        ctx.fillStyle = "rgba(148, 163, 184, 0.06)";
        ctx.fillRect(x0, yTop, x1 - x0, yBottom - yTop);
      }

      const maxBarWidth = (x1 - x0) * MAX_BAR_WIDTH_FRACTION;
      for (let i = 0; i < profile.length; i++) {
        const b = profile[i];
        if (b.volume <= 0) continue;
        const yHigh = series.priceToCoordinate(b.priceHigh);
        const yLow = series.priceToCoordinate(b.priceLow);
        if (yHigh === null || yLow === null) continue;
        const barWidth = (b.volume / maxVol) * maxBarWidth;
        const barTop = Math.min(yHigh, yLow);
        const barHeight = Math.max(1, Math.abs(yLow - yHigh) - 1);
        const buyWidth = Math.max(0, Math.min(barWidth, barWidth * (b.buyVolume / b.volume)));
        ctx.fillStyle = BUY_BAR_COLOR;
        ctx.fillRect(x0, barTop, buyWidth, barHeight);
        ctx.fillStyle = SELL_BAR_COLOR;
        ctx.fillRect(x0 + buyWidth, barTop, Math.max(0, barWidth - buyWidth), barHeight);

        const inValueArea = i >= vaLowIdx && i <= vaHighIdx;
        if (i === pocIdx) {
          ctx.strokeStyle = POC_COLOR;
          ctx.lineWidth = 1.5;
          ctx.strokeRect(x0, barTop, Math.max(1, barWidth), barHeight);
        } else if (inValueArea) {
          ctx.strokeStyle = VA_BORDER_COLOR;
          ctx.lineWidth = 1;
          ctx.strokeRect(x0, barTop, Math.max(1, barWidth), barHeight);
        }
      }

      if (!draft) {
        const pocPrice = (profile[pocIdx].priceLow + profile[pocIdx].priceHigh) / 2;
        const vahPrice = profile[vaHighIdx].priceHigh;
        const valPrice = profile[vaLowIdx].priceLow;
        const drawLevel = (price: number, color: string, dashed: boolean, label: string) => {
          const y = series.priceToCoordinate(price);
          if (y === null) return;
          ctx.strokeStyle = color;
          ctx.lineWidth = dashed ? 1 : 1.5;
          ctx.setLineDash(dashed ? [4, 3] : []);
          ctx.beginPath();
          ctx.moveTo(x0, y);
          ctx.lineTo(x1, y);
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.font = "10px sans-serif";
          ctx.fillStyle = color;
          ctx.textBaseline = "middle";
          ctx.fillText(label, x1 + 4, y);
        };
        // VAH/VAL primero, POC encima -- si dos niveles caen muy cerca (rango
        // angosto), la linea/etiqueta del POC es la que mas importa ver.
        drawLevel(vahPrice, VA_LINE_COLOR, true, `VAH ${vahPrice.toLocaleString()}`);
        drawLevel(valPrice, VA_LINE_COLOR, true, `VAL ${valPrice.toLocaleString()}`);
        drawLevel(pocPrice, POC_LINE_COLOR, false, `POC ${pocPrice.toLocaleString()}`);
      }

      return { x0, x1, yTop };
    };

    const draw = () => {
      const container = canvas.parentElement;
      if (!container) return;
      // Ancho REAL del area de ploteo -- ver chartGeometry.ts (bug real
      // reportado por el usuario, 2026-09-21: el rango FRVP/POC/VAH/VAL
      // invadia los numeros del eje de precio).
      const width = plotWidth(container);
      const height = chart.panes()[0]?.getHeight() ?? container.clientHeight;
      if (width === 0 || height === 0) return;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = width * dpr;
      canvas.height = height * dpr;
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);

      const barTimes = getBarTimes();

      for (const range of rangesRef.current) {
        const box = renderRange(range.startTimeMs, range.endTimeMs, barTimes, false);
        if (!box) continue;
        const closeRect = closeButtonRect(box.x1, box.yTop);
        ctx.strokeStyle = "#94a3b8";
        ctx.lineWidth = 1.5;
        const cx = closeRect.left + closeRect.width / 2;
        const cy = closeRect.top + closeRect.height / 2;
        const r = 4;
        ctx.beginPath();
        ctx.moveTo(cx - r, cy - r);
        ctx.lineTo(cx + r, cy + r);
        ctx.moveTo(cx + r, cy - r);
        ctx.lineTo(cx - r, cy + r);
        ctx.stroke();
      }

      if (placing && draftRef.current && draftEndRef.current !== null) {
        renderRange(draftRef.current.startTimeMs, draftEndRef.current, barTimes, true);
      }
    };

    let rafId: number | null = null;
    const scheduleDraw = () => {
      if (rafId !== null) return;
      rafId = requestAnimationFrame(() => {
        rafId = null;
        draw();
      });
    };

    // --- colocar un rango nuevo (dos clics) -----------------------------
    // Tiempo (ms) del pixel `x`. En la zona futura (despues de la ultima
    // vela) el INICIO del rango se ancla a la ultima vela real -- no hay
    // volumen ahi que perfilar -- y el FINAL se deja donde se hizo clic: la
    // caja se extiende hacia adelante, pero el perfil solo usa las velas que
    // existen (`computeProfile` filtra por `c.time`), asi que la "ultima
    // vela" es de hecho el dato final hasta que lleguen velas nuevas.
    const timeMsAt = (x: number, role: "start" | "end"): number | null => {
      const barTimes = getBarTimes();
      const t = xToTimeSec(chart, barTimes, x);
      if (t === null) return null;
      if (t.isFuture && role === "start") return barTimes[barTimes.length - 1] * 1000;
      return t.timeSec * 1000;
    };

    const onClick = (param: { point?: { x: number; y: number } }) => {
      if (!placing || !param.point) return;
      const timeMs = timeMsAt(param.point.x, draftRef.current ? "end" : "start");
      if (timeMs === null) return;
      if (!draftRef.current) {
        draftRef.current = { startTimeMs: timeMs };
        draftEndRef.current = timeMs;
        scheduleDraw();
        return;
      }
      const range: FrvpRange = {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        startTimeMs: draftRef.current.startTimeMs,
        endTimeMs: timeMs,
      };
      draftRef.current = null;
      draftEndRef.current = null;
      setRanges((prev) => [...prev, range]);
      onPlaced();
    };
    chart.subscribeClick(onClick);

    // Previsualizacion en vivo entre el primer y el segundo clic.
    const onMouseMove = (evt: MouseEvent) => {
      if (!placing || !draftRef.current) return;
      const rect = chartEl.getBoundingClientRect();
      const x = evt.clientX - rect.left;
      const timeMs = timeMsAt(x, "end");
      if (timeMs === null) return;
      draftEndRef.current = timeMs;
      scheduleDraw();
    };
    chartEl.addEventListener("mousemove", onMouseMove);

    // --- cerrar un rango con el boton "x" -------------------------------
    const onMouseDown = (evt: MouseEvent) => {
      const rect = chartEl.getBoundingClientRect();
      const x = evt.clientX - rect.left;
      const y = evt.clientY - rect.top;
      const barTimes = getBarTimes();
      for (const range of rangesRef.current) {
        const lo = Math.min(range.startTimeMs, range.endTimeMs);
        const hi = Math.max(range.startTimeMs, range.endTimeMs);
        const x0 = snappedX(lo, barTimes);
        const x1 = snappedX(hi, barTimes);
        if (x0 === null || x1 === null) continue;
        const profile = computeProfile(candlesRef.current, lo / 1000, hi / 1000);
        if (!profile) continue;
        const yTop = series.priceToCoordinate(profile[profile.length - 1].priceHigh);
        if (yTop === null) continue;
        const closeRect = closeButtonRect(x1, yTop);
        if (inRect(x, y, closeRect)) {
          evt.stopPropagation();
          evt.preventDefault();
          setRanges((prev) => prev.filter((r) => r.id !== range.id));
          return;
        }
      }
    };
    chartEl.addEventListener("mousedown", onMouseDown, { capture: true });

    draw();
    const timeScale = chart.timeScale();
    timeScale.subscribeVisibleTimeRangeChange(scheduleDraw);
    window.addEventListener("resize", scheduleDraw);
    window.addEventListener(CHART_PANES_CHANGED_EVENT, scheduleDraw);
    // Misma red de seguridad que el resto de los overlays de canvas de este
    // proyecto (ver el comentario largo en HeatmapOverlay.tsx) -- ningun
    // evento disponible cubre todos los cambios de geometria de forma
    // fiable.
    const geometryWatchdog = window.setInterval(scheduleDraw, 200);

    return () => {
      chart.unsubscribeClick(onClick);
      chartEl.removeEventListener("mousemove", onMouseMove);
      chartEl.removeEventListener("mousedown", onMouseDown, { capture: true });
      timeScale.unsubscribeVisibleTimeRangeChange(scheduleDraw);
      window.removeEventListener("resize", scheduleDraw);
      window.removeEventListener(CHART_PANES_CHANGED_EVENT, scheduleDraw);
      window.clearInterval(geometryWatchdog);
      if (rafId !== null) cancelAnimationFrame(rafId);
    };
    // ranges/candles se leen via rangesRef/candlesRef dentro de draw() --
    // ver el mismo comentario en TradePositionsOverlay.tsx.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chart, series, symbol, placing, onPlaced]);

  return (
    <canvas
      ref={canvasRef}
      style={{ position: "absolute", top: 0, left: 0, pointerEvents: "none", zIndex: 4 }}
    />
  );
}
