import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  createChart,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import type { Candle } from "../api";

interface CandleColors {
  upColor: string;
  downColor: string;
}

interface Props {
  candles: Candle[];
  onChartReady?: (chart: IChartApi, series: ISeriesApi<"Candlestick">) => void;
  /** Colores de vela elegidos por el usuario (2026-09-20, "el usuario debe
   * poder cambiar el color de las velas y se debe guardar el cambio") --
   * opcional para no romper otros usos de este componente que no lo pasen;
   * cae a los mismos literales que ya estaban hardcodeados aca. */
  candleColors?: CandleColors;
}

const DEFAULT_COLORS: CandleColors = { upColor: "#26a69a", downColor: "#ef5350" };

export function PriceChart({ candles, onChartReady, candleColors = DEFAULT_COLORS }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);

  // Se crea una sola vez: recrear el chart en cada render perderia el zoom
  // y la posicion que el usuario ya haya elegido. `autoSize: true` deja que
  // la propia libreria trackee el tamano del contenedor con su ResizeObserver
  // interno -- mas fiable que leer clientWidth a mano en el efecto, que
  // puede correr antes de que el layout de flexbox este resuelto.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const chart = createChart(container, {
      layout: { background: { color: "#0b0e11" }, textColor: "#d1d4dc" },
      grid: { vertLines: { color: "#1a1e27" }, horzLines: { color: "#1a1e27" } },
      // rightOffset: espacio vacio reservado despues de la ultima vela --
      // ahi es donde se proyecta el heatmap (ver HeatmapOverlow/VolumeProfile),
      // no encima de las velas ya ocurridas. Es el mismo formato que usan
      // los heatmaps publicados (CoinGlass, BitcoinCounterFlow): la
      // proyeccion futura ocupa espacio propio, no se superpone al historico.
      timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 40 },
      rightPriceScale: { borderColor: "#2a2e39" },
      autoSize: true,
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: candleColors.upColor,
      downColor: candleColors.downColor,
      borderVisible: false,
      wickUpColor: candleColors.upColor,
      wickDownColor: candleColors.downColor,
    });

    chartRef.current = chart;
    seriesRef.current = series;
    onChartReady?.(chart, series);

    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!seriesRef.current) return;
    const data: CandlestickData[] = candles.map((c) => ({
      time: c.time as UTCTimestamp,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));
    seriesRef.current.setData(data);
    chartRef.current?.timeScale().fitContent();
    // `fitContent()` solo reajusta el eje de TIEMPO. El eje de precio tiene
    // `autoScale` activado por defecto, pero arrastrar manualmente sobre la
    // escala de precio (gesto facil de hacer sin querer cerca del borde
    // derecho) lo desactiva de forma persistente -- queda fijo en el rango
    // del simbolo anterior hasta que algo lo reactive a mano. Bug real
    // reportado 2026-10-16 con captura: al cambiar a ETHUSDT viniendo de
    // BTCUSDT, el eje seguia en 76000-100000 (rango de BTC) y las velas de
    // ETH (~$2-4k) se dibujaban pegadas al piso, invisibles -- solo
    // aparecian si el usuario arrastraba el eje a mano. Reactivar
    // `autoScale` en cada carga de velas (symbol/interval/evidencia) cubre
    // esto sin tocar nada mas.
    chartRef.current?.priceScale("right").applyOptions({ autoScale: true });
  }, [candles]);

  // Separado del efecto de creacion (que solo corre una vez, ver arriba):
  // `applyOptions` cambia el color SIN recrear la serie/perder el zoom --
  // recrear el chart entero cada vez que el usuario elige un color en
  // ChartToolsPanel.tsx le reiniciaria el pan/zoom en cada click.
  useEffect(() => {
    seriesRef.current?.applyOptions({
      upColor: candleColors.upColor,
      downColor: candleColors.downColor,
      wickUpColor: candleColors.upColor,
      wickDownColor: candleColors.downColor,
    });
  }, [candleColors]);

  return <div ref={containerRef} style={{ width: "100%", height: "100%" }} />;
}
