export interface Candle {
  time: number; // unix seconds, UTC -- close_time de la vela
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  // Porcion de `volume` ejecutada por el TAKER comprador -- viene directo
  // de Binance (klines historicos Y en vivo, ver api/routers/candles.py y
  // api/routers/live.py). `null` solo en filas historicas viejas sin esta
  // columna poblada (ver db/schema.sql, columna nullable) -- nunca se
  // inventa un valor para esas.
  taker_buy_volume: number | null;
}

export interface HeatmapLevel {
  price: number;
  notional_usd: number;
}

export interface HeatmapCell {
  time_ms: number; // inicio EXACTO de este intervalo
  price: number;
  notional_usd: number;
  // Duracion EXACTA de este intervalo en ms -- cada celda dura lo que ese
  // nivel de notional estuvo activo, no un ancho de bucket fijo (ver
  // api/heatmap_model.py compute_liquidity_intervals).
  bucket_ms: number;
}

export interface HeatmapTier {
  leverage: number;
  levels: HeatmapLevel[]; // snapshot de "ahora", usado por VolumeProfile
  cells: HeatmapCell[]; // cuadricula precio x tiempo, usada por HeatmapOverlay
}

export interface ClusterSide {
  distance_pct: number | null;
  notional_usd: number;
}

export interface MarketSnapshot {
  funding_rate: number;
  oi_notional_usd: number;
  long_short_ratio: number | null;
  cluster_proximity: {
    long_cluster_below: ClusterSide;
    short_cluster_above: ClusterSide;
  };
}

export interface HeatmapResponse {
  label: string; // "MODELED . UNCALIBRATED" -- mostrar siempre visible
  symbol: string;
  as_of: string;
  reference_price: number;
  reference_price_time: string;
  open_interest_usd: number;
  market_snapshot: MarketSnapshot | null; // null en modo historico (as_of explicito)
  basket: { name: string; version: string; weights: Record<string, number> };
  source: "big_trades" | "open_interest";
  big_trades_count: number;
  big_trade_threshold_usd: number;
  grid_bucket_ms: number;
  bin_size_usd: number;
  tiers: HeatmapTier[];
}

export interface IndicatorPoint {
  time: number; // unix seconds
  value: number;
}

export interface RealLiquidationEvent {
  event_time_ms: number;
  side: "long" | "short"; // lado de la POSICION liquidada, ver recorder/liqrec/normalize.py
  price: number;
  price_kind: "avg_fill" | "bankruptcy";
  qty_base: number;
  quote_qty: number;
  exchange: string;
}

export interface RealLiquidationsResponse {
  source: string;
  label: string; // "REAL" -- a proposito distinto del "MODELED . UNCALIBRATED" del heatmap
  symbol: string;
  events: RealLiquidationEvent[];
}

/** Ver api/setups.py -- el "pool fade" del usuario (2026-09-04) llevado a
 * la pantalla. NO es una estrategia validada, es un panel de decision. */
export interface Setup {
  trigger: {
    event_time_ms: number;
    side: "long" | "short";
    price: number;
    quote_qty_usd: number;
  };
  pool: {
    total_notional_usd: number;
    leverage_tiers: number[];
    correlation: number;
    per_leverage_usd: Record<string, number>;
  };
  suggested: {
    direction: "long" | "short";
    entry: number;
    stop: number;
    target_low: number;
    target_high: number;
  };
  confirmations: string[];
  market_context: {
    funding_zscore: number | null;
    oi_trend: number | null;
    regime: string | null;
    cvd_trend_recent: number | null;
  };
}

export interface SetupsResponse {
  symbol: string;
  setups: Setup[];
  checked_liquidations: number;
}

export interface StrategyStatus {
  // "condiciones_cumplidas" | "analizando" | "sin_datos" -- ver
  // backtest/strategies/funding_squeeze.py Strategy.live_status.
  state: string;
  detail: string;
}

export interface StrategyInfo {
  key: string;
  title: string;
  description: string;
  direction: "long" | "short";
  size_usd: number;
  status: StrategyStatus;
}

