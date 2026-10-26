"""Smart Money Concepts (SMC) -- estructura de precio tipo ICT, pensada
para usarse como CONTEXTO de una temporalidad mayor sobre las entradas de
5m ("tendencia" + SMC), no como estrategia propia todavia. Modulo aparte
de `ta.py` (TA clasico) y `regime.py` (regimen de volatilidad/tendencia
por ATR) -- mismo patron de separacion que ya usa el proyecto para no
mezclar familias de indicador independientes en un solo archivo.

Todas las funciones son puras sobre `list[Bar]` y CAUSALES/point-in-time
por diseño: cada estructura devuelta sabe desde que barra (indice) existe
y, cuando aplica, desde que barra queda invalidada -- igual que
`LiquidationLevel.trade_time_ms`/`filter_consumed_levels` en
heatmap_model.py. Una estrategia que consulte estas funciones en la
barra `i` nunca deberia poder ver una estructura formada con datos de
barras > i (ver la nota de "confirmacion tardia" en `swing_points`).

ADVERTENCIA DE DISEÑO (mismo espiritu que la cesta de apalancamientos de
heatmap_model.py): "Order Block"/"Breaker Block"/"Mitigation Block"/
"FVG"/"ChoCH"/"BOS" NO tienen una formula unica publicada -- son
conceptos de trading discrecional (comunidad ICT/Smart Money Concepts)
con variantes segun el autor. Las definiciones de aqui son una
INTERPRETACION MECANICA explicita, documentada funcion por funcion, NO
"la" definicion correcta. Simplificacion deliberada: no se implementa un
tipo `MitigationBlock` separado -- la definicion describe una zona
"similar a un Order Block" que el precio revisita antes de seguir la
tendencia, que es exactamente el campo `mitigated_at_index` de
`OrderBlock` mas abajo (la primera vez que el precio vuelve a tocar la
zona). Un tipo nuevo solo para renombrar ese mismo campo no agrega
informacion independiente -- el mismo criterio que ya evito agregar MACD
en `ta.py`.
"""
from __future__ import annotations

from typing import NamedTuple, Optional

from .data import Bar


class SwingPoint(NamedTuple):
    index: int      # indice de la barra donde esta el swing
    price: float
    kind: str       # "high" o "low"
    # Indice en el que el swing queda CONFIRMADO (index + lookback) --
    # hace falta ver `lookback` barras despues para saber que index fue
    # el maximo/minimo local. Cualquier consumidor causal debe usar ESTE
    # campo, no `index`, para decidir desde que barra "existe" el swing.
    confirmed_at_index: int


def swing_points(bars: list[Bar], lookback: int = 2) -> list[SwingPoint]:
    """Fractal simple: la barra `i` es swing high si su `high` es el
    maximo estricto de la ventana `[i-lookback, i+lookback]`, swing low
    si su `low` es el minimo estricto de esa misma ventana. `lookback=2`
    (5 barras de ventana) es el valor mas comun en herramientas de
    charting con "swing structure" -- expuesto como parametro porque no
    hay un valor "correcto" unico, igual que `regime.py` expone
    `atr_period`.

    Estricto (no `>=`/`<=`) para no marcar un tramo plano entero como
    "swing" repetido -- un empate exacto en OHLC de 5 minutos de un
    simbolo liquido es casi inexistente en la practica, y de ocurrir,
    ninguna de las barras empatadas es un extremo real mas que las otras.
    """
    points: list[SwingPoint] = []
    n = len(bars)
    for i in range(lookback, n - lookback):
        window = bars[i - lookback : i + lookback + 1]
        if bars[i].high == max(b.high for b in window) and sum(
            1 for b in window if b.high == bars[i].high
        ) == 1:
            points.append(SwingPoint(i, bars[i].high, "high", i + lookback))
        if bars[i].low == min(b.low for b in window) and sum(
            1 for b in window if b.low == bars[i].low
        ) == 1:
            points.append(SwingPoint(i, bars[i].low, "low", i + lookback))
    return points


class StructureEvent(NamedTuple):
    index: int          # barra cuyo CIERRE rompe el nivel (evento causal real)
    kind: str           # "BOS" (continuacion) o "ChoCH" (cambio de caracter)
    direction: str       # "up" o "down" -- direccion del rompimiento
    level: float          # precio del swing roto
    swing_index: int      # indice de la barra donde estaba ese swing


