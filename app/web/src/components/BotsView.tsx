import { useEffect, useState } from "react";
import {
  fetchBacktestRun,
  fetchBacktestRuns,
  fetchBacktestStrategies,
  fetchValidation,
  fetchValidations,
  type BacktestRunDetail,
  type BacktestRunSummary,
  type BacktestTradeDetail,
  type StrategyInfo,
  type ValidationDetail as ValidationDetailData,
  type ValidationSummary,
} from "../api";
import { formatNotional, formatUtc } from "../format";
import type { TradePosition } from "./TradePositionsOverlay";
import { EquityCurveChart } from "./EquityCurveChart";
import { PaperTradingView } from "./PaperTradingView";

/** "Evidencia" de uno o varios trades reales sobre el chart principal --
 * App.tsx consume esto para (1) dibujar `positions` con
 * `TradePositionsOverlay` en modo de solo lectura, y (2) acotar el fetch
 * de velas a `[startMs, endMs]` en vez de la ventana normal (un
 * walk-forward puede cubrir un rango historico largo, muy por fuera de
 * eso). */
export interface ChartEvidence {
  positions: TradePosition[];
  startMs: number;
  endMs: number;
}

// Contexto antes/despues del rango real del trade -- para que la vela de
// entrada/salida no quede pegada al borde del grafico.
const EVIDENCE_BUFFER_MS = 2 * 24 * 3_600_000;

function tradeToPosition(t: BacktestTradeDetail, id: string): TradePosition {
  const entryPrice = t.entry_price;
  // Sin SL/TP en precio (estrategias que salen solo por señal, ver
  // describeExit mas abajo) el "indicador" colapsa al precio de salida
  // real -- sigue siendo una lectura honesta de que paso, no se inventa
  // un SL/TP que la estrategia nunca definio.
  const exitPrice = t.exit_price ?? entryPrice;
  return {
    id,
    side: t.side === "long" ? "long" : "short",
    entryTimeMs: new Date(t.entry_time).getTime(),
    entryPrice,
    tpPrice: t.take_profit_price ?? exitPrice,
    slPrice: t.stop_loss_price ?? exitPrice,
  };
}

/** Ventana [startMs, endMs] que cubre TODOS los trades dados -- bucle
 * simple, NUNCA `Math.min/max(...array)` (desborda la pila de JS con un
 * array grande, y la vista de "totales" puede traer cientos o miles de
 * trades de un walk-forward completo). */
function evidenceForTrades(trades: BacktestTradeDetail[], idPrefix: string): ChartEvidence {
  const positions = trades.map((t, i) => tradeToPosition(t, `${idPrefix}-${t.trade_seq}-${i}`));
  let minEntryMs = Infinity;
  let maxExitMs = -Infinity;
  for (const t of trades) {
    const entryMs = new Date(t.entry_time).getTime();
    if (entryMs < minEntryMs) minEntryMs = entryMs;
    const exitMs = t.exit_time ? new Date(t.exit_time).getTime() : Date.now();
    if (exitMs > maxExitMs) maxExitMs = exitMs;
  }
  return { positions, startMs: minEntryMs - EVIDENCE_BUFFER_MS, endMs: maxExitMs + EVIDENCE_BUFFER_MS };
}

/** "Disparador (Trigger) / Setup" de un trade -- no un texto generico
 * repetido en cada fila, sino los datos REALES de la estrategia que
 * dispararon ESE trade en particular, sacados de `signal_snapshot` (los
 * indicadores en el momento exacto de la señal). */
function describeTrigger(title: string, snapshot: Record<string, unknown>): string {
  const parts = [title];
  const z = snapshot.funding_zscore;
  if (typeof z === "number") parts.push(`funding z-score ${z.toFixed(2)}`);
  const oiTrend = snapshot.oi_trend;
  if (typeof oiTrend === "number" && oiTrend !== 0) parts.push(`OI trend ${(oiTrend * 100).toFixed(1)}%`);
  const regime = snapshot.regime;
  if (typeof regime === "string") parts.push(`regimen ${regime}`);
  const cluster = snapshot.liquidation_cluster as
    | {
        long_cluster_below?: { notional_usd?: number };
        short_cluster_above?: { notional_usd?: number };
      }
    | undefined;
  const longUsd = cluster?.long_cluster_below?.notional_usd ?? 0;
  const shortUsd = cluster?.short_cluster_above?.notional_usd ?? 0;
  if (longUsd > 0) parts.push(`cluster largo ${formatNotional(longUsd)} debajo`);
  if (shortUsd > 0) parts.push(`cluster corto ${formatNotional(shortUsd)} arriba`);
  return parts.join(" -- ");
}

/** "Notas / Errores de Conclusion" -- igual que el disparador, derivado de
 * datos reales del propio trade (`exit_reason` + z-score de entrada vs
 * salida), no una nota generica. */
