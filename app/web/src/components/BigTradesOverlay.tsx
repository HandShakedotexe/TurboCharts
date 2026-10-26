import { useEffect, useRef, useState } from "react";
import type { IChartApi, ISeriesApi, UTCTimestamp } from "lightweight-charts";
import { fetchBigTrades, type BigTradeEvent } from "../api";
import { CHART_PANES_CHANGED_EVENT } from "../chartLayoutEvent";
import { barTimeForEvent } from "../barTimeForEvent";
import { plotWidth } from "../chartGeometry";

interface Props {
  chart: IChartApi | null;
  series: ISeriesApi<"Candlestick"> | null;
  symbol: string;
  interval: string;
  /** Oculta trades por debajo de este notional -- configurable desde el
   * toolbar (App.tsx), mismo patron que "Liquidity Threshold" del heatmap.
   * Nunca baja del piso real del backend (`bigtrades.BIG_TRADE_THRESHOLD_USD`,
   * $100k): un valor menor a eso no traeria mas datos, el backend ya los
   * descarto antes de que lleguen aca. */
  minNotionalUsd: number;
  /** Multiplicador del radio maximo de burbuja -- configurable, ver
   * BUBBLE_SCALE_MIN/MAX en App.tsx. */
  bubbleScale: number;
  /** Dias hacia atras a pedir/dibujar -- configurable desde el toolbar
   * (App.tsx, slider "Dias" 1-7, pedido del usuario 2026-09-18). Antes
   * era el fijo LOOKBACK_HOURS de abajo (48h todo el tiempo). */
  lookbackDays: number;
  pollMs?: number;
}

// Colores por lado -- pedido del usuario (2026-09-15): "los vendedores
// que sean de color rojo y los verdes [compradores]". Mismos tonos que ya
// usa el resto del dashboard para long/short (TradePositionsOverlay.tsx,
// LiquidationVolumePane.tsx) para no introducir una paleta roja/verde
// nueva. `side === null` (trades historicos de antes de que se guardara
// `is_buyer_maker`, ver db/schema.sql) cae en un color neutro -- nunca se
// inventa un lado que no se sabe.
const BUY_FILL_COLOR = "rgba(38, 166, 154, 0.30)";
const BUY_BORDER_COLOR = "rgba(38, 166, 154, 0.9)";
const SELL_FILL_COLOR = "rgba(239, 83, 80, 0.30)";
const SELL_BORDER_COLOR = "rgba(239, 83, 80, 0.9)";
const UNKNOWN_FILL_COLOR = "rgba(148, 163, 184, 0.30)";
const UNKNOWN_BORDER_COLOR = "rgba(148, 163, 184, 0.9)";
const MIN_RADIUS_PX = 3;
const BASE_MAX_RADIUS_PX = 26; // radio a bubbleScale=1, antes de escalar
const HOVER_EXTRA_TOLERANCE_PX = 3;

// FETCH_LIMIT subido a 20000 (2026-09-18, junto con el nuevo slider de
// "Dias" de App.tsx, 1-7): con `minNotionalUsd` ahora filtrando en el
// propio backend ANTES del recorte por limite (ver api/routers/big_trades.py,
// `min_notional_usd`), una ventana de varios dias con un umbral alto no
// necesita miles de filas de trades chicos "de paso" -- el filtro real ya
// paso del lado del servidor. Sigue acotado (no ilimitado): mismo motivo
// que MAX_LIMIT del backend, evitar una respuesta de decenas de miles de
// filas si alguien baja el umbral al piso real con la ventana mas larga.
const FETCH_LIMIT = 20000;

interface DrawnBubble {
  x: number;
  y: number;
  radius: number;
  event: BigTradeEvent;
}