def structure_events(bars: list[Bar], lookback: int = 2) -> list[StructureEvent]:
    """BOS (Break of Structure) = el cierre rompe el swing mas reciente
    EN LA MISMA direccion de la tendencia vigente (continuacion: un
    higher-high en tendencia alcista, un lower-low en bajista). ChoCH
    (Change of Character) = el cierre rompe el swing mas reciente EN
    DIRECCION CONTRARIA a la tendencia vigente -- la primera señal
    mecanica de posible reversion.

    "Tendencia vigente" se rastrea aqui con la maquina de estados mas
    simple posible (1 bit: None/"up"/"down"), fijada por el ULTIMO
    evento de estructura visto -- deliberadamente MAS simple que
    `regime.classify_regime` (que usa pendiente de EMA + ATR): mezclar
    los dos criterios de tendencia en una sola funcion haria imposible
    saber cual de los dos esta realmente decidiendo BOS vs ChoCH. Un
    caller que quiera cruzar esto con el regimen de `regime.py` lo hace
    el mismo, comparando `direction` contra `regime` barra a barra.

    Solo el swing NO ROTO mas reciente de cada lado (high/low) puede
    romperse -- una vez roto, ese swing se descarta (no puede volver a
    "romperse" dos veces) y se pasa al siguiente swing pendiente de cada
    lado."""
    points = swing_points(bars, lookback)
    # Swings pendientes de romper, en el orden en que se CONFIRMARON
    # (no en el que ocurrieron) -- un swing solo puede actuar como nivel
    # una vez que un caller causal ya podria haberlo visto.
    pending_highs = sorted((p for p in points if p.kind == "high"), key=lambda p: p.confirmed_at_index)
    pending_lows = sorted((p for p in points if p.kind == "low"), key=lambda p: p.confirmed_at_index)
    events: list[StructureEvent] = []
    trend: Optional[str] = None
    high_ptr, low_ptr = 0, 0

    for i, bar in enumerate(bars):
        # Rompimiento hacia arriba: el swing high pendiente MAS VIEJO ya
        # confirmado (confirmed_at_index <= i, sin eso seria adivinar con
        # datos futuros) queda roto si el cierre lo supera.
        while high_ptr < len(pending_highs) and pending_highs[high_ptr].confirmed_at_index <= i:
            swing = pending_highs[high_ptr]
            if bar.close > swing.price:
                kind = "BOS" if trend != "down" else "ChoCH"
                events.append(StructureEvent(i, kind, "up", swing.price, swing.index))
                trend = "up"
                high_ptr += 1
            else:
                break
        while low_ptr < len(pending_lows) and pending_lows[low_ptr].confirmed_at_index <= i:
            swing = pending_lows[low_ptr]
            if bar.close < swing.price:
                kind = "BOS" if trend != "up" else "ChoCH"
                events.append(StructureEvent(i, kind, "down", swing.price, swing.index))
                trend = "down"
                low_ptr += 1
            else:
                break
    return events


class FairValueGap(NamedTuple):
    start_index: int          # indice de la vela 1 del patron de 3
    direction: str             # "up" (gap alcista) o "down" (bajista)
    top: float
    bottom: float
    filled_at_index: Optional[int] = None  # None = sigue sin llenar


def fair_value_gaps(bars: list[Bar]) -> list[FairValueGap]:
    """Patron de 3 velas clasico: hueco alcista cuando `bars[i-1].high <
    bars[i+1].low` (la vela `i`, la del medio, es el impulso que deja el
    hueco); hueco bajista cuando `bars[i-1].low > bars[i+1].high`. El
    hueco existe DESDE la barra `i+1` (recien ahi es visible con datos
    causales) -- `start_index` guarda `i-1` (el borde real del hueco en
    el grafico) pero un caller causal debe tratarlo como "existe desde
    `i+1`", igual que con `confirmed_at_index` en `swing_points`.

    `filled_at_index`: la primera barra POSTERIOR a `i+1` cuyo precio
    (high/low) toca de nuevo la zona del hueco -- un FVG "se llena" y
    deja de ser una zona de liquidez pendiente, mismo espiritu que
    `heatmap_model.filter_consumed_levels` con los niveles de
    liquidacion. Un FVG sin llenar nunca devuelve `filled_at_index=None`.
    """
    gaps: list[FairValueGap] = []
    n = len(bars)
    for i in range(1, n - 1):
        prev_bar, next_bar = bars[i - 1], bars[i + 1]
        if prev_bar.high < next_bar.low:
            gaps.append(FairValueGap(i - 1, "up", top=next_bar.low, bottom=prev_bar.high))
        elif prev_bar.low > next_bar.high:
            gaps.append(FairValueGap(i - 1, "down", top=prev_bar.low, bottom=next_bar.high))

    filled: list[FairValueGap] = []
    for gap in gaps:
        fill_index: Optional[int] = None
        # +3, no +2: `start_index+2` es la propia vela que confirma el
        # hueco (`next_bar`, la que define uno de sus dos bordes) --
        # revisarla a ella misma siempre "toca" el hueco de forma
        # trivial, no es un llenado real por otra vela.
        for j in range(gap.start_index + 3, n):
            if bars[j].low <= gap.top and bars[j].high >= gap.bottom:
                fill_index = j
                break
        filled.append(gap._replace(filled_at_index=fill_index))
    return filled