function describeExit(t: BacktestTradeDetail): string {
  if (!t.exit_time) return "Posicion sigue abierta.";
  if (t.exit_reason === "end_of_window") {
    return "Cierre forzoso: la ventana del backtest termino con la posicion todavia abierta (no fue una senal de la estrategia).";
  }
  if (t.exit_reason === "stop_loss") {
    return `Stop loss tocado en ${t.exit_price?.toLocaleString()}.`;
  }
  if (t.exit_reason === "take_profit") {
    return `Take profit tocado en ${t.exit_price?.toLocaleString()}.`;
  }
  const entryZ = t.signal_snapshot.funding_zscore;
  const exitZ = t.exit_signal_snapshot?.funding_zscore;
  if (typeof entryZ === "number" && typeof exitZ === "number") {
    return `Salida por senal: funding z-score volvio a banda neutral (${entryZ.toFixed(2)} -> ${exitZ.toFixed(2)}).`;
  }
  return "Salida por senal de la estrategia.";
}

/** "Por que no fue rentable" + recomendaciones -- reglas basadas en los
 * numeros REALES de la validacion (win rate, profit factor, expectancy,
 * drawdown, y -- cuando hay suficientes perdedores cerrados por stop --
 * cuanta excursion favorable tuvieron antes de revertir), no un texto
 * generico. */
interface Diagnosis {
  reasons: string[];
  recommendations: string[];
}

function diagnoseValidation(detail: ValidationDetailData): Diagnosis {
  const reasons: string[] = [];
  const recommendations: string[] = [];
  const pf = detail.profit_factor;
  const winPct = detail.win_rate * 100;

  if (detail.verdict === "PASA") {
    reasons.push(
      "Pasa los tres criterios del arnes de validacion: el intervalo de confianza de la expectancy en R es positivo por completo, la muestra es suficiente, y el profit factor supera 1.0.",
    );
    recommendations.push(
      "Seguir confirmando en vivo antes de asignar capital real -- un PASA en walk-forward historico no reemplaza esa confirmacion.",
    );
    return { reasons, recommendations };
  }

  if (detail.sample_size === "INSUFFICIENT_SAMPLE") {
    reasons.push(
      `Muestra insuficiente: solo ${detail.oos_trades} trades out-of-sample -- el intervalo de confianza de la expectancy es demasiado ancho para concluir nada con seguridad todavia.`,
    );
    recommendations.push("Correr sobre un rango de fechas mas largo, o poolear varios simbolos, antes de aceptar o descartar esta señal.");
    return { reasons, recommendations };
  }

  if (pf !== null && pf < 1) {
    const loseMultiple = pf > 0 ? (1 / pf).toFixed(2) : "mucho mas";
    reasons.push(
      `Profit factor ${pf.toFixed(2)} -- por cada $1 que gana, pierde ~$${loseMultiple}. La cuenta pierde dinero en promedio con cada trade adicional, sin importar cuanto tiempo se sostenga.`,
    );
  }
  if (winPct < 35) {
    reasons.push(
      `Win rate muy bajo (${winPct.toFixed(1)}%) -- el problema principal es la ENTRADA: la señal falla la mayoria de las veces, no el tamaño de las perdidas.`,
    );
    recommendations.push("No vale la pena ajustar el stop loss/take profit -- la premisa de entrada necesitaria cambiar por completo, o descartarse.");
  } else if (pf !== null && pf < 1) {
    reasons.push(
      `Win rate razonable (${winPct.toFixed(1)}%) pero el resultado neto es negativo -- la asimetria esta en el TAMAÑO de ganancias vs perdidas, no en la direccion.`,
    );
    recommendations.push(
      "Explorar un SL/TP con mejor relacion riesgo/beneficio -- pero validar SIEMPRE con un walk-forward completo nuevo, no con una estimacion sobre los trades ya vistos (una estimacion asi de prometedora puede no replicar en la practica).",
    );
  }
  if (detail.max_drawdown_pct !== null && detail.max_drawdown_pct > 0.2) {
    reasons.push(
      `Drawdown maximo real de ${(detail.max_drawdown_pct * 100).toFixed(1)}% sobre una cuenta de $100 -- muy por encima de lo que cualquier gestion de riesgo razonable toleraria.`,
    );
  }
  if (detail.expectancy_r !== null && detail.expectancy_r < -0.5) {
    reasons.push(
      `Expectancy muy negativa (${detail.expectancy_r.toFixed(2)}R por trade) -- no es un resultado marginal, es una perdida sistematica y grande.`,
    );
  }

  // Excursion favorable de los perdedores cerrados por stop -- mismo
  // tipo de analisis que se haria a mano con un script aparte, calculado
  // aca en vivo sobre ESTA validacion.
  const stoppedLosers = detail.trades.filter(
    (t) => t.exit_reason === "stop_loss" && t.pnl_usd != null && t.pnl_usd < 0,
  );
  if (stoppedLosers.length >= 20) {
    const withMargin = stoppedLosers.filter((t) => t.risk_usd && t.risk_usd > 0 && t.mfe_usd / t.risk_usd >= 0.3);
    const pct = (withMargin.length / stoppedLosers.length) * 100;
    if (pct >= 30) {
      reasons.push(
        `${pct.toFixed(0)}% de los ${stoppedLosers.length} trades que perdieron todo el riesgo primero habian alcanzado al menos 0.3R a favor antes de revertir -- hay margen real para proteger perdedores, pero ojo: un stop mas ajustado tambien puede cortar ganadores antes de tiempo.`,
      );
    }
  }

  if (reasons.length === 0) {
    reasons.push(
      "No pasa el criterio (el intervalo de confianza de la expectancy cruza cero), aunque el resultado no es dramaticamente negativo -- podria ser ruido, no necesariamente una señal mala.",
    );
    recommendations.push("Correr con mas datos o un rango de fechas distinto antes de descartarla del todo.");
  }
  if (pf !== null && pf < 0.5 && winPct < 25) {
    recommendations.push(
      "Con esta combinacion (profit factor y win rate muy bajos, con muestra suficiente) la evidencia es fuerte: la estrategia no funciona y es poco probable que un ajuste de SL/TP la salve sin cambiar la premisa de entrada.",
    );
  }

  return { reasons, recommendations };
}

