import { useEffect, useRef, useState } from "react";
import type { IChartApi, ISeriesApi, Logical, MouseEventParams, Time, UTCTimestamp } from "lightweight-charts";
import type { HeatmapTier } from "../api";
import { heatColor } from "../heatColor";
import { formatNotional } from "../format";
import { mergeCells } from "../mergeTiers";
import { CHART_PANES_CHANGED_EVENT } from "../chartLayoutEvent";
import { plotWidth } from "../chartGeometry";

interface Props {
  chart: IChartApi | null;
  series: ISeriesApi<"Candlestick"> | null;
  tiers: HeatmapTier[];
  visibleLeverages: Set<number>;
  gridBucketMs: number;
  binSizeUsd: number;
  /** Oculta pools con notional por debajo de este umbral -- "Liquidity
   * Threshold" en CoinGlass. Sin esto, el heatmap pinta hasta el pool mas
   * chico modelado y satura de ruido visual (ver capturas del usuario). */
  minNotionalUsd: number;
  /** "1h", "5m", etc -- hace falta para saber cuantos segundos representa
   * UNA barra y poder interpolar tiempo->pixel entre barras reales (ver
   * `secondsPerBar`). */
  interval: string;
  /** time (unix seconds) de la ultima vela real -- el heatmap se pinta
   * SOLO hasta ahi (el historico), nunca en el hueco vacio de ahi en
   * adelante (ver referencia bitcoincounterflow.com/es/heatmap-de-liquidaciones). */
  lastCandleTime?: number;
  /** `heatmap.as_of` (ms epoch) -- referencia de "ahora" para el
   * decaimiento por antiguedad de `mergeCells` (ver heatmapDecay.ts). El
   * `as_of` que ya trae la respuesta, no `Date.now()`: es el mismo
   * instante contra el que el backend ya calculo consumo/OI/precio, asi
   * que la edad de cada celda queda consistente con esos otros numeros en
   * vez de depender del reloj del navegador. */
  asOfMs: number;
}

interface Tooltip {
  x: number;
  y: number;
  timeMs: number;
  price: number;
  notional: number;
}

/** "5m" -> 300, "1h" -> 3600, "1d" -> 86400. Los intervalos que sirve la API
 * son siempre "<numero><unidad>" (ver app/api/routers/candles.py) -- sin
 * tabla fija para no tener que ampliarla si se agrega una temporalidad. */
function secondsPerBar(interval: string): number {
  const m = /^(\d+)([mhdw])$/.exec(interval);
  if (!m) return 3600;
  const unitSeconds = { m: 60, h: 3600, d: 86400, w: 604800 }[m[2]] ?? 3600;
  return Number(m[1]) * unitSeconds;
}

/**
 * Capa canvas propia sobre el chart, sincronizada por escala de precios
 * (Lightweight Charts no permite superponer datos propios dentro de un
 * iframe de TradingView -- por eso precio + canvas propio).
 *
 * Dibuja una CUADRICULA precio x tiempo (una celda por INTERVALO exacto,
 * ancho variable por celda -- ver `bucketMs` de cada una), no bandas de
 * ancho completo -- cada celda vale lo que
 * `heatmap_model.compute_liquidity_intervals` calculo para ese tramo, igual
 * que un heatmap publicado (CoinGlass, CoinAnk): cuando el precio "come"
 * una zona, las celdas de ahi en adelante simplemente dejan de existir, en
 * vez de que la banda entera se apague o se quede pintada de ancho
 * completo.
 *
 * No se suscribe a un evento "cambio de escala de precio" porque la
 * libreria no expone uno directo; time-range + resize cubre en la practica
 * los casos que mueven la escala de precios (zoom, scroll, arrastrar el
 * eje). El redibujado NO esta atado a crosshair-move -- el contenido del
 * heatmap no depende de donde este el cursor (reportado por el usuario:
 * "se volvio inoperable").
 */
