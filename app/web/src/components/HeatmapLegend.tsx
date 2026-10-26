import { colorForLeverage } from "../leverageColors";
import type { HeatmapTier } from "../api";

interface Props {
  tiers: HeatmapTier[];
  visible: Set<number>;
  onToggle: (leverage: number) => void;
}

function formatUsd(n: number): string {
  if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(1)}M`;
  return `$${n.toFixed(0)}`;
}

export function HeatmapLegend({ tiers, visible, onToggle }: Props) {
  return (
    <div className="heatmap-legend">
      {tiers.map((tier) => {
        const lev = tier.leverage;
        const on = visible.has(lev);
        const total = tier.levels.reduce((sum, l) => sum + l.notional_usd, 0);
        return (
          <button
            key={lev}
            className={`legend-chip${on ? " on" : ""}`}
            style={{
              borderColor: colorForLeverage(lev),
              background: on ? colorForLeverage(lev) : "transparent",
              color: on ? "#0b0e11" : colorForLeverage(lev),
            }}
            onClick={() => onToggle(lev)}
            title={`${on ? "Ocultar" : "Mostrar"} ${lev}x -- ${formatUsd(total)} modelado (supuesto, no medido)`}
          >
            <span className="legend-dot" style={{ background: colorForLeverage(lev) }} />
            {lev}x
          </button>
        );
      })}
    </div>
  );
}
