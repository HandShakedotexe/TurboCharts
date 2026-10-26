"""Indicadores para el backtester -- funciones puras sobre series que el
caller ya obtuvo, sin I/O, mismo patron que api/heatmap_model.py
(testeables con datos sinteticos, sin DB en los tests).

El set no es una libreria generica de TA (RSI/MACD) pegada encima: sale de
pensar que indicadores combinan bien con un chart centrado en heatmap de
liquidaciones + datos de derivados. Los cinco salen ENTERO de tablas que
este proyecto ya ingesta (candles, funding_rate, open_interest_metrics,
big_trades) -- sin ingesta nueva.
"""
from __future__ import annotations

import statistics
from typing import Optional, Sequence

from api.heatmap_model import LiquidationLevel, bin_levels
from .data import Bar, FundingPoint, OiPoint


def bar_delta(bars: Sequence[Bar]) -> list[float]:
    """Volume Delta de CADA barra por separado (compra taker menos venta
    taker) -- a diferencia de `cvd()`, que es la suma ACUMULADA. Hace
    falta el valor de UNA sola vela para `strategies/frvp_absorption.py`:
    "un pico de Volume Delta" o "Delta < Umbral Negativo" EN LA VELA que
    rompe/toca el nivel, no un cambio acumulado de varias barras como ya
    usa `cvd_change` para `lvn_breakout`."""
    out = []
    for b in bars:
        taker_sell = b.volume - b.taker_buy_volume
        out.append(b.taker_buy_volume - taker_sell)
    return out


def cvd(bars: Sequence[Bar]) -> list[float]:
    """Cumulative Volume Delta: compra taker menos venta taker, acumulado.
    `candles.taker_buy_volume` ya distingue las dos (Binance klines), asi
    que no hace falta el flag `is_buyer_maker` de aggTrades -- CVD sale
    directo de una tabla que ya se ingesta.

    Divergencia entre CVD y precio cerca de un cluster de liquidacion es
    una señal de confluencia razonable (real compra/venta empujando el
    acercamiento, no solo precio)."""
    out = []
    running = 0.0
    for b in bars:
        taker_sell = b.volume - b.taker_buy_volume
        running += b.taker_buy_volume - taker_sell
        out.append(running)
    return out


def cvd_change(bars: Sequence[Bar], lookback: int = 20) -> list[float]:
    """Cambio de `cvd()` en las ultimas `lookback` barras. `cvd()` es una
    suma acumulada desde el inicio de la serie CARGADA (arbitraria --
    depende de donde empieza `bars`, no del mercado), asi que su valor
    absoluto no es comparable entre corridas con distinto `start`; solo su
    CAMBIO en una ventana fija lo es (mismo motivo por el que
    `oi_trend`/`funding_zscore` de mas abajo tampoco usan el nivel crudo).
    Calentamiento (i<lookback): cambio desde el inicio de la serie, igual
    criterio que el resto de este modulo (sin `None`, ver docstring del
    modulo)."""
    series = cvd(bars)
    out = []
    for i in range(len(series)):
        j = max(0, i - lookback)
        out.append(series[i] - series[j])
    return out


def oi_trend(oi_rows: Sequence[OiPoint], lookback: int = 12) -> list[float]:
    """Tasa de cambio de `sum_open_interest` sobre `lookback` puntos. OI
    creciente hacia un cluster de liquidacion = apalancamiento nuevo
    apilandose, mas probabilidad de cascada (hallazgo de la investigacion:
    "rising OI with stable price near a liquidation cluster signals an
    imminent move"). 0.0 en el periodo de calentamiento (sin suficiente
    historia todavia) en vez de None -- mas simple de consumir en el motor,
    y "sin tendencia medible" es una lectura razonable para ese tramo."""
    out = []
    for i, row in enumerate(oi_rows):
        if i < lookback:
            out.append(0.0)
            continue
        prev = oi_rows[i - lookback].sum_open_interest
        cur = row.sum_open_interest
        if not prev or cur is None:
            out.append(0.0)
            continue
        out.append((cur - prev) / prev)
    return out


