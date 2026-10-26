import { useEffect, useRef, useState } from "react";
import type { IChartApi, ISeriesApi, UTCTimestamp } from "lightweight-charts";
import { CHART_PANES_CHANGED_EVENT } from "../chartLayoutEvent";
import { barTimeForEvent } from "../barTimeForEvent";
import { plotWidth } from "../chartGeometry";

export type PositionSide = "long" | "short";

export interface TradePosition {
  id: string;
  side: PositionSide;
  entryTimeMs: number;
  entryPrice: number;
  tpPrice: number;
  slPrice: number;
}

interface Props {
  chart: IChartApi | null;
  series: ISeriesApi<"Candlestick"> | null;
  symbol: string;
  /** Si esta seteado, el proximo click sobre el pane de precio coloca una
   * posicion nueva de este lado -- lo controla App.tsx (botones "+ Long"/
   * "+ Short" del toolbar). `onPlaced` se llama para que App.tsx desarme
   * el modo despues de colocar una. */
  placingSide: PositionSide | null;
  onPlaced: () => void;
  /** Trades reales de un backtest/validacion ("evidencia", vista Bots,
   * 2026-09-19) -- se dibujan con la MISMA herramienta long/short que ya
   * existe (pedido explicito del usuario: "usemos los indicadores de long
   * y short que ya creamos"), pero de solo lectura: no se guardan en
   * localStorage, no se pueden arrastrar/cerrar con click (no son
   * posiciones hipoteticas del usuario, son resultados YA ocurridos de un
   * backtest). `id` debe ser estable entre renders (no `Date.now()`) para
   * no repintar de mas. */
  readOnlyPositions?: TradePosition[];
  /** Paper trading (2026-10-20): pulsar el boton PAPER de una posicion
   * propia pide lanzarla como operacion simulada (App.tsx abre el
   * formulario). */
  onLaunchPaper?: (position: TradePosition) => void;
  /** id de posicion -> id del trade de paper ya lanzado: esas posiciones
   * muestran "PAPER #n" en vez del boton, para no lanzar la misma dos veces. */
  paperLaunched?: Record<string, number>;
}

// Offsets por defecto al colocar una posicion nueva -- R:R 1:2 (SL 1%,
// TP 2%), un punto de partida razonable que el usuario arrastra despues
// para ajustar a su gusto. No hay "el offset correcto" universal, esto
// es solo el valor inicial.
const DEFAULT_SL_PCT = 0.01;
const DEFAULT_TP_PCT = 0.02;
// Ancho fijo de la caja TP/SL, en pixeles -- mismo enfoque que el ancho
// fijo de CoinGlass/TradingView para su herramienta de posicion larga/
// corta; ajustar el ancho (arrastrando el borde derecho) queda fuera de
// alcance de esta primera version.
const BOX_WIDTH_PX = 140;
const HIT_TOLERANCE_PX = 7;
const CLOSE_BUTTON_SIZE_PX = 14;
// Manija para arrastrar la posicion en el eje X (mover en el tiempo, sin
// tocar los precios) -- pedida por el usuario (2026-09-15): "una vez que lo
// pongo ya no lo puedo mover a ningun lado". Un circulo pequeño arriba de
// la caja, separado de las tres lineas de precio para que no compitan por
// el mismo gesto de arrastre.
const MOVE_HANDLE_SIZE_PX = 10;
const MOVE_HANDLE_GAP_PX = 4;
// Boton "PAPER" junto a la "x" de cerrar (esquina superior derecha de la caja).
const PAPER_BUTTON_WIDTH_PX = 40;
const PAPER_BUTTON_GAP_PX = 4;

const LONG_COLOR = "#26a69a";
const SHORT_COLOR = "#ef5350";
const TP_COLOR = "#26a69a";
const SL_COLOR = "#ef5350";

function storageKey(symbol: string): string {
  return `liq-intel:positions:${symbol}`;
}

