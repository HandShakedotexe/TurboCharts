import { useEffect, useRef } from "react";
import type { IChartApi, ISeriesApi } from "lightweight-charts";
import type { HeatmapTier } from "../api";
import { mergeTiers } from "../mergeTiers";
import { CHART_PANES_CHANGED_EVENT } from "../chartLayoutEvent";
import { priceScaleGutterWidth } from "../chartGeometry";

interface Props {
  chart: IChartApi | null;
  series: ISeriesApi<"Candlestick"> | null;
  tiers: HeatmapTier[];
  visibleLeverages: Set<number>;
}

const PANEL_WIDTH = 90;

/**
 * Perfil de volumen de liquidaciones: barras horizontales ancladas al
 * borde derecho, una por nivel de precio, con longitud proporcional al
 * notional modelado ahi -- el mismo dato que pinta HeatmapOverlay como
 * gradiente, aqui como barras (como el volume profile de cualquier
 * plataforma de trading, pero con notional de liquidacion en vez de
 * volumen operado). Canvas propio en vez de una serie mas de Lightweight
 * Charts por la misma razon que el heatmap: sincronizado por
 * priceToCoordinate, no por una escala de tiempo que no tiene.
 */
export function VolumeProfile({ chart, series, tiers, visibleLeverages }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    if (!chart || !series || !canvasRef.current) return;
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const draw = () => {
      const container = canvas.parentElement;
      if (!container) return;
      // Alto del PANE DE PRECIO, no del contenedor entero -- mismo arreglo
      // que HeatmapOverlay.tsx: con panes de indicadores activos, una barra
      // de perfil de volumen que caiga fuera del rango de precio visible
      // podia extenderse sobre los panes de abajo.
      const height = chart.panes()[0]?.getHeight() ?? container.clientHeight;
      if (height === 0) return;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = PANEL_WIDTH * dpr;
      canvas.height = height * dpr;
      canvas.style.width = `${PANEL_WIDTH}px`;
      // Ancho real de la escala de precio de Lightweight Charts, no un
      // numero fijo adivinado -- si el usuario cambia el formato de precio
      // (mas o menos decimales) ese ancho cambia, y un valor fijo dejaria
      // el panel encima de las etiquetas o con un hueco. NUNCA
      // `chart.priceScale('right').width()` -- confirmado (ver
      // chartGeometry.ts) que esa API devuelve SIEMPRE 0 en este proyecto,
      // por eso el panel quedaba pegado al borde real (`right:0`) en vez
      // de justo antes de las etiquetas -- bug real reportado por el
      // usuario, 2026-09-21.
      canvas.style.right = `${priceScaleGutterWidth(container)}px`;
      canvas.style.height = `${height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, PANEL_WIDTH, height);

      const merged = mergeTiers(tiers, visibleLeverages);
      // NUNCA Math.max(1, ...array) sobre un Map que puede crecer -- ver el
      // mismo arreglo en HeatmapOverlay.tsx (RangeError real: desparramar
      // muchos valores como argumentos individuales desborda la pila).
      // Este `merged` es el snapshot chico (tiers[].levels, no .cells), pero
      // el patron es igual de fragil si algun dia crece.
      let maxNotional = 1;
      for (const notional of merged.values()) {
        if (notional > maxNotional) maxNotional = notional;
      }

      for (const [price, notional] of merged) {
        const y = series.priceToCoordinate(price);
        if (y === null) continue;
        const barWidth = (notional / maxNotional) * (PANEL_WIDTH - 4);
        const barHeight = 5;
        ctx.fillStyle = "rgba(148, 163, 184, 0.55)"; // gris azulado neutro: esto es volumen, no densidad de calor
        ctx.fillRect(PANEL_WIDTH - barWidth, y - barHeight / 2, barWidth, barHeight);
      }
    };

    draw();
    const timeScale = chart.timeScale();
    timeScale.subscribeVisibleTimeRangeChange(draw);
    chart.subscribeCrosshairMove(draw);
    window.addEventListener("resize", draw);
    // Ver chartLayoutEvent.ts -- agregar/quitar un pane de indicador
    // cambia el alto del pane de precio y el ancho de la escala de precio
    // compartida sin que `.chart-main` cambie de tamaño.
    window.addEventListener(CHART_PANES_CHANGED_EVENT, draw);
    // Arrastrar el separador ENTRE panes a mano tampoco dispara nada de lo
    // de arriba -- evento NATIVO de la libreria para esto, ver el mismo
    // comentario en HeatmapOverlay.tsx.
    const onSizeChange = () => draw();
    timeScale.subscribeSizeChange(onSizeChange);

    // Mismo problema de timing que HeatmapOverlay/PriceChart -- ver sus
    // comentarios. ResizeObserver de respaldo + reintentos por setTimeout
    // porque ninguno de los dos dispara de forma fiable si la pestana no
    // esta visible/compuesta.
    const container = canvas.parentElement;
    const ro = container ? new ResizeObserver(draw) : null;
    if (container) ro!.observe(container);
    // Ver el mismo comentario en HeatmapOverlay.tsx -- `.chart-main` nunca
    // cambia de tamaño con nada de esto (alto fijo por CSS); el elemento
    // DOM real del pane de precio (`getHTMLElement()`) si, sea cual sea
    // la causa. Fuente de verdad, no una lista de eventos que adivinar.
    const pricePaneEl = chart.panes()[0]?.getHTMLElement() ?? null;
    const paneRo = pricePaneEl ? new ResizeObserver(draw) : null;
    if (pricePaneEl) paneRo!.observe(pricePaneEl);

    // Red de seguridad final -- vigila el alto del pane cada 300ms, ver el
    // comentario largo en HeatmapOverlay.tsx sobre por que (diagnosticado
    // en vivo, 2026-09-01: `chart.priceScale('right').width()` resulto
    // devolver SIEMPRE 0 -- API rota o no aplicable aqui, se descarto
    // como señal). Este componente no depende del mapeo TIEMPO->pixel
    // (dibuja barras por PRECIO, `series.priceToCoordinate`, que ya se
    // recalcula fresco en cada `draw()`) asi que no le afecta el
    // corrimiento de rango visible que si rompia a HeatmapOverlay -- esto
    // es solo para el caso, mas simple, de que cambie el alto del pane.
    let lastPaneHeight = chart.panes()[0]?.getHeight() ?? 0;
    const geometryWatchdog = window.setInterval(() => {
      const paneHeight = chart.panes()[0]?.getHeight() ?? 0;
      if (paneHeight !== lastPaneHeight) {
        lastPaneHeight = paneHeight;
        draw();
      }
    }, 300);

    const retryTimers = [50, 250, 750, 2000].map((ms) => window.setTimeout(draw, ms));

    return () => {
      timeScale.unsubscribeVisibleTimeRangeChange(draw);
      chart.unsubscribeCrosshairMove(draw);
      window.removeEventListener("resize", draw);
      window.removeEventListener(CHART_PANES_CHANGED_EVENT, draw);
      timeScale.unsubscribeSizeChange(onSizeChange);
      ro?.disconnect();
      paneRo?.disconnect();
      window.clearInterval(geometryWatchdog);
      retryTimers.forEach((id) => window.clearTimeout(id));
    };
  }, [chart, series, tiers, visibleLeverages]);

  return (
    <canvas
      ref={canvasRef}
      style={{ position: "absolute", top: 0, pointerEvents: "none", zIndex: 3 }}
    />
  );
}
