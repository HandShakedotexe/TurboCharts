import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  cancelPaperTrade,
  closePaperTrade,
  fetchPaperAnalysis,
  fetchPaperTrades,
  patchPaperTrade,
  putPaperSettings,
  type PaperAnalysis,
  type PaperGroup,
  type PaperSettings,
  type PaperTrade,
  type PaperTradesResponse,
} from "../api";
import { formatUtc } from "../format";
import { EquityCurveChart } from "./EquityCurveChart";
import type { ChartEvidence } from "./BotsView";

const POLL_MS = 15_000;
// Contexto alrededor de la operacion al abrir su evidencia en el chart: en day
// trading unas horas bastan (los backtests usan 2 dias porque abarcan semanas).
const EVIDENCE_BUFFER_MS = 6 * 3_600_000;

function fmtUsd(v: number | null | undefined): string {
  if (v === null || v === undefined) return "--";
  // Con balances de ~$100 un P&L tipico es de centavos: formatNotional()
  // (pensado para millones) lo mostraria como "0".
  const abs = Math.abs(v);
  return `${v < 0 ? "-" : ""}$${abs.toFixed(abs < 1 ? 4 : 2)}`;
}

function fmtMs(ms: number | null): string {
  return ms === null ? "--" : formatUtc(new Date(ms).toISOString());
}

function evidenceFor(t: PaperTrade): ChartEvidence {
  const start = t.filled_at_ms ?? t.created_ms;
  const end = t.closed_at_ms ?? Date.now();
  return {
    positions: [{
      id: `paper-${t.trade_id}`, side: t.side, entryTimeMs: start,
      entryPrice: t.entry_price, tpPrice: t.tp_price, slPrice: t.sl_price,
    }],
    startMs: t.created_ms - EVIDENCE_BUFFER_MS,
    endMs: end + EVIDENCE_BUFFER_MS,
  };
}

/** `lightweight-charts` exige tiempos estrictamente crecientes: dos cierres en
 * el mismo segundo (o el punto inicial, que comparte instante con el primer
 * trade) harian lanzar una excepcion al pintar la curva. */
function curvePoints(curve: { time_ms: number; equity: number }[]): { time: string; equity: number }[] {
  const out: { time: string; equity: number }[] = [];
  let lastSec = -Infinity;
  for (const p of curve) {
    const sec = Math.max(Math.floor(p.time_ms / 1000), lastSec + 1);
    lastSec = sec;
    out.push({ time: new Date(sec * 1000).toISOString(), equity: p.equity });
  }
  return out;
}

function exitNote(t: PaperTrade): string {
  switch (t.status) {
    case "pending":
      return `Esperando que el precio toque ${t.entry_price.toLocaleString()}.`;
    case "open":
      return `Abierta: SL ${t.sl_price.toLocaleString()} / TP ${t.tp_price.toLocaleString()} vigentes.`;
    case "cancelled":
      return "Cancelada antes de llenarse.";
    default: {
      const base = t.exit_reason === "tp" ? "Take profit alcanzado." : t.exit_reason === "sl" ? "Stop loss tocado." : "Cerrada manualmente al precio de mercado.";
      return t.ambiguous_exit ? `${base} La misma vela toco SL y TP: se asume SL.` : base;
    }
  }
}

function setupNote(t: PaperTrade): string {
  const parts = [
    `${t.side === "long" ? "Long" : "Short"} del chart`,
    t.order_type === "market" ? "orden a mercado" : "orden pendiente",
    `R:R planeado ${t.planned_rr !== null ? t.planned_rr.toFixed(2) : "--"}`,
  ];
  if (t.context.session) parts.push(`sesion ${t.context.session.replace("_", " ")}`);
  if (t.capped) parts.push("tamano recortado por apalancamiento maximo");
  return parts.join(" · ");
}

function statusLabel(t: PaperTrade): string {
  return { pending: "Pendiente", open: "Abierta", closed: "Cerrada", cancelled: "Cancelada" }[t.status];
}

