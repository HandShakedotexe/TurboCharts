import { useCallback, useEffect, useRef, useState } from "react";
import type { IChartApi, ISeriesApi, UTCTimestamp } from "lightweight-charts";
import { PriceChart } from "./components/PriceChart";
import { HeatmapOverlay } from "./components/HeatmapOverlay";
import { HeatmapLegend } from "./components/HeatmapLegend";
import { VolumeProfile } from "./components/VolumeProfile";
import { IndicatorPanes } from "./components/IndicatorPanes";
import { MarketSnapshotPanel } from "./components/MarketSnapshotPanel";
import { LiquidationVolumePane } from "./components/LiquidationVolumePane";
import { BigTradesOverlay } from "./components/BigTradesOverlay";
import { FixedRangeVolumeProfile } from "./components/FixedRangeVolumeProfile";
import { TradePositionsOverlay, type PositionSide, type TradePosition } from "./components/TradePositionsOverlay";
import { PaperOrderModal } from "./components/PaperOrderModal";
import { DrawingToolsOverlay } from "./components/DrawingToolsOverlay";
import { ChartToolsPanel } from "./components/ChartToolsPanel";
import { useDrawingsHistory } from "./drawingsHistory";
import { BotsView, type ChartEvidence } from "./components/BotsView";
import {
  fetchCandles,
  fetchHeatmap,
  fetchIntervals,
  fetchLiveKlines,
  fetchSymbols,
  type Candle,
  type PaperTrade,
  type HeatmapResponse,
  type IndicatorKind,
} from "./api";
import {
  DEFAULT_DRAW_COLOR,
  DEFAULT_DRAW_THICKNESS,
  DEFAULT_FILL_COLOR,
  DEFAULT_FILL_OPACITY,
  loadCandleColors,
  loadDrawings,
  saveCandleColors,
  saveDrawings,
  type Drawing,
  type DrawingKind,
  type LineStyle,
  type RectStyle,
} from "./drawings";
import { niceBinSizeUsd } from "./binSize";
import "./App.css";

// Etiquetas de los toggles del toolbar -- los colores de cada linea viven
// en IndicatorPanes.tsx (junto al resto de su logica de renderizado).
const INDICATOR_LABELS: Record<IndicatorKind, string> = {
  oi: "Open Interest",
  cvd: "CVD",
  funding: "Funding",
};

const LIVE_POLL_MS = 5000;
// Mas espaciado que el precio: el heatmap pide precio Y open interest en
// vivo a Binance (dos llamadas), y el modelo no cambia lo bastante rapido
// como para justificar refrescarlo cada 5s.
const HEATMAP_POLL_MS = 20000;

// Cuanta historia de velas pedir por defecto -- sin esto, /api/candles
// devuelve TODO lo archivado (6 años de 5m = ~622,000 filas, 61 MB, ~6s
// de descarga, medido de verdad el 2026-09-01) en cada carga. 90 dias,
// igual que la profundidad del heatmap (ver HISTORICAL_LOOKBACK_DAYS en
// api/routers/heatmap.py) -- coherente con lo que de verdad se puede
// modelar, y de sobra para el uso normal (zoom mas alla de esto queda
// pendiente de un fetch bajo demanda, no implementado todavia).
const CANDLES_LOOKBACK_DAYS = 90;

// "Liquidity Threshold" tipo CoinGlass: oculta pools por debajo de este
// notional. Rango pedido por el usuario, $5M-$1000M en pasos de $5M --
// lineal y en millones (no dolares crudos) para que el slider Y el campo de
// edicion numerica trabajen con el mismo numero chico que ve el usuario.
//
// Piso bajado de 5M a 0.5M (2026-09-09, bug real reportado por el usuario:
// "llevamos dias sin pools"). Diagnosticado con datos reales (no adivinado):
// las celdas de la CUADRICULA (heatmap_model.compute_liquidity_intervals)
// se recalculan por CADA trade individual dentro de las ultimas 24h
// (api/routers/heatmap.py, AGGREGATE_HISTORICAL_TRADES_OLDER_THAN_HOURS) --
// su notional individual es de miles a un puñado de millones, ORDENES DE
// MAGNITUD menor que las celdas viejas (>24h), que SI se agregan por hora
// en SQL y facilmente superan $30-100M. Con el piso viejo de 5M (y el
// default de 30M), NINGUNA celda de las ultimas 24h podia superar el
// umbral -- confirmado contra /api/heatmap real: los clusters mas cercanos
// al precio actual que el propio panel MERCADO ya mostraba eran de
// $983K y $3.1M, ambos por DEBAJO del piso del slider, asi que ni bajando
// el threshold al minimo el usuario podia verlos. No es que el heatmap no
// se este pintando -- es que el rango completo del slider quedaba por
// encima de la liquidez real reciente.
const THRESHOLD_MIN_MILLIONS = 0.5;
const THRESHOLD_MAX_MILLIONS = 1000;
const THRESHOLD_STEP_MILLIONS = 0.5;

function clampThresholdMillions(value: number): number {
  if (Number.isNaN(value)) return THRESHOLD_MIN_MILLIONS;
  return Math.min(THRESHOLD_MAX_MILLIONS, Math.max(THRESHOLD_MIN_MILLIONS, value));
}

// Burbujas de "Big trades" (BigTradesOverlay.tsx) -- configurable a pedido
// del usuario (2026-09-15). El piso REAL, 0.1M ($100k), es el mismo umbral
// fijo del backend (api/bigtrades.BIG_TRADE_THRESHOLD_USD): un valor menor
// no traeria mas datos, el backend ya los descarta antes de que lleguen
// aca -- se mantiene como piso del campo numerico (monto especifico) pero
// ya NO es el piso del SLIDER (ver `BIG_TRADES_MIN_SLIDER_FLOOR_MILLIONS`).
//
// Ajuste 2026-09-18, pedido del usuario: (1) arrancar siempre en 2M, no en
// el piso; (2) el slider se mueve de 1M en 1M, no de 0.1M en 0.1M -- ese
// paso fino queda reservado al campo numerico para un monto especifico.
const BIG_TRADES_MIN_FLOOR_MILLIONS = 0.1;
const BIG_TRADES_MIN_CEIL_MILLIONS = 5;
const BIG_TRADES_MIN_STEP_MILLIONS = 0.1;
const BIG_TRADES_MIN_DEFAULT_MILLIONS = 2;
const BIG_TRADES_MIN_SLIDER_FLOOR_MILLIONS = 1;
const BIG_TRADES_MIN_SLIDER_STEP_MILLIONS = 1;

