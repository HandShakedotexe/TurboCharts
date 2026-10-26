import { useEffect, useRef } from "react";
import { HistogramSeries, type IChartApi, type IPaneApi, type ISeriesApi, type Time, type UTCTimestamp } from "lightweight-charts";
import { fetchRealLiquidations, type RealLiquidationEvent } from "../api";
import { CHART_PANES_CHANGED_EVENT } from "../chartLayoutEvent";
import { barTimeForEvent } from "../barTimeForEvent";

interface Props {
  chart: IChartApi | null;
  /** Serie de velas -- hace falta para anclar cada bucket a un tiempo de
   * vela REAL que ya existe en el eje compartido, ver `barTimeForEvent`
   * mas abajo. */
  series: ISeriesApi<"Candlestick"> | null;
  symbol: string;
  interval: string;
  pollMs?: number;
}

// Mismos colores que ya usaba el tooltip de `LiquidationMarkers` (side-coded,
// pedido del usuario 2026-09-04) -- se conservan aqui para no introducir una
// paleta nueva para el mismo dato.
const LONG_LIQ_COLOR = "#ef5350";
const SHORT_LIQ_COLOR = "#26a69a";

// 2026-09-25: bajado de 90 a 3 dias (pedido del usuario, criterio
// "intradia"). Con 90 dias de LOOKBACK y el grabador corriendo sin
// cortes desde el
// 2026-08-27 (a diferencia de cuando se escribio el valor original, con
// "nunca hay 90 dias de datos reales todavia"), BTCUSDT solo ya generaba
// ~1200-1400 liquidaciones/dia -- de sobra para agotar FETCH_LIMIT=5000
// en poco mas de un dia. `fetch_liquidations_since` (real_liquidations.py)
// ordena por mas reciente y CORTA ahi, asi que cualquier fecha mas vieja
// que ese corte se descartaba en silencio sin importar que tan atras
// pidiera `start` -- el usuario lo vio como "las liquidaciones empiezan a
// mitad del 21 de sep" (el dato en disco SI llega hasta el 27 de agosto,
// confirmado leyendo los .jsonl.gz a mano). Una ventana de 90 dias
// tambien obligaba a leer TODOS los archivos de hora comprimidos de ese
// rango en CADA poll de 8s (`pollMs`), cada vez mas lento con el tiempo.
const LOOKBACK_DAYS = 3;
// ~1200-1400/dia tipico de BTCUSDT (medido 2026-09-22/23/24) * 3 dias con
// margen para dias volatiles -- sigue MUY por debajo del costo de leer 90
// dias de archivos, pero ya no trunca dentro de la ventana en un dia
// normal.
const FETCH_LIMIT = 8000;

/**
 * Barras de volumen de liquidaciones REALES (no modeladas), separadas por
 * lado -- reemplaza los puntos sobre las velas (`LiquidationMarkers`,
 * retirado 2026-09-09 a peticion del usuario: "estoy teniendo dificultad
 * para poder observar la informacion"). Un pane aparte, mismo patron que
 * `IndicatorPanes.tsx` (OI/CVD/Funding), con DOS series de histograma en
 * el mismo pane: longs liquidados como barras POSITIVAS (arriba de cero),
 * shorts liquidados como NEGATIVAS (abajo) -- lightweight-charts no
 * soporta dos barras en el mismo timestamp dentro de una serie, asi que
 * "espejado" alrededor de cero es la forma estandar de mostrar dos
 * categorias en un histograma sin superponerlas; el total en cada bucket
 * de tiempo es la envergadura combinada arriba+abajo, visible de un
 * vistazo sin necesitar una tercera serie.
 */