def oi_trend_zscore(oi_rows: Sequence[OiPoint], trend_lookback: int = 12, zscore_window: int = 60) -> list[float]:
    """Z-score de `oi_trend` contra su propia distribucion reciente (misma
    idea que `funding_zscore`). Un umbral FIJO sobre `oi_trend` tiene el
    problema de que la escala tipica del indicador no es estable: lo que
    cuenta como "movimiento grande" de OI cambia segun el periodo de
    baja o alta volatilidad de OI, asi que un numero fijo puede cumplirse
    tan poco que no junte muestra, sin relacion clara con "que tan
    inusual es esto" en cada momento. El z-score normaliza contra la
    propia ventana reciente en vez de depender de un numero absoluto."""
    trend = oi_trend(oi_rows, lookback=trend_lookback)
    out = []
    for i in range(len(trend)):
        window = trend[max(0, i - zscore_window + 1):i + 1]
        if len(window) < 2:
            out.append(0.0)
            continue
        stdev = statistics.pstdev(window)
        # Umbral, no `== 0` exacto: `trend` es un cociente `(cur-prev)/prev`
        # calculado con `**` de forma independiente por indice, asi que un
        # tramo VERDADERAMENTE plano (mismo ratio geometrico) igual trae
        # ruido de punto flotante del orden de 1e-15 -- con `== 0` ese
        # ruido pasaba como "desviacion real" y el z-score, division de
        # ruido entre ruido, podia salir grande de forma completamente
        # espuria (confirmado con un test que arma una serie plana de
        # verdad y ve z-scores de hasta ~2 sin este guardado). 1e-9 esta
        # nueve ordenes de magnitud por debajo de la desviacion real medida
        # en BTCUSDT (~0.028), asi que no esconde ninguna señal genuina.
        out.append(0.0 if stdev < 1e-9 else (trend[i] - statistics.fmean(window)) / stdev)
    return out


def funding_zscore(funding_rows: Sequence[FundingPoint], lookback: int = 8) -> list[float]:
    """Z-score del funding actual contra su propia distribucion reciente
    (ventana movil de `lookback` eventos, ~un dia con funding cada 8h).

    La investigacion es explicita en que importa el funding EXTREMO, no el
    nivel crudo ("extreme positive funding means longs are crowded and
    vulnerable"; ">0.05%/h" como umbral de alarma en una de las fuentes) --
    un z-score responde "extremo respecto a que" sin fijar un umbral
    absoluto que cambiaria de simbolo a simbolo."""
    out = []
    for i in range(len(funding_rows)):
        window = [r.rate for r in funding_rows[max(0, i - lookback + 1):i + 1]]
        if len(window) < 2:
            out.append(0.0)
            continue
        stdev = statistics.pstdev(window)
        out.append(0.0 if stdev == 0 else (funding_rows[i].rate - statistics.fmean(window)) / stdev)
    return out


def _ratio_value(row: OiPoint) -> Optional[float]:
    # count_long_short_ratio (todas las cuentas) preferido sobre
    # sum_toptrader_long_short_ratio (solo top traders) -- muestra mas
    # amplia; se cae al segundo solo si el primero no vino en esa fila.
    return row.count_long_short_ratio if row.count_long_short_ratio is not None else row.sum_toptrader_long_short_ratio


def long_short_ratio_trend(oi_rows: Sequence[OiPoint], lookback: int = 12) -> list[float]:
    """Tasa de cambio del long/short ratio -- confirma o contradice lo que
    dice `oi_trend`: OI subiendo con el ratio inclinandose hacia largos es
    apalancamiento largo nuevo, no simplemente "mas actividad"."""
    out = []
    for i, row in enumerate(oi_rows):
        cur = _ratio_value(row)
        if i < lookback or cur is None:
            out.append(0.0)
            continue
        prev = _ratio_value(oi_rows[i - lookback])
        if not prev:
            out.append(0.0)
            continue
        out.append((cur - prev) / prev)
    return out


def liquidation_cluster_proximity(
    price: float, levels: Sequence[LiquidationLevel], bin_size: float = 100.0,
) -> dict:
    """Distancia % y densidad de notional del cluster de liquidacion mas
    cercano por debajo (largos, que se liquidan bajo su entrada) y por
    encima (cortos) del precio actual -- la pieza que NO es un indicador
    generico: reusa `heatmap_model.bin_levels` tal cual sobre los niveles ya
    modelados por `compute_levels_from_trades`, la MISMA data que ya
    alimenta el heatmap visible.

    El hallazgo de mayor conviccion de la investigacion combina esto con
    `funding_zscore`: funding extremo positivo + cluster largo denso
    debajo del precio = setup de long-squeeze de alta probabilidad (ver
    strategies/funding_squeeze.py)."""
    long_bins = bin_levels([lvl for lvl in levels if lvl.side == "long"], bin_size)
    short_bins = bin_levels([lvl for lvl in levels if lvl.side == "short"], bin_size)
    below = [(p, n) for p, n in long_bins.items() if p < price]
    above = [(p, n) for p, n in short_bins.items() if p > price]
    nearest_below = max(below, key=lambda x: x[0]) if below else None
    nearest_above = min(above, key=lambda x: x[0]) if above else None

    def _describe(nearest):
        if nearest is None:
            return {"distance_pct": None, "notional_usd": 0.0}
        p, n = nearest
        return {"distance_pct": abs(p - price) / price if price else None, "notional_usd": n}

    return {
        "long_cluster_below": _describe(nearest_below),
        "short_cluster_above": _describe(nearest_above),
    }
