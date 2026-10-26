"""Primera estrategia que usa el modulo SMC (`backtest/smc.py` +
`backtest/htf_context.py`): "tendencia" + Smart Money Concepts de una
temporalidad MAYOR como CONTEXTO de una entrada de 5m, nunca una
estrategia de 1h en si misma (`needs_htf_context = True`, ver
`engine.run_backtest`).

Premisa: un movimiento de 5m que reacciona (vela de confirmacion en la
DIRECCION del contexto) dentro de una zona de Order Block de 1h TODAVIA
VIGENTE (`htf_context.zone_signal_at`), Y en la MISMA direccion que la
ultima estructura de 1h (BOS/ChoCH, `htf_context.trend_at`), tiene mas
probabilidad de continuar que una entrada de 5m sin ese contexto -- la
idea central de "smart money concepts": el precio reacciona en zonas
donde quedaron ordenes institucionales sin ejecutar, no en niveles
arbitrarios (ver docstring de `smc.py` para la advertencia de que esto es
una INTERPRETACION MECANICA, no "la" definicion unica del concepto).

Requiere LAS DOS condiciones de temporalidad mayor juntas (tendencia Y
zona), no una sola -- una zona sin tendencia a favor no da ventaja
direccional (es tan valido operar en contra como a favor), y una
tendencia sin una zona de reaccion concreta es un sesgo, no un punto de
entrada.

SL/TP: mismo patron ATR que el resto de las estrategias
(`stop_atr_mult`/`target_r`) -- ninguna estrategia nueva de este proyecto
inventa un numero de riesgo propio sin motivo. `take_profit = target_r *
distancia_del_stop`, asi que TP se ensancha/achica junto con el stop
automaticamente (ver docstring de `liquidation_bounce.py`, que usa el
mismo mecanismo).

PENDIENTE (fuera de alcance de este cambio): `live_status` degrada a
"sin_datos" porque el snapshot en vivo de `api/routers/backtest.py`
todavia no calcula `htf_trend`/`htf_zone_signal` -- esta estrategia SI
funciona en el backtest (`engine.run_backtest` con `htf_interval`), falta
conectar el mismo calculo al lado en vivo para que la vista "Bots"
muestre su estado real."""
from __future__ import annotations

from typing import Optional

from ..engine import BacktestContext, TargetPosition

DEFAULT_SIZE_USD = 500.0
DEFAULT_STOP_ATR_MULT = 2.0
DEFAULT_TARGET_R = 1.5
# Mismo techo que volume_breakout.py -- $100 de capital a 100x, el maximo
# real de Binance para casi todos estos simbolos.
DEFAULT_MAX_SIZE_USD = 10_000.0


def _sized_position(price: float, stop_dist: float, size_usd: float,
                     risk_usd_target: Optional[float], max_size_usd: float) -> float:
    if risk_usd_target is None:
        return size_usd
    if stop_dist <= 0:
        return 0.0
    return min(risk_usd_target * price / stop_dist, max_size_usd)


class SmcConfluenceLong:
    direction = "long"
    title = "Confluencia SMC (Largo)"
    description = (
        "Entra en LARGO en 5m cuando (1) la ultima estructura de 1h "
        "(BOS/ChoCH) rompio hacia ARRIBA, (2) el precio esta dentro de un "
        "Order Block alcista de 1h todavia vigente (no roto), y (3) la "
        "propia vela de 5m cierra por encima de su apertura (confirmacion "
        "de reaccion, no solo estar dentro de la zona). Las tres "
        "condiciones juntas, no una sola -- ver docstring del modulo."
    )
    needs_htf_context = True

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD, stop_atr_mult: float = DEFAULT_STOP_ATR_MULT,
        target_r: float = DEFAULT_TARGET_R, risk_usd_target: Optional[float] = None,
        max_size_usd: float = DEFAULT_MAX_SIZE_USD,
    ):
        self.size_usd = size_usd
        self.stop_atr_mult = stop_atr_mult
        self.target_r = target_r
        self.risk_usd_target = risk_usd_target
        self.max_size_usd = max_size_usd

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "stop_atr_mult": self.stop_atr_mult,
            "target_r": self.target_r, "risk_usd_target": self.risk_usd_target,
            "max_size_usd": self.max_size_usd,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        if ctx.indicators["htf_trend"] != "up":
            return None
        if ctx.indicators["htf_zone_signal"] != "bullish":
            return None
        if ctx.bar.close <= ctx.bar.open:
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
        trend = snapshot.get("htf_trend")
        zone = snapshot.get("htf_zone_signal")
        if trend is None or zone is None:
            return {"state": "sin_datos", "detail": "Contexto de 1h (SMC) todavia no disponible en vivo."}
        if trend == "up" and zone == "bullish":
            return {"state": "condiciones_cumplidas", "detail": "Tendencia 1h alcista + zona de Order Block vigente."}
        return {
            "state": "analizando",
            "detail": f"Esperando tendencia 1h alcista + zona bullish (actual: trend={trend}, zona={zone}).",
        }


class SmcConfluenceShort:
    direction = "short"
    title = "Confluencia SMC (Corto)"
    description = (
        "Espejo de 'Confluencia SMC (Largo)': entra en CORTO en 5m cuando "
        "la ultima estructura de 1h rompio hacia ABAJO, el precio esta "
        "dentro de un Order Block bajista de 1h todavia vigente, y la "
        "vela de 5m cierra por debajo de su apertura."
    )
    needs_htf_context = True

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD, stop_atr_mult: float = DEFAULT_STOP_ATR_MULT,
        target_r: float = DEFAULT_TARGET_R, risk_usd_target: Optional[float] = None,
        max_size_usd: float = DEFAULT_MAX_SIZE_USD,
    ):
        self.size_usd = size_usd
        self.stop_atr_mult = stop_atr_mult
        self.target_r = target_r
        self.risk_usd_target = risk_usd_target
        self.max_size_usd = max_size_usd

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "stop_atr_mult": self.stop_atr_mult,
            "target_r": self.target_r, "risk_usd_target": self.risk_usd_target,
            "max_size_usd": self.max_size_usd,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        if ctx.indicators["htf_trend"] != "down":
            return None
        if ctx.indicators["htf_zone_signal"] != "bearish":
            return None
        if ctx.bar.close >= ctx.bar.open:
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
        trend = snapshot.get("htf_trend")
        zone = snapshot.get("htf_zone_signal")
        if trend is None or zone is None:
            return {"state": "sin_datos", "detail": "Contexto de 1h (SMC) todavia no disponible en vivo."}
        if trend == "down" and zone == "bearish":
            return {"state": "condiciones_cumplidas", "detail": "Tendencia 1h bajista + zona de Order Block vigente."}
        return {
            "state": "analizando",
            "detail": f"Esperando tendencia 1h bajista + zona bearish (actual: trend={trend}, zona={zone}).",
        }
