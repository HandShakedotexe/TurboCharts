"""Familia deliberadamente NUEVA y no correlacionada con `rsi_pullback`/
`rsi_reversion`/`oi_trend_continuation` (todas RSI/regimen/derivados):
Volumen + Estructura de precio (Breakout).

Premisa: un cierre que ROMPE el rango de las ultimas 20 barras (canal de
Donchian, el default clasico, no un numero ajustado a resultados)
respaldado por un volumen inusualmente alto (z-score >= umbral) tiene mas
probabilidad de ser un movimiento real que un quiebre en baja
participacion (fakeout). Motivo para exigir las dos condiciones juntas,
no una sola: un breakout de precio SIN volumen es la señal clasica de
"nadie le esta metiendo dinero a esto", y un pico de volumen SIN romper
el rango no es una estructura de entrada (es solo actividad).

SL/TP: mismo patron ATR que usan `rsi_pullback`/`oi_trend_continuation`
(`stop_atr_mult`/`target_r`) -- mismos defaults (2.0/1.5), no numeros
nuevos elegidos para esta estrategia en particular.

Sizing: con SL/TP en ATR, un notional FIJO (`size_usd` constante en todo
trade) significa que las barras mas volatiles arriesgan MAS dolares por
trade sin que la estrategia lo decida -- el riesgo en R puede verse bien
mientras el riesgo real en USD varia de trade a trade de forma no
controlada, justo lo opuesto de lo que busca un position sizing
"volatility-adjusted". `risk_usd_target` (opcional): en vez de notional
fijo, arriesga un monto en DOLARES fijo por trade --
`size_usd = risk_usd_target * price / stop_dist`, mas grande cuando el
stop es angosto (baja volatilidad), mas chico cuando el stop es ancho
(alta volatilidad) -- el riesgo en USD queda CONSTANTE de trade a trade.
`max_size_usd` evita que el notional explote si el ATR es casi cero
(calentamiento o un periodo anormalmente plano)."""
from __future__ import annotations

from typing import Optional

from ..engine import BacktestContext, TargetPosition

DEFAULT_SIZE_USD = 500.0
DEFAULT_VOLUME_ZSCORE_THRESHOLD = 2.0
DEFAULT_STOP_ATR_MULT = 2.0
DEFAULT_TARGET_R = 1.5
# $100 de capital a 100x (el maximo real de Binance para casi todos estos
# simbolos) = $10,000 de notional -- techo duro para el sizing por riesgo,
# no un numero arbitrario.
DEFAULT_MAX_SIZE_USD = 10_000.0


def _sized_position(price: float, stop_dist: float, size_usd: float,
                     risk_usd_target: Optional[float], max_size_usd: float) -> float:
    if risk_usd_target is None:
        return size_usd
    if stop_dist <= 0:
        return 0.0
    return min(risk_usd_target * price / stop_dist, max_size_usd)


class VolumeBreakoutLong:
    direction = "long"
    title = "Ruptura con Volumen (Largo)"
    description = (
        "Entra en LARGO cuando el cierre rompe el maximo de las ultimas 20 "
        "barras (canal de Donchian) Y el volumen de esa barra es "
        "inusualmente alto (z-score >= 2 contra su propia ventana movil de "
        "60 barras) -- un quiebre de rango respaldado por participacion "
        "real, no un movimiento en baja liquidez. Familia nueva (Volumen + "
        "Estructura), sin RSI ni derivados de por medio."
    )

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD, volume_zscore_threshold: float = DEFAULT_VOLUME_ZSCORE_THRESHOLD,
        stop_atr_mult: float = DEFAULT_STOP_ATR_MULT, target_r: float = DEFAULT_TARGET_R,
        risk_usd_target: Optional[float] = None, max_size_usd: float = DEFAULT_MAX_SIZE_USD,
        require_trend_regime: bool = False,
    ):
        self.size_usd = size_usd
        self.volume_zscore_threshold = volume_zscore_threshold
        self.stop_atr_mult = stop_atr_mult
        self.target_r = target_r
        self.risk_usd_target = risk_usd_target
        self.max_size_usd = max_size_usd
        self.require_trend_regime = require_trend_regime

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "volume_zscore_threshold": self.volume_zscore_threshold,
            "stop_atr_mult": self.stop_atr_mult, "target_r": self.target_r,
            "risk_usd_target": self.risk_usd_target, "max_size_usd": self.max_size_usd,
            "require_trend_regime": self.require_trend_regime,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        if ctx.bar.close <= ctx.indicators["prior_max_high_20"]:
            return None
        if ctx.indicators["volume_zscore_60"] < self.volume_zscore_threshold:
            return None
        if self.require_trend_regime and ctx.indicators["regime"] != "trending_up":
            return None
        atr = ctx.indicators["atr"]
        if atr <= 0:
            return None
        price = ctx.bar.close
        stop_dist = self.stop_atr_mult * atr
        size = _sized_position(price, stop_dist, self.size_usd, self.risk_usd_target, self.max_size_usd)
        if size <= 0:
            return None
        return TargetPosition(
            "long", size,
            stop_loss_price=price - stop_dist,
            take_profit_price=price + stop_dist * self.target_r,
        )

    def live_status(self, snapshot: dict) -> dict:
        vol_z = snapshot.get("volume_zscore_60")
        breakout = snapshot.get("broke_prior_high_20")
        if vol_z is None or breakout is None:
            return {"state": "sin_datos", "detail": "Sin volumen/rango previo suficiente todavia."}
        if breakout and vol_z >= self.volume_zscore_threshold:
            return {
                "state": "condiciones_cumplidas",
                "detail": f"Ruptura de rango con volumen z={vol_z:.1f} (>= {self.volume_zscore_threshold}).",
            }
        return {
            "state": "analizando",
            "detail": f"Esperando ruptura del rango de 20 barras con volumen z >= {self.volume_zscore_threshold} (actual: {vol_z:.1f}).",
        }