export interface BacktestRunSummary {
  run_id: string;
  started_at: string;
  symbol: string;
  interval: string;
  strategy: string;
  data_start: string;
  data_end: string;
  trades: number;
  win_rate: number;
  total_pnl_usd: number;
}

export interface BacktestTradeDetail {
  trade_seq: number;
  side: string;
  entry_time: string;
  entry_price: number;
  exit_time: string | null;
  exit_price: number | null;
  size_usd: number;
  commission_usd: number;
  funding_paid_usd: number;
  pnl_usd: number | null;
  signal_snapshot: Record<string, unknown>;
  slippage_usd: number;
  mfe_usd: number;
  mae_usd: number;
  exit_signal_snapshot: Record<string, unknown> | null;
  exit_reason: string | null;
  stop_loss_price: number | null;
  take_profit_price: number | null;
  risk_usd: number | null;
  r_multiple: number | null;
}

export interface BigTradeEvent {
  event_time_ms: number;
  price: number;
  notional_usd: number;
  // "sell" = taker vendio agresivo, "buy" = taker compro agresivo (campo
  // "m" de Binance, is_buyer_maker) -- `null` cuando no se sabe (trades
  // historicos de antes de que se guardara este dato, ver db/schema.sql).
  side: "buy" | "sell" | null;
}

export interface BigTradesResponse {
  symbol: string;
  threshold_usd: number;
  events: BigTradeEvent[];
}

/** Ver api/routers/big_trades.py -- combina el historico archivado
 * (`big_trades`, TimescaleDB) con la cola en vivo (REST directo a Binance,
 * `api/bigtrades.py`). Sin `startIso`, el backend pide por defecto las
 * ultimas 48h -- a proposito mucho menos que `CANDLES_LOOKBACK_DAYS`: con
 * 3 meses de backfill CONTINUO de BTCUSDT a ~3,450 trades/dia (umbral
 * $100k), pedir 90 dias serian cientos de miles de filas por peticion. */
export function fetchBigTrades(
  symbol: string,
  opts: { startIso?: string; limit?: number; minNotionalUsd?: number } = {},
): Promise<BigTradesResponse> {
  const params = new URLSearchParams({ symbol });
  if (opts.startIso) params.set("start", opts.startIso);
  if (opts.limit) params.set("limit", String(opts.limit));
  if (opts.minNotionalUsd) params.set("min_notional_usd", String(opts.minNotionalUsd));
  return getJson<BigTradesResponse>(`/big-trades?${params}`);
}

/** Un reporte de validacion walk-forward (Fase 3, `backtest/validation.py`)
 * -- se guarda SIEMPRE (pase o no el veredicto, ver
 * `backtest/__main__.py --validate`): un
 * NO PASA es un resultado honesto, no un error que se descarta. */
export interface ValidationSummary {
  validation_id: string;
  started_at: string;
  symbol: string;
  interval: string;
  strategy: string;
  data_start: string;
  data_end: string;
  min_train_days: number;
  test_days: number;
  folds: number;
  total_trials_count: number;
  oos_trades: number;
  win_rate: number;
  win_rate_ci_95: [number, number];
  expectancy_r: number | null;
  expectancy_r_ci_95: [number, number] | null;
  // Agregado 2026-09-05 (goal de laboratorio, puntos 15/16/18) -- sobre
  // TODOS los trades OOS concatenados, mismo criterio
  // que ya usa BacktestRunDetail via `statistics.summarize`. `null` si no
  // hay perdidas cerradas (profit_factor) o ningun trade OOS cerrado.
  profit_factor: number | null;
  expectancy_usd: number | null;
  // Corregido 2026-09-19 (la meta real es un % chico y consistente por
  // semana/mes, no "50% diario"): caida maxima real de
  // una cuenta de $100 corriendo todos los trades OOS en orden
  // cronologico (fraccion, 0.10 = 10%) + $/dia neto de cada mes y $/semana
  // neto de cada semana ISO con trades. `{}` (no null) en validaciones sin
  // ningun trade cerrado, o guardadas antes de esta migracion.
  max_drawdown_pct: number | null;
  monthly_usd_per_day: Record<string, number>;
  weekly_usd_per_week: Record<string, number>;
  sample_size: string; // "OK" | "INSUFFICIENT_SAMPLE"
  verdict: string; // "PASA" | "NO PASA"
}