// Escala del radio maximo de burbuja -- 1 = BASE_MAX_RADIUS_PX de
// BigTradesOverlay.tsx tal cual.
const BUBBLE_SCALE_MIN = 0.5;
const BUBBLE_SCALE_MAX = 3;
const BUBBLE_SCALE_STEP = 0.1;

// Slider de dias hacia atras para Big Trades (nuevo, 2026-09-18, pedido
// del usuario: "un slide mas que sea de dias, de 1 a 7 ... mostrar
// tooooodos los bigtrades de lo que se seleccione"). 1D por defecto al
// cargar. Reemplaza el LOOKBACK_HOURS fijo (48h) que tenia
// BigTradesOverlay.tsx -- ahora configurable desde el toolbar, mismo
// patron que el resto de los controles de Big Trades.
const BIG_TRADES_DAYS_MIN = 1;
const BIG_TRADES_DAYS_MAX = 7;
const BIG_TRADES_DAYS_DEFAULT = 1;

const PAPER_LAUNCHED_KEY = "liq-intel:paper-launched";

// El ancho de bin del heatmap se calcula en vivo a partir del precio actual
// -- ver binSize.ts (bug real 2026-09-29: $100 fijo se veia bien en BTCUSDT
// pero dejaba los pools de ETHUSDT como bloques solidos, 30x mas grueso en
// terminos relativos).

function clampBigTradesMinMillions(value: number): number {
  if (Number.isNaN(value)) return BIG_TRADES_MIN_FLOOR_MILLIONS;
  return Math.min(BIG_TRADES_MIN_CEIL_MILLIONS, Math.max(BIG_TRADES_MIN_FLOOR_MILLIONS, value));
}

function mergeCandles(historical: Candle[], live: Candle[]): Candle[] {
  const byTime = new Map<number, Candle>();
  for (const c of historical) byTime.set(c.time, c);
  for (const c of live) byTime.set(c.time, c); // en vivo pisa lo archivado si coinciden
  return Array.from(byTime.values()).sort((a, b) => a.time - b.time);
}