function DiagnosisPanel({ detail }: { detail: ValidationDetailData }) {
  const { reasons, recommendations } = diagnoseValidation(detail);
  return (
    <div className="validation-diagnosis">
      <div>
        <h4>Por que {detail.verdict === "PASA" ? "pasa" : "no fue rentable"}</h4>
        <ul>
          {reasons.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      </div>
      <div>
        <h4>Recomendaciones</h4>
        <ul>
          {recommendations.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      </div>
    </div>
  );
}

// Cada cuanto se refresca el estado en vivo de las estrategias (mismo
// orden de magnitud que el poll del heatmap en App.tsx -- el z-score de
// funding y el cluster de liquidacion no cambian mas rapido que eso).
const STRATEGY_STATUS_POLL_MS = 20000;

function StrategyCard({ strategy }: { strategy: StrategyInfo }) {
  return (
    <div className="strategy-card">
      <div className="strategy-card-header">
        <h3>{strategy.title}</h3>
        <span className={`strategy-direction ${strategy.direction}`}>
          {strategy.direction === "long" ? "LONG" : "SHORT"}
        </span>
      </div>
      <p className="strategy-description">{strategy.description}</p>
      <p className="strategy-size">Tamano de orden: ${strategy.size_usd.toLocaleString()}</p>
      <div className={`strategy-status ${strategy.status.state}`} title={strategy.status.detail}>
        <span className="strategy-status-dot" />
        <span>Status: {statusLabel(strategy.status.state)}</span>
      </div>
    </div>
  );
}

function statusLabel(state: string): string {
  if (state === "condiciones_cumplidas") return "Condiciones cumplidas";
  if (state === "sin_datos") return "Sin datos";
  return "Analizando";
}

/**
 * Tabla compacta de estrategias (2026-09-19, pedido del usuario: el grid
 * de tarjetas con descripcion completa "deberia limpiar el campo visual
 * del apartado de bots y solo dejar la tabla de estrategias") -- el
 * detalle completo (descripcion, tamaño, status) se mudo DENTRO de
 * RunDetail/ValidationDetailView (`StrategyCard` arriba, reusado tal
 * cual), no se perdio, solo dejo de repetirse en la pantalla principal.
 */
function StrategiesTable({ strategies }: { strategies: StrategyInfo[] }) {
  return (
    <table className="bots-runs">
      <thead>
        <tr>
          <th>Estrategia</th>
          <th>Direccion</th>
          <th>Tamano</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        {strategies.map((s) => (
          <tr key={s.key}>
            <td>{s.title}</td>
            <td>
              <span className={`strategy-direction ${s.direction}`}>
                {s.direction === "long" ? "LONG" : "SHORT"}
              </span>
            </td>
            <td>${s.size_usd.toLocaleString()}</td>
            <td className={`strategy-status ${s.status.state}`} title={s.status.detail}>
              <span className="strategy-status-dot" /> {statusLabel(s.status.state)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Journal de trades completo (~20 columnas, pedido del usuario,
 * 2026-09-03) -- extraido de RunDetail (2026-09-05) para que
 * ValidationDetailView pueda mostrar los trades OOS de una validacion
 * walk-forward en el MISMO formato, en vez de solo un resumen por tramo
 * (pedido explicito: "lo que no veo es nuestro formato de tabla para los
 * trades"). Sin logica propia mas alla de renderizar -- `trades` ya viene
 * en el shape que necesita (`BacktestTradeDetail`), sea de una corrida
 * normal o de una validacion. */
// Clave compuesta para el set de "trades vistos" -- NUNCA solo trade_seq,
// que se reinicia en cada corrida/validacion y sombrearia filas de otra
// corrida que comparten numero por casualidad (ver declaracion de
// `botsViewedTrades` en App.tsx).
function viewedKey(contextId: string, tradeSeq: number): string {
  return `${contextId}:${tradeSeq}`;
}

function TradeJournalTable({
  trades,
  symbol,
  interval,
  strategyTitle,
  contextId,
  viewedTrades,
  onMarkViewed,
  onViewInChart,
}: {
  trades: BacktestTradeDetail[];
  symbol: string;
  interval: string;
  strategyTitle: string;
  contextId: string;
  viewedTrades: Set<string>;
  onMarkViewed: (ids: string[]) => void;
  onViewInChart: (symbol: string, interval: string, evidence: ChartEvidence) => void;
}) {
  // Totales (pedido del usuario, 2026-09-19): "agrega los totales al
  // final" -- mismos numeros que ya resume ValidationDetailView arriba,
  // pero sobre EXACTAMENTE los trades de esta tabla (util tambien en
  // RunDetail, que no tiene ese resumen).
  const closed = trades.filter((t) => t.pnl_usd != null);
  const wins = closed.filter((t) => (t.pnl_usd as number) >= 0).length;
  const totalPnl = closed.reduce((sum, t) => sum + (t.pnl_usd as number), 0);
  return (
    <div className="bots-trades-scroll">
      <table className="bots-trades">
        <thead>
          <tr>
            <th>ID</th>
            <th>Entrada</th>
            <th>Instrumento</th>
            <th>Temporalidad</th>
            <th>Direccion</th>
            <th>Precio Entrada</th>
            <th>SL Inicial</th>
            <th>TP Teorico</th>
            <th>Precio Salida</th>
            <th>Salida</th>
            <th>Riesgo %</th>
            <th>P&amp;L</th>
            <th>R:R</th>
            <th>Costos Ejecucion</th>
            <th>Funding</th>
            <th>MFE</th>
            <th>MAE</th>
            <th>Disparador / Setup</th>
            <th>Evidencia</th>
            <th>Notas</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t, i) => {
            const pnlClass = t.pnl_usd != null ? (t.pnl_usd >= 0 ? "trade-win" : "trade-loss") : "";
            // Sombreado de "ya visto" (pedido del usuario, 2026-09-20): se
            // conserva al cerrar evidencia y visitar otras, solo se limpia
            // desde App.tsx (ver `botsViewedTrades`).
            const viewedClass = viewedTrades.has(viewedKey(contextId, t.trade_seq)) ? "trade-viewed" : "";
            return (
            <tr
              key={i}
              className={[pnlClass, viewedClass].filter(Boolean).join(" ")}
            >
              <td>#{String(t.trade_seq).padStart(3, "0")}</td>
              <td>{formatUtc(t.entry_time)}</td>
              <td>{symbol}</td>
              <td>{interval}</td>
              <td>{t.side === "long" ? "Largo" : "Corto"}</td>
              <td>{t.entry_price.toLocaleString()}</td>
              {t.stop_loss_price != null ? (
                <td>{t.stop_loss_price.toLocaleString()}</td>
              ) : (
                <td
                  className="cell-na"
                  title="Esta estrategia no define un stop por precio (ver su descripcion). Sin un SL no hay como calcular esto sin inventar un numero."
                >
                  N/A
                </td>
              )}
              {t.take_profit_price != null ? (
                <td>{t.take_profit_price.toLocaleString()}</td>
              ) : (
                <td className="cell-na" title="Sin objetivo de precio fijo -- mismo motivo que el SL Inicial.">
                  N/A
                </td>
              )}
              <td>{t.exit_price != null ? t.exit_price.toLocaleString() : "--"}</td>
              <td>{t.exit_time ? formatUtc(t.exit_time) : "abierta"}</td>
              {t.risk_usd != null ? (
                <td title={`Riesgo: ${formatNotional(t.risk_usd)} sobre ${formatNotional(t.size_usd)} de tamano`}>
                  {((t.risk_usd / t.size_usd) * 100).toFixed(2)}%
                </td>
              ) : (
                <td
                  className="cell-na"
                  title="Riesgo % = distancia al SL sobre el tamano de la posicion -- depende de un SL en precio, que esta estrategia no define."
                >
                  N/A
                </td>
              )}
              <td>{t.pnl_usd != null ? formatNotional(t.pnl_usd) : "--"}</td>
              {t.r_multiple != null ? (
                <td className={t.r_multiple >= 0 ? "trade-win" : "trade-loss"}>
                  {t.r_multiple.toFixed(2)}R
                </td>
              ) : (
                <td
                  className="cell-na"
                  title="(Salida - Entrada) / Riesgo -- depende de un SL en precio, que esta estrategia no define."
                >
                  N/A
                </td>
              )}
              <td
                title={`Comision: ${formatNotional(t.commission_usd)}\nSlippage: ${formatNotional(t.slippage_usd)}`}
              >
                {formatNotional(t.commission_usd + t.slippage_usd)}
              </td>
              <td>{formatNotional(t.funding_paid_usd)}</td>
              <td>{formatNotional(t.mfe_usd)}</td>
              <td>{formatNotional(t.mae_usd)}</td>
              <td className="cell-wrap" title={JSON.stringify(t.signal_snapshot, null, 2)}>
                {describeTrigger(strategyTitle, t.signal_snapshot)}
              </td>
              <td>
                <button
                  type="button"
                  className="bots-view-in-chart"
                  onClick={() => {
                    onMarkViewed([viewedKey(contextId, t.trade_seq)]);
                    onViewInChart(symbol, interval, evidenceForTrades([t], "trade"));
                  }}
                >
                  Ver en el chart &rarr;
                </button>
              </td>
              <td className="cell-wrap">{describeExit(t)}</td>
            </tr>
            );
          })}
        </tbody>
        <tfoot>
          <tr className="bots-trades-totals">
            <td colSpan={11}>
              Totales: {closed.length} trades cerrados &middot; win rate{" "}
              {closed.length ? ((wins / closed.length) * 100).toFixed(1) : "0.0"}%
            </td>
            <td className={totalPnl >= 0 ? "trade-win" : "trade-loss"}>{formatNotional(totalPnl)}</td>
            <td colSpan={6} />
            <td>
              {closed.length > 0 && (
                <button
                  type="button"
                  className="bots-view-in-chart"
                  onClick={() => {
                    onMarkViewed(closed.map((t) => viewedKey(contextId, t.trade_seq)));
                    onViewInChart(symbol, interval, evidenceForTrades(closed, "total"));
                  }}
                >
                  Ver todos en el chart &rarr;
                </button>
              )}
            </td>
            <td />
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

function RunDetail({
  runId,
  strategies,
  viewedTrades,
  onMarkViewed,
  onBack,
  onViewInChart,
}: {
  runId: string;
  strategies: StrategyInfo[];
  viewedTrades: Set<string>;
  onMarkViewed: (ids: string[]) => void;
  onBack: () => void;
  onViewInChart: (symbol: string, interval: string, evidence: ChartEvidence) => void;
}) {
  const [detail, setDetail] = useState<BacktestRunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDetail(null);
    fetchBacktestRun(runId)
      .then(setDetail)
      .catch((e) => setError(String(e)));
  }, [runId]);

  // Corridas viejas pueden referenciar una estrategia ya renombrada (ver
  // titleFor en BotsView mas abajo) -- mismo criterio, cae al slug crudo.
  const strategy = detail ? strategies.find((s) => s.key === detail.strategy) : undefined;
  const strategyTitle = detail ? (strategy?.title ?? detail.strategy) : "";

  return (
    <div className="bots-detail">
      <button className="bots-back" onClick={onBack}>
        &larr; Volver
      </button>
      {error && <p className="error">{error}</p>}
      {!detail && !error && <p className="panel-empty">Cargando...</p>}
      {detail && (
        <>
          <h2>
            {detail.symbol} &middot; {detail.strategy}
          </h2>
          <p className="bots-meta">
            {detail.interval} &middot; {new Date(detail.data_start).toLocaleDateString()} &ndash;{" "}
            {new Date(detail.data_end).toLocaleDateString()} &middot; comision {detail.commission_bps}bps
            &middot; slippage {detail.slippage_bps}bps
          </p>
          {/* Detalle de la estrategia -- se mudo aca (2026-09-19, pedido
              del usuario) desde el grid de tarjetas de la pantalla
              principal de Bots, ahora una tabla compacta. */}
          {strategy && <StrategyCard strategy={strategy} />}
          {detail.equity_curve.length > 0 ? (
            <EquityCurveChart points={detail.equity_curve} />
          ) : (
            <p className="panel-empty">
              Esta corrida se guardo antes de que la curva de equity se persistiera --
              solo hay tabla de trades.
            </p>
          )}
          <TradeJournalTable
            trades={detail.trades}
            symbol={detail.symbol}
            interval={detail.interval}
            strategyTitle={strategyTitle}
            contextId={runId}
            viewedTrades={viewedTrades}
            onMarkViewed={onMarkViewed}
            onViewInChart={onViewInChart}
          />
        </>
      )}
    </div>
  );
}

function formatCiR(ci: [number, number] | null): string {
  if (ci === null) return "N/A";
  return `[${ci[0].toFixed(2)}R, ${ci[1].toFixed(2)}R]`;
}

function formatCiPct(ci: [number, number]): string {
  return `[${(ci[0] * 100).toFixed(0)}%, ${(ci[1] * 100).toFixed(0)}%]`;
}

/** Badge de veredicto -- "PASA"/"NO PASA" solos no dicen si NO PASA fue
 * por expectancy negativa (IC95% entero por debajo de cero) o por falta
 * de muestra (IC95% cruza cero, o menos del minimo de trades que exige
 * el criterio de validacion) -- esa distincion se muestra siempre, no
 * solo un si/no. */
function VerdictBadge({ verdict, sampleSize }: { verdict: string; sampleSize: string }) {
  if (verdict === "PASA") return <span className="verdict-badge verdict-pasa">PASA</span>;
  if (sampleSize === "INSUFFICIENT_SAMPLE") {
    return <span className="verdict-badge verdict-insuficiente">NO PASA -- muestra insuficiente</span>;
  }
  return <span className="verdict-badge verdict-no-pasa">NO PASA</span>;
}

function ValidationDetailView({
  validationId,
  strategies,
  viewedTrades,
  onMarkViewed,
  onBack,
  onViewInChart,
}: {
  validationId: string;
  strategies: StrategyInfo[];
  viewedTrades: Set<string>;
  onMarkViewed: (ids: string[]) => void;
  onBack: () => void;
  onViewInChart: (symbol: string, interval: string, evidence: ChartEvidence) => void;
}) {
  const [detail, setDetail] = useState<ValidationDetailData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDetail(null);
    fetchValidation(validationId)
      .then(setDetail)
      .catch((e) => setError(String(e)));
  }, [validationId]);

  // Mismo criterio de fallback que RunDetail -- ver titleFor en BotsView.
  const strategy = detail ? strategies.find((s) => s.key === detail.strategy) : undefined;
  const strategyTitle = detail ? (strategy?.title ?? detail.strategy) : "";

  return (
    <div className="bots-detail">
      <button className="bots-back" onClick={onBack}>
        &larr; Volver
      </button>
      {error && <p className="error">{error}</p>}
      {!detail && !error && <p className="panel-empty">Cargando...</p>}
      {detail && (
        <>
          <h2>
            {detail.symbol} &middot; {detail.strategy} &middot; walk-forward
          </h2>
          <p className="bots-meta">
            {detail.interval} &middot; {new Date(detail.data_start).toLocaleDateString()} &ndash;{" "}
            {new Date(detail.data_end).toLocaleDateString()} &middot; entrenamiento minimo{" "}
            {detail.min_train_days}d &middot; prueba {detail.test_days}d por tramo
          </p>
          {/* Detalle de la estrategia -- ver mismo comentario en RunDetail. */}
          {strategy && <StrategyCard strategy={strategy} />}
          <div className="validation-summary">
            <div>
              <span className="validation-summary-label">Veredicto</span>
              <VerdictBadge verdict={detail.verdict} sampleSize={detail.sample_size} />
            </div>
            <div>
              <span className="validation-summary-label">Tramos / Trials</span>
              <span>{detail.folds} / {detail.total_trials_count}</span>
            </div>
            <div>
              <span className="validation-summary-label">Trades OOS</span>
              <span>{detail.oos_trades}</span>
            </div>
            <div>
              <span className="validation-summary-label">Win rate</span>
              <span>{(detail.win_rate * 100).toFixed(1)}% {formatCiPct(detail.win_rate_ci_95)}</span>
            </div>
            <div>
              <span className="validation-summary-label">Profit Factor</span>
              <span className={detail.profit_factor != null && detail.profit_factor >= 1 ? "trade-win" : "trade-loss"}>
                {detail.profit_factor != null ? detail.profit_factor.toFixed(2) : "N/A"}
              </span>
            </div>
            <div>
              <span className="validation-summary-label">Expectancy</span>
              <span className={detail.expectancy_r != null && detail.expectancy_r >= 0 ? "trade-win" : "trade-loss"}>
                {detail.expectancy_r != null ? `${detail.expectancy_r.toFixed(3)}R` : "N/A"}{" "}
                {formatCiR(detail.expectancy_r_ci_95)}
                {detail.expectancy_usd != null ? ` (${detail.expectancy_usd.toFixed(2)} USD/trade)` : ""}
              </span>
            </div>
          </div>

          {/* Diagnostico (pedido del usuario, 2026-09-19): "por que" no
              fue rentable + recomendaciones, ANTES de las tablas -- es la
              lectura que mas importa, no algo que haya que bajar a buscar. */}
          <DiagnosisPanel detail={detail} />

          {/* Orden invertido (pedido del usuario, 2026-09-19): el journal
              de trades primero -- antes quedaba varias pantallas mas
              abajo del resumen por tramo, que es menos accionable. */}
          <h3>Trades out-of-sample (todos los tramos, formato de journal)</h3>
          {detail.trades.length === 0 ? (
            <p className="panel-empty">
              Esta validacion se guardo antes de que el journal de trades se persistiera para
              validaciones -- solo queda el resumen por tramo de abajo.
            </p>
          ) : (
            <TradeJournalTable
              trades={detail.trades}
              symbol={detail.symbol}
              interval={detail.interval}
              strategyTitle={strategyTitle}
              contextId={validationId}
              viewedTrades={viewedTrades}
              onMarkViewed={onMarkViewed}
              onViewInChart={onViewInChart}
            />
          )}

          <h3>Tramos del walk-forward</h3>
          <div className="bots-trades-scroll">
            <table className="bots-trades">
              <thead>
                <tr>
                  <th>Entrenamiento</th>
                  <th>Prueba (OOS)</th>
                  <th>Trials</th>
                  <th>Parametros elegidos</th>
                  <th>Sobrevive BH</th>
                  <th>Trades OOS</th>
                </tr>
              </thead>
              <tbody>
                {detail.by_fold.map((f, i) => (
                  <tr key={i} className={f.oos_trades > 0 ? "" : "cell-na"}>
                    <td>
                      {new Date(f.train_start).toLocaleDateString()} &ndash;{" "}
                      {new Date(f.train_end).toLocaleDateString()}
                    </td>
                    <td>
                      {new Date(f.test_start).toLocaleDateString()} &ndash;{" "}
                      {new Date(f.test_end).toLocaleDateString()}
                    </td>
                    <td>{f.trials_count}</td>
                    <td className="cell-wrap">
                      {f.chosen_params
                        ? Object.entries(f.chosen_params)
                            .map(([k, v]) => `${k}=${v}`)
                            .join(", ")
                        : "sin candidato con trades en entrenamiento"}
                    </td>
                    <td>{f.chosen_survives_bh ? "si" : "no"}</td>
                    <td>{f.oos_trades}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

/**
 * Vista "Bots": resultados de las corridas de backtest guardadas con
 * `python -m backtest --save` (ver app/backtest/__main__.py). Sin router
 * nuevo -- App.tsx solo cambia que renderiza en `.chart-container`.
 */
export function BotsView({
  selectedRun,
  onSelectRun,
  selectedValidation,
  onSelectValidation,
  viewedTrades,
  onMarkViewed,
  onViewInChart,
  section,
  onSectionChange,
}: {
  /** Seccion activa de la lista principal (2026-10-20): "estrategias" =
   * backtests/validaciones de siempre; "daytrading" = paper trading. Vive en
   * App.tsx por la misma razon que `selectedRun`: BotsView se desmonta al
   * entrar a una evidencia. */
  section: "estrategias" | "daytrading";
  onSectionChange: (section: "estrategias" | "daytrading") => void;
  // Controlados desde App.tsx (2026-09-20, arreglo de bug: "al abrir la
  // evidencia y despues cerrarla, no me regresa al listado anterior") --
  // antes eran useState local de este componente, pero entrar/salir de
  // evidencia desmonta BotsView entero (cambia `view` en App.tsx), asi que
  // cualquier estado que deba sobrevivir ese ciclo tiene que vivir un nivel
  // arriba. Ver la declaracion de `botsSelectedRun` etc. en App.tsx.
  selectedRun: string | null;
  onSelectRun: (id: string | null) => void;
  selectedValidation: string | null;
  onSelectValidation: (id: string | null) => void;
  viewedTrades: Set<string>;
  onMarkViewed: (ids: string[]) => void;
  onViewInChart: (symbol: string, interval: string, evidence: ChartEvidence) => void;
}) {
  const [runs, setRuns] = useState<BacktestRunSummary[]>([]);
  const [validations, setValidations] = useState<ValidationSummary[]>([]);
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchBacktestRuns()
      .then(setRuns)
      .catch((e) => setError(String(e)));
    fetchValidations()
      .then(setValidations)
      .catch((e) => setError(String(e)));
  }, []);

  // Estado en vivo de las estrategias -- se refresca solo, no depende de
  // interaccion del usuario, para que "Status: Analizando" refleje datos
  // recientes de verdad (ver api/routers/backtest.py::list_strategies).
  useEffect(() => {
    let cancelled = false;
    const load = () => {
      fetchBacktestStrategies()
        .then((list) => {
          if (!cancelled) setStrategies(list);
        })
        .catch(() => {});
    };
    load();
    const id = window.setInterval(load, STRATEGY_STATUS_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  if (selectedRun) {
    return (
      <RunDetail
        runId={selectedRun}
        strategies={strategies}
        viewedTrades={viewedTrades}
        onMarkViewed={onMarkViewed}
        onBack={() => onSelectRun(null)}
        onViewInChart={onViewInChart}
      />
    );
  }

  if (selectedValidation) {
    return (
      <ValidationDetailView
        validationId={selectedValidation}
        strategies={strategies}
        viewedTrades={viewedTrades}
        onMarkViewed={onMarkViewed}
        onBack={() => onSelectValidation(null)}
        onViewInChart={onViewInChart}
      />
    );
  }

  // Corridas VIEJAS pueden referenciar una estrategia ya renombrada/partida
  // -- se cae al slug crudo en vez de romper.
  const titleFor = (key: string) => strategies.find((s) => s.key === key)?.title ?? key;

  return (
    <div className="bots-view">
      <div className="bots-tabs">
        <button type="button" className={`toolbar-button${section === "estrategias" ? " on" : ""}`} onClick={() => onSectionChange("estrategias")}>
          Estrategias y backtests
        </button>
        <button type="button" className={`toolbar-button${section === "daytrading" ? " on" : ""}`} onClick={() => onSectionChange("daytrading")}>
          DAY TRADING
        </button>
      </div>
      {section === "daytrading" ? (
        <PaperTradingView onViewInChart={onViewInChart} />
      ) : (
      <>
      <h2>Bots &mdash; estrategias y corridas de backtest</h2>
      {error && <p className="error">{error}</p>}
      {strategies.length > 0 && <StrategiesTable strategies={strategies} />}
      {runs.length === 0 && !error && (
        <p className="panel-empty">
          Sin corridas guardadas todavia -- correlas con <code>python -m backtest --save</code>.
        </p>
      )}
      {runs.length > 0 && (
        <table className="bots-runs">
          <thead>
            <tr>
              <th>Fecha</th>
              <th>Simbolo</th>
              <th>Estrategia</th>
              <th>Rango</th>
              <th>Trades</th>
              <th>Win rate</th>
              <th>P&amp;L</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.run_id} onClick={() => onSelectRun(r.run_id)} className="bots-run-row">
                <td>{new Date(r.started_at).toLocaleString()}</td>
                <td>{r.symbol}</td>
                <td>{titleFor(r.strategy)}</td>
                <td>
                  {new Date(r.data_start).toLocaleDateString()} &ndash; {new Date(r.data_end).toLocaleDateString()}
                </td>
                <td>{r.trades}</td>
                <td>{(r.win_rate * 100).toFixed(0)}%</td>
                <td className={r.total_pnl_usd >= 0 ? "trade-win" : "trade-loss"}>
                  {formatNotional(r.total_pnl_usd)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2>Validaciones (walk-forward)</h2>
      <p className="bots-meta">
        Criterio completo de validacion -- walk-forward anchored, barrido de parametros por
        tramo, correccion de Benjamini-Hochberg, resultado out-of-sample con su intervalo de confianza.
        Se muestran TODAS las corridas, incluidas las que NO PASAN -- un resultado negativo tambien es
        un dato real.
      </p>
      {validations.length === 0 && !error && (
        <p className="panel-empty">
          Sin validaciones guardadas todavia -- correlas con{" "}
          <code>python -m backtest --validate</code>.
        </p>
      )}
      {validations.length > 0 && (
        <table className="bots-runs">
          <thead>
            <tr>
              <th>Fecha</th>
              <th>Simbolo</th>
              <th>Estrategia</th>
              <th>Rango</th>
              <th>Tramos</th>
              <th>Trades OOS</th>
              <th>Win rate</th>
              <th>Profit Factor</th>
              <th>Expectancy (IC95%)</th>
              <th>Veredicto</th>
            </tr>
          </thead>
          <tbody>
            {validations.map((v) => (
              <tr
                key={v.validation_id}
                onClick={() => onSelectValidation(v.validation_id)}
                className="bots-run-row"
              >
                <td>{new Date(v.started_at).toLocaleString()}</td>
                <td>{v.symbol}</td>
                <td>{titleFor(v.strategy)}</td>
                <td>
                  {new Date(v.data_start).toLocaleDateString()} &ndash; {new Date(v.data_end).toLocaleDateString()}
                </td>
                <td>{v.folds}</td>
                <td>{v.oos_trades}</td>
                <td>{(v.win_rate * 100).toFixed(0)}%</td>
                <td className={v.profit_factor != null && v.profit_factor >= 1 ? "trade-win" : "trade-loss"}>
                  {v.profit_factor != null ? v.profit_factor.toFixed(2) : "N/A"}
                </td>
                <td className={v.expectancy_r != null && v.expectancy_r >= 0 ? "trade-win" : "trade-loss"}>
                  {v.expectancy_r != null ? `${v.expectancy_r.toFixed(3)}R` : "N/A"} {formatCiR(v.expectancy_r_ci_95)}
                </td>
                <td>
                  <VerdictBadge verdict={v.verdict} sampleSize={v.sample_size} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      </>
      )}
    </div>
  );
}