export interface ValidationFold {
  train_start: string;
  train_end: string;
  test_start: string;
  test_end: string;
  trials_count: number;
  chosen_params: Record<string, unknown> | null;
  chosen_survives_bh: boolean;
  oos_trades: number;
}

export interface ValidationDetail extends ValidationSummary {
  by_fold: ValidationFold[];
  // Trades OOS reales, todos los tramos juntos -- MISMO shape que
  // BacktestTradeDetail (backtest_trades) para reusar el mismo componente
  // de tabla que ya usa RunDetail, ver TradeJournalTable en BotsView.tsx.
  trades: BacktestTradeDetail[];
}

export function fetchValidations(): Promise<ValidationSummary[]> {
  return getJson<ValidationSummary[]>("/backtest/validations");
}

export function fetchValidation(validationId: string): Promise<ValidationDetail> {
  return getJson<ValidationDetail>(`/backtest/validations/${validationId}`);
}

export interface BacktestRunDetail {
  run_id: string;
  started_at: string;
  symbol: string;
  interval: string;
  strategy: string;
  params: Record<string, unknown>;
  data_start: string;
  data_end: string;
  commission_bps: number;
  slippage_bps: number;
  trades: BacktestTradeDetail[];
  equity_curve: { time: string; equity: number }[];
}

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000/api";

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) {
    throw new Error(`${path}: HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export function fetchSymbols(): Promise<string[]> {
  return getJson<string[]>("/symbols");
}

export function fetchIntervals(symbol: string): Promise<string[]> {
  return getJson<string[]>(`/intervals?${new URLSearchParams({ symbol })}`);
}

/** `startIso`: sin esto, `/api/candles` (limit=1,000,000 por defecto, ver
 * api/routers/candles.py) devuelve TODA la historia archivada -- con 6
 * años de velas de 5 minutos son ~622,000 filas, 61 MB, ~6s solo de
 * descarga (medido de verdad, 2026-09-01), antes de que el navegador
 * tenga que parsear+ordenar+`setData()` eso. Mientras esa carga sigue en
 * curso, cualquier interaccion (activar un indicador, por ejemplo) se
 * pisa con un chart todavia asentandose -- la causa real detras de varios
 * reportes de "desfase" de esta sesion que no eran del heatmap en si.
 * Sin `startIso` el caller pide el default de `App.tsx`.
 *
 * `endIso` (2026-09-19, vista "Bots" -- ver App.tsx "modo evidencia"):
 * acotar tambien el final del rango para poder pedir una ventana ANGOSTA
 * alrededor de un trade viejo (walk-forwards cubren 2024-2026 completo,
 * muy por fuera de los `CANDLES_LOOKBACK_DAYS` normales) sin traer toda
 * la historia desde ese punto hasta hoy. */
export function fetchCandles(
  symbol: string, interval: string, startIso?: string, endIso?: string,
): Promise<Candle[]> {
  const params = new URLSearchParams({ symbol, interval });
  if (startIso) params.set("start", startIso);
  if (endIso) params.set("end", endIso);
  return getJson<Candle[]>(`/candles?${params}`);
}

/** Directo a Binance, sin pasar por la DB -- ver api/routers/live.py.
 * Con `sinceUnixSeconds`, pagina desde ahi hasta ahora sin importar cuantas
 * velas haga falta (necesario: un hueco de un mes en 5m son miles de velas,
 * no un puñado). Sin eso, se comporta como antes: las ultimas `limit`. */
export function fetchLiveKlines(
  symbol: string,
  interval: string,
  opts: { sinceUnixSeconds?: number; limit?: number } = {},
): Promise<Candle[]> {
  const params = new URLSearchParams({ symbol, interval });
  if (opts.sinceUnixSeconds !== undefined) {
    params.set("start", new Date(opts.sinceUnixSeconds * 1000).toISOString());
  } else {
    params.set("limit", String(opts.limit ?? 200));
  }
  return getJson<Candle[]>(`/live-klines?${params}`);
}

/** `binSizeUsd`: ver `binSize.ts` -- sin esto, el backend cae a su propio
 * default fijo de $100 (bien para BTCUSDT, demasiado grueso para cualquier
 * simbolo mas barato). */
export function fetchHeatmap(symbol: string, binSizeUsd?: number): Promise<HeatmapResponse> {
  const params = new URLSearchParams({ symbol });
  if (binSizeUsd !== undefined) params.set("bin_size", String(binSizeUsd));
  return getJson<HeatmapResponse>(`/heatmap?${params}`);
}

export type IndicatorKind = "oi" | "cvd" | "funding";

/** Historico (DB) + cola en vivo ya combinados por el backend -- ver
 * api/routers/indicators.py. `cvd` es el unico que necesita `interval`
 * (es una transformacion de las velas; `oi`/`funding` son series propias). */
export function fetchIndicator(
  kind: IndicatorKind,
  symbol: string,
  opts: { interval?: string; startIso?: string; endIso?: string } = {},
): Promise<IndicatorPoint[]> {
  const params = new URLSearchParams({ symbol });
  if (opts.interval) params.set("interval", opts.interval);
  if (opts.startIso) params.set("start", opts.startIso);
  if (opts.endIso) params.set("end", opts.endIso);
  return getJson<IndicatorPoint[]>(`/indicators/${kind}?${params}`);
}

/** `startIso`: sin esto, el backend solo lee la hora en curso + la anterior
 * (ver api/real_liquidations.py::fetch_recent_liquidations) -- un "cola en
 * vivo", no una consulta historica. Con `startIso` recorre TODAS las horas
 * desde esa fecha (2026-09-04, reporte del usuario: los marcadores
 * "no persisten a lo largo del grafico" -- desaparecian en cuanto se
 * desplazaba mas de 1-2h hacia atras). `limit` sube de 50 a lo que haga
 * falta cuando se usa `startIso`, ver LiquidationVolumePane.tsx. */
export function fetchRealLiquidations(
  symbol: string, opts: { limit?: number; startIso?: string } = {},
): Promise<RealLiquidationsResponse> {
  const params = new URLSearchParams({ symbol, limit: String(opts.limit ?? 50) });
  if (opts.startIso) params.set("start", opts.startIso);
  return getJson<RealLiquidationsResponse>(`/real-liquidations?${params}`);
}

export function fetchSetups(symbol: string): Promise<SetupsResponse> {
  return getJson<SetupsResponse>(`/setups?${new URLSearchParams({ symbol })}`);
}

export function fetchBacktestRuns(): Promise<BacktestRunSummary[]> {
  return getJson<BacktestRunSummary[]>("/backtest/runs");
}

export function fetchBacktestRun(runId: string): Promise<BacktestRunDetail> {
  return getJson<BacktestRunDetail>(`/backtest/runs/${runId}`);
}

export function fetchBacktestStrategies(symbol = "BTCUSDT"): Promise<StrategyInfo[]> {
  return getJson<StrategyInfo[]>(`/backtest/strategies?${new URLSearchParams({ symbol })}`);
}

// --- Paper trading "DAY TRADING" (2026-10-20) -- api/routers/paper.py -----

async function sendJson<T>(method: "POST" | "PUT" | "PATCH", path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    // La API devuelve {"detail": "..."} con el motivo en espanol (SL del lado
    // equivocado, orden ya cerrada...) -- eso es lo que el usuario debe leer,
    // no "HTTP 422".
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (typeof data?.detail === "string") detail = data.detail;
      else if (Array.isArray(data?.detail)) detail = data.detail.map((d: { msg?: string }) => d.msg).join("; ");
    } catch {
      // cuerpo no-JSON: se queda el HTTP status
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export interface PaperSettings {
  initial_balance: number;
  fee_pct: number;
  default_risk_pct: number;
  max_leverage: number;
  analyst_min_trades: number;
}

export type PaperStatus = "pending" | "open" | "closed" | "cancelled";

export interface PaperTrade {
  trade_id: number;
  created_ms: number;
  symbol: string;
  interval: string;
  side: "long" | "short";
  status: PaperStatus;
  order_type: "market" | "limit";
  entry_price: number;
  sl_price: number;
  tp_price: number;
  qty: number;
  notional_usd: number;
  risk_usd: number;
  risk_pct: number;
  capped: boolean;
  balance_at_open: number;
  note: string;
  tags: string[];
  confidence: number | null;
  review_note: string | null;
  followed_plan: boolean | null;
  source_position_id: string | null;
  filled_at_ms: number | null;
  closed_at_ms: number | null;
  exit_price: number | null;
  exit_reason: "tp" | "sl" | "manual" | "cancelled" | null;
  ambiguous_exit: boolean;
  gross_pnl: number | null;
  fees: number | null;
  net_pnl: number | null;
  r_multiple: number | null;
  context: { session?: string; weekday?: string; hour_utc?: number };
  planned_rr: number | null;
  rr_bucket: string;
  leverage: number | null;
  mfe_usd: number | null;
  mae_usd: number | null;
}

export interface PaperSummary {
  initial_balance: number;
  balance: number;
  net_pnl: number;
  return_pct: number;
  closed_trades: number;
  open_trades: number;
  pending_trades: number;
  win_rate: number | null;
  profit_factor: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  expectancy_r: number | null;
  max_drawdown_usd: number;
  max_drawdown_pct: number;
  equity_curve: { time_ms: number; equity: number }[];
}

export interface PaperTradesResponse {
  trades: PaperTrade[];
  summary: PaperSummary;
  settings: PaperSettings;
  /** Texto si algun simbolo no se pudo consultar en Binance: la lista se
   * sirve igual, pero las ordenes vivas pueden estar desactualizadas. */
  refresh_error: string | null;
}

export interface PaperTradeInput {
  symbol: string;
  interval: string;
  side: "long" | "short";
  entry_price: number;
  sl_price: number;
  tp_price: number;
  order_type: "market" | "limit";
  risk_pct?: number;
  risk_usd?: number;
  note: string;
  tags: string[];
  confidence?: number;
  source_position_id?: string;
}

export interface PaperTradePatch {
  note?: string;
  tags?: string[];
  confidence?: number;
  review_note?: string;
  followed_plan?: boolean | null;
}

export interface PaperGroup {
  trades: number;
  wins: number;
  win_rate: number;
  net_pnl: number;
  profit_factor: number | null;
  expectancy_r: number | null;
  trade_ids: number[];
  top_terms: string[];
  verdict: "candidata" | "evitar" | "neutral" | "pocos_datos";
  verdict_text: string;
  logic: string;
}

export interface PaperUniqueCase {
  trade_id: number;
  symbol: string;
  side: "long" | "short";
  net_pnl: number;
  r_multiple: number | null;
  winner: boolean;
  note: string;
  message: string;
}

export type PaperAnalysis =
  | { status: "waiting"; closed_trades: number; min_trades: number; message: string }
  | {
      status: "ready";
      forced: boolean;
      closed_trades: number;
      min_trades: number;
      groups: PaperGroup[];
      unique_cases: PaperUniqueCase[];
      insights: string[];
    };

export function fetchPaperTrades(refresh = true): Promise<PaperTradesResponse> {
  return getJson<PaperTradesResponse>(`/paper/trades?refresh=${refresh}`);
}

export function createPaperTrade(body: PaperTradeInput): Promise<PaperTrade> {
  return sendJson<PaperTrade>("POST", "/paper/trades", body);
}

export function patchPaperTrade(id: number, body: PaperTradePatch): Promise<PaperTrade> {
  return sendJson<PaperTrade>("PATCH", `/paper/trades/${id}`, body);
}

export function closePaperTrade(id: number): Promise<PaperTrade> {
  return sendJson<PaperTrade>("POST", `/paper/trades/${id}/close`);
}

export function cancelPaperTrade(id: number): Promise<PaperTrade> {
  return sendJson<PaperTrade>("POST", `/paper/trades/${id}/cancel`);
}

export function putPaperSettings(body: Partial<PaperSettings>): Promise<PaperSettings> {
  return sendJson<PaperSettings>("PUT", "/paper/settings", body);
}

export function fetchPaperAnalysis(force = false): Promise<PaperAnalysis> {
  return getJson<PaperAnalysis>(`/paper/analysis?force=${force}`);
}
