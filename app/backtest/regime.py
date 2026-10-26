"""Clasificador de regimen de mercado -- explicito, sin caja negra.
Volatilidad
realizada (ATR) cruzada con la pendiente de una media movil propia, no un
modelo entrenado ni un umbral magico sin explicar. Se usa solo para
SEGMENTAR estadisticas despues (ver statistics.stats_by_regime), nunca
para filtrar la estrategia en este pase.
"""
from __future__ import annotations

from typing import Sequence

from .data import Bar

REGIME_TRENDING_UP = "trending_up"
REGIME_TRENDING_DOWN = "trending_down"
REGIME_RANGING_HIGH_VOL = "ranging_high_vol"
REGIME_RANGING_LOW_VOL = "ranging_low_vol"


def true_range(bars: Sequence[Bar]) -> list[float]:
    out = []
    prev_close = None
    for b in bars:
        tr = (b.high - b.low) if prev_close is None else max(
            b.high - b.low, abs(b.high - prev_close), abs(b.low - prev_close),
        )
        out.append(tr)
        prev_close = b.close
    return out


def atr(bars: Sequence[Bar], period: int = 14) -> list[float]:
    """Suavizado de Wilder. Los primeros `period` valores son un promedio
    simple creciente (no hay suficiente historia para el suavizado
    completo) en vez de None -- mismo criterio de "calentamiento" que el
    resto de indicators.py."""
    trs = true_range(bars)
    out: list[float] = []
    running_sum = 0.0
    for i, tr in enumerate(trs):
        if i < period:
            running_sum += tr
            out.append(running_sum / (i + 1))
        else:
            out.append((out[-1] * (period - 1) + tr) / period)
    return out


def sma(values: Sequence[float], period: int) -> list[float]:
    out = []
    window: list[float] = []
    total = 0.0
    for v in values:
        window.append(v)
        total += v
        if len(window) > period:
            total -= window.pop(0)
        out.append(total / len(window))
    return out


def classify_regime(
    bars: Sequence[Bar],
    atr_period: int = 14,
    trend_period: int = 20,
    vol_lookback: int = 60,
    trend_threshold_pct: float = 0.03,
) -> list[str]:
    """Por barra: si el precio se movio mas de `trend_threshold_pct` desde
    `trend_period` barras atras (medido sobre una SMA propia, no el precio
    crudo, para no confundir ruido de una barra con tendencia), el regimen
    es direccional (`trending_up`/`trending_down`) sin importar la
    volatilidad. Si no, se etiqueta por volatilidad: `ranging_high_vol` si
    el ATR actual esta en la mitad superior de su ventana movil de
    `vol_lookback` barras, `ranging_low_vol` si no.

    Deliberadamente NO hay una quinta etiqueta "trending + alta vol": una
    tendencia clara ya es la señal mas fuerte: distinguir su volatilidad
    no cambia que hacer con ella en este pase (se puede afinar cuando haga
    falta, medido, no adivinado)."""
    if not bars:
        return []
    atrs = atr(bars, atr_period)
    closes = [b.close for b in bars]
    smas = sma(closes, trend_period)
    labels = []
    for i in range(len(bars)):
        ref_idx = max(0, i - trend_period)
        base = smas[ref_idx]
        slope_pct = (smas[i] - base) / base if base else 0.0
        if slope_pct >= trend_threshold_pct:
            labels.append(REGIME_TRENDING_UP)
            continue
        if slope_pct <= -trend_threshold_pct:
            labels.append(REGIME_TRENDING_DOWN)
            continue
        window = atrs[max(0, i - vol_lookback + 1):i + 1]
        pct_rank = sum(1 for x in window if x <= atrs[i]) / len(window)
        labels.append(REGIME_RANGING_HIGH_VOL if pct_rank >= 0.5 else REGIME_RANGING_LOW_VOL)
    return labels