function loadPositions(symbol: string): TradePosition[] {
  try {
    const raw = window.localStorage.getItem(storageKey(symbol));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function savePositions(symbol: string, positions: TradePosition[]): void {
  try {
    window.localStorage.setItem(storageKey(symbol), JSON.stringify(positions));
  } catch {
    // localStorage lleno o bloqueado -- las posiciones siguen funcionando
    // en memoria esta sesion, solo no sobreviven un reload.
  }
}

interface DragState {
  positionId: string;
  which: "entry" | "tp" | "sl" | "move";
}

interface Rect {
  left: number;
  top: number;
  width: number;
  height: number;
}

/**
 * Herramienta de posicion Long/Short arrastrable sobre el chart -- pedida
 * por el usuario (2026-09-10): poder colocar una operacion hipotetica en
 * cualquier parte del grafico y ajustar TP/SL a mano, igual que la
 * herramienta nativa de TradingView.com (que `lightweight-charts`, la
 * libreria open-source que usa este proyecto, NO trae incluida -- hay que
 * construirla).
 *
 * Mismo patron de canvas propio que `HeatmapOverlay.tsx`, pero con una
 * diferencia clave: el canvas tiene `pointerEvents:none` iguel, PERO el
 * arrastre de las lineas se intercepta escuchando `mousedown` en el DOM
 * real del chart (`chart.chartElement()`) en fase de CAPTURA, antes de
 * que Lightweight Charts procese ese mismo evento como el inicio de un
 * pan -- si el click cae cerca de una linea nuestra, se llama
 * `stopPropagation()` para que el chart no arranque su propio arrastre, y
 * el resto del gesto (mousemove/mouseup en `window`) lo maneja este
 * componente. Sin esto, cualquier intento de arrastrar una linea
 * terminaria arrastrando el grafico entero por debajo.
 *
 * Dos arreglos (2026-09-15, pedidos por el usuario):
 *
 * 1. **Mover en el eje X.** Antes solo se podian arrastrar las tres lineas
 *    de PRECIO (entry/TP/SL) -- no habia forma de reubicar la posicion en
 *    el TIEMPO una vez colocada. Se agrego una manija circular arriba de
 *    la caja (`which: "move"` en `DragState`) que arrastra `entryTimeMs`
 *    entero (todas las lineas se mueven juntas, los precios no cambian).
 *
 * 2. **Persistencia al cambiar de temporalidad.** `entryTimeMs` se
 *    guardaba TAL CUAL se capturo (un tiempo exacto de vela de 5m, por
 *    ejemplo) y se le pedia a `timeToCoordinate` una coordenada para ESE
 *    mismo valor sin importar la temporalidad activa. Pero
 *    `timeToCoordinate`/`coordinateToTime` no interpolan: solo resuelven
 *    tiempos que YA existen en el eje compartido de la serie actual -- el
 *    mismo bug de fondo, ya documentado y arreglado en
 *    `LiquidationVolumePane.tsx`, que hace que un tiempo de 5m casi nunca
 *    caiga en el grid de 1h. Resultado: al cambiar de 5m a 1h,
 *    `timeToCoordinate` devolvia `null` para el `entryTimeMs` guardado y la
 *    posicion entera desaparecia (`if (x0 === null ...) continue`).
 *    Arreglado con el mismo `barTimeForEvent` (busqueda binaria del tiempo
 *    de vela real mas cercano) usado ahi -- SOLO para resolver la
 *    coordenada de dibujo/hit-testing, nunca para sobreescribir el
 *    `entryTimeMs` guardado.
 */
export function TradePositionsOverlay({
  chart, series, symbol, placingSide, onPlaced, readOnlyPositions = [], onLaunchPaper, paperLaunched = {},
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [positions, setPositions] = useState<TradePosition[]>(() => loadPositions(symbol));
  const positionsRef = useRef<TradePosition[]>(positions);
  positionsRef.current = positions;
  const readOnlyRef = useRef<TradePosition[]>(readOnlyPositions);
  readOnlyRef.current = readOnlyPositions;
  const onLaunchPaperRef = useRef(onLaunchPaper);
  onLaunchPaperRef.current = onLaunchPaper;
  const paperLaunchedRef = useRef(paperLaunched);
  paperLaunchedRef.current = paperLaunched;
  const dragRef = useRef<DragState | null>(null);
  const [dragTooltip, setDragTooltip] = useState<{ x: number; y: number; text: string } | null>(null);

  // Simbolo distinto -> otras posiciones (guardadas aparte por simbolo,
  // no tiene sentido ver un long de BTCUSDT dibujado sobre ETHUSDT).
  useEffect(() => {
    setPositions(loadPositions(symbol));
  }, [symbol]);

  useEffect(() => {
    savePositions(symbol, positions);
  }, [symbol, positions]);

  useEffect(() => {
    if (!chart || !series || !canvasRef.current) return;
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const chartEl = chart.chartElement();

    const getBarTimes = () => series.data().map((b) => Number(b.time)).sort((a, b) => a - b);

    // Snapea `entryTimeMs` al tiempo de vela REAL mas cercano de la serie
    // ACTUAL antes de pedir coordenada -- ver el punto 2 del docstring de
    // arriba. `barTimes` se recibe ya calculado (una vez por draw()/evento
    // de mouse, no una vez por posicion) para no releer `series.data()` en
    // un bucle.
    const snappedX0 = (position: TradePosition, barTimes: number[]): number | null => {
      const snapped = barTimeForEvent(barTimes, position.entryTimeMs / 1000);
      if (snapped === null) return null;
      return chart.timeScale().timeToCoordinate(snapped as UTCTimestamp);
    };

    const priceRect = (
      position: TradePosition, priceKey: "entryPrice" | "tpPrice" | "slPrice", barTimes: number[],
    ): Rect | null => {
      const x0 = snappedX0(position, barTimes);
      const y = series.priceToCoordinate(position[priceKey]);
      if (x0 === null || y === null) return null;
      return { left: x0, top: y - HIT_TOLERANCE_PX, width: BOX_WIDTH_PX, height: HIT_TOLERANCE_PX * 2 };
    };

    const closeButtonRect = (position: TradePosition, barTimes: number[]): Rect | null => {
      const x0 = snappedX0(position, barTimes);
      const yEntry = series.priceToCoordinate(position.entryPrice);
      const yTp = series.priceToCoordinate(position.tpPrice);
      const ySl = series.priceToCoordinate(position.slPrice);
      if (x0 === null || yEntry === null || yTp === null || ySl === null) return null;
      const top = Math.min(yEntry, yTp, ySl);
      return {
        left: x0 + BOX_WIDTH_PX - CLOSE_BUTTON_SIZE_PX - 2, top: top + 2,
        width: CLOSE_BUTTON_SIZE_PX, height: CLOSE_BUTTON_SIZE_PX,
      };
    };

    const paperButtonRect = (position: TradePosition, barTimes: number[]): Rect | null => {
      const close = closeButtonRect(position, barTimes);
      if (!close) return null;
      return {
        left: close.left - PAPER_BUTTON_GAP_PX - PAPER_BUTTON_WIDTH_PX, top: close.top,
        width: PAPER_BUTTON_WIDTH_PX, height: close.height,
      };
    };

    // Manija de arrastre en X -- circulo pequeño arriba-izquierda de la
    // caja, lejos de las tres lineas de precio para que no se disputen el
    // mismo gesto de mousedown.
    const moveHandleRect = (position: TradePosition, barTimes: number[]): Rect | null => {
      const x0 = snappedX0(position, barTimes);
      const yEntry = series.priceToCoordinate(position.entryPrice);
      const yTp = series.priceToCoordinate(position.tpPrice);
      const ySl = series.priceToCoordinate(position.slPrice);
      if (x0 === null || yEntry === null || yTp === null || ySl === null) return null;
      const top = Math.min(yEntry, yTp, ySl);
      return {
        left: x0 - MOVE_HANDLE_SIZE_PX / 2, top: top - MOVE_HANDLE_SIZE_PX - MOVE_HANDLE_GAP_PX,
        width: MOVE_HANDLE_SIZE_PX, height: MOVE_HANDLE_SIZE_PX,
      };
    };

    const inRect = (x: number, y: number, r: Rect) =>
      x >= r.left && x <= r.left + r.width && y >= r.top && y <= r.top + r.height;

    const draw = () => {
      const container = canvas.parentElement;
      if (!container) return;
      // Ancho REAL del area de ploteo -- ver chartGeometry.ts (bug real
      // reportado por el usuario, 2026-09-21: las cajas TP/SL/entrada
      // invadian los numeros del eje de precio).
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

      // `editable`: dibuja ademas la manija de mover y el boton de cerrar
      // -- SOLO para las posiciones propias del usuario (`positionsRef`).
      // Las de solo lectura (`readOnlyRef`, evidencia de un backtest) usan
      // la misma caja/lineas pero sin esos controles, que no tendrian
      // sentido sobre un trade ya ocurrido.
      const drawPosition = (position: TradePosition, editable: boolean) => {
        const x0 = snappedX0(position, barTimes);
        const yEntry = series.priceToCoordinate(position.entryPrice);
        const yTp = series.priceToCoordinate(position.tpPrice);
        const ySl = series.priceToCoordinate(position.slPrice);
        if (x0 === null || yEntry === null || yTp === null || ySl === null) return;
        const x1 = x0 + BOX_WIDTH_PX;

        // Zona de ganancia (entre entrada y TP) y de riesgo (entre
        // entrada y SL) -- coloreadas independientemente de long/short,
        // el TP siempre pinta verde y el SL siempre rojo, sea cual sea
        // la direccion (asi se lee de un vistazo cual es cual sin tener
        // que fijarse en el lado de la posicion).
        ctx.fillStyle = "rgba(38, 166, 154, 0.15)";
        ctx.fillRect(x0, Math.min(yEntry, yTp), x1 - x0, Math.abs(yTp - yEntry));
        ctx.fillStyle = "rgba(239, 83, 80, 0.15)";
        ctx.fillRect(x0, Math.min(yEntry, ySl), x1 - x0, Math.abs(ySl - yEntry));

        const drawLine = (y: number, color: string, dashed: boolean, label: string) => {
          ctx.strokeStyle = color;
          ctx.lineWidth = dashed ? 1.5 : 2;
          ctx.setLineDash(dashed ? [4, 3] : []);
          ctx.beginPath();
          ctx.moveTo(x0, y);
          ctx.lineTo(x1, y);
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.font = "11px sans-serif";
          ctx.fillStyle = color;
          ctx.textBaseline = "middle";
          ctx.fillText(label, x1 + 4, y);
        };

        const pct = (p: number) => (((p - position.entryPrice) / position.entryPrice) * 100).toFixed(2);
        drawLine(yTp, TP_COLOR, true, `TP ${position.tpPrice.toLocaleString()} (${pct(position.tpPrice)}%)`);
        drawLine(ySl, SL_COLOR, true, `SL ${position.slPrice.toLocaleString()} (${pct(position.slPrice)}%)`);
        drawLine(yEntry, position.side === "long" ? LONG_COLOR : SHORT_COLOR, false,
          `${position.side === "long" ? "LONG" : "SHORT"} ${position.entryPrice.toLocaleString()}`);

        if (!editable) return;

        // Manija de arrastre en X -- circulo con dos puntitos ("grip"),
        // color del lado de la posicion para que sea obvio a que caja
        // pertenece cuando hay varias cerca.
        const moveRect = moveHandleRect(position, barTimes);
        if (moveRect) {
          const cx = moveRect.left + moveRect.width / 2;
          const cy = moveRect.top + moveRect.height / 2;
          ctx.fillStyle = position.side === "long" ? LONG_COLOR : SHORT_COLOR;
          ctx.beginPath();
          ctx.arc(cx, cy, moveRect.width / 2, 0, Math.PI * 2);
          ctx.fill();
          ctx.fillStyle = "#0b0e11";
          for (const dx of [-2, 2]) {
            ctx.beginPath();
            ctx.arc(cx + dx, cy, 1, 0, Math.PI * 2);
            ctx.fill();
          }
        }

        // Boton PAPER (lanzar como operacion simulada) o, si ya se lanzo, la
        // etiqueta con el numero del trade.
        const paperRect = paperButtonRect(position, barTimes);
        if (paperRect && onLaunchPaperRef.current) {
          const launchedId = paperLaunchedRef.current[position.id];
          const color = position.side === "long" ? LONG_COLOR : SHORT_COLOR;
          ctx.font = "bold 9px sans-serif";
          ctx.textBaseline = "middle";
          ctx.textAlign = "center";
          if (launchedId !== undefined) {
            ctx.fillStyle = "#94a3b8";
            ctx.fillText(`PAPER #${launchedId}`, paperRect.left + paperRect.width / 2, paperRect.top + paperRect.height / 2);
          } else {
            ctx.fillStyle = color;
            ctx.fillRect(paperRect.left, paperRect.top, paperRect.width, paperRect.height);
            ctx.fillStyle = "#0b0e11";
            ctx.fillText("PAPER", paperRect.left + paperRect.width / 2, paperRect.top + paperRect.height / 2);
          }
          ctx.textAlign = "start";
        }

        // Boton de cerrar -- una "x" simple en la esquina superior derecha.
        const closeRect = closeButtonRect(position, barTimes);
        if (closeRect) {
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
      };

      for (const position of readOnlyRef.current) drawPosition(position, false);
      for (const position of positionsRef.current) drawPosition(position, true);
    };

    let rafId: number | null = null;
    const scheduleDraw = () => {
      if (rafId !== null) return;
      rafId = requestAnimationFrame(() => {
        rafId = null;
        draw();
      });
    };

    // --- colocar una posicion nueva ------------------------------------
    const onClick = (param: { point?: { x: number; y: number }; time?: unknown }) => {
      if (!placingSide || !param.point) return;
      const price = series.coordinateToPrice(param.point.y);
      const time = chart.timeScale().coordinateToTime(param.point.x);
      if (price === null || time === null) return;
      const entryPrice = price;
      const sign = placingSide === "long" ? 1 : -1;
      const position: TradePosition = {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        side: placingSide,
        entryTimeMs: Number(time) * 1000,
        entryPrice,
        tpPrice: entryPrice * (1 + sign * DEFAULT_TP_PCT),
        slPrice: entryPrice * (1 - sign * DEFAULT_SL_PCT),
      };
      setPositions((prev) => [...prev, position]);
      onPlaced();
    };
    chart.subscribeClick(onClick);

    // --- arrastrar entrada/TP/SL, y cerrar con el boton "x" ------------
    const onMouseDown = (evt: MouseEvent) => {
      const rect = chartEl.getBoundingClientRect();
      const x = evt.clientX - rect.left;
      const y = evt.clientY - rect.top;
      const barTimes = getBarTimes();
      for (const position of positionsRef.current) {
        const paperRect = paperButtonRect(position, barTimes);
        if (paperRect && onLaunchPaperRef.current && inRect(x, y, paperRect)) {
          evt.stopPropagation();
          evt.preventDefault();
          if (paperLaunchedRef.current[position.id] === undefined) onLaunchPaperRef.current(position);
          return;
        }
        const closeRect = closeButtonRect(position, barTimes);
        if (closeRect && inRect(x, y, closeRect)) {
          evt.stopPropagation();
          evt.preventDefault();
          setPositions((prev) => prev.filter((p) => p.id !== position.id));
          return;
        }
        const moveRect = moveHandleRect(position, barTimes);
        if (moveRect && inRect(x, y, moveRect)) {
          evt.stopPropagation();
          evt.preventDefault();
          dragRef.current = { positionId: position.id, which: "move" };
          return;
        }
        for (const which of ["entry", "tp", "sl"] as const) {
          const key = which === "entry" ? "entryPrice" : which === "tp" ? "tpPrice" : "slPrice";
          const r = priceRect(position, key, barTimes);
          if (r && inRect(x, y, r)) {
            evt.stopPropagation();
            evt.preventDefault();
            dragRef.current = { positionId: position.id, which };
            return;
          }
        }
      }
    };
    // Captura: antes de que Lightweight Charts vea el mismo mousedown y
    // arranque su propio pan si no lo frenamos con stopPropagation.
    chartEl.addEventListener("mousedown", onMouseDown, { capture: true });

    const onWindowMouseMove = (evt: MouseEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      const rect = chartEl.getBoundingClientRect();
      const x = evt.clientX - rect.left;
      const y = evt.clientY - rect.top;
      if (drag.which === "move") {
        // Mover en X -- el tiempo que devuelve `coordinateToTime` en la
        // temporalidad ACTUAL ya es un tiempo de vela real (misma API que
        // usa `onClick` para colocar la posicion), no hace falta snapping.
        const time = chart.timeScale().coordinateToTime(x);
        if (time === null) return;
        const entryTimeMs = Number(time) * 1000;
        setPositions((prev) =>
          prev.map((p) => (p.id === drag.positionId ? { ...p, entryTimeMs } : p)),
        );
        setDragTooltip({
          x: x + 12, y: y + 12,
          text: new Date(entryTimeMs).toLocaleString(),
        });
        return;
      }
      const price = series.coordinateToPrice(y);
      if (price === null) return;
      setPositions((prev) =>
        prev.map((p) => {
          if (p.id !== drag.positionId) return p;
          if (drag.which === "entry") return { ...p, entryPrice: price };
          if (drag.which === "tp") return { ...p, tpPrice: price };
          return { ...p, slPrice: price };
        }),
      );
      setDragTooltip({ x: x + 12, y: y + 12, text: price.toLocaleString() });
    };
    const onWindowMouseUp = () => {
      dragRef.current = null;
      setDragTooltip(null);
    };
    window.addEventListener("mousemove", onWindowMouseMove);
    window.addEventListener("mouseup", onWindowMouseUp);

    draw();
    const timeScale = chart.timeScale();
    timeScale.subscribeVisibleTimeRangeChange(scheduleDraw);
    window.addEventListener("resize", scheduleDraw);
    window.addEventListener(CHART_PANES_CHANGED_EVENT, scheduleDraw);
    const geometryWatchdog = window.setInterval(scheduleDraw, 200);

    return () => {
      chart.unsubscribeClick(onClick);
      chartEl.removeEventListener("mousedown", onMouseDown, { capture: true });
      window.removeEventListener("mousemove", onWindowMouseMove);
      window.removeEventListener("mouseup", onWindowMouseUp);
      timeScale.unsubscribeVisibleTimeRangeChange(scheduleDraw);
      window.removeEventListener("resize", scheduleDraw);
      window.removeEventListener(CHART_PANES_CHANGED_EVENT, scheduleDraw);
      window.clearInterval(geometryWatchdog);
      if (rafId !== null) cancelAnimationFrame(rafId);
    };
    // positions se lee via positionsRef dentro de draw()/handlers para no
    // tener que re-suscribir todo el efecto en cada arrastre -- solo hace
    // falta re-ejecutar el efecto cuando cambia el chart/series/symbol.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chart, series, symbol, placingSide, onPlaced]);

  return (
    <>
      <canvas
        ref={canvasRef}
        style={{ position: "absolute", top: 0, left: 0, pointerEvents: "none", zIndex: 5 }}
      />
      {dragTooltip && (
        <div
          style={{
            position: "absolute", left: dragTooltip.x, top: dragTooltip.y, pointerEvents: "none",
            zIndex: 6, background: "rgba(15, 23, 42, 0.92)", border: "1px solid rgba(148, 163, 184, 0.35)",
            borderRadius: 4, padding: "2px 6px", fontSize: 12, color: "#e2e8f0",
          }}
        >
          {dragTooltip.text}
        </div>
      )}
    </>
  );
}
