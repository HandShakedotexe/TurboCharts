import { useEffect, useRef } from "react";
import { LineSeries, type IChartApi, type IPaneApi, type ISeriesApi, type Time, type UTCTimestamp } from "lightweight-charts";
import { fetchIndicator, type IndicatorKind } from "../api";
import { CHART_PANES_CHANGED_EVENT } from "../chartLayoutEvent";

interface Props {
  chart: IChartApi | null;
  /** Orden deseado, ya filtrado a los indicadores activos -- ver
   * `enabledIndicators` en App.tsx. */
  kinds: IndicatorKind[];
  symbol: string;
  interval: string;
  pollMs?: number;
}

const META: Record<IndicatorKind, { color: string; title: string }> = {
  oi: { color: "#60a5fa", title: "Open Interest" },
  cvd: { color: "#c084fc", title: "CVD" },
  funding: { color: "#fbbf24", title: "Funding" },
};

// Ventana por defecto: 90 dias, a proposito igual a la profundidad del
// backfill de aggTrades de BTCUSDT -- pedir desde 2020 (el default del
// backend sin este parametro) devuelve cientos de
// miles de puntos de OI por nada.
const LOOKBACK_DAYS = 90;

interface Entry {
  pane: IPaneApi<Time>;
  series: ISeriesApi<"Line">;
}

/**
 * Gestiona TODOS los panes de indicadores en un solo componente, en vez de
 * uno por indicador -- `chart.removePane(index)` reindexa los panes que
 * quedan (quitar el pane 1 corre el 2 al 1), y tres componentes hermanos
 * independientes no se pueden coordinar para eso sin pisarse el indice
 * unos a otros. Aqui se reconcilia `kinds` contra lo que ya existe en un
 * solo efecto.
 */
export function IndicatorPanes({ chart, kinds, symbol, interval, pollMs = 20000 }: Props) {
  const entriesRef = useRef<Map<IndicatorKind, Entry>>(new Map());
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!chart) return;
    // Si el chart cambio de instancia (PriceChart se desmonto y remonto --
    // pasa al cambiar a la vista "Bots" y volver), los panes/series viejos
    // ya no existen: las entradas guardadas quedarian apuntando a un chart
    // destruido. Se descartan sin llamar a removePane (no hay nada que
    // quitar, el chart entero ya no esta).
    if (chartRef.current !== chart) {
      entriesRef.current.clear();
      chartRef.current = chart;
    }
    const entries = entriesRef.current;
    const wanted = new Set(kinds);
    let changed = false;

    for (const [kind, entry] of Array.from(entries.entries())) {
      if (!wanted.has(kind)) {
        chart.removePane(entry.pane.paneIndex());
        entries.delete(kind);
        changed = true;
      }
    }
    for (const kind of kinds) {
      if (!entries.has(kind)) {
        const pane = chart.addPane();
        const meta = META[kind];
        const series = pane.addSeries(LineSeries, {
          color: meta.color, lineWidth: 1, title: meta.title, priceLineVisible: false,
        });
        entries.set(kind, { pane, series });
        changed = true;
      }
    }
    // Avisa a HeatmapOverlay/VolumeProfile (que no saben nada de panes de
    // indicadores) que su mapeo de pixeles quedo obsoleto -- ver
    // chartLayoutEvent.ts. Disparado DESPUES de addPane/removePane, nunca
    // antes: son llamadas sincronas, asi que para cuando este evento llega
    // a los listeners el layout del chart ya esta actualizado.
    if (changed) window.dispatchEvent(new Event(CHART_PANES_CHANGED_EVENT));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chart, kinds.join(",")]);

  useEffect(() => {
    if (!chart || kinds.length === 0) return;
    let cancelled = false;
    const load = () => {
      const startIso = new Date(Date.now() - LOOKBACK_DAYS * 86_400_000).toISOString();
      for (const kind of kinds) {
        const entry = entriesRef.current.get(kind);
        if (!entry) continue;
        // `interval` para los tres, no solo CVD -- el backend reasigna
        // cada punto de OI/funding al `time` exacto de la vela de esta
        // temporalidad (ver api/routers/indicators.py::_resample_to_bar_grid).
        // Antes solo CVD lo pedia, y OI/funding se desalineaban en
        // cualquier temporalidad que no coincidiera con su cadencia
        // nativa (5m para OI archivado) -- reportado por el usuario en 1h,
        // 2026-09-03.
        fetchIndicator(kind, symbol, { interval, startIso })
          .then((points) => {
            if (cancelled) return;
            entry.series.setData(points.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })));
          })
          .catch(() => {}); // un indicador fallando no debe tumbar el resto del chart
      }
    };
    load();
    const id = window.setInterval(load, pollMs);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chart, symbol, interval, kinds.join(","), pollMs]);

  return null;
}