export function LiquidationVolumePane({ chart, series, symbol, interval, pollMs = 8000 }: Props) {
  const paneRef = useRef<IPaneApi<Time> | null>(null);
  const longSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const shortSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!chart) return;
    // Mismo motivo que IndicatorPanes.tsx: si el chart cambio de
    // instancia (remount al volver de "Bots"), lo guardado ya no existe.
    if (chartRef.current !== chart) {
      paneRef.current = null;
      longSeriesRef.current = null;
      shortSeriesRef.current = null;
      chartRef.current = chart;
    }
    if (!paneRef.current) {
      const pane = chart.addPane();
      paneRef.current = pane;
      // priceFormat 'volume': "1.2K"/"12.67M" en vez de "30000000.00" --
      // ademas de mas legible, el gutter de precio de este pane se queda
      // angosto y estable en vez de crecer con cada liquidacion grande
      // (2026-09-10, sospecha del usuario: este pane nuevo interfiere con
      // el heatmap -- un gutter que cambia de ancho segun el dato mas
      // reciente es exactamente el tipo de cosa que ya causo desincronia
      // de panes antes).
      const volumeFormat = { type: "volume" as const, precision: 2, minMove: 0.01 };
      longSeriesRef.current = pane.addSeries(HistogramSeries, {
        color: LONG_LIQ_COLOR, priceLineVisible: false, lastValueVisible: false,
        title: "Liq. long", priceFormat: volumeFormat,
      });
      shortSeriesRef.current = pane.addSeries(HistogramSeries, {
        color: SHORT_LIQ_COLOR, priceLineVisible: false, lastValueVisible: false,
        title: "Liq. short", priceFormat: volumeFormat,
      });
      window.dispatchEvent(new Event(CHART_PANES_CHANGED_EVENT));
    }
    return () => {
      // try/catch: mismo problema documentado en LiquidationMarkers.tsx --
      // si PriceChart ya desmonto el chart (p.ej. al entrar a "Bots" en el
      // mismo commit), `chart.removePane` ya esta disuelto y lanza sin
      // capturar, tumbando el arbol de React entero.
      try {
        if (paneRef.current) chart.removePane(paneRef.current.paneIndex());
      } catch {
        // chart ya disuelto -- nada que hacer.
      }
      paneRef.current = null;
      longSeriesRef.current = null;
      shortSeriesRef.current = null;
    };
  }, [chart]);

  useEffect(() => {
    if (!chart) return;
    let cancelled = false;
    const startIso = new Date(Date.now() - LOOKBACK_DAYS * 86_400_000).toISOString();

    const load = () => {
      // Sin velas reales todavia no hay donde anclar nada -- se reintenta
      // en el proximo ciclo de sondeo, nunca se inventa un tiempo propio.
      if (!series) return;
      const barTimes = series
        .data()
        .map((b) => Number(b.time))
        .sort((a, b) => a - b);
      if (barTimes.length === 0) return;
      fetchRealLiquidations(symbol, { limit: FETCH_LIMIT, startIso })
        .then((res) => {
          if (cancelled || !longSeriesRef.current || !shortSeriesRef.current) return;
          const longByBucket = new Map<number, number>();
          const shortByBucket = new Map<number, number>();
          for (const e of res.events as RealLiquidationEvent[]) {
            // Anclar SIEMPRE a un tiempo de vela que ya existe en la serie
            // de precio -- nunca a un bucket calculado de forma independiente
            // (version anterior: `Math.floor(t/bucketSeconds)*bucketSeconds`,
            // que asume que `time` de cada vela es su APERTURA). Confirmado
            // con datos reales (2026-09-11): `time` es en realidad el CIERRE
            // de la vela (`open + interval - 1s` -- en 5m, cada valor cae en
            // `... % 300 === 299`, nunca alineado a una apertura), asi que
            // ese bucket calculado NUNCA coincidia con un tiempo real de
            // vela -- el 100% de los eventos probados, no solo los que caen
            // en un hueco. Lightweight Charts reparte el ancho de barra por
            // indice logico COMPARTIDO entre todas las series del mismo eje
            // de tiempo -- un punto de tiempo que no esta en la serie de
            // velas mete un indice extra ahi, y eso comprime/descoloca las
            // velas reales alrededor de ese punto (reportado por el usuario
            // 2026-09-11: velas "amontonadas" y un hueco negro en el heatmap
            // justo en esa zona, que coincidio con agregar este panel).
            // `IndicatorPanes.tsx` no tiene este problema porque el backend
            // ya reasigna cada punto a un tiempo de vela real
            // (`_resample_to_bar_grid`); aqui el bucketing era del lado del
            // cliente y no tenia ese resguardo.
            const bucketTime = barTimeForEvent(barTimes, e.event_time_ms / 1000);
            if (bucketTime === null) continue;
            const target = e.side === "long" ? longByBucket : shortByBucket;
            target.set(bucketTime, (target.get(bucketTime) ?? 0) + e.quote_qty);
          }
          const toPoints = (byBucket: Map<number, number>, sign: 1 | -1) =>
            Array.from(byBucket.entries())
              .sort((a, b) => a[0] - b[0])
              .map(([time, value]) => ({ time: time as UTCTimestamp, value: value * sign }));
          longSeriesRef.current.setData(toPoints(longByBucket, 1));
          shortSeriesRef.current.setData(toPoints(shortByBucket, -1));
        })
        .catch(() => {}); // un fallo aca no debe tumbar el resto del chart
    };
    load();
    const id = window.setInterval(load, pollMs);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [chart, series, symbol, interval, pollMs]);

  return null;
}