/**
 * "Big trades" como burbujas sobre el precio -- pedido por el usuario
 * (2026-09-15) en reemplazo del histograma de `BigTradesVolumePane`
 * ("no lo quiero como histograma sino como burbujas en el grafico, donde
 * entre mas grande sea la orden ejecutada... mas grande la burbuja").
 * Cada trade grande se pinta en su precio EXACTO (`series.priceToCoordinate`,
 * sin snapping -- a diferencia del eje de tiempo, el de precio no tiene la
 * restriccion de "solo tiempos que ya existen en la serie") con un radio
 * proporcional a `sqrt(notional)` (area, no radio, proporcional al
 * notional -- lectura perceptual correcta de un bubble chart) contra el
 * trade MAS GRANDE del propio dataset cargado, para que el tamaño de una
 * burbuja se mantenga estable mientras se hace pan/zoom (no se recalcula
 * relativo a "lo visible ahora mismo"). Color por lado (2026-09-15,
 * pedido del usuario: "los vendedores que sean de color rojo y los
 * verdes"): verde si el TAKER compro agresivo, rojo si vendio agresivo
 * (campo "m" de Binance, ver api/routers/big_trades.py) -- gris cuando no
 * se sabe (trades historicos de antes de que se guardara ese dato).
 *
 * El eje de tiempo SI necesita snapping (`barTimeForEvent`, mismo patron
 * que el resto de los overlays de este proyecto): `timeToCoordinate` no
 * interpola, solo resuelve tiempos que ya existen en el eje compartido de
 * la serie de velas actual.
 */