class OrderBlock(NamedTuple):
    index: int                          # barra del Order Block (la vela opuesta al impulso)
    direction: str                       # "bullish" (soporte) o "bearish" (resistencia)
    top: float
    bottom: float
    impulse_event_index: int             # indice del StructureEvent (BOS/ChoCH) que lo origino
    mitigated_at_index: Optional[int] = None   # primer regreso del precio a la zona
    broken_at_index: Optional[int] = None       # primer cierre que atraviesa la zona entera


def order_blocks(bars: list[Bar], lookback: int = 2) -> list[OrderBlock]:
    """Order Block = la ULTIMA vela de color opuesto al impulso, justo
    antes de que ese impulso rompa estructura (BOS o ChoCH, ver
    `structure_events`). Un impulso alcista (rompe hacia arriba) que deja
    un Order Block ALCISTA es la ultima vela BAJISTA (close < open) antes
    del rompimiento -- la zona de la que "salio" el movimiento, candidata
    a soporte si el precio vuelve. Simetrico para impulsos bajistas.

    Ciclo de vida, calculado una sola vez por bloque (point-in-time,
    nunca mira hacia atras de su propio indice para decidir su propio
    estado):
    - `mitigated_at_index`: primera barra POSTERIOR que toca la zona
      [bottom, top] de nuevo (el precio "vuelve a mitigar" -- ver la nota
      sobre "Mitigation Block" en el docstring del modulo).
    - `broken_at_index`: primera barra POSTERIOR cuyo CIERRE atraviesa
      la zona entera (para un OB alcista, cierra por debajo de `bottom`;
      para uno bajista, por encima de `top`) -- desde ahi el bloque es un
      **Breaker Block**: ya no es soporte/resistencia en su direccion
      original, sino en la CONTRARIA (`direction` sigue describiendo el
      bloque original; un caller que quiera tratarlo como breaker debe
      invertir el lado al usarlo despues de `broken_at_index`, exactamente
      como pide la definicion: "actua como soporte/resistencia en la
      direccion contraria").
    """
    events = structure_events(bars, lookback)
    blocks: list[OrderBlock] = []
    for event in events:
        # Busca hacia atras desde el swing roto (no desde el evento) la
        # ultima vela de color opuesto al impulso -- el swing_index es el
        # extremo real que el impulso rompio, el punto de partida
        # correcto para "la vela antes de que empezara el movimiento".
        opposite_is_bearish = event.direction == "up"
        ob_index: Optional[int] = None
        for j in range(event.swing_index, -1, -1):
            bar = bars[j]
            is_bearish = bar.close < bar.open
            if is_bearish == opposite_is_bearish:
                ob_index = j
                break
        if ob_index is None:
            continue
        ob_bar = bars[ob_index]
        top, bottom = max(ob_bar.open, ob_bar.close), min(ob_bar.open, ob_bar.close)
        direction = "bullish" if event.direction == "up" else "bearish"

        mitigated_at: Optional[int] = None
        broken_at: Optional[int] = None
        for j in range(event.index + 1, len(bars)):
            bar = bars[j]
            touches = bar.low <= top and bar.high >= bottom
            if mitigated_at is None and touches:
                mitigated_at = j
            if direction == "bullish" and bar.close < bottom:
                broken_at = j
                break
            if direction == "bearish" and bar.close > top:
                broken_at = j
                break
        blocks.append(OrderBlock(
            ob_index, direction, top, bottom, event.index,
            mitigated_at_index=mitigated_at, broken_at_index=broken_at,
        ))
    return blocks
