import { useEffect, useState } from "react";
import type { MarketSnapshot, RealLiquidationEvent, Setup } from "../api";
import { fetchRealLiquidations, fetchSetups } from "../api";
import { formatNotional } from "../format";

interface Props {
  symbol: string;
  marketSnapshot: MarketSnapshot | null;
}

// Los ficheros del grabador solo se releen del disco, sin red externa que
// cuidar (ver api/real_liquidations.py) -- se puede sondear mas seguido
// que el resto de la app sin coste real.
const LIQUIDATIONS_POLL_MS = 5000;
// Setups reusa la respuesta CACHEADA de /api/heatmap (TTL 15s, ver
// heatmap.py) mas dos llamadas en vivo a Binance (funding, CVD reciente)
// -- sondear igual de seguido que las liquidaciones crudas no aportaria
// nada nuevo la mayoria de las veces y multiplicaria las llamadas en vivo.
const SETUPS_POLL_MS = 15000;

function formatPct(p: number | null): string {
  return p === null ? "--" : `${(p * 100).toFixed(2)}%`;
}

/**
 * Panel lateral colapsable: lo que NO tiene sentido dibujar como una celda
 * mas del heatmap (un solo numero por instante -- funding, OI, long/short
 * ratio, proximidad de clusters, ver `market_snapshot` en
 * api/routers/heatmap.py) mas la lista en vivo de alertas de liquidacion
 * REALES (api/real_liquidations.py -- grabador de Fase 0, no el modelo).
 */
export function MarketSnapshotPanel({ symbol, marketSnapshot }: Props) {
  const [liquidations, setLiquidations] = useState<RealLiquidationEvent[]>([]);
  const [setups, setSetups] = useState<Setup[]>([]);
  const [setupsError, setSetupsError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      fetchRealLiquidations(symbol, { limit: 30 })
        .then((res) => {
          if (!cancelled) setLiquidations(res.events);
        })
        .catch(() => {});
    };
    load();
    const id = window.setInterval(load, LIQUIDATIONS_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [symbol]);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      fetchSetups(symbol)
        .then((res) => {
          if (cancelled) return;
          setSetups(res.setups);
          setSetupsError(null);
        })
        .catch((e) => {
          // Un tick fallido no debe vaciar los setups que ya se veian --
          // solo se guarda el error para mostrarlo aparte.
          if (!cancelled) setSetupsError(String(e));
        });
    };
    load();
    const id = window.setInterval(load, SETUPS_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [symbol]);

  const cluster = marketSnapshot?.cluster_proximity;

  return (
    <aside className="market-snapshot-panel">
      <section>
        <h3>Mercado</h3>
        {marketSnapshot ? (
          <dl className="snapshot-grid">
            <dt>Funding</dt>
            <dd>{(marketSnapshot.funding_rate * 100).toFixed(4)}%</dd>
            <dt>Open Interest</dt>
            <dd>{formatNotional(marketSnapshot.oi_notional_usd)}</dd>
            <dt>Long/Short ratio</dt>
            <dd>{marketSnapshot.long_short_ratio?.toFixed(3) ?? "--"}</dd>
            <dt>Cluster largo (abajo)</dt>
            <dd>
              {formatNotional(cluster!.long_cluster_below.notional_usd)} @{" "}
              {formatPct(cluster!.long_cluster_below.distance_pct)}
            </dd>
            <dt>Cluster corto (arriba)</dt>
            <dd>
              {formatNotional(cluster!.short_cluster_above.notional_usd)} @{" "}
              {formatPct(cluster!.short_cluster_above.distance_pct)}
            </dd>
          </dl>
        ) : (
          <p className="panel-empty">Sin datos en vivo (modo historico)</p>
        )}
      </section>
      <section>
        <h3>
          Liquidaciones <span className="badge badge-real">REAL</span>
        </h3>
        <div className="liquidations-list">
          {liquidations.length === 0 && <p className="panel-empty">Sin eventos recientes</p>}
          {liquidations.map((e) => (
            <div key={`${e.event_time_ms}-${e.price}-${e.qty_base}`} className={`liquidation-row liq-${e.side}`}>
              <span className="liq-time">{new Date(e.event_time_ms).toLocaleTimeString()}</span>
              <span className="liq-side">{e.side === "long" ? "Long liq." : "Short liq."}</span>
              <span className="liq-price">{e.price.toLocaleString()}</span>
              <span className="liq-notional">{formatNotional(e.quote_qty)}</span>
            </div>
          ))}
        </div>
      </section>
      <section>
        <h3>
          Setups <span className="badge badge-experimental">EXPERIMENTAL</span>
        </h3>
        <p className="setups-disclaimer">
          Liquidacion real + pool denso cerca + confirmaciones -- el proceso manual del usuario llevado a
          la pantalla. NO es una estrategia validada.
        </p>
        {setupsError && <p className="error">{setupsError}</p>}
        {!setupsError && setups.length === 0 && (
          <p className="panel-empty">Sin setups activos ahora mismo</p>
        )}
        <div className="setups-list">
          {setups.map((s) => (
            <div
              key={`${s.trigger.event_time_ms}-${s.trigger.price}`}
              className={`setup-card setup-${s.suggested.direction}`}
            >
              <div className="setup-header">
                <span>{new Date(s.trigger.event_time_ms).toLocaleTimeString()}</span>
                <span className={`setup-direction-badge ${s.suggested.direction}`}>
                  {s.suggested.direction === "long" ? "LARGO" : "CORTO"}
                </span>
              </div>
              <div className="setup-trigger">
                Liquido {s.trigger.side === "long" ? "largo" : "corto"} de{" "}
                {formatNotional(s.trigger.quote_qty_usd)} @ {s.trigger.price.toLocaleString()}
              </div>
              <div className="setup-pool">
                Pool: {formatNotional(s.pool.total_notional_usd)} en {s.pool.correlation} apalancamientos (
                {s.pool.leverage_tiers.map((l) => `${l}x`).join(", ")})
              </div>
              <div className="setup-levels">
                <span>Entrada {s.suggested.entry.toLocaleString()}</span>
                <span>SL {s.suggested.stop.toLocaleString()}</span>
                <span>
                  TP {s.suggested.target_low.toLocaleString()}&ndash;{s.suggested.target_high.toLocaleString()}
                </span>
              </div>
              {s.confirmations.length > 0 && (
                <ul className="setup-confirmations">
                  {s.confirmations.map((c) => (
                    <li key={c}>{c}</li>
                  ))}
                </ul>
              )}
            </div>
          ))}
        </div>
      </section>
    </aside>
  );
}