export function BigTradesOverlay({
  chart, series, symbol, interval, minNotionalUsd, bubbleScale, lookbackDays, pollMs = 15000,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rawEventsRef = useRef<BigTradeEvent[]>([]);
  const drawnRef = useRef<DrawnBubble[]>([]);
  const [tooltip, setTooltip] = useState<{ x: number; y: number; text: string } | null>(null);

  // --- sondeo de datos -- depende de minNotionalUsd/lookbackDays (2026-09-18):
  // ambos ahora viajan al backend (ver api/routers/big_trades.py) para que
  // el filtro por umbral pase ANTES del recorte por `limit`, no solo al
  // dibujar -- si no, una ventana de varios dias con miles de trades
  // chicos "de paso" podia descartar trades grandes de mas atras. El
  // escalado de radio (`bubbleScale`) sigue siendo puramente de dibujo,
  // no dispara un refetch.
  useEffect(() => {
    if (!chart) return;
    let cancelled = false;
    const startIso = new Date(Date.now() - lookbackDays * 24 * 3600_000).toISOString();

    const load = () => {
      fetchBigTrades(symbol, { startIso, limit: FETCH_LIMIT, minNotionalUsd })
        .then((res) => {
          if (cancelled) return;
          rawEventsRef.current = res.events;
        })
        .catch(() => {}); // un fallo aca no debe tumbar el resto del chart
    };
    load();
    const id = window.setInterval(load, pollMs);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [chart, symbol, interval, pollMs, lookbackDays, minNotionalUsd]);

  // --- dibujo -------------------------------------------------------------
  useEffect(() => {
    if (!chart || !series || !canvasRef.current) return;
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const chartEl = chart.chartElement();

    const draw = () => {
      const container = canvas.parentElement;
      if (!container) return;
      // Ancho REAL del area de ploteo -- ver chartGeometry.ts (bug real
      // reportado por el usuario, 2026-09-21: las burbujas invadian los
      // numeros del eje de precio).
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

      const filtered = rawEventsRef.current.filter((e) => e.notional_usd >= minNotionalUsd);
      drawnRef.current = [];
      if (filtered.length === 0) return;

      let maxNotional = 0;
      for (const e of filtered) if (e.notional_usd > maxNotional) maxNotional = e.notional_usd;
      if (maxNotional <= 0) return;
      const maxRadius = BASE_MAX_RADIUS_PX * bubbleScale;

      const barTimes = series.data().map((b) => Number(b.time)).sort((a, b) => a - b);
      if (barTimes.length === 0) return;

      for (const event of filtered) {
        const snapped = barTimeForEvent(barTimes, event.event_time_ms / 1000);
        if (snapped === null) continue;
        const x = chart.timeScale().timeToCoordinate(snapped as UTCTimestamp);
        const y = series.priceToCoordinate(event.price);
        if (x === null || y === null) continue;
        const radius = MIN_RADIUS_PX + Math.sqrt(event.notional_usd / maxNotional) * (maxRadius - MIN_RADIUS_PX);
        const [fillColor, borderColor] =
          event.side === "buy" ? [BUY_FILL_COLOR, BUY_BORDER_COLOR]
          : event.side === "sell" ? [SELL_FILL_COLOR, SELL_BORDER_COLOR]
          : [UNKNOWN_FILL_COLOR, UNKNOWN_BORDER_COLOR];

        ctx.beginPath();
        ctx.arc(x, y, radius, 0, Math.PI * 2);
        ctx.fillStyle = fillColor;
        ctx.fill();
        ctx.strokeStyle = borderColor;
        ctx.lineWidth = 1;
        ctx.stroke();

        drawnRef.current.push({ x, y, radius, event });
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

    const onMouseMove = (evt: MouseEvent) => {
      const rect = chartEl.getBoundingClientRect();
      const x = evt.clientX - rect.left;
      const y = evt.clientY - rect.top;
      let hit: DrawnBubble | null = null;
      let bestDist = Infinity;
      for (const b of drawnRef.current) {
        const dist = Math.hypot(x - b.x, y - b.y);
        if (dist <= b.radius + HOVER_EXTRA_TOLERANCE_PX && dist < bestDist) {
          hit = b;
          bestDist = dist;
        }
      }
      if (!hit) {
        setTooltip(null);
        return;
      }
      const when = new Date(hit.event.event_time_ms).toLocaleString();
      const sideLabel = hit.event.side === "buy" ? "compra" : hit.event.side === "sell" ? "venta" : "lado desconocido";
      setTooltip({
        x: x + 12, y: y + 12,
        text: `$${hit.event.notional_usd.toLocaleString(undefined, { maximumFractionDigits: 0 })} (${sideLabel}) @ ${hit.event.price.toLocaleString()} -- ${when}`,
      });
    };
    chartEl.addEventListener("mousemove", onMouseMove);
    const onMouseLeave = () => setTooltip(null);
    chartEl.addEventListener("mouseleave", onMouseLeave);

    draw();
    const timeScale = chart.timeScale();
    timeScale.subscribeVisibleTimeRangeChange(scheduleDraw);
    window.addEventListener("resize", scheduleDraw);
    window.addEventListener(CHART_PANES_CHANGED_EVENT, scheduleDraw);
    // Misma red de seguridad que el resto de los overlays de canvas de este
    // proyecto (ver el comentario largo en HeatmapOverlay.tsx) -- tambien
    // cubre el poll de datos (rawEventsRef cambia fuera de React, sin
    // disparar un re-render propio).
    const geometryWatchdog = window.setInterval(scheduleDraw, 200);

    return () => {
      chartEl.removeEventListener("mousemove", onMouseMove);
      chartEl.removeEventListener("mouseleave", onMouseLeave);
      timeScale.unsubscribeVisibleTimeRangeChange(scheduleDraw);
      window.removeEventListener("resize", scheduleDraw);
      window.removeEventListener(CHART_PANES_CHANGED_EVENT, scheduleDraw);
      window.clearInterval(geometryWatchdog);
      if (rafId !== null) cancelAnimationFrame(rafId);
    };
  }, [chart, series, minNotionalUsd, bubbleScale]);

  return (
    <>
      <canvas
        ref={canvasRef}
        style={{ position: "absolute", top: 0, left: 0, pointerEvents: "none", zIndex: 3 }}
      />
      {tooltip && (
        <div
          style={{
            position: "absolute", left: tooltip.x, top: tooltip.y, pointerEvents: "none",
            zIndex: 6, background: "rgba(15, 23, 42, 0.92)", border: "1px solid rgba(148, 163, 184, 0.35)",
            borderRadius: 4, padding: "2px 6px", fontSize: 12, color: "#e2e8f0", whiteSpace: "nowrap",
          }}
        >
          {tooltip.text}
        </div>
      )}
    </>
  );
}
