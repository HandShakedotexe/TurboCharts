"""Features nuevos para el laboratorio de descubrimiento de estrategias
-- complementa `indicators.py` (que
ya cubre CVD/OI-trend/funding-zscore/long-short-ratio/cluster-proximity) y
`ta.py` (RSI) sin duplicarlos. Mismo patron: funciones puras sobre series ya
cargadas, causales (el valor en `i` solo usa datos hasta `i`), testeables sin
DB.

Por que estos y no CVD crudo como feature de IC: `indicators.cvd` es una
suma ACUMULADA desde el inicio de la ventana -- no estacionaria (crece sin
limite), asi que su correlacion de Spearman contra el retorno futuro puede
salir "significativa" solo porque ambas series tienden con el tiempo, no
porque una prediga la otra. `taker_buy_ratio` de abajo mide lo mismo
(presion compradora vs vendedora) pero acotado en [0,1] -- estacionario,
apto para IC. Ver `screen.py`, que es quien consume esto."""
from __future__ import annotations

import statistics
from math import log
from typing import Sequence

from .data import Bar, OiPoint


def log_returns(bars: Sequence[Bar]) -> list[float]:
    """Retorno logaritmico barra a barra. 0.0 en la primera barra (no hay
    barra anterior) -- mismo criterio de "calentamiento en 0" que el resto
    del modulo de indicadores."""
    out = [0.0]
    for i in range(1, len(bars)):
        prev = bars[i - 1].close
        out.append(log(bars[i].close / prev) if prev > 0 else 0.0)
    return out


def realized_vol(bars: Sequence[Bar], window: int = 20) -> list[float]:
    """Desviacion estandar de los retornos logaritmicos en una ventana
    movil -- volatilidad realizada, la contraparte de
    `regime.atr` medida en retornos en vez de rango de precio."""
    rets = log_returns(bars)
    out = []
    for i in range(len(rets)):
        w = rets[max(0, i - window + 1):i + 1]
        out.append(statistics.pstdev(w) if len(w) >= 2 else 0.0)
    return out


def candle_body_ratio(bars: Sequence[Bar]) -> list[float]:
    """`|close-open| / (high-low)` -- 1.0 es una vela de tendencia limpia
    (poca mecha), cerca de 0 es indecision/rechazo. 0.0 en velas de rango
    cero (high==low) en vez de dividir por cero."""
    out = []
    for b in bars:
        rng = b.high - b.low
        out.append(abs(b.close - b.open) / rng if rng > 0 else 0.0)
    return out


def taker_buy_ratio(bars: Sequence[Bar]) -> list[float]:
    """Fraccion del volumen de la barra que fue compra taker -- version
    ACOTADA (0..1, estacionaria) de la misma idea que `indicators.cvd`
    (acumulada, no estacionaria). 0.5 (neutral) en barras sin volumen."""
    out = []
    for b in bars:
        out.append(b.taker_buy_volume / b.volume if b.volume > 0 else 0.5)
    return out


def rolling_zscore(values: Sequence[float], window: int) -> list[float]:
    """Z-score generico contra la propia ventana movil reciente -- mismo
    calculo que ya se repite tres veces (`oi_trend_zscore`, `funding_zscore`,
    y ahora esto) en el proyecto; se generaliza aqui para no repetirlo una
    cuarta vez con `volume`/`realized_vol` como entrada."""
    out = []
    for i in range(len(values)):
        window_vals = values[max(0, i - window + 1):i + 1]
        if len(window_vals) < 2:
            out.append(0.0)
            continue
        stdev = statistics.pstdev(window_vals)
        out.append(0.0 if stdev < 1e-12 else (values[i] - statistics.fmean(window_vals)) / stdev)
    return out


def momentum(bars: Sequence[Bar], lookback: int = 20) -> list[float]:
    """Retorno simple sobre `lookback` barras -- el bloque basico de
    trend-following y de mean-reversion (su negativo)."""
    out = []
    for i, b in enumerate(bars):
        if i < lookback:
            out.append(0.0)
            continue
        prev = bars[i - lookback].close
        out.append((b.close - prev) / prev if prev > 0 else 0.0)
    return out


def prior_rolling_max_high(bars: Sequence[Bar], window: int) -> list[float]:
    """Maximo de `high` en los `window` barras ANTERIORES a `i` (excluye
    la barra `i` misma) -- la referencia contra la que un breakout real se
    compara. Si se incluyera la barra actual, un breakout seria trivial
    (toda barra "rompe" su propio high). `float('inf')` en el
    calentamiento (sin suficiente historia): un breakout nunca puede ser
    mayor que infinito, asi que la estrategia que consuma esto
    simplemente no dispara todavia, en vez de disparar con una referencia
    inventada."""
    highs = [b.high for b in bars]
    out = []
    for i in range(len(bars)):
        window_vals = highs[max(0, i - window):i]
        out.append(max(window_vals) if window_vals else float("inf"))
    return out


def prior_rolling_min_low(bars: Sequence[Bar], window: int) -> list[float]:
    """Espejo de `prior_rolling_max_high` para el lado bajista --
    `float('-inf')` en el calentamiento por el mismo motivo."""
    lows = [b.low for b in bars]
    out = []
    for i in range(len(bars)):
        window_vals = lows[max(0, i - window):i]
        out.append(min(window_vals) if window_vals else float("-inf"))
    return out


def price_oi_divergence(bars: Sequence[Bar], oi_rows: Sequence[OiPoint], lookback: int = 12) -> list[float]:
    """Momentum de precio MENOS momentum de OI sobre la misma ventana --
    combina precio + OI en un solo feature. Positivo
    grande: precio subiendo con OI plano/cayendo (posible short-covering,
    poco apalancamiento nuevo). Negativo grande: OI subiendo mas rapido que
    el precio (apalancamiento nuevo entrando sin que el precio siga --
    "rising OI with stable price", la misma lectura que ya usa
    `oi_trend_continuation`, aqui expuesta como feature independiente en vez
    de logica de estrategia).

    Asume `bars` y `oi_rows` alineados 1:1 por indice (mismo timestamp) --
    responsabilidad del caller, ver `screen.py::_align`."""
    price_mom = momentum(bars, lookback)
    out = []
    for i, row in enumerate(oi_rows):
        if i < lookback or row.sum_open_interest is None:
            out.append(0.0)
            continue
        prev_oi = oi_rows[i - lookback].sum_open_interest
        oi_mom = (row.sum_open_interest - prev_oi) / prev_oi if prev_oi else 0.0
        out.append(price_mom[i] - oi_mom)
    return out
