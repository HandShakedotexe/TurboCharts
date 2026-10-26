"""Premisa: entrar en el REBOTE que sigue a una liquidacion REAL
considerable.

SL y TP en unidades de ATR en vez de % fijo -- un % fijo no se adapta a
cuanto se mueve el precio en cada momento (misma distancia porcentual es
"ajustada" en un dia tranquilo y "regalada" en uno volatil). El diseño
reusa el mismo patron que `rsi_pullback`/`oi_trend_continuation`
(`stop_atr_mult` + `target_r`): el STOP se mide en ATR (volatilidad
reciente), y el TP se mide como un MULTIPLO DEL STOP (`target_r`) -- asi
que el TP tambien es volatility-adaptive de forma automatica (si el stop
se ensancha porque el mercado esta mas volatil, el TP se ensancha con el,
manteniendo la misma proporcion de riesgo/recompensa). No hizo falta un
indicador nuevo: `regime.atr` ya existe y ya lo expone el motor
(`ind["atr"]`) para cualquier estrategia.

`target_r` escala con el tamaño de la cascada ("mas contratos liquidados
= rebote mas agresivo") -- entre `target_r_min` (cascada apenas sobre el
umbral) y `target_r_max` (cascada mucho mayor), escalado logaritmico.

Decisiones de diseño:
* Trigger: SUMA de eventos reales del mismo lado dentro de la barra
  (cascada, no solo el evento mas grande -- Binance throttla el stream).
* El apalancamiento de las posiciones liquidadas no es observable en el
  stream real -- proxy: tamaño de la cascada en USD (`min_quote_qty_usd`,
  barrido en un rango, no fijo -- ver `backtest/sweep_liquidation_bounce.py`).
* Entrada escalonada en 2-3 partes: sigue sin implementar (requeriria un
  cambio de motor).
"""
from __future__ import annotations

import math
from typing import Optional

from ..engine import BacktestContext, TargetPosition

DEFAULT_SIZE_USD = 500.0
# Umbral de referencia cercano al percentil alto del tamaño de evento
# real observado para BTCUSDT -- no es el UNICO valor probado, ver el
# sweep en `backtest/sweep_liquidation_bounce.py`.
DEFAULT_MIN_QUOTE_QTY_USD = 200_000.0
# Calibrado contra la propia distribucion de ATR del simbolo (no contra
# el resultado de un backtest ya corrido, para no deslizarse a "ajustar
# hasta que pase"): un multiplicador mas chico (ej. el default que ya
# usan `rsi_pullback`/`oi_trend_continuation`) da un stop demasiado
# ajustado para la volatilidad tipica de este simbolo/intervalo. Este
# valor SI se ensancha/achica con la volatilidad real del momento, en
# vez de ser una distancia fija sin importar el contexto.
DEFAULT_STOP_ATR_MULT = 7.5
DEFAULT_TARGET_R_MIN = 1.0
DEFAULT_TARGET_R_MAX = 2.5
# log10(ratio) * este factor + target_r_min = target_r objetivo, saturado
# en target_r_max. Calibrado para que un evento varios ordenes de
# magnitud por encima del umbral llegue cerca de target_r_max, en
# unidades de R.
DEFAULT_TARGET_R_SCALE = 0.75


def _cascade_notional(events: list, side: str) -> float:
    return sum(e.quote_qty_usd for e in events if e.side == side)


def _target_r(cascade_usd: float, min_qty: float, r_min: float, r_max: float, r_scale: float) -> float:
    ratio = max(cascade_usd / min_qty, 1.0)
    scale = math.log10(ratio)
    return min(r_max, r_min + scale * r_scale)