export function HeatmapOverlay({
  chart,
  series,
  tiers,
  visibleLeverages,
  gridBucketMs,
  binSizeUsd,
  minNotionalUsd,
  interval,
  lastCandleTime,
  asOfMs,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  // Buffer fuera de pantalla: las celdas se dibujan aqui SIN difuminar (un
  // fillRect solido es barato) y el difuminado se aplica una sola vez al
  // volcar el buffer completo sobre el canvas visible -- ver comentario en
  // `draw()`. Nunca se agrega al DOM.
  const bufferRef = useRef<HTMLCanvasElement | null>(null);
  // Capa separada solo para el contorno de la celda bajo el cursor -- se
  // redibuja en cada crosshair-move (barato: un clearRect + un strokeRect,
  // nada que ver con volver a pintar miles de celdas), sin tocar el canvas
  // principal ni su pipeline de difuminado.
  const highlightCanvasRef = useRef<HTMLCanvasElement>(null);
  const [tooltip, setTooltip] = useState<Tooltip | null>(null);
  // Cache del ultimo merge + lista de buckets ordenada, para que el
  // handler de crosshair no tenga que recalcular el mapa en cada
  // movimiento del mouse -- se recalcula solo cuando cambian los datos.
  const mergedRef = useRef<{
    cells: Map<string, { timeMs: number; price: number; notional: number; bucketMs: number }>;
    bucketTimes: number[];
  }>({ cells: new Map(), bucketTimes: [] });

  // Recalcular el merge de celdas (~7,000-8,000 entradas con los 5 tramos
  // visibles) es caro (arma un Map nuevo y ordena los tiempos de bucket) --
  // tiene que pasar SOLO cuando cambian los datos o los tramos activados,
  // nunca en el propio movimiento del mouse. Iba dentro de `draw()` en un
  // primer intento y el heatmap se volvio inoperable: `draw()` se llamaba en
  // cada crosshair-move (cada pixel de movimiento del mouse), asi que se
  // reconstruia y reordenaba el Map completo en cada uno.
  useEffect(() => {
    const merged = mergeCells(tiers, visibleLeverages, asOfMs);
    mergedRef.current = {
      cells: merged,
      bucketTimes: Array.from(new Set(Array.from(merged.values()).map((c) => c.timeMs))).sort(
        (a, b) => a - b,
      ),
    };
  }, [tiers, visibleLeverages, asOfMs]);

  useEffect(() => {
    if (!chart || !series || !canvasRef.current || lastCandleTime === undefined) return;
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // Interpolacion lineal tiempo->pixel, NO `timeToCoordinate`/`timeToIndex`
    // por celda. Los limites de nuestros buckets de 5 min casi nunca
    // coinciden con una vela real (1h o 5m), y `timeToIndex(t, true)` --
    // que redondea "al mas cercano" -- colapsaba la mayoria de los buckets
    // de 5 min al MISMO indice de vela cuando el chart esta en 1h (12
    // buckets por vela): ancho cero, celda descartada. Con 1h de por medio
    // solo ~730 de 8,471 celdas sobrevivian, dispersas -- de ahi los
    // "pools" pequeños y huecos que reporto el usuario. Lightweight Charts
    // espacia las barras UNIFORMEMENTE por indice logico, asi que con dos
    // puntos de referencia reales (la ultima vela, que siempre existe, y el
    // espaciado entre dos indices logicos cualquiera) se puede interpolar
    // el pixel de CUALQUIER instante con aritmetica pura -- sin volver a
    // llamar a la API del chart por cada celda. Se recalcula en cada
    // draw() Y en cada hover (`updateTooltip`) porque el mapeo cambia con
    // el zoom/pan; el costo es de un puñado de llamadas al chart, no un
    // recorrido de celdas.
    const computeMapping = (): { anchorCoord: number; paintLimit: number; pxPerMs: number; anchorMs: number } | null => {
      const timeScale = chart.timeScale();
      const anchorCoord = timeScale.timeToCoordinate(lastCandleTime as UTCTimestamp);
      const coordA = timeScale.logicalToCoordinate(0 as Logical);
      const coordB = timeScale.logicalToCoordinate(500 as Logical);
      if (anchorCoord === null || coordA === null || coordB === null) return null;
      // `paintLimit`, NO `anchorCoord`, es lo que se recorta contra el ancho
      // REAL del area de tiempo/precio -- sin esto, un `lastCandleTime`
      // momentaneamente adelantado a lo que el chart ya registro
      // internamente (posible mientras se sigue el precio en vivo
      // desplazandose, cada tick nuevo via series.update()) puede resolver
      // un anchorCoord mas alla del area de trazado real, y como `cellRect`
      // solo acota el borde derecho contra este limite (nunca contra el
      // ancho del propio chart), las celdas terminan pintando DENTRO del
      // gutter del eje de precio -- reportado por el usuario con captura
      // (2026-09-03): "los pools invaden el eje y... cuando me desplazo a
      // la derecha con el precio". `timeScale.width()` es el ancho del area
      // de trazado SIN el gutter (a diferencia de
      // `chart.priceScale('right').width()`, que en este entorno devolvia
      // 0 de forma poco confiable) -- un limite que no depende de que
      // `anchorCoord` este bien calculado.
      //
      // Bug real encontrado el 2026-09-11 (reproducido y medido, no solo
      // sospechado): ANTES, este tope se aplicaba al propio `anchorCoord`
      // devuelto -- que TAMBIEN es el punto de referencia de
      // `x0 = anchorCoord + (timeMs - anchorMs) * pxPerMs` en `cellRect`.
      // Al desplazar el grafico lejos de "ahora" (`lastCandleTime` queda
      // fuera de pantalla a la derecha, coordenada real de miles de px),
      // el tope lo aplastaba contra `timeScaleWidth` (~1200px) -- un
      // desplazamiento CONSTANTE (medido en consola: 1430px) se colaba en
      // CADA celda por igual, porque `x0`/`x1` usan ese mismo valor
      // corrompido como origen. Efecto visible: el heatmap entero
      // "se recorre a la izquierda" al desplazar a la derecha -- exactamente
      // lo reportado por el usuario, cuatro veces. `paintLimit` separa las
      // dos responsabilidades: `anchorCoord` (sin recortar) sigue siendo el
      // origen real de la interpolacion; `paintLimit` (recortado) solo se
      // usa para el CLIP del borde derecho en `cellRect`, nunca para
      // calcular una posicion.
      const timeScaleWidth = timeScale.width();
      const paintLimit = timeScaleWidth > 0 ? Math.min(anchorCoord, timeScaleWidth) : anchorCoord;
      const pxPerBar = (coordB - coordA) / 500;
      // Duracion real de una barra, medida de las DOS ultimas velas de la
      // serie -- NO `secondsPerBar(interval)` (2026-09-10, bug real
      // confirmado con datos de la consola del usuario en vivo: el heatmap
      // se desincronizaba porque este calculo daba el valor de 1h --
      // 0.0000016667 px/ms, exactamente 1/12 de lo que corresponde a 5m --
      // mientras el chart mostraba velas de 5m. La prop `interval` esta en
      // las dependencias del efecto y en teoria siempre deberia coincidir
      // con lo que se ve, pero claramente en algun momento del ciclo de
      // vida del componente queda desalineada; medir la duracion real
      // entre las dos ultimas barras de `series.data()` es la MISMA fuente
      // de verdad que usa el chart para dibujar las velas, asi que no
      // puede desalinearse de lo que el usuario ve en pantalla, pase lo
      // que pase con la prop.
      const seriesData = series.data();
      const n = seriesData.length;
      const realSecondsPerBar =
        n >= 2 ? Number(seriesData[n - 1].time) - Number(seriesData[n - 2].time) : secondsPerBar(interval);
      const pxPerMs = pxPerBar / ((realSecondsPerBar || secondsPerBar(interval)) * 1000);
      return { anchorCoord, paintLimit, pxPerMs, anchorMs: lastCandleTime * 1000 };
    };

    // Rectangulo en pixeles de una celda -- compartido entre `draw()` (para
    // pintarla) y `updateTooltip()` (para el contorno de hover), asi los
    // dos usan EXACTAMENTE la misma geometria. La altura sale de la
    // distancia real en pixeles entre el borde superior e inferior del bin
    // de precio (`priceToCoordinate` en los dos extremos), no de un numero
    // fijo: antes era un `bandHeight = 6` constante sin relacion con el
    // zoom, y a la mayoria de niveles de zoom eso dejaba un hueco visible
    // entre filas (reportado por el usuario) o, muy de cerca, un pisado.
    // El limite derecho es siempre la ultima vela real (`anchorCoord`): el
    // hueco vacio que deja `rightOffset` en PriceChart, a la derecha de esa
    // vela, no tiene trades detras que lo justifiquen.
    const cellRect = (
      timeMs: number,
      price: number,
      bucketMs: number,
      mapping: { anchorCoord: number; paintLimit: number; pxPerMs: number; anchorMs: number },
    ): { left: number; top: number; width: number; height: number } | null => {
      // `x0`/`x1` SIEMPRE contra `anchorCoord` sin recortar -- es el origen
      // real de la interpolacion. Solo el CLIP final usa `paintLimit` (ver
      // el comentario en `computeMapping`): recortar el origen mismo, en
      // vez del resultado, desplazaba cada celda por igual.
      const x0 = mapping.anchorCoord + (timeMs - mapping.anchorMs) * mapping.pxPerMs;
      const x1 = mapping.anchorCoord + (timeMs + bucketMs - mapping.anchorMs) * mapping.pxPerMs;
      const left = Math.max(0, x0);
      const right = Math.min(mapping.paintLimit, x1);
      if (right <= left) return null;
      const yTop = series.priceToCoordinate(price + binSizeUsd / 2);
      const yBottom = series.priceToCoordinate(price - binSizeUsd / 2);
      if (yTop === null || yBottom === null) return null;
      const top = Math.min(yTop, yBottom);
      // +1px de solape: cierra la costura de antialiasing entre celdas
      // contiguas del mismo tramo de precio.
      const height = Math.max(1, Math.abs(yBottom - yTop) + 1);
      return { left, top, width: right - left, height };
    };

    const draw = () => {
      const container = canvas.parentElement;
      if (!container) return;
      // Ancho REAL del area de ploteo, sin la columna de la escala de
      // precio -- ver chartGeometry.ts (bug real reportado por el
      // usuario, 2026-09-21: las celdas invadian los numeros del eje).
      const width = plotWidth(container);
      // Alto del PANE DE PRECIO (indice 0), no del contenedor entero: con
      // panes de indicadores activos (OI/CVD/Funding, ver IndicatorPanes.tsx)
      // `container` mide la suma de TODOS los panes apilados, y un canvas
      // a ese alto completo pinta celdas tambien sobre los panes de abajo
      // -- reportado por el usuario con captura ("los pools cubren la info
      // del indicador"). `chart.panes()[0]` es siempre el de precio (los
      // indicadores se agregan despues, ver IndicatorPanes.tsx).
      const height = chart.panes()[0]?.getHeight() ?? container.clientHeight;
      if (width === 0 || height === 0) return;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = width * dpr;
      canvas.height = height * dpr;
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);

      const highlightCanvas = highlightCanvasRef.current;
      if (highlightCanvas) {
        highlightCanvas.width = width * dpr;
        highlightCanvas.height = height * dpr;
        highlightCanvas.style.width = `${width}px`;
        highlightCanvas.style.height = `${height}px`;
        // El mapeo de pixeles cambio (zoom/pan/resize): un contorno pintado
        // con la geometria anterior quedaria desalineado hasta el proximo
        // movimiento del mouse. Se limpia aqui; `updateTooltip` lo vuelve a
        // pintar en la posicion correcta si el cursor sigue sobre una celda.
        const hctx = highlightCanvas.getContext("2d");
        hctx?.clearRect(0, 0, highlightCanvas.width, highlightCanvas.height);
      }

      const { cells: merged } = mergedRef.current;
      if (merged.size === 0) return;

      if (!bufferRef.current) bufferRef.current = document.createElement("canvas");
      const buffer = bufferRef.current;
      buffer.width = canvas.width;
      buffer.height = canvas.height;
      const bctx = buffer.getContext("2d");
      if (!bctx) return;
      bctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      bctx.clearRect(0, 0, width, height);

      const mapping = computeMapping();
      if (!mapping) return;

      // Un solo recorrido: calcula el rectangulo de cada celda (para saber
      // si de verdad esta en pantalla) y junta las que pasan el umbral de
      // liquidez -- `merged` tiene hasta ~130,000 celdas de 90 dias de
      // historia, pero en cualquier zoom normal solo una fraccion cae
      // dentro del rango de tiempo visible.
      const visible: { rect: { left: number; top: number; width: number; height: number }; notional: number }[] = [];
      for (const { timeMs, price, notional, bucketMs } of merged.values()) {
        if (notional < minNotionalUsd) continue;
        const rect = cellRect(timeMs, price, bucketMs, mapping);
        if (!rect) continue;
        visible.push({ rect, notional });
      }

      // Referencia de color = percentil de lo VISIBLE, no el maximo de los
      // 90 dias completos -- a peticion del usuario (2026-09-01,
      // comparando contra un heatmap de referencia): con el maximo global,
      // un solo pico historico (semanas atras, fuera de pantalla) aplastaba
      // la escala para todo lo demas y el heatmap se veia "todo azul o un
      // poco verde" sin importar cuantos apalancamientos o que Liquidity
      // Threshold se activara -- el rojo intenso de una zona de verdad
      // densa nunca aparecia porque se comparaba contra un pico que ni
      // siquiera estaba en pantalla. Recalcular sobre `visible` en cada
      // draw() hace que el color se reajuste solo al hacer zoom/pan (como
      // el heatmap de referencia) y que activar/desactivar apalancamientos
      // o mover el Liquidity Threshold cambien la escala de color de forma
      // consistente entre si, no solo cuales celdas se ven.
      // Percentil (no el maximo puro): un solo pixel extremo en pantalla no
      // deberia definir el punto de corte del rojo -- el tramo mas denso de
      // verdad (no un unico outlier) es el que llega a rojo pleno, dandole
      // ancho a la banda caliente en vez de un solo pixel, igual que en la
      // referencia. Bajado de 97 a 90 (2026-09-04, reportado por el
      // usuario): a zoom MUY alejado en 1h (~90 dias en pantalla a la vez)
      // el 97 seguia dejando el heatmap "todo azul" con apenas un par de
      // lineas calientes sueltas -- medido contra la respuesta real de
      // /api/heatmap con el threshold del usuario (55M): p97 dejaba 58.7%
      // de las celdas visibles en azul y solo 13.7% en rojo/naranja; p90
      // baja eso a 37.8% azul / 22.3% rojo-naranja, sin aplastar la
      // distincion entre picos (el gamma de abajo ya se encarga de eso).
      let hotReference = 1;
      if (visible.length > 0) {
        const sorted = visible.map((v) => v.notional).sort((a, b) => a - b);
        const idx = Math.min(sorted.length - 1, Math.floor(sorted.length * 0.90));
        hotReference = Math.max(1, sorted[idx]);
      }

      // Cada celda se dibuja SOLIDA (sin `ctx.filter`) en el buffer -- con
      // miles de rectangulos por dibujado, difuminar cada uno por separado
      // es la diferencia entre ~1ms y ~25ms de trabajo por frame (medido).
      // El difuminado se aplica UNA vez, de golpe,
      // al volcar el buffer completo sobre el canvas visible.
      for (const { rect, notional } of visible) {
        // Celdas por encima del percentil de referencia quedan clipeadas a
        // intensidad maxima (rojo pleno), no solo la unica celda mas grande.
        const intensity = Math.min(1, notional / hotReference);
        // Curva gamma (exponente < 1): empuja los pools MEDIOS hacia el
        // extremo caliente del gradiente (amarillo/rojo) en vez de
        // quedarse azules -- a peticion del usuario (2026-08-31), "gama de
        // colores mas intensa". Sin esto, con notional distribuido de forma
        // dispareja (pocos pools gigantes, muchos chicos) la mayoria caia
        // por debajo de intensity=0.3 y el heatmap se veia casi todo azul.
        const boosted = Math.pow(intensity, 0.55);
        // Alpha subido de [0.55, 1.0] a [0.65, 1.0] (2026-08-30 -> 2026-08-31,
        // mismo motivo): con la cuadricula densa, la transparencia baja
        // dejaba el heatmap muy lavado -- CoinGlass/CoinAnk se ven solidos
        // incluso en las zonas debiles.
        bctx.fillStyle = heatColor(boosted, 0.65 + boosted * 0.35);
        bctx.fillRect(rect.left, rect.top, rect.width, rect.height);
      }

      // El volcado va con la transformacion identidad (coordenadas de pixel
      // de dispositivo 1:1) porque el buffer YA tiene el DPR horneado en su
      // contenido -- por eso el radio del filtro se escala por `dpr` aqui,
      // si no el difuminado saldria mas fino de lo debido en pantallas de
      // alta densidad. Radio bajado de 7 a 3: a peticion del usuario, mas
      // nitido -- 7 disolvia demasiado el borde de cada celda de la
      // cuadricula, que ahora es el propio dato (antes eran ~100 bandas
      // anchas y hacia falta mas difuminado para que no se vieran a rayas).
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.filter = `blur(${3 * dpr}px)`;
      ctx.drawImage(buffer, 0, 0);
      ctx.filter = "none";
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    // Contorno blanco de la celda bajo el cursor, a peticion del usuario --
    // dibujado en su propia capa (`highlightCanvasRef`) para no repetir el
    // costo de repintar+difuminar todo el heatmap en cada movimiento del
    // mouse (la misma razon por la que el tooltip ya no dependia de
    // `draw()`, ver mas abajo).
    const drawHighlight = (rect: { left: number; top: number; width: number; height: number } | null) => {
      const highlightCanvas = highlightCanvasRef.current;
      if (!highlightCanvas) return;
      const hctx = highlightCanvas.getContext("2d");
      if (!hctx) return;
      hctx.setTransform(1, 0, 0, 1, 0, 0);
      hctx.clearRect(0, 0, highlightCanvas.width, highlightCanvas.height);
      if (!rect) return;
      const dpr = window.devicePixelRatio || 1;
      hctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      hctx.strokeStyle = "#ffffff";
      hctx.lineWidth = 1.5;
      // Con el chart alejado (semanas/meses en pantalla) un bucket de 5 min
      // mide una fraccion de pixel de ancho -- el contorno seria invisible
      // pese a que la celda (y el tooltip) sean reales. Se fuerza un minimo
      // visible, centrado sobre el mismo punto que el rect real.
      const w = Math.max(rect.width, 4);
      const h = Math.max(rect.height, 4);
      const left = rect.left + rect.width / 2 - w / 2;
      const top = rect.top + rect.height / 2 - h / 2;
      // 0.75px hacia adentro en cada lado: centra el trazo de 1.5px sobre
      // el borde real de la celda en vez de que la mitad caiga fuera.
      hctx.strokeRect(left + 0.75, top + 0.75, Math.max(0, w - 1.5), Math.max(0, h - 1.5));
    };

    // Tooltip tipo CoinGlass: precio + hora bajo el cursor y cuanto
    // notional modela la celda ahi -- incluido "0" cuando esta vacia (asi
    // se ve en la referencia: el cruce del precio deja la celda en cero,
    // no oculta el tooltip). Deliberadamente NO redibuja el heatmap -- su
    // contenido no depende de donde este el cursor, asi que repintar miles
    // de celdas en cada movimiento del mouse era trabajo puro desperdiciado
    // (y la causa del "se traba"). El contorno de
    // hover SI se redibuja aqui, pero en su propia capa barata (ver arriba).
    const updateTooltip = (param: MouseEventParams<Time>) => {
      if (!param.point || gridBucketMs <= 0) {
        setTooltip(null);
        drawHighlight(null);
        return;
      }
      const timeScale = chart.timeScale();
      const hoveredTime = timeScale.coordinateToTime(param.point.x);
      const price = series.coordinateToPrice(param.point.y);
      if (hoveredTime === null || price === null) {
        setTooltip(null);
        drawHighlight(null);
        return;
      }
      const hoveredMs = Number(hoveredTime) * 1000;
      const { cells, bucketTimes } = mergedRef.current;
      if (bucketTimes.length === 0 || hoveredMs < bucketTimes[0]) {
        setTooltip(null);
        drawHighlight(null);
        return;
      }
      // Intervalo que contiene hoveredMs: el mayor de bucketTimes <=
      // hoveredMs. Los intervalos vienen del backend (ver
      // heatmap_model.compute_liquidity_intervals) con ancho EXACTO y
      // VARIABLE por celda -- por eso se busca en la lista real en vez
      // de recalcular con un modulo sobre epoch;
      // cada celda encontrada trae su PROPIO ancho (`bucketMs`) para el
      // contorno.
      let bucketStartMs = bucketTimes[0];
      for (const t of bucketTimes) {
        if (t > hoveredMs) break;
        bucketStartMs = t;
      }
      const binnedPrice = Math.round(price / binSizeUsd) * binSizeUsd;
      const cell = cells.get(`${bucketStartMs}:${binnedPrice}`);
      setTooltip({
        x: param.point.x,
        y: param.point.y,
        timeMs: bucketStartMs,
        price: binnedPrice,
        notional: cell?.notional ?? 0,
      });
      const mapping = computeMapping();
      // Celda vacia (notional 0, sin entrada en `cells`): no hay bucketMs
      // propio que usar -- se cae al fino por defecto, la resolucion mas
      // comun de las dos.
      drawHighlight(mapping ? cellRect(bucketStartMs, binnedPrice, cell?.bucketMs ?? gridBucketMs, mapping) : null);
    };

    // Arrastrar (pan) o hacer zoom dispara `subscribeVisibleTimeRangeChange`
    // muchas veces por frame -- sin agrupar, cada uno encolaba un `draw()`
    // sincrono (~20-25ms con miles de celdas difuminadas) y se apilaban uno
    // detras de otro sin dejar respirar al hilo principal. `scheduleDraw`
    // colapsa cualquier cantidad de disparos entre dos frames en un solo
    // `draw()` real, como mucho uno por frame.
    let rafId: number | null = null;
    const scheduleDraw = () => {
      if (rafId !== null) return;
      rafId = requestAnimationFrame(() => {
        rafId = null;
        draw();
      });
    };

    draw();
    const timeScale = chart.timeScale();
    timeScale.subscribeVisibleTimeRangeChange(scheduleDraw);
    chart.subscribeCrosshairMove(updateTooltip);
    window.addEventListener("resize", scheduleDraw);
    // Agregar/quitar un pane de indicador (ver IndicatorPanes.tsx) cambia
    // el alto del pane de precio y el ancho de la escala de precio
    // compartida, pero NO el tamaño de `.chart-main` -- el ResizeObserver
    // de abajo no lo detecta. Ver chartLayoutEvent.ts.
    window.addEventListener(CHART_PANES_CHANGED_EVENT, scheduleDraw);
    // Arrastrar el separador ENTRE panes (redimensionar a mano, nativo de
    // Lightweight Charts) tampoco dispara ninguno de los eventos de arriba
    // -- ocurre por completo dentro de la libreria, sin pasar por
    // IndicatorPanes.tsx ni cambiar el tamaño de `.chart-main`. Reportado
    // por el usuario (2026-09-01): "cargue los indicadores y reajuste su
    // tamaño... el heatmap se sigue desfazando". `timeScale().
    // subscribeSizeChange` es el evento NATIVO de la libreria para esto --
    // cubre tanto el arrastre manual como, de forma redundante pero
    // inofensiva (scheduleDraw ya deduplica via rAF), los mismos casos que
    // CHART_PANES_CHANGED_EVENT.
    const onSizeChange = () => scheduleDraw();
    timeScale.subscribeSizeChange(onSizeChange);

    // El primer draw() de arriba puede caer con el contenedor todavia a 0
    // (mismo problema de timing que PriceChart -- ver su comentario). Un
    // ResizeObserver de respaldo cubre el caso normal, pero
    // ResizeObserver -- igual que requestAnimationFrame -- puede no
    // disparar en absoluto mientras la pestana no este visible/compuesta
    // (confirmado en pruebas). setTimeout no depende de eso, asi que
    // unos reintentos cortos por temporizador son
    // el respaldo que sí funciona aunque la pestana este en ese estado.
    const container = canvas.parentElement;
    const ro = container ? new ResizeObserver(scheduleDraw) : null;
    if (container) ro!.observe(container);
    // Ninguno de los disparadores de arriba resulto suficiente en la
    // practica (reportado por el usuario, 2026-09-01, con capturas: seguia
    // desfazado tras activar Y despues de desactivar indicadores, solo un
    // F5 lo arreglaba) -- la explicacion es que `.chart-main` (lo que
    // vigila `ro` de arriba) NUNCA cambia de tamaño con nada de esto (su
    // alto es fijo por CSS), asi que ese ResizeObserver nunca es el que
    // dispara; dependiamos solo de los eventos de la libreria, y evidentemente
    // no cubren todos los casos reales. Se observa en cambio el elemento
    // DOM REAL del pane de precio (`getHTMLElement()`) -- ese SI cambia de
    // tamaño fisicamente (mas angosto si el gutter de precio crece, mas
    // bajo si se redistribuye el alto entre panes) sea cual sea la causa,
    // asi que un ResizeObserver sobre el es la fuente de verdad, no una
    // lista de eventos que hay que adivinar.
    const panePaneEl = chart.panes()[0]?.getHTMLElement() ?? null;
    const paneRo = panePaneEl ? new ResizeObserver(scheduleDraw) : null;
    if (panePaneEl) paneRo!.observe(panePaneEl);

    // Red de seguridad final -- diagnosticado con acceso real al chart
    // (2026-09-01, `window.__debugChart` temporal): agregar un pane de
    // indicador no solo cambia alto/ancho de pane -- el RANGO DE TIEMPO
    // VISIBLE del chart se corre solo (confirmado en vivo: 65 minutos de
    // corrimiento al activar Open Interest), casi seguro porque
    // `IndicatorPanes.tsx` mete datos con un rango de tiempo propio
    // (`fetchIndicator`, ver ese archivo) en la escala de tiempo
    // COMPARTIDA. Ese corrimiento NO dispara
    // `subscribeVisibleTimeRangeChange` (ese evento parece ser solo para
    // pan/zoom del usuario, no para ajustes internos de la libreria por
    // datos nuevos) -- de ahi que el heatmap SOLO se desalineara cuando
    // habia paneles de indicador de por medio, exactamente lo que
    // reporto el usuario. Los intentos anteriores (ancho de escala de
    // precio, alto de pane) vigilaban los numeros EQUIVOCADOS -- ninguno
    // de los dos cambio en esta prueba, pese a que el heatmap si se
    // desalineaba. Se vigila en cambio el resultado de `computeMapping()`
    // en si -- el mismo calculo exacto que usa `draw()` -- comparado
    // contra el ultimo visto cada 300ms; si cambia por CUALQUIER motivo
    // (uno ya identificado, o alguno futuro que no se conoce todavia), se
    // redibuja. No hay una version mas directa de "vigilar lo que
    // realmente importa" que esta.
    // Intervalo bajado de 300 a 80ms (2026-09-09, reporte del usuario:
    // "los pools se desfasan al desplazar a la derecha en 5m, se
    // recolocan al volver a la izquierda"). No se pudo reproducir el pan
    // en vivo para diagnosticar la causa exacta (ver nota mas abajo) --
    // esto es una mitigacion defensiva, no una correccion confirmada:
    // si el desfase es un frame perdido de `subscribeVisibleTimeRangeChange`
    // (el mismo tipo de evento que ya demostro no cubrir todos los casos,
    // parrafo de arriba), un watchdog 3.75x mas frecuente lo autocorrige
    // en un tiempo mucho menos perceptible sin cambiar la logica.
    // Pendiente: si el usuario confirma que sigue pasando IGUAL (no solo
    // "mas rapido pero visible"), el problema no es de frecuencia de
    // sondeo sino de que `computeMapping()` en si calcula mal para ese
    // pan especifico -- ahi hace falta el dato de "¿se autocorrige solo
    // en 1-2 segundos, o se queda pegado hasta que el usuario vuelve a
    // mover el mouse?" para saber cual de las dos cosas es.
    let lastMapping = computeMapping();
    const geometryWatchdog = window.setInterval(() => {
      const m = computeMapping();
      if (!m || !lastMapping || m.anchorCoord !== lastMapping.anchorCoord || m.pxPerMs !== lastMapping.pxPerMs) {
        // Log permanente (no un hook temporal que hay que pedir cada vez,
        // 2026-09-10): si el desfase vuelve a pasar, F12 -> Console ya
        // tiene el historial completo de cada salto de geometria, sin
        // depender de que alguien capture el instante exacto a mano.
        // eslint-disable-next-line no-console
        console.log("[heatmap] geometria cambio", { antes: lastMapping, ahora: m, hora: new Date().toISOString() });
        lastMapping = m;
        scheduleDraw();
      }
    }, 80);

    const retryTimers = [50, 250, 750, 2000].map((ms) => window.setTimeout(draw, ms));

    return () => {
      timeScale.unsubscribeVisibleTimeRangeChange(scheduleDraw);
      chart.unsubscribeCrosshairMove(updateTooltip);
      window.removeEventListener("resize", scheduleDraw);
      window.removeEventListener(CHART_PANES_CHANGED_EVENT, scheduleDraw);
      timeScale.unsubscribeSizeChange(onSizeChange);
      ro?.disconnect();
      paneRo?.disconnect();
      window.clearInterval(geometryWatchdog);
      retryTimers.forEach((id) => window.clearTimeout(id));
      if (rafId !== null) cancelAnimationFrame(rafId);
    };
  }, [
    chart,
    series,
    tiers,
    visibleLeverages,
    gridBucketMs,
    binSizeUsd,
    minNotionalUsd,
    interval,
    lastCandleTime,
  ]);

  return (
    <>
      <canvas
        ref={canvasRef}
        // zIndex explicito: Lightweight Charts apila sus propios canvases
        // internos con z-index numerico (1 el fondo, 2 la serie). Un
        // z-index:auto (el valor por defecto) se ordena como si fuera 0 --
        // por debajo de esos, aunque el canvas este despues en el DOM. Sin
        // esto el heatmap se dibuja bien (verificable con getImageData) pero
        // queda tapado por la propia vela, invisible en pantalla.
        style={{ position: "absolute", top: 0, left: 0, pointerEvents: "none", zIndex: 3 }}
      />
      <canvas
        ref={highlightCanvasRef}
        style={{ position: "absolute", top: 0, left: 0, pointerEvents: "none", zIndex: 4 }}
      />
      {tooltip && (
        <div
          style={{
            position: "absolute",
            left: tooltip.x + 16,
            top: tooltip.y + 16,
            pointerEvents: "none",
            zIndex: 5,
            background: "rgba(15, 23, 42, 0.92)",
            border: "1px solid rgba(148, 163, 184, 0.35)",
            borderRadius: 6,
            padding: "8px 12px",
            fontSize: 12,
            color: "#e2e8f0",
            whiteSpace: "nowrap",
          }}
        >
          <div style={{ marginBottom: 4, color: "#94a3b8" }}>
            {new Date(tooltip.timeMs).toLocaleString()}
          </div>
          <div>Price: {tooltip.price.toLocaleString()}</div>
          <div>Liquidation Leverage: {formatNotional(tooltip.notional)}</div>
        </div>
      )}
    </>
  );
}
