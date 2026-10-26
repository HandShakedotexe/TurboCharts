"""Etapa 2 del laboratorio de estrategias -- candidato que sale DIRECTO
del screen de Etapa 1 (`backtest/screen.py`, information coefficient de
momentum/RSI contra retorno futuro), no de una hipotesis previa:
reversion, no continuacion, es la hipotesis que el screen respalda mejor
en este universo, a diferencia de `rsi_pullback.py` (la premisa anterior)
que ya apostaba a reversion pero DENTRO de una tendencia confirmada
(regimen trending_up/down).

Diferencia de fondo con `rsi_pullback`: SIN filtro de regimen, RSI mas
EXTREMO (25/75, no la zona moderada 40/60 de `rsi_pullback`, que existe
precisamente porque no filtra ya por tendencia primero) y sin el filtro
opcional de funding (esta version prueba la premisa de precio/RSI sola,
sin mezclar una pregunta de derivados en el mismo backtest -- si hace
falta, se prueba por separado via ablation, no desde el arranque)."""
from __future__ import annotations

from typing import Optional

from ..engine import BacktestContext, TargetPosition

DEFAULT_SIZE_USD = 500.0


class RsiReversionLong:
    direction = "long"
    title = "Reversion RSI Extrema (Largo)"
    description = (
        "Entra en LARGO cuando el RSI(14) cae a zona de sobreventa EXTREMA "
        "(<=25), sin condicionar a ningun regimen de tendencia -- apuesta a "
        "que el precio corrigio de mas y revierte, no a que siga una "
        "tendencia ya en marcha. Sale del screen de Etapa 1 (information "
        "coefficient de momentum/RSI). Stop y objetivo en multiplos de ATR."
    )

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD, rsi_oversold: float = 25.0,
        stop_atr_mult: float = 2.0, target_r: float = 1.5,
    ):
        self.size_usd = size_usd
        self.rsi_oversold = rsi_oversold
        self.stop_atr_mult = stop_atr_mult
        self.target_r = target_r

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "rsi_oversold": self.rsi_oversold,
            "stop_atr_mult": self.stop_atr_mult, "target_r": self.target_r,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        if ctx.indicators["rsi"] > self.rsi_oversold:
            return None
        atr = ctx.indicators["atr"]
        if atr <= 0:
            return None
        price = ctx.bar.close
        stop_dist = self.stop_atr_mult * atr
        return TargetPosition(
            "long", self.size_usd,
            stop_loss_price=price - stop_dist,
            take_profit_price=price + stop_dist * self.target_r,
        )

    def live_status(self, snapshot: dict) -> dict:
        rsi_val = snapshot.get("rsi")
        if rsi_val is None:
            return {"state": "sin_datos", "detail": "Sin RSI en vivo suficiente todavia."}
        if rsi_val <= self.rsi_oversold:
            return {
                "state": "condiciones_cumplidas",
                "detail": f"RSI(14) en {rsi_val:.1f} (<= {self.rsi_oversold}) -- entrada LARGO cumplida.",
            }
        return {
            "state": "analizando",
            "detail": f"Esperando RSI(14) <= {self.rsi_oversold} (actual: {rsi_val:.1f}).",
        }


class RsiReversionShort:
    direction = "short"
    title = "Reversion RSI Extrema (Corto)"
    description = (
        "Espejo de 'Reversion RSI Extrema (Largo)': entra en CORTO cuando "
        "el RSI(14) sube a zona de sobrecompra EXTREMA (>=75), sin "
        "condicionar a ningun regimen. Mismo SL/TP en multiplos de ATR."
    )

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD, rsi_overbought: float = 75.0,
        stop_atr_mult: float = 2.0, target_r: float = 1.5,
    ):
        self.size_usd = size_usd
        self.rsi_overbought = rsi_overbought
        self.stop_atr_mult = stop_atr_mult
        self.target_r = target_r

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "rsi_overbought": self.rsi_overbought,
            "stop_atr_mult": self.stop_atr_mult, "target_r": self.target_r,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        if ctx.indicators["rsi"] < self.rsi_overbought:
            return None
        atr = ctx.indicators["atr"]
        if atr <= 0:
            return None
        price = ctx.bar.close
        stop_dist = self.stop_atr_mult * atr
        return TargetPosition(
            "short", self.size_usd,
            stop_loss_price=price + stop_dist,
            take_profit_price=price - stop_dist * self.target_r,
        )

    def live_status(self, snapshot: dict) -> dict:
        rsi_val = snapshot.get("rsi")
        if rsi_val is None:
            return {"state": "sin_datos", "detail": "Sin RSI en vivo suficiente todavia."}
        if rsi_val >= self.rsi_overbought:
            return {
                "state": "condiciones_cumplidas",
                "detail": f"RSI(14) en {rsi_val:.1f} (>= {self.rsi_overbought}) -- entrada CORTO cumplida.",
            }
        return {
            "state": "analizando",
            "detail": f"Esperando RSI(14) >= {self.rsi_overbought} (actual: {rsi_val:.1f}).",
        }
