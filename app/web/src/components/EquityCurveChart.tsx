import { useEffect, useRef } from "react";
import { createChart, LineSeries, type UTCTimestamp } from "lightweight-charts";

/** Curva de equity standalone (sin velas). Compartida por la vista de
 * backtests (`BotsView`) y la de paper trading (`PaperTradingView`). */
export function EquityCurveChart({ points }: { points: { time: string; equity: number }[] }) {
  const containerRef = useRef<HTMLDivElement>(null);

  // Chart standalone, sin velas -- solo la curva de equity. Se crea de
  // nuevo si cambian los puntos (a diferencia de PriceChart, esta vista no
  // necesita preservar zoom entre actualizaciones: cada corrida es fija,
  // no un feed en vivo).
  useEffect(() => {
    const container = containerRef.current;
    if (!container || points.length === 0) return;
    const chart = createChart(container, {
      layout: { background: { color: "#0b0e11" }, textColor: "#d1d4dc" },
      grid: { vertLines: { color: "#1a1e27" }, horzLines: { color: "#1a1e27" } },
      timeScale: { timeVisible: true, secondsVisible: false },
      autoSize: true,
    });
    const series = chart.addSeries(LineSeries, { color: "#eab308", lineWidth: 2 });
    series.setData(
      points.map((p) => ({
        time: Math.floor(new Date(p.time).getTime() / 1000) as UTCTimestamp,
        value: p.equity,
      })),
    );
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [points]);

  return <div ref={containerRef} style={{ width: "100%", height: 260 }} />;
}
