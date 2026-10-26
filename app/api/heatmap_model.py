"""Modelo de heatmap de liquidaciones -- NO es un dato, es un modelo
("el heatmap es un modelo, no un dato"). Ningún exchange
publica dónde están las liquidaciones pendientes; esto se construye a partir
de:

  1. Precio y open interest OBSERVADOS (reales, de open_interest_metrics).
  2. Tramos de MMR REALES (de margin_brackets, ver ingest/binance_brackets.py).
  3. Una CESTA DE APALANCAMIENTOS -- esto SÍ es un supuesto, no un dato.
     Nadie publica cómo se reparte el open interest entre apalancamientos.
     Por eso la cesta tiene nombre y versión: cualquier cambio queda visible
     en la UI como "modelo vX", nunca como un ajuste silencioso.
  4. Reparto 50/50 long/short dentro de cada apalancamiento -- otro supuesto
     explícito. Sin datos de long/short ratio por apalancamiento no hay forma
     de saber la proporción real.

Fórmula (verificada contra fuentes citadas, no inventada):
    P_liq(long)  = Pe * (1 - 1/L + mmr)
    P_liq(short) = Pe * (1 + 1/L - mmr)

Lo que este módulo NO hace todavía (pendiente de Fase 4, que necesita 3-6
meses del stream completo de Bybit para calibrar):
  - retiro proporcional cuando cae el OI sin toque
  - calibración de la cesta de apalancamientos contra liquidaciones reales

Sí hace un decaimiento heurístico (`filter_consumed_levels`, 2026-08-29): un
nivel generado a partir de un trade real desaparece en cuanto el precio,
DESPUÉS de ese trade, cruza su precio de liquidación -- esa liquidez ya se
"comió". Es heurístico porque no sabemos si esa posición hipotética se
liquidó de verdad (podría haberse cerrado antes); es mejor que no decaer
nunca, que es lo que hacía esta versión hasta ahora y dejaba niveles ya
cruzados por el precio pintados indefinidamente hasta salir de la ventana de
`bigtrades.LOOKBACK_HOURS`.

Cualquier respuesta que use este módulo debe etiquetarse en la UI como
`MODELED · UNCALIBRATED`, con el nombre y la versión de la cesta visibles.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import NamedTuple, Optional


class MmrTier(NamedTuple):
    notional_floor: float
    notional_cap: float
    mmr: float
    max_leverage: int = 125  # por defecto generoso; ver MmrTable.max_leverage


class MmrTable:
    """Tramos de MMR de un símbolo. Busca el tramo que contiene un notional dado."""

    def __init__(self, tiers: list[MmrTier]):
        self.tiers = sorted(tiers, key=lambda t: t.notional_floor)

    @property
    def max_leverage(self) -> int:
        """El apalancamiento maximo real que Binance deja usar en este
        simbolo (tramo mas bajo, el de posiciones tipicas). No todos los
        simbolos llegan a 100x -- TIAUSDT, por ejemplo, tope en 75x. Usar
        una cesta con niveles por encima de esto modela un apalancamiento
        que ningun trader puede tener de verdad en ese simbolo."""
        return self.tiers[0].max_leverage if self.tiers else 125

    def mmr_for_notional(self, notional: float) -> float:
        for t in self.tiers:
            if t.notional_floor <= notional < t.notional_cap:
                return t.mmr
        # Por encima del ultimo tramo publicado (o tabla vacia): se usa el
        # mas alto conocido en vez de fallar -- mejor estimacion disponible,
        # no un dato inventado.
        return self.tiers[-1].mmr if self.tiers else 0.0


@dataclass(frozen=True)
class LeverageBasket:
    """Reparto SUPUESTO del open interest entre apalancamientos típicos.
    Los pesos deben sumar 1.0 -- se valida al construir."""
    name: str
    version: str
    weights: dict[int, float]

    def __post_init__(self):
        total = sum(self.weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"los pesos de {self.name} suman {total}, no 1.0")


# Cesta por defecto: SUPUESTO, no medido, y pesos IGUALES a proposito -- sin
# datos reales de como se reparte el open interest entre apalancamientos, un
# reparto uniforme es la eleccion que menos afirma saber. Los 5 niveles
# (10x/15x/25x/50x/100x) son los que la UI deja activar y desactivar por
# separado, uno a uno. Sustituir por pesos medidos en cuanto los haya
# (Fase 4, calibracion contra Bybit).
DEFAULT_BASKET = LeverageBasket(
    name="default_uniform_v2",
    version="2026-08-28",
    weights={10: 0.2, 15: 0.2, 25: 0.2, 50: 0.2, 100: 0.2},
)


class LiquidationLevel(NamedTuple):
    price: float
    side: str          # "long" o "short": lado de la POSICION que se liquidaria ahi
    leverage: int
    notional_usd: float
    # Momento del trade real que genero este nivel (ms epoch), o None si
    # viene de `compute_levels` (agregado de OI, sin trade real detras). Hace
    # falta para `filter_consumed_levels`: sin saber DESDE CUANDO existe la
    # posicion hipotetica, no se puede saber si el precio ya la "comio"
    # despues de que se abriera, o si ese cruce fue anterior y no cuenta.
    trade_time_ms: Optional[int] = None


def liq_price(entry_price: float, leverage: int, mmr: float, side: str) -> float:
    if side == "long":
        return entry_price * (1 - 1 / leverage + mmr)
    if side == "short":
        return entry_price * (1 + 1 / leverage - mmr)
    raise ValueError(f"side invalido: {side!r} (debe ser 'long' o 'short')")


def compute_levels(price: float, open_interest_notional_usd: float,
                    mmr_table: MmrTable, basket: LeverageBasket = DEFAULT_BASKET) -> list[LiquidationLevel]:
    """El MMR se busca con el notional de UNA posicion tipica (el tramo mas
    bajo de la tabla), no con `bucket_notional` (el agregado de todo el
    tramo de apalancamiento, que puede ser miles de millones de dolares).

    El tramo de MMR de Binance existe para limitar el apalancamiento
    efectivo de una cuenta segun EL TAMANO DE SU POSICION, no del mercado
    entero. `bucket_notional` es la suma de -- se estima -- miles de
    posiciones distintas; buscarle un tramo de MMR como si fuera una sola
    cuenta gigante da un MMR absurdamente alto (en BTCUSDT, sobre los
    $800M ya es 25-50%). Con mmr > 1/leverage la formula se invierte y la
    liquidacion "long" queda POR ENCIMA del precio de entrada, que no
    tiene sentido. El tramo mas bajo (posicion pequena/mediana, el grueso
    del retail) es la aproximacion menos mala sin datos reales del tamano
    de posicion tipico -- otro supuesto explicito, igual que la cesta.

    Los niveles de la cesta por encima de `mmr_table.max_leverage` se
    saltan enteros, no se recortan al maximo permitido: no todos los
    simbolos llegan a 100x (TIAUSDT, por ejemplo, tope en 75x), y modelar
    un apalancamiento que ningun trader puede tener de verdad ahi es peor
    que no modelarlo. El notional de esos niveles no se reparte entre los
    demas -- eso seria inventar otro supuesto encima del que ya hace la
    cesta."""
    if open_interest_notional_usd <= 0 or price <= 0:
        return []
    typical_position_mmr = mmr_table.mmr_for_notional(0)
    return _levels_for_entry(price, open_interest_notional_usd, typical_position_mmr, mmr_table.max_leverage, basket)


def _levels_for_entry(entry_price: float, entry_notional_usd: float, mmr: float,
                       max_leverage: int, basket: LeverageBasket,
                       trade_time_ms: Optional[int] = None) -> list[LiquidationLevel]:
    levels = []
    for leverage, weight in basket.weights.items():
        if leverage > max_leverage:
            continue
        bucket_notional = entry_notional_usd * weight
        for side in ("long", "short"):
            levels.append(LiquidationLevel(
                price=liq_price(entry_price, leverage, mmr, side),
                side=side, leverage=leverage,
                notional_usd=bucket_notional / 2,   # supuesto 50/50, ver docstring de compute_levels
                trade_time_ms=trade_time_ms,
            ))
    return levels


def compute_levels_from_trades(trades: list[tuple[float, float, int]], mmr_table: MmrTable,
                                basket: LeverageBasket = DEFAULT_BASKET) -> list[LiquidationLevel]:
    """Como `compute_levels`, pero con MUCHAS entradas reales (trades
    grandes, ver api/bigtrades.py) en vez de una sola sintetica desde el
    precio actual.

    `compute_levels` reparte TODO el open interest desde el precio de AHORA,
    como si la posicion agregada se hubiera abierto en este instante -- da
    un puñado de lineas limpias alrededor del precio actual. Un heatmap
    publicado (CoinGlass, etc.) se ve denso y con muchos niveles porque el
    open interest real se fue acumulando a lo largo de horas, a precios
    distintos. Tratar cada trade grande como una posible apertura de
    posicion EN SU PROPIO precio reproduce esa textura sin inventar nada
    nuevo: sigue siendo el mismo supuesto de la cesta de apalancamientos y
    el mismo MMR de posicion tipica, solo que aplicados muchas veces, una
    por trade, en vez de una vez sobre un agregado.

    `trades`: lista de (precio, notional_usd, trade_time_ms) -- ver
    `bigtrades.fetch_recent_big_trades`. No se distingue compra de venta
    (Binance sí lo publica en el propio trade, campo `m`), porque seguimos
    sin saber si ESA operacion terminó siendo la apertura de un long o de
    un short: se sigue asumiendo 50/50, igual que en `compute_levels`.

    `trade_time_ms` se guarda en cada nivel generado (ver `LiquidationLevel`)
    para que `filter_consumed_levels` pueda decidir despues si el precio ya
    cruzo ese nivel desde que el trade ocurrio."""
    typical_position_mmr = mmr_table.mmr_for_notional(0)
    levels = []
    for price, notional, trade_time_ms in trades:
        if price <= 0 or notional <= 0:
            continue
        levels.extend(_levels_for_entry(
            price, notional, typical_position_mmr, mmr_table.max_leverage, basket,
            trade_time_ms=trade_time_ms,
        ))
    return levels


def filter_consumed_levels(levels: list[LiquidationLevel],
                            price_history: list[tuple[int, float, float]]) -> list[LiquidationLevel]:
    """Descarta niveles cuyo precio el mercado YA cruzo despues de que el
    trade que los genero ocurriera -- esa liquidez ya se "comio". Sin esto,
    un nivel se queda pintado (y se recalcula igual en cada refresco) hasta
    que el trade que lo origino sale de `bigtrades.LOOKBACK_HOURS`, aunque el
    precio ya haya pasado por ahi hace rato -- justo lo contrario de como se
    ve un heatmap publicado (bitcoincounterflow.com/es/heatmap-de-liquidaciones):
    el precio "se come" el heatmap segun avanza.

    `price_history`: [(close_time_ms, high, low), ...] ordenado por tiempo
    ascendente (ver api/pricehistory.py).

    Un nivel sin `trade_time_ms` (la ruta de open interest agregado,
    `compute_levels`, sin trade real detras) no se puede evaluar -- no hay
    forma de saber desde cuando existe esa posicion hipotetica -- y se deja
    pasar sin filtrar."""
    if not price_history:
        return levels

    times = [t for t, _, _ in price_history]
    n = len(times)
    # Sufijos de maximo/minimo: para cada indice i, el maximo de highs[i:] y
    # el minimo de lows[i:]. Permite responder "el precio, desde tal punto en
    # adelante, ¿llego a tocar tal nivel?" en O(log n) por nivel (busqueda
    # binaria del punto de partida + lookup del sufijo) en vez de escanear
    # todo el historial por cada uno de los miles de niveles.
    suffix_max_high = [float("-inf")] * (n + 1)
    suffix_min_low = [float("inf")] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_max_high[i] = max(price_history[i][1], suffix_max_high[i + 1])
        suffix_min_low[i] = min(price_history[i][2], suffix_min_low[i + 1])

    result = []
    for lvl in levels:
        if lvl.trade_time_ms is None:
            result.append(lvl)
            continue
        i = bisect.bisect_right(times, lvl.trade_time_ms)
        if i >= n:
            result.append(lvl)  # nada ha pasado desde el trade todavia
            continue
        if lvl.side == "long" and suffix_min_low[i] <= lvl.price:
            continue  # el precio ya bajo hasta ahi: liquidez consumida
        if lvl.side == "short" and suffix_max_high[i] >= lvl.price:
            continue
        result.append(lvl)
    return result


DEFAULT_BIN_WIDTH_USD = 100.0


class GridCell(NamedTuple):
    time_ms: int          # inicio EXACTO de este intervalo (ms epoch)
    price: float          # bin de precio, mismo ancho fijo que bin_levels
    leverage: int
    notional_usd: float
    # Duracion EXACTA de este intervalo en ms -- cada celda vale lo que
    # duro ese nivel de notional activo, no un tamaño de bucket fijo (ver
    # compute_liquidity_intervals). El frontend usa esto para saber cuanto
    # pintar cada celda; sin este campo tendria que asumir un ancho fijo
    # compartido, que es exactamente lo que causaba el efecto "en bloques".
    bucket_ms: int = 300_000


class _SparseTable:
    """Estructura RMQ (range min/max query) clasica: O(n log n) para
    construir, O(1) por consulta de rango. Hace falta para que
    `_first_crossing_index` pueda encontrar la barra EXACTA donde un nivel
    se cruzo con busqueda binaria (O(log n)) en vez del escaneo lineal
    O(n) que tenia la version anterior de esta funcion -- ver docstring de
    `compute_liquidity_intervals` para el porque completo."""

    def __init__(self, values: list[float], op):
        n = len(values)
        self._op = op
        self._log = [0] * (n + 1)
        for i in range(2, n + 1):
            self._log[i] = self._log[i // 2] + 1
        table = [list(values)]
        j = 1
        while (1 << j) <= n:
            prev = table[-1]
            half = 1 << (j - 1)
            table.append([op(prev[i], prev[i + half]) for i in range(n - (1 << j) + 1)])
            j += 1
        self._table = table

    def query(self, lo: int, hi: int) -> float:
        """Min/max (segun `op`) de values[lo:hi+1], ambos inclusive."""
        j = self._log[hi - lo + 1]
        return self._op(self._table[j][lo], self._table[j][hi - (1 << j) + 1])


def _first_crossing_index(table: _SparseTable, start_idx: int, n: int, price: float, mode: str) -> int:
    """El menor indice j en [start_idx, n) donde la barra `j` cruza
    `price` -- mode="min" busca low<=price (lado long), mode="max" busca
    high>=price (lado short). Precondicion: SI cruza en algun punto de ese
    rango (verificar con `table.query(start_idx, n-1)` antes de llamar).

    Busqueda binaria sobre el rango, no bisect.bisect_right directo, porque
    el valor a buscar (el minimo/maximo ACUMULADO desde start_idx hasta
    cada punto) es monotono aunque la serie de lows/highs en si no lo sea
    -- exactamente la propiedad que permite bisectar con una RMQ."""
    lo, hi = start_idx, n - 1
    while lo < hi:
        mid = (lo + hi) // 2
        v = table.query(start_idx, mid)
        hit = v <= price if mode == "min" else v >= price
        if hit:
            hi = mid
        else:
            lo = mid + 1
    return lo


def compute_liquidity_intervals(
    levels: list[LiquidationLevel],
    price_history: list[tuple[int, float, float]],
    bin_size: float = DEFAULT_BIN_WIDTH_USD,
) -> list[GridCell]:
    """Reconstruye, con precision EXACTA (no discretizada en buckets),
    cuanto notional modelado seguia activo en cada bin de precio a lo
    largo del tiempo -- una version de `bin_levels_by_leverage` que varia
    en el tiempo, para que el heatmap se pueda dibujar como una CUADRICULA
    precio x tiempo en vez de una franja plana.

    Version anterior (compute_liquidity_grid, hasta 2026-09-01): discretizaba
    el tiempo en buckets de ancho fijo y escaneaba, para cada nivel que SI
    se cruzaba, barra por barra hasta encontrar la exacta -- O(niveles x
    barras) en el peor caso. Eso obligo, en dos intentos sucesivos, a
    resamplear `price_history` a resoluciones cada vez mas gruesas (diaria,
    luego un tramo intermedio horario) solo para que el calculo terminara
    en un tiempo razonable --
    y el usuario seguia viendo el heatmap "en bloques" porque el ANCHO DEL
    BUCKET, no el costo de computo, era lo que limitaba que tan pegado al
    precio quedaba el corte de decaimiento.

    Esta version separa las dos cosas. Encontrar la barra EXACTA del cruce
    es O(log barras) por nivel, no O(barras) -- una tabla RMQ (`_SparseTable`,
    O(n log n) a construir UNA vez) mas busqueda binaria (`_first_crossing_index`)
    en vez del escaneo lineal. Con eso, no hace falta resamplear
    `price_history` NUNCA: se puede usar la resolucion mas fina disponible
    (5 min, hasta 90 dias) para TODO el historico sin que el costo se
    dispare -- medido de verdad (2026-09-01, no una suposicion): 132,020
    niveles contra 26,499 barras de 5 min, 0.65s, 129,974 celdas (antes,
    con el tercer intento de buckets por tramos, 143,762 celdas en tiempo
    similar, pero con el escalon de decaimiento limitado a pasos de 4h/1
    dia en vez de la barra exacta).

    Ademas, en vez de materializar una celda POR BUCKET mientras el nivel
    sigue activo (lo que hacia crecer el conteo de celdas con la duracion
    de vida de cada nivel, el origen de los 17M celdas / 1.27GB de §7.12),
    esta version emite eventos de inicio/fin por nivel y arma, POR GRUPO
    (mismo bin de precio + apalancamiento + lado), la funcion escalonada
    real de la suma activa -- una celda por CADA CAMBIO DE VALOR, no una
    por cada bucket de tiempo que transcurre. El numero de celdas queda
    acotado por ~2x el numero de niveles (los puntos donde algo realmente
    cambia), no por cuantos buckets de tiempo caben en la ventana --
    mismo orden de magnitud que antes, pero con precision temporal exacta
    en vez de acotada por un ancho de bucket arbitrario.

    El inicio de cada intervalo usa `trade_time_ms` TAL CUAL (no redondeado
    a la barra siguiente): el nivel modela una posicion hipotetica abierta
    en ese instante exacto, y no hay motivo para perder esa precision que
    ya se tiene gratis. El FIN usa el tiempo de la barra exacta del cruce
    (`price_history` no tiene resolucion mas fina que eso).

    `price_history`: [(close_time_ms, high, low), ...] ascendente, igual
    que `filter_consumed_levels`. Devuelve solo celdas con notional != 0
    (dispersa)."""
    if not levels or not price_history or bin_size <= 0:
        return []

    times = [t for t, _, _ in price_history]
    highs = [h for _, h, _ in price_history]
    lows = [lo for _, _, lo in price_history]
    n = len(times)
    window_end_ms = times[-1]

    min_table = _SparseTable(lows, min)
    max_table = _SparseTable(highs, max)

    # events[(price_bin, leverage, side)] = [(tiempo_ms, delta_notional), ...]
    # -- un +notional al empezar el nivel, un -notional exacto donde se
    # consume (o al final de la ventana si nunca se consume).
    events: dict[tuple[float, int, str], list[tuple[int, float]]] = {}

    for lvl in levels:
        if lvl.price <= 0:
            continue
        start_idx = 0
        if lvl.trade_time_ms is not None:
            start_idx = bisect.bisect_right(times, lvl.trade_time_ms)
            if start_idx >= n:
                continue  # el trade es mas nuevo que toda la price_history disponible
            start_ms = max(lvl.trade_time_ms, times[0])
        else:
            start_ms = times[0]  # ruta de OI agregado, sin trade real detras

        if lvl.side == "long":
            crosses = min_table.query(start_idx, n - 1) <= lvl.price
        else:
            crosses = max_table.query(start_idx, n - 1) >= lvl.price

        if crosses:
            if lvl.side == "long":
                end_idx = _first_crossing_index(min_table, start_idx, n, lvl.price, "min")
            else:
                end_idx = _first_crossing_index(max_table, start_idx, n, lvl.price, "max")
            end_ms = times[end_idx]
        else:
            end_ms = window_end_ms  # sigue activo hasta el final de la ventana ("ahora")

        if end_ms <= start_ms:
            continue  # se abrio y se consumio en el mismo instante: sin textura que mostrar

        price_bin = round(lvl.price / bin_size) * bin_size
        key = (price_bin, lvl.leverage, lvl.side)
        group = events.setdefault(key, [])
        group.append((start_ms, lvl.notional_usd))
        group.append((end_ms, -lvl.notional_usd))

    # Umbral para "esta celda tiene notional de verdad", no `!= 0` exacto:
    # `running` es una suma/resta acumulada de CIENTOS de eventos por
    # grupo, cada uno del orden de millones a miles de millones de
    # dolares -- cuando un nivel se abre y se cierra (running vuelve a
    # donde estaba), el residuo de redondeo de punto flotante no es
    # exactamente 0.0, es del orden de 1e-8 a 1e-6 para sumas en ese
    # rango (float64 tiene ~15-17 digitos significativos). El umbral
    # anterior (1e-9) era mas chico que ese residuo real -- hallazgo real
    # (2026-09-04), no una suposicion: verificado contra la respuesta real
    # de /api/heatmap, celdas con `notional_usd` de 6.5e-09, 1.65e-08 etc.
    # pasaban el filtro como si fueran pools genuinos (encontrado
    # construyendo api/setups.py, que si les presta atencion a los
    # numeros -- el heatmap visual nunca lo noto porque cualquier
    # Liquidity Threshold realista, en millones, ya las descarta). $1 esta
    # muy por debajo de cualquier pool real (los mas chicos que se han
    # visto son miles de dolares) y muy por encima de cualquier residuo de
    # redondeo posible en este rango.
    MIN_NOTIONAL_USD = 1.0

    cells: list[GridCell] = []
    for (price_bin, leverage, _side), evs in events.items():
        evs.sort(key=lambda e: e[0])
        running = 0.0
        prev_time: Optional[int] = None
        prev_value = 0.0
        i = 0
        m = len(evs)
        while i < m:
            t = evs[i][0]
            # Todos los eventos en el MISMO instante se aplican juntos antes
            # de emitir -- dos niveles que empiezan/terminan exactamente a
            # la vez no deben generar un intervalo de duracion cero en medio.
            while i < m and evs[i][0] == t:
                running += evs[i][1]
                i += 1
            if prev_time is not None and prev_value > MIN_NOTIONAL_USD:
                cells.append(GridCell(time_ms=prev_time, price=price_bin, leverage=leverage,
                                       notional_usd=prev_value, bucket_ms=t - prev_time))
            prev_time = t
            prev_value = running
        # El ultimo tramo (desde el evento mas nuevo hasta "ahora") no tiene
        # un evento de cierre propio -- si sigue activo, se extiende hasta
        # window_end_ms.
        if prev_time is not None and prev_value > MIN_NOTIONAL_USD and prev_time < window_end_ms:
            cells.append(GridCell(time_ms=prev_time, price=price_bin, leverage=leverage,
                                   notional_usd=prev_value, bucket_ms=window_end_ms - prev_time))
    return cells


def bin_levels(levels: list[LiquidationLevel], bin_size: float = DEFAULT_BIN_WIDTH_USD) -> dict[float, float]:
    """Agrupa niveles en bins de precio de ancho FIJO (en dolares) sumando
    notional -- una cuadricula de $100 en vez de un porcentaje del precio,
    a peticion del usuario (2026-08-28): un ancho relativo daba bins de
    tamaño distinto segun el simbolo y el precio del momento, sin la
    cuadricula regular que se ve en un heatmap publicado.

    El bin se calcula sobre el precio de CADA nivel con un ancho FIJO
    compartido, asi que no hay el problema de redondeo que si habria con un
    ancho relativo (dos precios casi iguales podrian caer en bins con un
    tamaño ligeramente distinto cada uno)."""
    if bin_size <= 0:
        return {}
    bins: dict[float, float] = {}
    for lvl in levels:
        if lvl.price <= 0:
            continue
        key = round(lvl.price / bin_size) * bin_size
        bins[key] = bins.get(key, 0.0) + lvl.notional_usd
    return bins


def bin_levels_by_leverage(levels: list[LiquidationLevel],
                            bin_size: float = DEFAULT_BIN_WIDTH_USD) -> dict[int, dict[float, float]]:
    """Como `bin_levels`, pero sin mezclar apalancamientos entre si -- hace
    falta para que la UI pueda activar/desactivar cada nivel (10x/15x/25x/
    50x/100x) por separado sin tener que deshacer una suma ya hecha."""
    by_leverage: dict[int, list[LiquidationLevel]] = {}
    for lvl in levels:
        by_leverage.setdefault(lvl.leverage, []).append(lvl)
    return {
        leverage: bin_levels(lvls, bin_size)
        for leverage, lvls in by_leverage.items()
    }
