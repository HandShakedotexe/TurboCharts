import { useEffect, useRef, useState } from "react";
import type { IChartApi, ISeriesApi, UTCTimestamp } from "lightweight-charts";
import { CHART_PANES_CHANGED_EVENT } from "../chartLayoutEvent";
import { barTimeForEvent } from "../barTimeForEvent";
import { plotWidth } from "../chartGeometry";
import { timeSecToX, xToTimeSec } from "../futureTime";
import { makeRect, makeTrend, type Drawing, type DrawingKind, type LineStyle, type RectStyle } from "../drawings";

interface Point {
  timeMs: number;
  price: number;
}

interface Props {
  chart: IChartApi | null;
  series: ISeriesApi<"Candlestick"> | null;
  drawings: Drawing[];
  onDrawingsChange: (next: Drawing[]) => void;
  selectedId: string | null;
  onSelectedIdChange: (id: string | null) => void;
  /** Herramienta armada desde ChartToolsPanel.tsx -- null = nada armado,
   * el overlay solo permite seleccionar/arrastrar lo que ya existe. */
  placing: DrawingKind | null;
  onPlaced: () => void;
  /** Estilo "pluma" actual -- se aplica a la PROXIMA linea/rectangulo que
   * se dibuje (ver ChartToolsPanel.tsx). */
  lineStyle: LineStyle;
  rectStyle: RectStyle;
  /** "Ocultar dibujos" del panel de herramientas: no se pintan ni reaccionan
   * al mouse, pero siguen en `drawings` (y en localStorage). */
  hidden: boolean;
  /** Se llama UNA vez al empezar cada gesto que va a cambiar `drawings`
   * (crear, arrastrar, redimensionar), antes del primer cambio -- App.tsx
   * guarda ahi el punto de deshacer. */
  onBeginChange: () => void;
}

const HIT_TOLERANCE_PX = 6;
const HANDLE_SIZE_PX = 8;
const MIN_DRAG_PX = 4;

interface Rect {
  left: number;
  top: number;
  width: number;
  height: number;
}

function inRect(x: number, y: number, r: Rect): boolean {
  return x >= r.left && x <= r.left + r.width && y >= r.top && y <= r.top + r.height;
}

function handleRect(cx: number, cy: number): Rect {
  return { left: cx - HANDLE_SIZE_PX / 2, top: cy - HANDLE_SIZE_PX / 2, width: HANDLE_SIZE_PX, height: HANDLE_SIZE_PX };
}

// Distancia perpendicular de (x,y) al segmento (x1,y1)-(x2,y2), acotada a
// los extremos del segmento (no a la recta infinita) -- una linea de
// tendencia es un tramo, no un rayo.
function distToSegment(x: number, y: number, x1: number, y1: number, x2: number, y2: number): number {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const lenSq = dx * dx + dy * dy;
  let t = lenSq === 0 ? 0 : ((x - x1) * dx + (y - y1) * dy) / lenSq;
  t = Math.max(0, Math.min(1, t));
  const px = x1 + t * dx;
  const py = y1 + t * dy;
  return Math.hypot(x - px, y - py);
}

type DragKind =
  | { kind: "hline" | "vline"; id: string }
  | { kind: "trend-p1" | "trend-p2" | "trend-move"; id: string; startTimeMs: number; startPrice: number; origP1: Point; origP2: Point }
  | { kind: "rect-corner"; id: string; fixedTimeMs: number; fixedPrice: number }
  | { kind: "rect-move"; id: string; startTimeMs: number; startPrice: number; origP1: Point; origP2: Point };

interface Draft {
  kind: "trend" | "rect";
  start: Point;
  end: Point;
}