export default function App() {
  const [symbols, setSymbols] = useState<string[]>([]);
  const [symbol, setSymbol] = useState("BTCUSDT");
  // Default "5m", no "1h" -- a peticion del usuario (2026-09-01): es la
  // temporalidad en la que vive la mayor parte del tiempo.
  const [intervals, setIntervals] = useState<string[]>(["5m"]);
  const [interval, setInterval_] = useState("5m");
  const [candles, setCandles] = useState<Candle[]>([]);
  const [heatmap, setHeatmap] = useState<HeatmapResponse | null>(null);
  const [chart, setChart] = useState<IChartApi | null>(null);
  const [series, setSeries] = useState<ISeriesApi<"Candlestick"> | null>(null);
  const [showHeatmap, setShowHeatmap] = useState(true);
  const [showVolumeProfile, setShowVolumeProfile] = useState(true);
  const [visibleLeverages, setVisibleLeverages] = useState<Set<number>>(
    new Set([10, 15, 25, 50, 100]),
  );
  // En millones, no dolares crudos -- ver constantes THRESHOLD_* arriba.
  // Vuelto a 30M a peticion explicita del usuario (2026-09-10) -- bajado
  // brevemente a 1M el 2026-09-09 porque con 30M las celdas sin agregar
  // de las ultimas 24h quedaban ocultas, pero el usuario prefiere
  // arrancar en 30M y ajustar el slider el mismo si
  // hace falta ver detalle mas fino. El piso del slider se queda en 0.5M
  // (bajado el mismo dia) para que esa opcion siga disponible.
  const [thresholdMillions, setThresholdMillions] = useState(30);
  // Escalado por el MISMO factor que el bin_size (ver binSize.ts): un bin
  // de $2 en ETHUSDT reparte el mismo notional total entre ~50x mas celdas
  // que un bin de $100 en BTCUSDT, asi que cada celda individual tiene
  // ~50x menos notional "propio" aunque el pool de liquidez de fondo sea
  // igual de real -- sin este ajuste, el umbral de 30M (pensado para
  // celdas de $100) esconde TODO en cualquier simbolo con bin mas angosto
  // (bug real encontrado 2026-10-01 al verificar el fix del bin_size: el
  // heatmap de ETHUSDT quedo completamente vacio, no solo mas fino).
  const minNotionalUsd = thresholdMillions * 1_000_000 * ((heatmap?.bin_size_usd ?? 100) / 100);
  const [visibleIndicators, setVisibleIndicators] = useState<Set<IndicatorKind>>(new Set());
  const [showMarketPanel, setShowMarketPanel] = useState(false);
  const [showLiquidationAlerts, setShowLiquidationAlerts] = useState(true);
  const [showBigTrades, setShowBigTrades] = useState(true);
  const [bigTradesMinMillions, setBigTradesMinMillions] = useState(BIG_TRADES_MIN_DEFAULT_MILLIONS);
  // Campo numerico de "Big trades min" cerrado por defecto (solo el
  // slider, con el valor actual como etiqueta) -- pedido del usuario
  // (2026-09-18): "solo si el usuario escribe una cantidad especifica
  // debe quedar el campo abierto". Se abre al hacer click en la etiqueta
  // (intencion de escribir un monto propio) y se queda abierto desde ahi.
  const [bigTradesMinFieldOpen, setBigTradesMinFieldOpen] = useState(false);
  const [bigTradesBubbleScale, setBigTradesBubbleScale] = useState(1);
  const [bigTradesDays, setBigTradesDays] = useState(BIG_TRADES_DAYS_DEFAULT);
  const [view, setView] = useState<"chart" | "bots">("chart");
  const [error, setError] = useState<string | null>(null);
  // Modo "colocar posicion" -- no null mientras se espera el proximo
  // click sobre el pane de precio (ver TradePositionsOverlay.tsx). Un
  // solo estado para los dos lados: activar Long desarma Short y
  // viceversa, nunca los dos modos armados a la vez.
  const [placingSide, setPlacingSide] = useState<PositionSide | null>(null);
  // Modo "colocar rango FRVP" -- mutuamente excluyente con placingSide
  // (armar uno desarma el otro), ver FixedRangeVolumeProfile.tsx.
  const [placingFrvp, setPlacingFrvp] = useState(false);
  // La vela mas reciente de verdad -- separado de `candles` (que solo se
  // reemplaza al cambiar de simbolo/temporalidad, ver el tick de abajo).
  // El heatmap usa esto para su borde derecho: anclarlo a
  // `candles[candles.length-1].time` significaba que, mientras el precio
  // seguia avanzando en pantalla via `series.update()`, el heatmap se
  // quedaba pegado en el momento en que se cargo la pagina -- un hueco que
  // crecia vela a vela hasta refrescar. Bug real, reportado por el usuario
  // con capturas del hueco creciendo.
  const [latestCandleTime, setLatestCandleTime] = useState<number | undefined>(undefined);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  // Espejo de `candles` para los efectos de heatmap mas abajo, que NO
  // pueden depender de `candles` en su lista de deps (correrian de nuevo en
  // cada tick en vivo, resuscribiendose sin necesidad).
  const candlesRef = useRef<Candle[]>([]);
  useEffect(() => {
    candlesRef.current = candles;
  }, [candles]);
  // "Evidencia" de un trade real (vista Bots, 2026-09-19): al entrar,
  // acota el fetch de velas a la ventana exacta del/de los trade(s) en vez
  // de los ultimos CANDLES_LOOKBACK_DAYS, y dibuja su(s) posicion(es) de
  // solo lectura con la misma herramienta long/short ya existente (ver
  // TradePositionsOverlay.tsx). `null` = vista normal. `showHeatmapBeforeEvidence`
  // recuerda la preferencia del usuario para restaurarla al salir -- entrar
  // en modo evidencia apaga el heatmap (pedido explicito: "debe cargar sin
  // pools", los pools modelados no aportan nada para leer un trade ya
  // ocurrido y tapan las lineas de entrada/SL/TP).
  const [evidence, setEvidence] = useState<ChartEvidence | null>(null);
  const showHeatmapBeforeEvidence = useRef(true);
  // Navegacion DENTRO de Bots (que corrida/validacion esta abierta) y el
  // set de trades ya vistos en evidencia -- viven aca, no como useState
  // local de BotsView, a proposito: entrar/salir de evidencia cambia
  // `view` entre "chart"/"bots", y ESO desmonta BotsView por completo (ver
  // el boton de toolbar abajo) -- cualquier estado que necesite sobrevivir
  // a "cerrar evidencia y volver al listado" (pedido del usuario,
  // 2026-09-20: "no me regresa al listado anterior, me deja el grafico")
  // tiene que vivir un nivel arriba, fuera de lo que se desmonta.
  const [botsSelectedRun, setBotsSelectedRun] = useState<string | null>(null);
  const [botsSelectedValidation, setBotsSelectedValidation] = useState<string | null>(null);
  // Claves compuestas "<runId|validationId>:<trade_seq>" -- NO solo
  // trade_seq, que se reinicia en cada corrida/validacion y sombrearia
  // filas de otra corrida que por casualidad comparten el mismo numero.
  const [botsViewedTrades, setBotsViewedTrades] = useState<Set<string>>(new Set());

  // Herramientas de dibujo (2026-09-20, pedido del usuario: lineas
  // horizontal/vertical/diagonal + rectangulo, en un panel flotante).
  // `drawings` es por simbolo (igual que las posiciones/FRVP), cargado/
  // guardado aca -- no dentro de DrawingToolsOverlay.tsx -- porque
  // ChartToolsPanel.tsx tambien necesita leer/editar el dibujo
  // seleccionado (mismo motivo por el que `botsViewedTrades` etc. viven
  // aca y no local a BotsView).
  const [drawings, setDrawings] = useState<Drawing[]>(() => loadDrawings(symbol));
  const [selectedDrawingId, setSelectedDrawingId] = useState<string | null>(null);
  const [placingDrawing, setPlacingDrawingRaw] = useState<DrawingKind | null>(null);
  const [showToolsPanel, setShowToolsPanel] = useState(false);
  // "Pluma" actual -- estilo que se aplica al PROXIMO dibujo nuevo (o al
  // seleccionado, ver `activeLineStyle`/`activeRectStyle` mas abajo).
  const [lineStyle, setLineStyle] = useState<LineStyle>({ color: DEFAULT_DRAW_COLOR, thickness: DEFAULT_DRAW_THICKNESS });
  const [rectStyle, setRectStyle] = useState<RectStyle>({
    color: DEFAULT_DRAW_COLOR, thickness: DEFAULT_DRAW_THICKNESS, fillColor: DEFAULT_FILL_COLOR, fillOpacity: DEFAULT_FILL_OPACITY,
  });
  const [candleColors, setCandleColors] = useState(loadCandleColors);
  // "Ocultar dibujos" (2026-10-20): solo oculta el canvas de DrawingToolsOverlay,
  // los dibujos siguen en `drawings` y en localStorage. No se persiste a
  // proposito: reabrir la app con dibujos invisibles confunde mas de lo que ayuda.
  const [hideDrawings, setHideDrawings] = useState(false);
  // Paper trading (2026-10-20): `paperDraft` = posicion Long/Short para la que
  // se esta abriendo el formulario; `paperLaunched` recuerda que posiciones
  // ya se lanzaron (id de posicion -> id de trade) para no duplicarlas, y
  // sobrevive al reload igual que las posiciones mismas (localStorage).
  const [botsSection, setBotsSection] = useState<"estrategias" | "daytrading">("estrategias");
  const [paperDraft, setPaperDraft] = useState<TradePosition | null>(null);
  const [paperToast, setPaperToast] = useState<string | null>(null);
  const [paperLaunched, setPaperLaunched] = useState<Record<string, number>>(() => {
    try {
      const raw = window.localStorage.getItem(PAPER_LAUNCHED_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch {
      return {};
    }
  });
  useEffect(() => {
    try {
      window.localStorage.setItem(PAPER_LAUNCHED_KEY, JSON.stringify(paperLaunched));
    } catch {
      // Solo memoria esta sesion: el boton PAPER reaparece tras recargar, y el
      // trade ya creado sigue en la bitacora.
    }
  }, [paperLaunched]);
  useEffect(() => {
    if (!paperToast) return;
    const id = window.setTimeout(() => setPaperToast(null), 8000);
    return () => window.clearTimeout(id);
  }, [paperToast]);
  const {
    snapshot: snapshotDrawings, undo: undoDrawings, redo: redoDrawings, canUndo, canRedo,
  } = useDrawingsHistory(drawings, setDrawings, symbol);

  useEffect(() => {
    setDrawings(loadDrawings(symbol));
    setSelectedDrawingId(null);
  }, [symbol]);

  useEffect(() => {
    saveDrawings(symbol, drawings);
  }, [symbol, drawings]);

  useEffect(() => {
    saveCandleColors(candleColors);
  }, [candleColors]);

  // Armar una herramienta de dibujo desarma Long/Short/FRVP y viceversa --
  // mismo invariante "nunca dos modos de colocar armados a la vez" que ya
  // documentaban `placingSide`/`placingFrvp` entre si.
  const setPlacingDrawing = useCallback((kind: DrawingKind | null) => {
    setPlacingSide(null);
    setPlacingFrvp(false);
    // Armar una herramienta con los dibujos ocultos los muestra: dibujar a
    // ciegas no tiene sentido.
    if (kind) setHideDrawings(false);
    setPlacingDrawingRaw(kind);
  }, []);

  const selectedDrawing = drawings.find((d) => d.id === selectedDrawingId) ?? null;
  const activeLineStyle: LineStyle = selectedDrawing ? selectedDrawing.style : lineStyle;
  const activeRectStyle: RectStyle = selectedDrawing?.kind === "rect" ? selectedDrawing.style : rectStyle;

  const handleLineStyleChange = useCallback(
    (style: LineStyle) => {
      if (selectedDrawing) {
        snapshotDrawings(`style:${selectedDrawing.id}`);
        setDrawings((prev) =>
          prev.map((d) => {
            if (d.id !== selectedDrawing.id) return d;
            // TS no reduce bien `{...d, style: {...d.style, ...style}}` a
            // traves de la union `Drawing` (no sabe que el `style` de
            // salida sigue perteneciendo al MISMO miembro `d.kind`) --
            // bifurcar por rect/no-rect en vez de un solo spread generico.
            if (d.kind === "rect") return { ...d, style: { ...d.style, ...style } };
            return { ...d, style };
          }),
        );
      } else {
        setLineStyle(style);
      }
    },
    [selectedDrawing, snapshotDrawings],
  );

  const handleRectStyleChange = useCallback(
    (style: RectStyle) => {
      if (selectedDrawing?.kind === "rect") {
        snapshotDrawings(`style:${selectedDrawing.id}`);
        setDrawings((prev) => prev.map((d) => (d.id === selectedDrawing.id ? { ...d, style } : d)));
      } else {
        setRectStyle(style);
      }
    },
    [selectedDrawing, snapshotDrawings],
  );

  const handleDeleteSelectedDrawing = useCallback(() => {
    if (!selectedDrawingId) return;
    snapshotDrawings();
    setDrawings((prev) => prev.filter((d) => d.id !== selectedDrawingId));
    setSelectedDrawingId(null);
  }, [selectedDrawingId, snapshotDrawings]);

  const handleUndoDrawings = useCallback(() => {
    if (undoDrawings()) setSelectedDrawingId(null);
  }, [undoDrawings]);
  const handleRedoDrawings = useCallback(() => {
    if (redoDrawings()) setSelectedDrawingId(null);
  }, [redoDrawings]);

  // Ctrl+Z / Ctrl+Y (o Ctrl+Shift+Z) sobre los dibujos -- solo en la vista del
  // chart y nunca mientras se escribe en un campo de texto/numero (ahi Ctrl+Z
  // es del propio input).
  useEffect(() => {
    if (view !== "chart") return;
    const onKeyDown = (evt: KeyboardEvent) => {
      if (!(evt.ctrlKey || evt.metaKey)) return;
      const target = evt.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target?.isContentEditable) {
        // Los input type=color/range/checkbox no tienen texto que deshacer.
        const type = (target as HTMLInputElement).type;
        if (type !== "color" && type !== "range" && type !== "checkbox") return;
      }
      const key = evt.key.toLowerCase();
      if (key === "z" && !evt.shiftKey) {
        evt.preventDefault();
        handleUndoDrawings();
      } else if (key === "y" || (key === "z" && evt.shiftKey)) {
        evt.preventDefault();
        handleRedoDrawings();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [view, handleUndoDrawings, handleRedoDrawings]);

  useEffect(() => {
    fetchSymbols()
      .then(setSymbols)
      .catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    fetchIntervals(symbol)
      .then((list) => {
        const available = list.length ? list : ["1h"];
        setIntervals(available);
        setInterval_((current) => (available.includes(current) ? current : available[0]));
      })
      .catch((e) => setError(String(e)));
  }, [symbol]);

  // Historico (TimescaleDB) primero, y el tramo en vivo (directo a Binance)
  // pedido DESDE donde termina lo archivado -- no por un numero fijo de
  // velas, que para temporalidades finas (5m) se queda corto y deja un
  // hueco real de semanas en medio del grafico.
  useEffect(() => {
    setError(null);
    let cancelled = false;
    // Modo evidencia (ver `evidence` arriba): ventana EXACTA del/de los
    // trade(s), no los ultimos CANDLES_LOOKBACK_DAYS -- los walk-forward
    // cubren 2024-2026 completo, muy por fuera de la ventana normal. Sin
    // cola en vivo (`fetchLiveKlines`) para trades historicos (el archivo ya
    // cubre su ventana); solo se pide si la ventana pasa del fin del archivo
    // (ver `load` mas abajo -- trades de paper trading).
    const startIso = evidence
      ? new Date(evidence.startMs).toISOString()
      : new Date(Date.now() - CANDLES_LOOKBACK_DAYS * 86_400_000).toISOString();
    const endIso = evidence ? new Date(evidence.endMs).toISOString() : undefined;

    const load = (): Promise<void> =>
      fetchCandles(symbol, interval, startIso, endIso).then((hist) => {
        if (evidence) {
          const lastHist = hist.length ? hist[hist.length - 1].time : undefined;
          const endSec = Math.floor(evidence.endMs / 1000);
          // Paper trading (2026-10-20): una operacion de hoy cae DESPUES de
          // donde termina el archivo (`candles` no pasa del mes cerrado), asi
          // que sin la cola en vivo el chart quedaba casi vacio. Los trades
          // de backtest (historicos) siguen sin pedirla: el archivo ya cubre
          // su ventana completa, lastHist ~ endSec.
          if (endSec > (lastHist ?? 0) + 86_400) {
            const sinceSec = Math.max(lastHist ?? 0, Math.floor(evidence.startMs / 1000));
            return fetchLiveKlines(symbol, interval, { sinceUnixSeconds: sinceSec }).then((live) => {
              if (cancelled) return;
              const merged = mergeCandles(hist, live).filter((c) => c.time <= endSec);
              setCandles(merged);
              setLatestCandleTime(merged.length ? merged[merged.length - 1].time : undefined);
            });
          }
          if (cancelled) return;
          setCandles(hist);
          setLatestCandleTime(lastHist);
          return;
        }
        const since = hist.length ? hist[hist.length - 1].time : undefined;
        return fetchLiveKlines(symbol, interval, { sinceUnixSeconds: since }).then((live) => {
          if (cancelled) return;
          const merged = mergeCandles(hist, live);
          setCandles(merged);
          setLatestCandleTime(merged.length ? merged[merged.length - 1].time : undefined);
        });
      });

    // Un reintento automatico, no ninguno -- reportado por el usuario
    // 2026-09-14 con captura: un HTTP 502 aislado (Binance con un blip de
    // red) dejaba el grafico roto para siempre, sin mas recurso que
    // cambiar de simbolo/temporalidad a mano para que este efecto
    // volviera a correr. El backend ya reintenta 3 veces internamente
    // (ver api/routers/live.py) -- esto es una segunda red de seguridad
    // para lo que se le escape a esa, no el mecanismo principal. Un solo
    // reintento (no un bucle infinito): si tras 4s TODAVIA falla, es mas
    // probable que sea un problema real (simbolo invalido, backend caido)
    // que quedarse reintentando en silencio sin que el usuario se entere.
    load().catch(() => {
      if (cancelled) return;
      window.setTimeout(() => {
        if (cancelled) return;
        load().catch((e) => {
          if (!cancelled) setError(String(e));
        });
      }, 4000);
    });

    return () => {
      cancelled = true;
    };
  }, [symbol, interval, evidence]);

  // Reafirma el rango visible del modo evidencia DESPUES de que las demas
  // series (liquidaciones reales, big trades, indicadores) hagan su
  // propio fetch async y llamen a su propio setData -- Lightweight Charts
  // comparte UN solo eje de tiempo entre todas las series/panes del
  // chart, asi que una serie con puntos recientes (liquidaciones reales
  // "en vivo" no tienen por que respetar la ventana angosta de un trade
  // viejo) puede estirar el rango visible mucho mas alla del `fitContent()`
  // que ya hizo PriceChart al cargar las velas acotadas, dejando el
  // trade real apretado en una esquina. Un solo `setVisibleRange` con un
  // pequeño retraso (no un watchdog continuo -- eso le pelearia al
  // usuario si intenta hacer scroll/zoom el mismo mientras mira la
  // evidencia) alcanza para el caso comun: las demas series resuelven su
  // fetch en milisegundos, bastante antes de los 600ms de margen.
  useEffect(() => {
    if (!evidence || !chart) return;
    const id = window.setTimeout(() => {
      chart.timeScale().setVisibleRange({
        from: Math.floor(evidence.startMs / 1000) as UTCTimestamp,
        to: Math.floor(evidence.endMs / 1000) as UTCTimestamp,
      });
    }, 600);
    return () => window.clearTimeout(id);
  }, [evidence, chart, candles]);

  // El heatmap NO depende de `interval` (es precio x tiempo en bruto, ver
  // api/routers/heatmap.py) -- efecto SEPARADO del de arriba a proposito.
  // Antes compartian un solo `useEffect([symbol, interval])`: cambiar de
  // temporalidad disparaba un fetch (con cache de 15s en el backend, pero
  // igual una respuesta NUEVA en referencia) que forzaba a HeatmapOverlay a
  // re-ejecutar TODO su efecto principal (rehacer el merge de ~130,000
  // celdas y repintar) sin que el dato hubiera cambiado -- el "delay" al
  // cambiar de temporalidad que reporto el usuario. Separado, cambiar de
  // temporalidad ya no toca el heatmap en absoluto.
  useEffect(() => {
    const lastClose = candlesRef.current[candlesRef.current.length - 1]?.close;
    fetchHeatmap(symbol, niceBinSizeUsd(lastClose)).then(setHeatmap).catch((e) => setError(String(e)));
  }, [symbol]);

  // El heatmap usa precio y OI EN VIVO (ver api/routers/heatmap.py); sin
  // refrescarlo, se queda calculado contra el precio del momento en que se
  // cargo la pagina y se desalinea del precio que sigue moviendose en el
  // chart.
  useEffect(() => {
    const id = window.setInterval(() => {
      const lastClose = candlesRef.current[candlesRef.current.length - 1]?.close;
      fetchHeatmap(symbol, niceBinSizeUsd(lastClose)).then(setHeatmap).catch(() => {});
    }, HEATMAP_POLL_MS);
    return () => window.clearInterval(id);
  }, [symbol]);

  // La vela en curso se actualiza sola via series.update() en vez de
  // reemplazar `candles` entero -- reemplazarlo dispararia otro setData()
  // en PriceChart y con el, otro fitContent() que le resetea el zoom al
  // usuario cada vez que hace tick el poll.
  //
  // Solo se actualiza la ULTIMA vela devuelta, nunca las anteriores:
  // Lightweight Charts no permite tocar una vela que ya dejo de ser la mas
  // reciente ("Cannot update oldest data"). Pedir 2 y solo usar la ultima
  // es a proposito -- la penultima sirve de contexto en el log si algo
  // falla, pero intentar "actualizarla" tambien solo generaba errores sin
  // ningun efecto util.
  useEffect(() => {
    const tick = () => {
      fetchLiveKlines(symbol, interval, { limit: 2 })
        .then((live) => {
          const latest = live[live.length - 1];
          if (!latest) return;
          seriesRef.current?.update({
            time: latest.time as UTCTimestamp,
            open: latest.open,
            high: latest.high,
            low: latest.low,
            close: latest.close,
          });
          setLatestCandleTime(latest.time);
        })
        .catch(() => {}); // un tick fallido no debe interrumpir el poll
    };
    const id = window.setInterval(tick, LIVE_POLL_MS);
    return () => window.clearInterval(id);
  }, [symbol, interval]);

  const onChartReady = useCallback((c: IChartApi, s: ISeriesApi<"Candlestick">) => {
    setChart(c);
    setSeries(s);
    seriesRef.current = s;
  }, []);

  const toggleLeverage = useCallback((lev: number) => {
    setVisibleLeverages((prev) => {
      const next = new Set(prev);
      if (next.has(lev)) next.delete(lev);
      else next.add(lev);
      return next;
    });
  }, []);

  const toggleIndicator = useCallback((kind: IndicatorKind) => {
    setVisibleIndicators((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  }, []);

  // Orden fijo (no el de insercion en el Set) para que el pane de cada
  // indicador no salte de lugar al prender/apagar otro -- paneIndex 0 es
  // siempre el chart de precio, 1..N los indicadores activos en este orden.
  const enabledIndicators = (["oi", "cvd", "funding"] as IndicatorKind[]).filter((k) =>
    visibleIndicators.has(k),
  );

  return (
    <div className="app">
      <header className="toolbar">
        <h1>Liquidation Intelligence</h1>
        <select
          value={symbol}
          onChange={(e) => {
            // Cambiar de simbolo a mano invalida cualquier evidencia
            // activa (posiciones de OTRO simbolo no tienen sentido aca) --
            // vuelve a la ventana normal de velas.
            setEvidence(null);
            setSymbol(e.target.value);
          }}
        >
          {symbols.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select value={interval} onChange={(e) => setInterval_(e.target.value)}>
          {intervals.map((i) => (
            <option key={i} value={i}>
              {i}
            </option>
          ))}
        </select>
        {/* Controles exclusivos del chart -- pedido del usuario (2026-09-20,
            con captura marcando en verde este bloque completo): "si estoy
            viendo el apartado de bots, hay que ocultar los demas botones ya
            que son exclusivos del grafico". Todo desde Heatmap hasta "Panel
            de mercado" solo tiene sentido con PriceChart montado (view ===
            "chart") -- en Bots, chart/series ya estan en null (ver el boton
            de toggle mas abajo), asi que estos controles no hacian nada
            util ahi, solo ruido visual. */}
        {view === "chart" && (
          <>
        <label className="toggle">
          <input
            type="checkbox"
            checked={showHeatmap}
            onChange={(e) => setShowHeatmap(e.target.checked)}
          />
          Heatmap
        </label>
        <label className="toggle">
          <input
            type="checkbox"
            checked={showVolumeProfile}
            onChange={(e) => setShowVolumeProfile(e.target.checked)}
          />
          Perfil de volumen
        </label>
        {showHeatmap && heatmap && (
          <HeatmapLegend tiers={heatmap.tiers} visible={visibleLeverages} onToggle={toggleLeverage} />
        )}
        {showHeatmap && (
          <label
            className="toggle threshold-control"
            title="Oculta pools de liquidez modelados por debajo de este notional -- 'Liquidity Threshold' en CoinGlass."
          >
            Liquidity Threshold:
            <input
              type="range"
              min={THRESHOLD_MIN_MILLIONS}
              max={THRESHOLD_MAX_MILLIONS}
              step={THRESHOLD_STEP_MILLIONS}
              value={thresholdMillions}
              onChange={(e) => setThresholdMillions(Number(e.target.value))}
            />
            <input
              className="threshold-value"
              type="number"
              min={THRESHOLD_MIN_MILLIONS}
              max={THRESHOLD_MAX_MILLIONS}
              step={THRESHOLD_STEP_MILLIONS}
              value={thresholdMillions}
              onChange={(e) => setThresholdMillions(Number(e.target.value))}
              onBlur={(e) => setThresholdMillions(clampThresholdMillions(Number(e.target.value)))}
            />
            M
          </label>
        )}
        {heatmap && (
          <span
            className="badge"
            title={
              `Cesta de apalancamientos: ${heatmap.basket.name} v${heatmap.basket.version} (supuesto, no medido).\n` +
              (heatmap.source === "big_trades"
                ? `Fuente: ${heatmap.big_trades_count} trades >= $${heatmap.big_trade_threshold_usd.toLocaleString()} en las ultimas horas.`
                : `Fuente: open interest agregado (sin trades grandes recientes en la ventana).`)
            }
          >
            {heatmap.label}
          </span>
        )}
        <span className="toolbar-spacer" />
        {(["oi", "cvd", "funding"] as IndicatorKind[]).map((kind) => (
          <label className="toggle" key={kind}>
            <input
              type="checkbox"
              checked={visibleIndicators.has(kind)}
              onChange={() => toggleIndicator(kind)}
            />
            {INDICATOR_LABELS[kind]}
          </label>
        ))}
        <label className="toggle">
          <input
            type="checkbox"
            checked={showLiquidationAlerts}
            onChange={(e) => setShowLiquidationAlerts(e.target.checked)}
          />
          Liquidaciones <span className="badge badge-real badge-inline">REAL</span>
        </label>
        <label className="toggle">
          <input
            type="checkbox"
            checked={showBigTrades}
            onChange={(e) => setShowBigTrades(e.target.checked)}
          />
          Big trades
        </label>
        {showBigTrades && (
          <>
            <label
              className="toggle threshold-control"
              title="Oculta burbujas de trades grandes por debajo de este notional. Arrastra el slider de a 1M, o hace click en el numero para escribir un monto especifico."
            >
              Big trades min:
              <input
                type="range"
                min={BIG_TRADES_MIN_SLIDER_FLOOR_MILLIONS}
                max={BIG_TRADES_MIN_CEIL_MILLIONS}
                step={BIG_TRADES_MIN_SLIDER_STEP_MILLIONS}
                value={bigTradesMinMillions}
                onChange={(e) => setBigTradesMinMillions(Number(e.target.value))}
              />
              {bigTradesMinFieldOpen ? (
                <input
                  className="threshold-value"
                  type="number"
                  autoFocus
                  min={BIG_TRADES_MIN_FLOOR_MILLIONS}
                  max={BIG_TRADES_MIN_CEIL_MILLIONS}
                  step={BIG_TRADES_MIN_STEP_MILLIONS}
                  value={bigTradesMinMillions}
                  onChange={(e) => setBigTradesMinMillions(Number(e.target.value))}
                  onBlur={(e) => setBigTradesMinMillions(clampBigTradesMinMillions(Number(e.target.value)))}
                />
              ) : (
                <button
                  type="button"
                  className="threshold-value threshold-value-label"
                  title="Escribir un monto especifico"
                  onClick={() => setBigTradesMinFieldOpen(true)}
                >
                  {bigTradesMinMillions}
                </button>
              )}
              M
            </label>
            <label
              className="toggle threshold-control"
              title="Escala del tamaño de las burbujas -- entre mas grande la orden, mas grande la burbuja; este slider ajusta que tan grandes se pintan todas."
            >
              Tamaño:
              <input
                type="range"
                min={BUBBLE_SCALE_MIN}
                max={BUBBLE_SCALE_MAX}
                step={BUBBLE_SCALE_STEP}
                value={bigTradesBubbleScale}
                onChange={(e) => setBigTradesBubbleScale(Number(e.target.value))}
              />
            </label>
            <label
              className="toggle threshold-control"
              title="Cuantos dias hacia atras de big trades traer y dibujar en el grafico."
            >
              Dias:
              <input
                type="range"
                min={BIG_TRADES_DAYS_MIN}
                max={BIG_TRADES_DAYS_MAX}
                step={1}
                value={bigTradesDays}
                onChange={(e) => setBigTradesDays(Number(e.target.value))}
              />
              {bigTradesDays}D
            </label>
          </>
        )}
        <button
          type="button"
          className={`toolbar-button${placingSide === "long" ? " on" : ""}`}
          title="Clic aqui y despues clic en el grafico para colocar una posicion Long -- arrastra las lineas TP/SL despues para ajustarlas, o la manija de arriba para moverla en el tiempo."
          onClick={() => {
            setPlacingFrvp(false);
            setPlacingDrawingRaw(null);
            setPlacingSide((v) => (v === "long" ? null : "long"));
          }}
        >
          {placingSide === "long" ? "Clic en el grafico..." : "+ Long"}
        </button>
        <button
          type="button"
          className={`toolbar-button${placingSide === "short" ? " on" : ""}`}
          title="Clic aqui y despues clic en el grafico para colocar una posicion Short -- arrastra las lineas TP/SL despues para ajustarlas, o la manija de arriba para moverla en el tiempo."
          onClick={() => {
            setPlacingFrvp(false);
            setPlacingDrawingRaw(null);
            setPlacingSide((v) => (v === "short" ? null : "short"));
          }}
        >
          {placingSide === "short" ? "Clic en el grafico..." : "+ Short"}
        </button>
        <button
          type="button"
          className={`toolbar-button${placingFrvp ? " on" : ""}`}
          title="Clic para marcar el inicio del rango y otro clic para el final -- perfil de volumen por precio dentro de ese rango, con el nivel de mayor volumen (POC) resaltado."
          onClick={() => {
            setPlacingSide(null);
            setPlacingDrawingRaw(null);
            setPlacingFrvp((v) => !v);
          }}
        >
          {placingFrvp ? "Clic en el grafico..." : "+ FRVP"}
        </button>
        <button
          type="button"
          className={`toolbar-button${showMarketPanel ? " on" : ""}`}
          onClick={() => setShowMarketPanel((v) => !v)}
        >
          {showMarketPanel ? "Ocultar panel" : "Panel de mercado"}
        </button>
        {/* Unico boton nuevo que se agrega al header (pedido del usuario,
            2026-09-20: "evitar seguir incluyendo cosas al header") -- el
            resto de las herramientas de dibujo/apariencia viven DENTRO del
            panel flotante que este boton abre/cierra. */}
        <button
          type="button"
          className={`toolbar-button${showToolsPanel ? " on" : ""}`}
          title="Herramientas de dibujo (lineas, rectangulo, colores)"
          onClick={() => setShowToolsPanel((v) => !v)}
        >
          {showToolsPanel ? "Ocultar herramientas" : "Herramientas"}
          {hideDrawings ? " (dibujos ocultos)" : ""}
        </button>
          </>
        )}
        <button
          type="button"
          className={`toolbar-button${view === "bots" ? " on" : ""}`}
          onClick={() => {
            // Entrar/salir de Bots a mano (NO cerrando una evidencia, ver el
            // boton de abajo) resetea el sombreado de "trades vistos" --
            // pedido explicito del usuario (2026-09-20): el sombreado solo
            // debe sobrevivir a cerrar evidencia, no a salirse de Bots del
            // todo. Tambien se descarta cualquier evidencia activa (si el
            // usuario sale con el boton "Bots" en vez de "cerrar", no debe
            // quedar una evidencia vieja escondida para la proxima vez que
            // entre a "chart").
            setBotsViewedTrades(new Set());
            if (evidence) {
              setEvidence(null);
              setShowHeatmap(showHeatmapBeforeEvidence.current);
            }
            // Entrar a "Bots" desmonta PriceChart (y con el, HeatmapOverlay/
            // VolumeProfile/LiquidationVolumePane/IndicatorPanes) al swapear
            // .chart-container -- `chart`/`series` en React state NO se
            // limpian solos al desmontar el componente que los creo, asi
            // que sin este reset se quedan apuntando a una instancia YA
            // destruida (`chart.remove()` corrio en el cleanup de
            // PriceChart). Al volver a "chart", esos hijos remontan y
            // reciben esa referencia muerta en su primer render -- ANTES
            // de que el nuevo onChartReady la reemplace -- y revientan
            // (`_internal_state` undefined). Limpiarlos aqui hace que ese
            // primer render pase `null`, que todos ya manejan sin problema.
            setView((v) => {
              const next = v === "bots" ? "chart" : "bots";
              if (next === "bots") {
                setChart(null);
                setSeries(null);
              }
              return next;
            });
          }}
        >
          {view === "bots" ? "Ver chart" : "Bots"}
        </button>
        {evidence && (
          <button
            type="button"
            className="toolbar-button on"
            title="Volviendo al listado de trades: rango de velas de siempre y el heatmap como estaba antes de ver esta evidencia."
            onClick={() => {
              // Bug real (2026-09-20, reportado por el usuario): esto solo
              // apagaba `evidence`, pero `view` se habia quedado en "chart"
              // desde que se entro a la evidencia (ver onViewInChart mas
              // abajo) -- nunca regresaba al listado. `botsSelectedRun`/
              // `botsSelectedValidation`/`botsViewedTrades` viven arriba de
              // este componente a proposito (ver su declaracion) para que
              // sobrevivan el remount de BotsView y el listado reaparezca
              // exactamente donde se dejo, con el sombreado intacto.
              setEvidence(null);
              setShowHeatmap(showHeatmapBeforeEvidence.current);
              setChart(null);
              setSeries(null);
              setView("bots");
            }}
          >
            Evidencia activa &mdash; cerrar &times;
          </button>
        )}
        {error && <span className="error">{error}</span>}
      </header>
      {view === "bots" ? (
        <div className="chart-container">
          <BotsView
            selectedRun={botsSelectedRun}
            onSelectRun={setBotsSelectedRun}
            selectedValidation={botsSelectedValidation}
            onSelectValidation={setBotsSelectedValidation}
            section={botsSection}
            onSectionChange={setBotsSection}
            viewedTrades={botsViewedTrades}
            onMarkViewed={(ids) =>
              setBotsViewedTrades((prev) => {
                const next = new Set(prev);
                for (const id of ids) next.add(id);
                return next;
              })
            }
            onViewInChart={(sym, tradeInterval, chartEvidence) => {
              // chart/series ya quedaron en null al ENTRAR a "Bots" (ver el
              // boton de arriba) -- no hace falta repetir esa limpieza aqui.
              // "sin pools" (pedido explicito del usuario): apaga el heatmap
              // pero recuerda como estaba para restaurarlo al cerrar la
              // evidencia (boton de arriba), en vez de forzar el estado del
              // usuario para siempre. SOLO se captura si TODAVIA no estabamos
              // en modo evidencia -- entrar a una SEGUNDA evidencia sin
              // cerrar la primera (ej. "Ver en el chart" de otro trade, o
              // "Ver todos" despues de ver uno solo) no debe pisar el valor
              // ya guardado con el `false` que dejo la primera entrada
              // (bug real, encontrado probando esto mismo en el navegador).
              if (!evidence) showHeatmapBeforeEvidence.current = showHeatmap;
              setShowHeatmap(false);
              setSymbol(sym);
              setInterval_(tradeInterval);
              setEvidence(chartEvidence);
              setView("chart");
            }}
          />
        </div>
      ) : (
        <div className="chart-container">
          <div className="chart-main">
            <PriceChart candles={candles} onChartReady={onChartReady} candleColors={candleColors} />
            {showHeatmap && heatmap && (
              <HeatmapOverlay
                chart={chart}
                series={series}
                tiers={heatmap.tiers}
                visibleLeverages={visibleLeverages}
                gridBucketMs={heatmap.grid_bucket_ms}
                binSizeUsd={heatmap.bin_size_usd}
                minNotionalUsd={minNotionalUsd}
                interval={interval}
                lastCandleTime={latestCandleTime}
                asOfMs={Date.parse(heatmap.as_of)}
              />
            )}
            {showVolumeProfile && heatmap && (
              <VolumeProfile
                chart={chart}
                series={series}
                tiers={heatmap.tiers}
                visibleLeverages={visibleLeverages}
              />
            )}
            {showLiquidationAlerts && (
              <LiquidationVolumePane chart={chart} series={series} symbol={symbol} interval={interval} />
            )}
            {showBigTrades && (
              <BigTradesOverlay
                chart={chart}
                series={series}
                symbol={symbol}
                interval={interval}
                minNotionalUsd={bigTradesMinMillions * 1_000_000}
                bubbleScale={bigTradesBubbleScale}
                lookbackDays={bigTradesDays}
              />
            )}
            <IndicatorPanes chart={chart} kinds={enabledIndicators} symbol={symbol} interval={interval} />
            <FixedRangeVolumeProfile
              chart={chart}
              series={series}
              symbol={symbol}
              candles={candles}
              placing={placingFrvp}
              onPlaced={() => setPlacingFrvp(false)}
            />
            <TradePositionsOverlay
              chart={chart}
              series={series}
              symbol={symbol}
              placingSide={placingSide}
              onPlaced={() => setPlacingSide(null)}
              readOnlyPositions={evidence?.positions}
              onLaunchPaper={setPaperDraft}
              paperLaunched={paperLaunched}
            />
            <DrawingToolsOverlay
              chart={chart}
              series={series}
              drawings={drawings}
              onDrawingsChange={setDrawings}
              selectedId={selectedDrawingId}
              onSelectedIdChange={setSelectedDrawingId}
              placing={placingDrawing}
              onPlaced={() => setPlacingDrawingRaw(null)}
              lineStyle={activeLineStyle}
              rectStyle={activeRectStyle}
              hidden={hideDrawings}
              onBeginChange={snapshotDrawings}
            />
            {paperDraft && (
              <PaperOrderModal
                position={paperDraft}
                symbol={symbol}
                interval={interval}
                onClose={() => setPaperDraft(null)}
                onLaunched={(trade: PaperTrade) => {
                  setPaperLaunched((prev) => ({ ...prev, [paperDraft.id]: trade.trade_id }));
                  setPaperDraft(null);
                  setPaperToast(
                    trade.status === "open"
                      ? `Paper #${trade.trade_id} abierta a mercado en ${trade.entry_price.toLocaleString()}.`
                      : `Paper #${trade.trade_id} creada: pendiente hasta que el precio toque ${trade.entry_price.toLocaleString()}.`,
                  );
                }}
              />
            )}
            {paperToast && (
              <div className="paper-toast">
                <span>{paperToast}</span>
                <button
                  type="button"
                  className="bots-view-in-chart"
                  onClick={() => {
                    setPaperToast(null);
                    setBotsViewedTrades(new Set());
                    setBotsSelectedRun(null);
                    setBotsSelectedValidation(null);
                    setBotsSection("daytrading");
                    setChart(null);
                    setSeries(null);
                    setView("bots");
                  }}
                >
                  Ver en DAY TRADING &rarr;
                </button>
              </div>
            )}
            {showToolsPanel && (
              <ChartToolsPanel
                placing={placingDrawing}
                onSetPlacing={setPlacingDrawing}
                lineStyle={activeLineStyle}
                onLineStyleChange={handleLineStyleChange}
                rectStyle={activeRectStyle}
                onRectStyleChange={handleRectStyleChange}
                hasSelection={selectedDrawing !== null}
                selectedIsRect={selectedDrawing?.kind === "rect"}
                onDeleteSelected={handleDeleteSelectedDrawing}
                hideDrawings={hideDrawings}
                onToggleHideDrawings={() => setHideDrawings((v) => !v)}
                canUndo={canUndo}
                canRedo={canRedo}
                onUndo={handleUndoDrawings}
                onRedo={handleRedoDrawings}
                candleColors={candleColors}
                onCandleColorsChange={setCandleColors}
                onClose={() => setShowToolsPanel(false)}
              />
            )}
          </div>
          {showMarketPanel && (
            <MarketSnapshotPanel symbol={symbol} marketSnapshot={heatmap?.market_snapshot ?? null} />
          )}
        </div>
      )}
    </div>
  );
}
