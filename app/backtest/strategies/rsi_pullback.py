"""Premisa, deliberadamente MINIMA en piezas nuevas: se añade UN solo
indicador nuevo (`ta.rsi`, ver su docstring sobre por que no EMA+MACD
tambien) y se reutiliza TODO lo que ya existe y esta probado -- el
regimen de `regime.classify_regime` (no un cruce de EMA nuevo, que seria
una segunda forma de medir lo mismo) y el ATR para SL/TP (igual que
`oi_trend_continuation`). La diferencia de fondo con `oi_trend_continuation`
no es "mas indicadores": es CUANDO entra dentro de la tendencia.
`oi_trend_continuation` entra persiguiendo apalancamiento nuevo mientras
la tendencia ya esta en marcha (entrada tarde, en la parte mas cara del
movimiento). Esta entra en el RETROCESO dentro de una tendencia ya
confirmada (RSI cae a zona de sobreventa/sobrecompra MODERADA, no
extrema -- en una tendencia fuerte el RSI rara vez toca 30, suele
encontrar soporte cerca de 40-50, hallazgo de TA clasico, no inventado
aqui) -- mejor precio de entrada, mismo filtro de tendencia que ya
existe.

`max_funding_zscore` es el unico lugar donde este proyecto aporta algo
que un sistema de TA puro no tiene: filtro OPCIONAL (por defecto
practicamente apagado, sweepable) que bloquea la entrada si el
posicionamiento de derivados ya esta extremo A FAVOR del lado que se
quiere abrir (funding muy positivo = largos ya apretados = mal momento
para sumarse a mas largos, aunque el precio y el RSI digan que si). Es
la pregunta que de verdad vale la pena que este backtest conteste:
¿el filtro de derivados mejora un sistema de TA clasico, o no aporta
nada? -- no "¿funciona RSI?", que ya se sabe de sobra que funciona a
veces y falla otras en cualquier mercado."""
from __future__ import annotations

from typing import Optional

from ..engine import BacktestContext, TargetPosition

DEFAULT_SIZE_USD = 500.0


class RsiPullbackLong:
    direction = "long"
    title = "Retroceso RSI en Tendencia (Largo)"
    description = (
        "Entra en LARGO cuando el precio ya esta en tendencia alcista "
        "(regimen 'trending_up', igual que las demas estrategias de esta "
        "fase) Y el RSI(14) acaba de caer a una zona de sobreventa MODERADA "
        "-- un retroceso dentro de la tendencia, no perseguirla en caliente. "
        "Filtro opcional: no entra si el funding ya esta extremo a favor de "
        "los largos (posicionamiento apretado). Stop y objetivo en multiplos "
        "de ATR, igual que las demas estrategias del motor."
    )

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD, rsi_oversold: float = 40.0,
        max_funding_zscore: float = 3.0, stop_atr_mult: float = 2.0, target_r: float = 1.5,
    ):
        self.size_usd = size_usd
        self.rsi_oversold = rsi_oversold
        self.max_funding_zscore = max_funding_zscore
        self.stop_atr_mult = stop_atr_mult
        self.target_r = target_r

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "rsi_oversold": self.rsi_oversold,
            "max_funding_zscore": self.max_funding_zscore,
            "stop_atr_mult": self.stop_atr_mult, "target_r": self.target_r,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        if ctx.indicators["regime"] != "trending_up":
            return None
        if ctx.indicators["rsi"] > self.rsi_oversold:
            return None
        if ctx.indicators["funding_zscore"] > self.max_funding_zscore:
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
        regime = snapshot.get("regime")
        funding_zscore = snapshot.get("funding_zscore")
        if rsi_val is None or regime is None:
            return {"state": "sin_datos", "detail": "Sin RSI/regimen en vivo suficiente todavia."}
        trend_ok = regime == "trending_up"
        rsi_ok = rsi_val <= self.rsi_oversold
        funding_ok = funding_zscore is None or funding_zscore <= self.max_funding_zscore
        if trend_ok and rsi_ok and funding_ok:
            return {
                "state": "condiciones_cumplidas",
                "detail": (
                    f"Regimen trending_up con RSI(14) en {rsi_val:.1f} "
                    f"(<= {self.rsi_oversold}) -- condiciones de entrada LARGO cumplidas."
                ),
            }
        return {
            "state": "analizando",
            "detail": (
                f"Esperando regimen trending_up (actual: {regime}) con RSI(14) "
                f"<= {self.rsi_oversold} (actual: {rsi_val:.1f})."
            ),
        }


class RsiPullbackShort:
    direction = "short"
    title = "Retroceso RSI en Tendencia (Corto)"
    description = (
        "Espejo de 'Retroceso RSI en Tendencia (Largo)': entra en CORTO "
        "cuando el precio esta en tendencia bajista (regimen 'trending_down') "
        "Y el RSI(14) acaba de subir a una zona de sobrecompra moderada -- un "
        "rebote dentro de la tendencia bajista, no perseguirla en caliente. "
        "Mismo filtro opcional de funding (extremo negativo) y mismo SL/TP "
        "en multiplos de ATR."
    )

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD, rsi_overbought: float = 60.0,
        max_funding_zscore: float = 3.0, stop_atr_mult: float = 2.0, target_r: float = 1.5,
    ):
        self.size_usd = size_usd
        self.rsi_overbought = rsi_overbought
        self.max_funding_zscore = max_funding_zscore
        self.stop_atr_mult = stop_atr_mult
        self.target_r = target_r

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "rsi_overbought": self.rsi_overbought,
            "max_funding_zscore": self.max_funding_zscore,
            "stop_atr_mult": self.stop_atr_mult, "target_r": self.target_r,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        if ctx.indicators["regime"] != "trending_down":
            return None
        if ctx.indicators["rsi"] < self.rsi_overbought:
            return None
        if ctx.indicators["funding_zscore"] < -self.max_funding_zscore:
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
        regime = snapshot.get("regime")
        funding_zscore = snapshot.get("funding_zscore")
        if rsi_val is None or regime is None:
            return {"state": "sin_datos", "detail": "Sin RSI/regimen en vivo suficiente todavia."}
        trend_ok = regime == "trending_down"
        rsi_ok = rsi_val >= self.rsi_overbought
        funding_ok = funding_zscore is None or funding_zscore >= -self.max_funding_zscore
        if trend_ok and rsi_ok and funding_ok:
            return {
                "state": "condiciones_cumplidas",
                "detail": (
                    f"Regimen trending_down con RSI(14) en {rsi_val:.1f} "
                    f"(>= {self.rsi_overbought}) -- condiciones de entrada CORTO cumplidas."
                ),
            }
        return {
            "state": "analizando",
            "detail": (
                f"Esperando regimen trending_down (actual: {regime}) con RSI(14) "
                f">= {self.rsi_overbought} (actual: {rsi_val:.1f})."
            ),
        }