/**
 * Herramientas de dibujo sobre el chart -- linea horizontal, vertical,
 * diagonal (trend line) y rectangulo, pedidas por el usuario (2026-09-20).
 * MISMO patron de canvas propio + interaccion por captura de mousedown en
 * `chart.chartElement()` que `TradePositionsOverlay.tsx` (ver su docstring
 * para el porque) -- generalizado a 4 tipos de objeto en vez de uno solo.
 *
 * Flujo de colocacion:
 * - hline/vline: un solo click (`chart.subscribeClick`), igual que colocar
 *   una posicion Long/Short.
 * - trend/rect: arrastre completo (mousedown fija el primer punto,
 *   mousemove en `window` dibuja una vista previa -- `draftRef`, nunca
 *   entra al array `drawings` hasta que se suelta -- y mouseup confirma).
 *
 * Edicion: click sobre un dibujo YA existente lo selecciona (no lo
 * arrastra todavia); con la seleccion activa, un segundo click-y-arrastre
 * sobre su cuerpo lo mueve, y sobre sus manijas (extremos de la linea de
 * tendencia, esquinas del rectangulo) lo redimensiona. El rectangulo usa
 * un truco para las 4 esquinas: al arrastrar una, la esquina OPUESTA se
 * congela como `p1` (se recalcula una sola vez al iniciar el arrastre) y
 * la esquina arrastrada pasa a ser `p2`, actualizada en vivo -- evita
 * tener que rastrear que punto original (p1 o p2) "es" cada esquina
 * visual, que cambia segun desde donde se dibujo el rectangulo.
 */
