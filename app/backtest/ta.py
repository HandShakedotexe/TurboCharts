"""Indicadores de analisis tecnico CLASICOS (precio puro, sin derivados) --
deliberadamente en un modulo aparte de `indicators.py`, cuyo docstring dice
explicitamente que ESE modulo "no es una libreria generica de TA (RSI/MACD)
pegada encima". Este si lo es, pero de forma deliberada, no exhaustiva.

De los indicadores clasicos tipicos (RSI, EMA, MACD) se implementa SOLO
RSI aqui. Motivo, no solo gusto -- MACD es matematicamente
`ema_rapida - ema_lenta`: casi la misma informacion que ya da un cruce de
EMAs o la pendiente del regimen que YA existe en `regime.classify_regime`
(probado, documentado). Añadir MACD al lado de un cruce de EMA no suma
una dimension independiente, suma un parametro mas que aumenta
`trials_count` sin aumentar poder explicativo -- exactamente el riesgo de
usar todos los indicadores disponibles sin necesidad. RSI si mide algo
que el resto del proyecto no mide todavia (momentum de PRECIO puro, ida y
vuelta rapida) y encaja con `strategies/rsi_pullback.py`: comprar una
caida dentro de una tendencia ya confirmada por `regime.py`, no perseguir
la tendencia en caliente."""
from __future__ import annotations

from typing import Sequence

from .data import Bar

RSI_NEUTRAL = 50.0


def rsi(bars: Sequence[Bar], period: int = 14) -> list[float]:
    """RSI de Wilder sobre `close`, causal (el valor en el indice i solo lee
    `bars[0..i]`). Calentamiento (i < period, sin suficientes deltas
    todavia) devuelve `RSI_NEUTRAL` (50.0) -- mismo criterio que el resto
    del modulo de indicadores para "sin señal medible todavia" (ver
    docstring de `regime.atr`), NUNCA None (mas simple de consumir en el
    motor y en las estrategias, que comparan con `<=`/`>=` directamente)."""
    out: list[float] = []
    if not bars:
        return out
    avg_gain = 0.0
    avg_loss = 0.0
    gains: list[float] = []
    losses: list[float] = []
    for i, b in enumerate(bars):
        if i == 0:
            out.append(RSI_NEUTRAL)
            continue
        delta = b.close - bars[i - 1].close
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        if i < period:
            # Fase de calentamiento: acumula deltas, sin suficiente
            # historia todavia para ni siquiera la semilla del promedio.
            gains.append(gain)
            losses.append(loss)
            out.append(RSI_NEUTRAL)
            continue
        if i == period:
            # Semilla: promedio simple de los `period` deltas reunidos --
            # el primer valor real de RSI, sin suavizado de Wilder todavia
            # (hace falta un valor anterior para suavizar, y este ES el
            # primero). Convencion estandar (Wilder, "New Concepts in
            # Technical Trading Systems"): con `period` deltas (period + 1
            # precios) ya se puede reportar un RSI real, no uno mas tarde.
            gains.append(gain)
            losses.append(loss)
            avg_gain = sum(gains) / len(gains)
            avg_loss = sum(losses) / len(losses)
        else:
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss < 1e-12:
            # Sin perdidas en la ventana suavizada: RSI = 100 si hubo
            # ganancias reales, o neutral si tampoco hubo ganancias (precio
            # plano de verdad, no solo "sin bajadas") -- mismo cuidado con
            # ruido de punto flotante que `oi_trend_zscore.stdev < 1e-9`.
            out.append(100.0 if avg_gain > 1e-12 else RSI_NEUTRAL)
            continue
        rs = avg_gain / avg_loss
        out.append(100.0 - 100.0 / (1.0 + rs))
    return out