class LiquidationBounceLong:
    direction = "long"
    title = "Rebote de Liquidacion Real (Largo)"
    description = (
        "Entra en LARGO cuando el grabador registra una liquidacion (o "
        "cascada) REAL de posiciones LARGAS considerable en la barra "
        "actual -- apuesta al REBOTE tras la cascada. Stop en multiplos de "
        "ATR (volatilidad reciente), objetivo como multiplo del riesgo "
        "(target_r) que ademas escala con el tamaño de la cascada -- SL y "
        "TP se ensanchan juntos si el mercado esta mas volatil. Trigger "
        "REAL, no el heatmap modelado."
    )
    needs_real_liquidations = True

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD, min_quote_qty_usd: float = DEFAULT_MIN_QUOTE_QTY_USD,
        stop_atr_mult: float = DEFAULT_STOP_ATR_MULT, target_r_min: float = DEFAULT_TARGET_R_MIN,
        target_r_max: float = DEFAULT_TARGET_R_MAX, target_r_scale: float = DEFAULT_TARGET_R_SCALE,
    ):
        self.size_usd = size_usd
        self.min_quote_qty_usd = min_quote_qty_usd
        self.stop_atr_mult = stop_atr_mult
        self.target_r_min = target_r_min
        self.target_r_max = target_r_max
        self.target_r_scale = target_r_scale

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "min_quote_qty_usd": self.min_quote_qty_usd,
            "stop_atr_mult": self.stop_atr_mult, "target_r_min": self.target_r_min,
            "target_r_max": self.target_r_max, "target_r_scale": self.target_r_scale,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        events = ctx.indicators.get("real_liquidations_this_bar", [])
        cascade = _cascade_notional(events, "long")
        if cascade < self.min_quote_qty_usd:
            return None
        atr = ctx.indicators["atr"]
        if atr <= 0:
            return None
        price = ctx.bar.close
        stop_dist = self.stop_atr_mult * atr
        target_r = _target_r(cascade, self.min_quote_qty_usd, self.target_r_min, self.target_r_max, self.target_r_scale)
        return TargetPosition(
            "long", self.size_usd,
            stop_loss_price=price - stop_dist,
            take_profit_price=price + stop_dist * target_r,
        )

    def live_status(self, snapshot: dict) -> dict:
        cascade = snapshot.get("recent_long_liquidation_usd")
        if cascade is None:
            return {"state": "sin_datos", "detail": "Sin liquidaciones reales recientes disponibles."}
        if cascade >= self.min_quote_qty_usd:
            return {
                "state": "condiciones_cumplidas",
                "detail": f"Cascada reciente de liquidacion de largos: ${cascade:,.0f} (>= ${self.min_quote_qty_usd:,.0f}).",
            }
        return {
            "state": "analizando",
            "detail": f"Sin cascada considerable todavia (ultima: ${cascade:,.0f}, umbral ${self.min_quote_qty_usd:,.0f}).",
        }


class LiquidationBounceShort:
    direction = "short"
    title = "Rebote de Liquidacion Real (Corto)"
    description = (
        "Espejo de 'Rebote de Liquidacion Real (Largo)': entra en CORTO "
        "cuando el grabador registra una cascada REAL de liquidacion de "
        "posiciones CORTAS considerable -- apuesta a la caida despues del "
        "short squeeze. Mismo diseño de SL/TP en ATR + target_r escalado "
        "por tamaño de cascada."
    )
    needs_real_liquidations = True

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD, min_quote_qty_usd: float = DEFAULT_MIN_QUOTE_QTY_USD,
        stop_atr_mult: float = DEFAULT_STOP_ATR_MULT, target_r_min: float = DEFAULT_TARGET_R_MIN,
        target_r_max: float = DEFAULT_TARGET_R_MAX, target_r_scale: float = DEFAULT_TARGET_R_SCALE,
    ):
        self.size_usd = size_usd
        self.min_quote_qty_usd = min_quote_qty_usd
        self.stop_atr_mult = stop_atr_mult
        self.target_r_min = target_r_min
        self.target_r_max = target_r_max
        self.target_r_scale = target_r_scale

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "min_quote_qty_usd": self.min_quote_qty_usd,
            "stop_atr_mult": self.stop_atr_mult, "target_r_min": self.target_r_min,
            "target_r_max": self.target_r_max, "target_r_scale": self.target_r_scale,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        events = ctx.indicators.get("real_liquidations_this_bar", [])
        cascade = _cascade_notional(events, "short")
        if cascade < self.min_quote_qty_usd:
            return None
        atr = ctx.indicators["atr"]
        if atr <= 0:
            return None
        price = ctx.bar.close
        stop_dist = self.stop_atr_mult * atr
        target_r = _target_r(cascade, self.min_quote_qty_usd, self.target_r_min, self.target_r_max, self.target_r_scale)
        return TargetPosition(
            "short", self.size_usd,
            stop_loss_price=price + stop_dist,
            take_profit_price=price - stop_dist * target_r,
        )

    def live_status(self, snapshot: dict) -> dict:
        cascade = snapshot.get("recent_short_liquidation_usd")
        if cascade is None:
            return {"state": "sin_datos", "detail": "Sin liquidaciones reales recientes disponibles."}
        if cascade >= self.min_quote_qty_usd:
            return {
                "state": "condiciones_cumplidas",
                "detail": f"Cascada reciente de liquidacion de cortos: ${cascade:,.0f} (>= ${self.min_quote_qty_usd:,.0f}).",
            }
        return {
            "state": "analizando",
            "detail": f"Sin cascada considerable todavia (ultima: ${cascade:,.0f}, umbral ${self.min_quote_qty_usd:,.0f}).",
        }