class VolumeBreakoutShort:
    direction = "short"
    title = "Ruptura con Volumen (Corto)"
    description = (
        "Espejo de 'Ruptura con Volumen (Largo)': entra en CORTO cuando el "
        "cierre rompe el minimo de las ultimas 20 barras con volumen "
        "inusualmente alto -- una quiebre BAJISTA respaldado por "
        "participacion real."
    )

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD, volume_zscore_threshold: float = DEFAULT_VOLUME_ZSCORE_THRESHOLD,
        stop_atr_mult: float = DEFAULT_STOP_ATR_MULT, target_r: float = DEFAULT_TARGET_R,
        risk_usd_target: Optional[float] = None, max_size_usd: float = DEFAULT_MAX_SIZE_USD,
        require_trend_regime: bool = False,
    ):
        self.size_usd = size_usd
        self.volume_zscore_threshold = volume_zscore_threshold
        self.stop_atr_mult = stop_atr_mult
        self.target_r = target_r
        self.risk_usd_target = risk_usd_target
        self.max_size_usd = max_size_usd
        self.require_trend_regime = require_trend_regime

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "volume_zscore_threshold": self.volume_zscore_threshold,
            "stop_atr_mult": self.stop_atr_mult, "target_r": self.target_r,
            "risk_usd_target": self.risk_usd_target, "max_size_usd": self.max_size_usd,
            "require_trend_regime": self.require_trend_regime,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        if ctx.bar.close >= ctx.indicators["prior_min_low_20"]:
            return None
        if ctx.indicators["volume_zscore_60"] < self.volume_zscore_threshold:
            return None
        if self.require_trend_regime and ctx.indicators["regime"] != "trending_down":
            return None
        atr = ctx.indicators["atr"]
        if atr <= 0:
            return None
        price = ctx.bar.close
        stop_dist = self.stop_atr_mult * atr
        size = _sized_position(price, stop_dist, self.size_usd, self.risk_usd_target, self.max_size_usd)
        if size <= 0:
            return None
        return TargetPosition(
            "short", size,
            stop_loss_price=price + stop_dist,
            take_profit_price=price - stop_dist * self.target_r,
        )

    def live_status(self, snapshot: dict) -> dict:
        vol_z = snapshot.get("volume_zscore_60")
        breakdown = snapshot.get("broke_prior_low_20")
        if vol_z is None or breakdown is None:
            return {"state": "sin_datos", "detail": "Sin volumen/rango previo suficiente todavia."}
        if breakdown and vol_z >= self.volume_zscore_threshold:
            return {
                "state": "condiciones_cumplidas",
                "detail": f"Ruptura bajista de rango con volumen z={vol_z:.1f} (>= {self.volume_zscore_threshold}).",
            }
        return {
            "state": "analizando",
            "detail": f"Esperando ruptura bajista del rango de 20 barras con volumen z >= {self.volume_zscore_threshold} (actual: {vol_z:.1f}).",
        }