function SettingsPanel({ settings, onSaved }: { settings: PaperSettings; onSaved: () => void }) {
  const [draft, setDraft] = useState({
    initial_balance: String(settings.initial_balance),
    fee_pct: String(settings.fee_pct),
    default_risk_pct: String(settings.default_risk_pct),
    max_leverage: String(settings.max_leverage),
    analyst_min_trades: String(settings.analyst_min_trades),
  });
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const field = (key: keyof typeof draft, label: string, title: string, step = 1) => (
    <label className="paper-field paper-field-inline" title={title}>
      <span>{label}</span>
      <input type="number" step={step} value={draft[key]} onChange={(e) => setDraft({ ...draft, [key]: e.target.value })} />
    </label>
  );

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await putPaperSettings({
        initial_balance: Number(draft.initial_balance),
        fee_pct: Number(draft.fee_pct),
        default_risk_pct: Number(draft.default_risk_pct),
        max_leverage: Number(draft.max_leverage),
        analyst_min_trades: Math.round(Number(draft.analyst_min_trades)),
      });
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="paper-settings">
      <div className="paper-row paper-row-wrap">
        {field("initial_balance", "Balance inicial ($)", "El balance actual es esto mas el P&L de las operaciones cerradas.", 10)}
        {field("fee_pct", "Comision por lado (%)", "Taker de Binance Futures: 0.04%. Se cobra en la entrada y en la salida.", 0.01)}
        {field("default_risk_pct", "Riesgo por operacion (%)", "Porcentaje del balance que se pierde si toca el SL. Se puede cambiar en cada orden.", 0.5)}
        {field("max_leverage", "Apalancamiento maximo", "Tope de nocional / balance: con un SL muy cercano el tamano se recorta.", 1)}
        {field("analyst_min_trades", "Analista desde (trades)", "El bot analista se activa al llegar a este numero de operaciones cerradas.", 1)}
        <button type="button" className="toolbar-button on" onClick={save} disabled={saving}>
          {saving ? "Guardando..." : "Guardar ajustes"}
        </button>
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function AnalystPanel({
  analysis, onForce, onHighlight, highlighted, loading,
}: {
  analysis: PaperAnalysis | null;
  onForce: () => void;
  onHighlight: (ids: number[]) => void;
  highlighted: Set<number>;
  loading: boolean;
}) {
  if (!analysis) return <p className="panel-empty">Cargando analisis...</p>;
  if (analysis.status === "waiting") {
    const pct = Math.min(100, (analysis.closed_trades / analysis.min_trades) * 100);
    return (
      <div className="paper-analyst">
        <div className="paper-progress" title={`${analysis.closed_trades} de ${analysis.min_trades}`}>
          <div className="paper-progress-bar" style={{ width: `${pct}%` }} />
        </div>
        <p className="bots-meta">
          {analysis.message}{" "}
          {analysis.closed_trades >= 2 && (
            <button type="button" className="bots-view-in-chart" onClick={onForce} disabled={loading}>
              Analizar ahora de todos modos &rarr;
            </button>
          )}
        </p>
      </div>
    );
  }
  const idsEqual = (ids: number[]) => ids.length === highlighted.size && ids.every((id) => highlighted.has(id));
  return (
    <div className="paper-analyst">
      {analysis.forced && (
        <p className="bots-meta">
          Analisis forzado con {analysis.closed_trades} operaciones (el minimo es {analysis.min_trades}): tomalo como una pista, no como una conclusion.
        </p>
      )}
      {analysis.insights.length > 0 && (
        <ul className="paper-insights">
          {analysis.insights.map((s) => <li key={s}>{s}</li>)}
        </ul>
      )}
      {analysis.groups.length === 0 && (
        <p className="bots-meta">Todavia no hay dos operaciones lo bastante parecidas (en lo que escribiste y en su estructura) como para formar un grupo.</p>
      )}
      <div className="paper-groups">
        {analysis.groups.map((g: PaperGroup, i) => (
          <div key={g.trade_ids.join("-")} className={`strategy-card paper-group ${g.verdict}`}>
            <div className="strategy-card-header">
              <h3>Grupo {i + 1} &middot; {g.trades} operaciones</h3>
              <span className={`paper-verdict ${g.verdict}`}>{
                { candidata: "Candidata", evitar: "Evitar", neutral: "Neutral", pocos_datos: "Pocos datos" }[g.verdict]
              }</span>
            </div>
            <p className="strategy-description">{g.logic}</p>
            <p className="strategy-size">
              Win rate {(g.win_rate * 100).toFixed(0)}% &middot; P&amp;L <span className={g.net_pnl >= 0 ? "trade-win" : "trade-loss"}>{fmtUsd(g.net_pnl)}</span>
              {g.expectancy_r !== null && <> &middot; expectancy {g.expectancy_r.toFixed(2)}R</>}
              {g.profit_factor !== null && <> &middot; PF {g.profit_factor.toFixed(2)}</>}
            </p>
            <p className="strategy-size">{g.verdict_text}</p>
            <div className="paper-row paper-row-wrap">
              {g.trade_ids.map((id) => <span key={id} className="paper-chip">#{id}</span>)}
              <button type="button" className="bots-view-in-chart" onClick={() => onHighlight(idsEqual(g.trade_ids) ? [] : g.trade_ids)}>
                {idsEqual(g.trade_ids) ? "Quitar marca de la tabla" : "Marcar en la tabla"}
              </button>
            </div>
          </div>
        ))}
      </div>
      {analysis.unique_cases.length > 0 && (
        <>
          <h3 className="paper-subtitle">Casos unicos</h3>
          <ul className="paper-unique">
            {analysis.unique_cases.map((c) => (
              <li key={c.trade_id} className={c.winner ? "winner" : ""}>
                <b>{c.winner ? "★ " : ""}#{c.trade_id}</b> {c.symbol} {c.side === "long" ? "LONG" : "SHORT"}{" "}
                <span className={c.net_pnl >= 0 ? "trade-win" : "trade-loss"}>{fmtUsd(c.net_pnl)}</span> &mdash; {c.message}
                {c.note && <em> «{c.note}»</em>}
              </li>
            ))}
          </ul>
        </>
      )}
      <p className="bots-meta">
        El analista agrupa por similitud estadistica (palabras de tus notas + lado, temporalidad, sesion y R:R); no entiende
        sinonimos ni mira el grafico. Escribir las notas con consistencia mejora los grupos.
      </p>
    </div>
  );
}

function ReasonCell({ t, onSaved }: { t: PaperTrade; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);
  const [note, setNote] = useState(t.note);
  const [review, setReview] = useState(t.review_note ?? "");
  const [tags, setTags] = useState(t.tags.join(", "));
  const [confidence, setConfidence] = useState<number | null>(t.confidence);
  const [followed, setFollowed] = useState<"" | "si" | "no">(t.followed_plan === null ? "" : t.followed_plan ? "si" : "no");
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setError(null);
    try {
      await patchPaperTrade(t.trade_id, {
        note: note.trim() || t.note,
        review_note: review.trim(),
        tags: tags.split(",").map((s) => s.trim()).filter(Boolean),
        confidence: confidence ?? undefined,
        followed_plan: followed === "" ? null : followed === "si",
      });
      setEditing(false);
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  if (!editing) {
    return (
      <>
        <div>{t.note}</div>
        {t.tags.length > 0 && (
          <div className="paper-row paper-row-wrap">
            {t.tags.map((tag) => <span key={tag} className="paper-chip">{tag}</span>)}
          </div>
        )}
        {(t.confidence !== null || t.followed_plan !== null) && (
          <div className="paper-meta">
            {t.confidence !== null && <>Confianza {t.confidence}/5</>}
            {t.confidence !== null && t.followed_plan !== null && " · "}
            {t.followed_plan !== null && (t.followed_plan ? "Segui mi plan" : "No segui mi plan")}
          </div>
        )}
        {t.review_note && <div className="paper-meta"><em>Revision: {t.review_note}</em></div>}
        <button type="button" className="bots-view-in-chart" onClick={() => setEditing(true)}>
          Editar / revisar
        </button>
      </>
    );
  }
  return (
    <div className="paper-edit">
      <textarea rows={3} value={note} onChange={(e) => setNote(e.target.value)} placeholder="Por que tomaste la operacion" />
      <textarea rows={2} value={review} onChange={(e) => setReview(e.target.value)} placeholder="Revision posterior: que harias distinto" />
      <input type="text" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="tags, separados por coma" />
      <div className="paper-row">
        {[1, 2, 3, 4, 5].map((n) => (
          <button key={n} type="button" className={`chart-tools-tool${confidence === n ? " on" : ""}`} onClick={() => setConfidence(confidence === n ? null : n)}>
            {n}
          </button>
        ))}
        <select value={followed} onChange={(e) => setFollowed(e.target.value as "" | "si" | "no")}>
          <option value="">¿Segui mi plan?</option>
          <option value="si">Si</option>
          <option value="no">No</option>
        </select>
      </div>
      {error && <p className="error">{error}</p>}
      <div className="paper-row">
        <button type="button" className="toolbar-button on" onClick={save}>Guardar</button>
        <button type="button" className="toolbar-button" onClick={() => setEditing(false)}>Cancelar</button>
      </div>
    </div>
  );
}

/**
 * Seccion "DAY TRADING" de la vista Bots: bitacora de paper trading con
 * las MISMAS columnas que el journal de los
 * backtests (ID, entrada, SL/TP, MFE/MAE, costos, evidencia...) mas una
 * columna "Mi razon" donde el usuario explica por que tomo cada operacion, y
 * el panel del bot analista que agrupa operaciones parecidas. Las ordenes se
 * crean desde el chart (boton PAPER de un indicador Long/Short), no aqui.
 */
export function PaperTradingView({ onViewInChart }: { onViewInChart: (symbol: string, interval: string, evidence: ChartEvidence) => void }) {
  const [data, setData] = useState<PaperTradesResponse | null>(null);
  const [analysis, setAnalysis] = useState<PaperAnalysis | null>(null);
  const [analysisLoading, setAnalysisLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [highlighted, setHighlighted] = useState<Set<number>>(new Set());

  const load = useCallback(() => {
    fetchPaperTrades(true)
      .then((r) => {
        setData(r);
        setError(null);
      })
      .catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    load();
    const id = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(id);
  }, [load]);

  const closedCount = data?.summary.closed_trades ?? 0;
  const loadAnalysis = useCallback((force: boolean) => {
    fetchPaperAnalysis(force)
      .then(setAnalysis)
      .catch((e) => setError(String(e)))
      .finally(() => setAnalysisLoading(false));
  }, []);
  const forceAnalysis = () => {
    setAnalysisLoading(true);
    loadAnalysis(true);
  };
  // Se re-analiza solo cuando cambia el numero de operaciones cerradas: el
  // agrupamiento no cambia con un tick de precio.
  useEffect(() => {
    if (data) loadAnalysis(false);
  }, [closedCount, data === null, loadAnalysis]); // eslint-disable-line react-hooks/exhaustive-deps

  const runAction = async (fn: () => Promise<unknown>) => {
    setActionError(null);
    try {
      await fn();
      load();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  };

  const points = useMemo(() => curvePoints(data?.summary.equity_curve ?? []), [data]);
  const closed = useMemo(() => (data?.trades ?? []).filter((t) => t.status === "closed"), [data]);

  if (!data) {
    return (
      <div>
        {error ? <p className="error">{error}</p> : <p className="panel-empty">Cargando paper trading...</p>}
      </div>
    );
  }

  const { summary, settings } = data;
  const stat = (label: string, value: ReactNode, cls = "") => (
    <div className="validation-summary-item">
      <span className="validation-summary-label">{label}</span>
      <span className={cls}>{value}</span>
    </div>
  );

  return (
    <div className="paper-view">
      <h2>DAY TRADING &mdash; paper trading</h2>
      <p className="bots-meta">
        Operaciones simuladas con el balance configurado. Las creas desde el chart: coloca un indicador <b>+ Long</b> / <b>+ Short</b>,
        ajusta entrada/SL/TP y pulsa <b>PAPER</b>. Aqui se registran, se resuelven solas contra las velas de 1 minuto de Binance y
        puedes explicar por que las tomaste.
      </p>
      {data.refresh_error && (
        <p className="error">No se pudo consultar Binance ({data.refresh_error}): las ordenes vivas pueden estar desactualizadas.</p>
      )}
      {error && <p className="error">{error}</p>}
      {actionError && <p className="error">{actionError}</p>}

      <div className="validation-summary">
        {stat("Balance", fmtUsd(summary.balance), summary.net_pnl >= 0 ? "trade-win" : "trade-loss")}
        {stat("P&L neto", `${fmtUsd(summary.net_pnl)} (${summary.return_pct.toFixed(2)}%)`, summary.net_pnl >= 0 ? "trade-win" : "trade-loss")}
        {stat("Balance inicial", fmtUsd(summary.initial_balance))}
        {stat("Operaciones", `${summary.closed_trades} cerradas · ${summary.open_trades} abiertas · ${summary.pending_trades} pendientes`)}
        {stat("Win rate", summary.win_rate !== null ? `${(summary.win_rate * 100).toFixed(0)}%` : "--")}
        {stat("Profit factor", summary.profit_factor !== null ? summary.profit_factor.toFixed(2) : "--")}
        {stat("Expectancy", summary.expectancy_r !== null ? `${summary.expectancy_r.toFixed(2)}R` : "--")}
        {stat("Max drawdown", `${fmtUsd(summary.max_drawdown_usd)} (${summary.max_drawdown_pct.toFixed(1)}%)`)}
      </div>

      <button type="button" className="bots-view-in-chart" onClick={() => setShowSettings((v) => !v)}>
        {showSettings ? "Ocultar ajustes de la cuenta" : "Ajustes de la cuenta (balance inicial, comision, riesgo)"}
      </button>
      {showSettings && <SettingsPanel settings={settings} onSaved={load} />}

      {points.length > 1 && (
        <>
          <h3 className="paper-subtitle">Curva de equity</h3>
          <EquityCurveChart points={points} />
        </>
      )}

      <h3 className="paper-subtitle">Bot analista</h3>
      <AnalystPanel
        analysis={analysis}
        onForce={forceAnalysis}
        onHighlight={(ids) => setHighlighted(new Set(ids))}
        highlighted={highlighted}
        loading={analysisLoading}
      />

      <h3 className="paper-subtitle">Bitacora de operaciones</h3>
      {data.trades.length === 0 ? (
        <p className="panel-empty">Sin operaciones todavia. Coloca un Long/Short en el chart y pulsa PAPER.</p>
      ) : (
        <div className="bots-trades-scroll">
          <table className="bots-trades paper-trades">
            <thead>
              <tr>
                <th>ID</th>
                <th>Estado</th>
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
                <th className="paper-reason-col">Mi razon (por que)</th>
                <th>Evidencia</th>
                <th>Notas</th>
                <th>Acciones</th>
              </tr>
            </thead>
            <tbody>
              {data.trades.map((t) => {
                const pnlClass = t.net_pnl !== null ? (t.net_pnl >= 0 ? "trade-win" : "trade-loss") : "";
                const hl = highlighted.has(t.trade_id) ? "trade-viewed" : "";
                return (
                  <tr key={t.trade_id} className={[pnlClass, hl].filter(Boolean).join(" ")}>
                    <td>#{String(t.trade_id).padStart(3, "0")}</td>
                    <td><span className={`paper-status ${t.status}`}>{statusLabel(t)}</span></td>
                    <td>{fmtMs(t.filled_at_ms ?? t.created_ms)}</td>
                    <td>{t.symbol}</td>
                    <td>{t.interval}</td>
                    <td>{t.side === "long" ? "Largo" : "Corto"}</td>
                    <td title={t.order_type === "limit" ? "Orden pendiente: entra al tocar este precio" : "A mercado: precio al momento de crear la orden"}>
                      {t.entry_price.toLocaleString()}
                    </td>
                    <td>{t.sl_price.toLocaleString()}</td>
                    <td>{t.tp_price.toLocaleString()}</td>
                    <td>{t.exit_price !== null ? t.exit_price.toLocaleString() : "--"}</td>
                    <td>{t.closed_at_ms !== null && t.status === "closed" ? fmtMs(t.closed_at_ms) : t.status === "open" ? "abierta" : "--"}</td>
                    <td title={`Riesgo ${fmtUsd(t.risk_usd)} sobre nocional ${fmtUsd(t.notional_usd)} (${t.leverage !== null ? t.leverage.toFixed(1) : "--"}x)`}>
                      {t.risk_pct.toFixed(2)}%
                    </td>
                    <td>{t.net_pnl !== null ? fmtUsd(t.net_pnl) : "--"}</td>
                    {t.r_multiple !== null ? (
                      <td className={t.r_multiple >= 0 ? "trade-win" : "trade-loss"}>{t.r_multiple.toFixed(2)}R</td>
                    ) : (
                      <td className="cell-na" title="Se calcula al cerrar: P&L neto / riesgo.">--</td>
                    )}
                    <td title={t.fees !== null ? `Comision ida y vuelta: ${fmtUsd(t.fees)}` : "Se cobra al cerrar"}>
                      {t.fees !== null ? fmtUsd(t.fees) : "--"}
                    </td>
                    <td className="cell-na" title="El simulador no cobra funding: no se inventa un numero.">N/A</td>
                    <td>{fmtUsd(t.mfe_usd)}</td>
                    <td>{fmtUsd(t.mae_usd)}</td>
                    <td className="cell-wrap">{setupNote(t)}</td>
                    <td className="cell-wrap paper-reason-col">
                      <ReasonCell key={`${t.trade_id}-${t.note}-${t.review_note}`} t={t} onSaved={load} />
                    </td>
                    <td>
                      <button
                        type="button"
                        className="bots-view-in-chart"
                        onClick={() => onViewInChart(t.symbol, t.interval, evidenceFor(t))}
                      >
                        Ver en el chart &rarr;
                      </button>
                    </td>
                    <td className="cell-wrap">{exitNote(t)}</td>
                    <td>
                      {t.status === "pending" && (
                        <button type="button" className="bots-view-in-chart" onClick={() => runAction(() => cancelPaperTrade(t.trade_id))}>
                          Cancelar orden
                        </button>
                      )}
                      {t.status === "open" && (
                        <button type="button" className="bots-view-in-chart" onClick={() => runAction(() => closePaperTrade(t.trade_id))}>
                          Cerrar ahora
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
            <tfoot>
              <tr className="bots-trades-totals">
                <td colSpan={12}>
                  Totales: {closed.length} cerradas &middot; win rate{" "}
                  {summary.win_rate !== null ? (summary.win_rate * 100).toFixed(1) : "0.0"}%
                </td>
                <td className={summary.net_pnl >= 0 ? "trade-win" : "trade-loss"}>{fmtUsd(summary.net_pnl)}</td>
                <td colSpan={10} />
              </tr>
            </tfoot>
          </table>
        </div>
      )}
    </div>
  );
}