export function DrawingToolsOverlay({
  chart, series, drawings, onDrawingsChange, selectedId, onSelectedIdChange, placing, onPlaced, lineStyle, rectStyle,
  hidden, onBeginChange,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const drawingsRef = useRef<Drawing[]>(drawings);
  drawingsRef.current = drawings;
  const selectedIdRef = useRef<string | null>(selectedId);
  selectedIdRef.current = selectedId;
  const placingRef = useRef<DrawingKind | null>(placing);
  placingRef.current = placing;
  const styleRef = useRef(lineStyle);
  styleRef.current = lineStyle;
  const rectStyleRef = useRef(rectStyle);
  rectStyleRef.current = rectStyle;
  const hiddenRef = useRef(hidden);
  hiddenRef.current = hidden;
  const beginChangeRef = useRef(onBeginChange);
  beginChangeRef.current = onBeginChange;
  const dragRef = useRef<DragKind | null>(null);
  // Ref (no variable local del efecto): el efecto se re-suscribe en cada
  // render de App (`onPlaced` llega como funcion nueva), y un `let` local
  // volveria a false en pleno arrastre -- un paso de deshacer POR mousemove.
  const gestureStartedRef = useRef(false);
  const draftRef = useRef<Draft | null>(null);
  const [dragTooltip, setDragTooltip] = useState<{ x: number; y: number; text: string } | null>(null);

  useEffect(() => {
    if (!chart || !series || !canvasRef.current) return;
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const chartEl = chart.chartElement();

    const getBarTimes = () => series.data().map((b) => Number(b.time)).sort((a, b) => a - b);

    // Un gesto (crear, o arrastrar de mousedown a mouseup) = UN paso de
    // deshacer: `emit` avisa a App.tsx antes del primer cambio del gesto y
    // nunca mas hasta que el gesto termina.
    const emit = (next: Drawing[]) => {
      if (!gestureStartedRef.current) {
        gestureStartedRef.current = true;
        beginChangeRef.current();
      }
      onDrawingsChange(next);
    };

    // `timeToCoordinate`/`coordinateToTime` devuelven `null` mas alla de la
    // ultima vela real (ver futureTime.ts, bug real 2026-09-21): se
    // extrapola a mano con el espaciado de barra actual.
    const snapX = (timeMs: number, barTimes: number[]): number | null => {
      const timeSec = timeMs / 1000;
      const x = timeSecToX(chart, barTimes, timeSec);
      if (x !== null) return x;
      // `barTimeForEvent` se queda como respaldo SOLO para tiempos DENTRO
      // del rango historico que no resuelven directo (p.ej. un ancla vieja
      // de OTRA temporalidad, ver TradePositionsOverlay.tsx) -- nunca para
      // el futuro, donde clampearia al ultimo real en vez de extrapolar.
      const snapped = barTimeForEvent(barTimes, timeSec);
      if (snapped === null) return null;
      return chart.timeScale().timeToCoordinate(snapped as UTCTimestamp);
    };

    const pointToPixel = (p: Point, barTimes: number[]): { x: number; y: number } | null => {
      const x = snapX(p.timeMs, barTimes);
      const y = series.priceToCoordinate(p.price);
      if (x === null || y === null) return null;
      return { x, y };
    };

    const pixelToPoint = (x: number, y: number): Point | null => {
      const price = series.coordinateToPrice(y);
      if (price === null) return null;
      const t = xToTimeSec(chart, getBarTimes(), x);
      return t ? { timeMs: t.timeSec * 1000, price } : null;
    };

    const draw = () => {
      const container = canvas.parentElement;
      if (!container) return;
      // Ancho REAL del area de ploteo -- ver chartGeometry.ts (bug real
      // reportado por el usuario, 2026-09-21: una linea horizontal
      // dibujada "de borde a borde" invadia los numeros del eje).
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
      if (hiddenRef.current) return;

      const barTimes = getBarTimes();
      const selected = selectedIdRef.current;

      const strokeLine = (x1: number, y1: number, x2: number, y2: number, style: LineStyle, isSelected: boolean) => {
        ctx.strokeStyle = style.color;
        ctx.lineWidth = style.thickness;
        ctx.setLineDash(isSelected ? [6, 4] : []);
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
        ctx.setLineDash([]);
      };

      const drawHandle = (x: number, y: number, color: string) => {
        ctx.fillStyle = color;
        ctx.fillRect(x - HANDLE_SIZE_PX / 2, y - HANDLE_SIZE_PX / 2, HANDLE_SIZE_PX, HANDLE_SIZE_PX);
        ctx.strokeStyle = "#0b0e11";
        ctx.lineWidth = 1;
        ctx.strokeRect(x - HANDLE_SIZE_PX / 2, y - HANDLE_SIZE_PX / 2, HANDLE_SIZE_PX, HANDLE_SIZE_PX);
      };

      for (const d of drawingsRef.current) {
        const isSelected = d.id === selected;
        if (d.kind === "hline") {
          const y = series.priceToCoordinate(d.price);
          if (y === null) continue;
          strokeLine(0, y, width, y, d.style, isSelected);
          ctx.font = "11px sans-serif";
          ctx.fillStyle = d.style.color;
          ctx.textBaseline = "middle";
          ctx.fillText(d.price.toLocaleString(), width - 70, y - 6);
        } else if (d.kind === "vline") {
          const x = snapX(d.timeMs, barTimes);
          if (x === null) continue;
          strokeLine(x, 0, x, height, d.style, isSelected);
        } else if (d.kind === "trend") {
          const p1 = pointToPixel(d.p1, barTimes);
          const p2 = pointToPixel(d.p2, barTimes);
          if (!p1 || !p2) continue;
          strokeLine(p1.x, p1.y, p2.x, p2.y, d.style, isSelected);
          if (isSelected) {
            drawHandle(p1.x, p1.y, d.style.color);
            drawHandle(p2.x, p2.y, d.style.color);
          }
        } else {
          const p1 = pointToPixel(d.p1, barTimes);
          const p2 = pointToPixel(d.p2, barTimes);
          if (!p1 || !p2) continue;
          const left = Math.min(p1.x, p2.x);
          const top = Math.min(p1.y, p2.y);
          const w = Math.abs(p2.x - p1.x);
          const h = Math.abs(p2.y - p1.y);
          ctx.fillStyle = hexToRgba(d.style.fillColor, d.style.fillOpacity);
          ctx.fillRect(left, top, w, h);
          ctx.strokeStyle = d.style.color;
          ctx.lineWidth = d.style.thickness;
          ctx.setLineDash(isSelected ? [6, 4] : []);
          ctx.strokeRect(left, top, w, h);
          ctx.setLineDash([]);
          if (isSelected) {
            for (const [cx, cy] of [[left, top], [left + w, top], [left, top + h], [left + w, top + h]] as const) {
              drawHandle(cx, cy, d.style.color);
            }
          }
        }
      }

      // Vista previa mientras se arrastra un trend/rect nuevo -- nunca
      // toca `drawings`, solo se dibuja.
      const draft = draftRef.current;
      if (draft) {
        const s = pointToPixel(draft.start, barTimes);
        const e = pointToPixel(draft.end, barTimes);
        if (s && e) {
          if (draft.kind === "trend") {
            strokeLine(s.x, s.y, e.x, e.y, styleRef.current, false);
          } else {
            const left = Math.min(s.x, e.x);
            const top = Math.min(s.y, e.y);
            const w = Math.abs(e.x - s.x);
            const h = Math.abs(e.y - s.y);
            ctx.fillStyle = hexToRgba(rectStyleRef.current.fillColor, rectStyleRef.current.fillOpacity);
            ctx.fillRect(left, top, w, h);
            ctx.strokeStyle = rectStyleRef.current.color;
            ctx.lineWidth = rectStyleRef.current.thickness;
            ctx.strokeRect(left, top, w, h);
          }
        }
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

    // --- colocar hline/vline con un solo click --------------------------
    const onClick = (param: { point?: { x: number; y: number } }) => {
      const mode = placingRef.current;
      if (!mode || !param.point || hiddenRef.current) return;
      if (mode === "hline") {
        const price = series.coordinateToPrice(param.point.y);
        if (price === null) return;
        const id = newId();
        emit([...drawingsRef.current, { id, kind: "hline", price, style: styleRef.current }]);
        // Seleccionado de una vez al crearlo -- el usuario puede arrastrarlo
        // de inmediato (o ver sus manijas, en trend/rect) sin un click
        // aparte solo para seleccionar lo que acaba de dibujar.
        onSelectedIdChange(id);
        gestureStartedRef.current = false;
        onPlaced();
      } else if (mode === "vline") {
        const t = xToTimeSec(chart, getBarTimes(), param.point.x);
        if (t === null) return;
        const id = newId();
        emit([
          ...drawingsRef.current,
          { id, kind: "vline", timeMs: t.timeSec * 1000, style: styleRef.current },
        ]);
        onSelectedIdChange(id);
        gestureStartedRef.current = false;
        onPlaced();
      }
    };
    chart.subscribeClick(onClick);

    // --- mousedown: iniciar trend/rect nuevo, o seleccionar/arrastrar ---
    const onMouseDown = (evt: MouseEvent) => {
      const rect = chartEl.getBoundingClientRect();
      const x = evt.clientX - rect.left;
      const y = evt.clientY - rect.top;
      if (hiddenRef.current) return;
      const mode = placingRef.current;

      if (mode === "trend" || mode === "rect") {
        const p = pixelToPoint(x, y);
        if (!p) return;
        evt.stopPropagation();
        evt.preventDefault();
        draftRef.current = { kind: mode, start: p, end: p };
        return;
      }

      const barTimes = getBarTimes();
      const selected = selectedIdRef.current;

      for (const d of drawingsRef.current) {
        if (d.kind === "trend" && d.id === selected) {
          const p1 = pointToPixel(d.p1, barTimes);
          const p2 = pointToPixel(d.p2, barTimes);
          if (p1 && inRect(x, y, handleRect(p1.x, p1.y))) {
            evt.stopPropagation();
            evt.preventDefault();
            dragRef.current = { kind: "trend-p1", id: d.id, startTimeMs: 0, startPrice: 0, origP1: d.p1, origP2: d.p2 };
            return;
          }
          if (p2 && inRect(x, y, handleRect(p2.x, p2.y))) {
            evt.stopPropagation();
            evt.preventDefault();
            dragRef.current = { kind: "trend-p2", id: d.id, startTimeMs: 0, startPrice: 0, origP1: d.p1, origP2: d.p2 };
            return;
          }
        }
        if (d.kind === "rect" && d.id === selected) {
          const p1 = pointToPixel(d.p1, barTimes);
          const p2 = pointToPixel(d.p2, barTimes);
          if (p1 && p2) {
            const left = Math.min(p1.x, p2.x);
            const top = Math.min(p1.y, p2.y);
            const w = Math.abs(p2.x - p1.x);
            const h = Math.abs(p2.y - p1.y);
            const corners: Array<[number, number]> = [[left, top], [left + w, top], [left, top + h], [left + w, top + h]];
            for (const [cx, cy] of corners) {
              if (inRect(x, y, handleRect(cx, cy))) {
                evt.stopPropagation();
                evt.preventDefault();
                // Esquina OPUESTA (diagonal) congelada como punto fijo --
                // ver docstring del componente.
                const oppX = cx === left ? left + w : left;
                const oppY = cy === top ? top + h : top;
                const opp = pixelToPointFromCanvas(oppX, oppY, chart, series);
                if (!opp) return;
                dragRef.current = { kind: "rect-corner", id: d.id, fixedTimeMs: opp.timeMs, fixedPrice: opp.price };
                return;
              }
            }
          }
        }
      }

      // Hit-test general del CUERPO (linea/segmento/relleno) -- a
      // diferencia de las manijas de arriba (solo si YA estaba
      // seleccionado, tienen sentido nada mas cuando se ven), esto arma
      // el arrastre en la MISMA pasada aunque el dibujo todavia no
      // estuviera seleccionado. Pedido del usuario (2026-09-21): "si la
      // selecciono despues de dibujarla debo poderla mover" -- exigir un
      // primer click SOLO para seleccionar y un segundo para recien poder
      // arrastrar no es como espera moverla nadie; un click sin arrastre
      // sigue sin mover nada (`origP1`==`origP2` al soltar sin mover el
      // mouse), asi que seleccionar-y-opcionalmente-mover en un solo
      // gesto no le quita la posibilidad de solo seleccionar.
      for (const d of drawingsRef.current) {
        let hit = false;
        if (d.kind === "hline") {
          const yy = series.priceToCoordinate(d.price);
          hit = yy !== null && Math.abs(y - yy) <= HIT_TOLERANCE_PX;
        } else if (d.kind === "vline") {
          const xx = snapX(d.timeMs, barTimes);
          hit = xx !== null && Math.abs(x - xx) <= HIT_TOLERANCE_PX;
        } else if (d.kind === "trend") {
          const p1 = pointToPixel(d.p1, barTimes);
          const p2 = pointToPixel(d.p2, barTimes);
          hit = !!p1 && !!p2 && distToSegment(x, y, p1.x, p1.y, p2.x, p2.y) <= HIT_TOLERANCE_PX;
        } else {
          const p1 = pointToPixel(d.p1, barTimes);
          const p2 = pointToPixel(d.p2, barTimes);
          if (p1 && p2) {
            const left = Math.min(p1.x, p2.x);
            const top = Math.min(p1.y, p2.y);
            hit = inRect(x, y, { left, top, width: Math.abs(p2.x - p1.x), height: Math.abs(p2.y - p1.y) });
          }
        }
        if (!hit) continue;
        evt.stopPropagation();
        evt.preventDefault();
        if (d.id !== selected) onSelectedIdChange(d.id);
        const p = pixelToPoint(x, y);
        if (!p) return;
        if (d.kind === "hline" || d.kind === "vline") {
          dragRef.current = { kind: d.kind, id: d.id };
        } else if (d.kind === "trend") {
          dragRef.current = { kind: "trend-move", id: d.id, startTimeMs: p.timeMs, startPrice: p.price, origP1: d.p1, origP2: d.p2 };
        } else {
          dragRef.current = { kind: "rect-move", id: d.id, startTimeMs: p.timeMs, startPrice: p.price, origP1: d.p1, origP2: d.p2 };
        }
        return;
      }

      // Ningun dibujo bajo el cursor -- deseleccionar, dejar que el chart
      // haga su propio pan normalmente (sin stopPropagation).
      if (selected) onSelectedIdChange(null);
    };
    chartEl.addEventListener("mousedown", onMouseDown, { capture: true });

    const onWindowMouseMove = (evt: MouseEvent) => {
      const rect = chartEl.getBoundingClientRect();
      const x = evt.clientX - rect.left;
      const y = evt.clientY - rect.top;

      if (draftRef.current) {
        const p = pixelToPoint(x, y);
        if (p) {
          draftRef.current = { ...draftRef.current, end: p };
          scheduleDraw();
        }
        return;
      }

      const drag = dragRef.current;
      if (!drag) return;
      const p = pixelToPoint(x, y);
      if (!p) return;

      if (drag.kind === "hline") {
        emit(
          drawingsRef.current.map((d) => (d.id === drag.id && d.kind === "hline" ? { ...d, price: p.price } : d)),
        );
        setDragTooltip({ x: x + 12, y: y + 12, text: p.price.toLocaleString() });
      } else if (drag.kind === "vline") {
        emit(
          drawingsRef.current.map((d) => (d.id === drag.id && d.kind === "vline" ? { ...d, timeMs: p.timeMs } : d)),
        );
        setDragTooltip({ x: x + 12, y: y + 12, text: new Date(p.timeMs).toLocaleString() });
      } else if (drag.kind === "trend-p1") {
        emit(drawingsRef.current.map((d) => (d.id === drag.id && d.kind === "trend" ? { ...d, p1: p } : d)));
      } else if (drag.kind === "trend-p2") {
        emit(drawingsRef.current.map((d) => (d.id === drag.id && d.kind === "trend" ? { ...d, p2: p } : d)));
      } else if (drag.kind === "trend-move") {
        const dt = p.timeMs - drag.startTimeMs;
        const dp = p.price - drag.startPrice;
        emit(
          drawingsRef.current.map((d) =>
            d.id === drag.id && d.kind === "trend"
              ? { ...d, p1: { timeMs: drag.origP1.timeMs + dt, price: drag.origP1.price + dp }, p2: { timeMs: drag.origP2.timeMs + dt, price: drag.origP2.price + dp } }
              : d,
          ),
        );
      } else if (drag.kind === "rect-corner") {
        emit(
          drawingsRef.current.map((d) =>
            d.id === drag.id && d.kind === "rect"
              ? { ...d, p1: { timeMs: drag.fixedTimeMs, price: drag.fixedPrice }, p2: p }
              : d,
          ),
        );
      } else if (drag.kind === "rect-move") {
        const dt = p.timeMs - drag.startTimeMs;
        const dp = p.price - drag.startPrice;
        emit(
          drawingsRef.current.map((d) =>
            d.id === drag.id && d.kind === "rect"
              ? { ...d, p1: { timeMs: drag.origP1.timeMs + dt, price: drag.origP1.price + dp }, p2: { timeMs: drag.origP2.timeMs + dt, price: drag.origP2.price + dp } }
              : d,
          ),
        );
      }
    };

    const onWindowMouseUp = (evt: MouseEvent) => {
      const draft = draftRef.current;
      if (draft) {
        draftRef.current = null;
        const rect = chartEl.getBoundingClientRect();
        const x = evt.clientX - rect.left;
        const y = evt.clientY - rect.top;
        const barTimes = getBarTimes();
        const s = pointToPixel(draft.start, barTimes);
        const e = pointToPixel(draft.end, barTimes);
        const movedEnough = !!s && !!e && Math.hypot(e.x - s.x, e.y - s.y) >= MIN_DRAG_PX;
        if (movedEnough) {
          const finalEnd = pixelToPoint(x, y) ?? draft.end;
          const next =
            draft.kind === "trend"
              ? makeTrend(draft.start, finalEnd, styleRef.current)
              : makeRect(draft.start, finalEnd, rectStyleRef.current);
          emit([...drawingsRef.current, next]);
          // Mismo criterio que hline/vline: seleccionado de una vez, sus
          // manijas quedan visibles sin un click aparte.
          onSelectedIdChange(next.id);
        }
        onPlaced();
        scheduleDraw();
      }
      dragRef.current = null;
      gestureStartedRef.current = false;
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
    // El resto de las props (drawings/selectedId/style) se leen via refs
    // dentro de draw()/handlers -- solo hace falta re-suscribir el efecto
    // cuando cambia el chart/series/placing (que si cambia el comportamiento
    // de mousedown/click en si).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chart, series, placing, onPlaced, onDrawingsChange, onSelectedIdChange]);

  return (
    <>
      <canvas ref={canvasRef} style={{ position: "absolute", top: 0, left: 0, pointerEvents: "none", zIndex: 7 }} />
      {dragTooltip && (
        <div
          style={{
            position: "absolute", left: dragTooltip.x, top: dragTooltip.y, pointerEvents: "none",
            zIndex: 8, background: "rgba(15, 23, 42, 0.92)", border: "1px solid rgba(148, 163, 184, 0.35)",
            borderRadius: 4, padding: "2px 6px", fontSize: 12, color: "#e2e8f0",
          }}
        >
          {dragTooltip.text}
        </div>
      )}
    </>
  );
}

function newId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function hexToRgba(hex: string, opacity: number): string {
  const clean = hex.replace("#", "");
  const r = parseInt(clean.slice(0, 2), 16);
  const g = parseInt(clean.slice(2, 4), 16);
  const b = parseInt(clean.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${opacity})`;
}

// Helper standalone (no captura closures del efecto) para resolver la
// esquina opuesta congelada al iniciar un arrastre de esquina del
// rectangulo -- necesita chart/series pero no el resto del estado del
// efecto, asi que vive fuera del componente. Usa la version con futuro
// (futureTime.ts): una esquina en el hueco despues de la ultima vela no
// resuelve con `coordinateToTime` a secas.
function pixelToPointFromCanvas(
  x: number, y: number, chart: IChartApi, series: ISeriesApi<"Candlestick">,
): Point | null {
  const barTimes = series.data().map((b) => Number(b.time)).sort((a, b) => a - b);
  const t = xToTimeSec(chart, barTimes, x);
  const price = series.coordinateToPrice(y);
  if (t === null || price === null) return null;
  return { timeMs: t.timeSec * 1000, price };
}
