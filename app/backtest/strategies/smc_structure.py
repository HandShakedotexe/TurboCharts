"""1h/4h como temporalidades de trading validas por si mismas, no solo
como contexto. Angulo: ESTRUCTURA DE PRECIO (Smart Money Concepts --
`smc.py`/`htf_context.py`) calculada y OPERADA sobre la MISMA vela que se
tradea, no como filtro de una temporalidad menor.

Distincion real con `smc_confluence.py`: esa estrategia entra en 5m
usando 1h solo como contexto (tendencia + zona de 1h, confirmacion en la
vela de 5m). Aqui `needs_htf_context=True` sigue siendo el mecanismo que
carga la
segunda serie de barras, pero se pide la MISMA temporalidad que la de
trading (`--interval 1h --htf-interval 1h` en el CLI, o 4h/4h) -- con las
dos series identicas, `htf_idx` en `engine.run_backtest_on_series` cae
siempre en la barra ACTUAL (mismo `time_ms`), asi que `htf_trend`/
`htf_zone_signal` terminan describiendo la estructura de la propia vela
que la estrategia esta operando, no de una vela mayor. No hizo falta
tocar `engine.py`: el mecanismo de "temporalidad mayor" ya es generico
sobre CUALQUIER segunda serie que el caller pida, con cualquier
temporalidad -- usarlo con la MISMA temporalidad es una combinacion valida
que el motor ya soporta, no una extension nueva.

Limitacion real, heredada de que `ctx.indicators` (poblado por
`engine.py`, que no se toca en este cambio) solo expone `htf_trend`
(direccion del ULTIMO `StructureEvent` visible) y `htf_zone_signal`
(bullish/bearish/None si el precio esta dentro de un Order Block activo) --
no un flag explicito de "un evento ocurrio EN ESTA barra" ni de Fair Value
Gaps (`smc.fair_value_gaps` esta construido pero el motor no lo expone
todavia). Las tres variantes de abajo se diseñaron para funcionar SOLO con
esas dos señales + el propio estado interno de la estrategia (que persiste
entre barras de una misma corrida, una instancia nueva por corrida/grid
point -- ver `strategy_factory` en validation.py):

1. **Confluencia estandar** (`SmcStructureTrendZoneLong/Short`): igual
   regla que `smc_confluence` (tendencia + zona + vela de confirmacion),
   pero sobre su PROPIA temporalidad -- dispara en CUALQUIER barra donde
   la condicion se sostenga (no solo en la primera), mismo criterio que la
   version original.
2. **Reversion de estructura (ChoCH)** (`SmcStructureChochReversalLong/
   Short`): dispara SOLO en la barra donde `htf_trend` CAMBIA de
   direccion (de "down"/None a "up", o viceversa) -- una transicion de
   `htf_trend` es, por construccion de `smc.structure_events`
   (`kind="ChoCH"` cuando el rompimiento es CONTRARIO a la tendencia
   vigente), exactamente un evento ChoCH real, nunca un BOS de
   continuacion (que no cambia la direccion ya vigente y por lo tanto no
   dispara una transicion). No exige zona -- es la señal de reversion MAS
   TEMPRANA posible con lo que el motor expone hoy, antes de que se forme
   ningun Order Block que mitigar.
3. **Mitigacion fresca de zona** (`SmcStructureFreshMitigationLong/
   Short`): dispara SOLO en la barra donde `htf_zone_signal` CAMBIA a
   "bullish"/"bearish" (el precio ACABA de entrar a una zona activa que
   un instante antes no lo contenia -- la version mecanica de "mitigacion
   de un Order Block FRESCO", ya que `OrderBlock.mitigated_at_index` en si
   no esta expuesto en `ctx.indicators`), exigiendo ADEMAS que la
   tendencia este alineada (`htf_trend` en la misma direccion) -- a
   diferencia de (1), que dispara en CADA barra que la condicion se
   sostiene, esta variante dispara una sola vez por "visita" a la zona.

Las tres comparten el mismo patron SL/TP en ATR que el resto de las
estrategias (`stop_atr_mult`/`target_r`) -- ninguna inventa un numero de
riesgo nuevo sin motivo, mismo criterio que `smc_confluence.py`/
`cvd_divergence.py`.
"""
from __future__ import annotations

from typing import Optional

from ..engine import BacktestContext, TargetPosition

DEFAULT_SIZE_USD = 500.0
DEFAULT_STOP_ATR_MULT = 2.0
DEFAULT_TARGET_R = 1.5
DEFAULT_MAX_SIZE_USD = 10_000.0


def _sized_position(price: float, stop_dist: float, size_usd: float,
                     risk_usd_target: Optional[float], max_size_usd: float) -> float:
    if risk_usd_target is None:
        return size_usd
    if stop_dist <= 0:
        return 0.0
    return min(risk_usd_target * price / stop_dist, max_size_usd)


class _SmcStructureBase:
    """Helpers comunes a las 6 clases de abajo -- construccion/params/
    sizing identicos, solo cambia `on_bar`. Evita repetir el mismo
    boilerplate 6 veces (otras familias de estrategias SI lo repiten
    porque cada una es un archivo propio; aqui las 3 variantes conviven a
    proposito, ver docstring del modulo, para compararlas de una)."""
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
        # Estado de transicion -- None al construir (una instancia nueva
        # por corrida/punto de grid, ver docstring del modulo), actualizado
        # en CADA llamada a on_bar sin importar si se opera o no, para que
        # la proxima barra pueda compararse contra el valor de ESTA.
        self._prev_trend: Optional[str] = None
        self._prev_zone: Optional[str] = None

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "stop_atr_mult": self.stop_atr_mult,
            "target_r": self.target_r, "risk_usd_target": self.risk_usd_target,
            "max_size_usd": self.max_size_usd,
        }

    def _target(self, side: str, price: float, atr: float) -> Optional[TargetPosition]:
        if atr <= 0:
            return None
        stop_dist = self.stop_atr_mult * atr
        size = _sized_position(price, stop_dist, self.size_usd, self.risk_usd_target, self.max_size_usd)
        if size <= 0:
            return None
        if side == "long":
            return TargetPosition(
                "long", size, stop_loss_price=price - stop_dist,
                take_profit_price=price + stop_dist * self.target_r,
            )
        return TargetPosition(
            "short", size, stop_loss_price=price + stop_dist,
            take_profit_price=price - stop_dist * self.target_r,
        )


class SmcStructureTrendZoneLong(_SmcStructureBase):
    direction = "long"
    title = "Estructura SMC propia -- Confluencia (Largo)"
    description = (
        "Entra en LARGO en su PROPIA temporalidad (1h/4h, no 5m) cuando "
        "(1) la ultima estructura de esa misma vela (BOS/ChoCH) rompio "
        "hacia ARRIBA, (2) el precio esta dentro de un Order Block alcista "
        "todavia vigente de esa misma temporalidad, y (3) la vela cierra "
        "por encima de su apertura (confirmacion). Variante 1 de 3 del "
        "modulo smc_structure.py -- dispara en CUALQUIER barra que sostenga "
        "la condicion, no solo la primera."
    )

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        trend = ctx.indicators["htf_trend"]
        zone = ctx.indicators["htf_zone_signal"]
        self._prev_trend, self._prev_zone = trend, zone
        if ctx.position.side != "flat":
            return None
        if trend != "up" or zone != "bullish":
            return None
        if ctx.bar.close <= ctx.bar.open:
            return None
        return self._target("long", ctx.bar.close, ctx.indicators["atr"])

    def live_status(self, snapshot: dict) -> dict:
        trend, zone = snapshot.get("htf_trend"), snapshot.get("htf_zone_signal")
        if trend is None and zone is None:
            return {"state": "sin_datos", "detail": "Estructura SMC propia todavia no disponible en vivo."}
        if trend == "up" and zone == "bullish":
            return {"state": "condiciones_cumplidas", "detail": "Tendencia alcista + zona de Order Block vigente, en la propia temporalidad."}
        return {"state": "analizando", "detail": f"Esperando tendencia alcista + zona bullish (actual: trend={trend}, zona={zone})."}


class SmcStructureTrendZoneShort(_SmcStructureBase):
    direction = "short"
    title = "Estructura SMC propia -- Confluencia (Corto)"
    description = (
        "Espejo de 'Estructura SMC propia -- Confluencia (Largo)': CORTO "
        "cuando la ultima estructura de la propia vela rompio hacia ABAJO, "
        "el precio esta dentro de un Order Block bajista vigente de esa "
        "misma temporalidad, y la vela cierra por debajo de su apertura."
    )

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        trend = ctx.indicators["htf_trend"]
        zone = ctx.indicators["htf_zone_signal"]
        self._prev_trend, self._prev_zone = trend, zone
        if ctx.position.side != "flat":
            return None
        if trend != "down" or zone != "bearish":
            return None
        if ctx.bar.close >= ctx.bar.open:
            return None
        return self._target("short", ctx.bar.close, ctx.indicators["atr"])

    def live_status(self, snapshot: dict) -> dict:
        trend, zone = snapshot.get("htf_trend"), snapshot.get("htf_zone_signal")
        if trend is None and zone is None:
            return {"state": "sin_datos", "detail": "Estructura SMC propia todavia no disponible en vivo."}
        if trend == "down" and zone == "bearish":
            return {"state": "condiciones_cumplidas", "detail": "Tendencia bajista + zona de Order Block vigente, en la propia temporalidad."}
        return {"state": "analizando", "detail": f"Esperando tendencia bajista + zona bearish (actual: trend={trend}, zona={zone})."}


class SmcStructureChochReversalLong(_SmcStructureBase):
    direction = "long"
    title = "Estructura SMC propia -- Reversion ChoCH (Largo)"
    description = (
        "Entra en LARGO justo en la barra donde la estructura de precio "
        "CAMBIA de direccion hacia arriba (ChoCH, no un BOS de "
        "continuacion -- ver docstring del modulo), confirmado con una "
        "vela que cierra por encima de su apertura. Variante 2 de 3: "
        "señal de reversion mas temprana posible, sin exigir una zona de "
        "Order Block todavia formada."
    )

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        trend = ctx.indicators["htf_trend"]
        prev_trend = self._prev_trend
        self._prev_trend, self._prev_zone = trend, ctx.indicators["htf_zone_signal"]
        if ctx.position.side != "flat":
            return None
        if trend != "up" or prev_trend == "up":
            return None
        if ctx.bar.close <= ctx.bar.open:
            return None
        return self._target("long", ctx.bar.close, ctx.indicators["atr"])

    def live_status(self, snapshot: dict) -> dict:
        trend = snapshot.get("htf_trend")
        if trend is None:
            return {"state": "sin_datos", "detail": "Estructura SMC propia todavia no disponible en vivo."}
        return {"state": "analizando", "detail": f"Esperando un ChoCH alcista recien confirmado (tendencia actual: {trend})."}


class SmcStructureChochReversalShort(_SmcStructureBase):
    direction = "short"
    title = "Estructura SMC propia -- Reversion ChoCH (Corto)"
    description = (
        "Espejo de 'Estructura SMC propia -- Reversion ChoCH (Largo)': "
        "CORTO justo en la barra donde la estructura cambia de direccion "
        "hacia abajo, confirmado con una vela que cierra por debajo de su "
        "apertura."
    )

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        trend = ctx.indicators["htf_trend"]
        prev_trend = self._prev_trend
        self._prev_trend, self._prev_zone = trend, ctx.indicators["htf_zone_signal"]
        if ctx.position.side != "flat":
            return None
        if trend != "down" or prev_trend == "down":
            return None
        if ctx.bar.close >= ctx.bar.open:
            return None
        return self._target("short", ctx.bar.close, ctx.indicators["atr"])

    def live_status(self, snapshot: dict) -> dict:
        trend = snapshot.get("htf_trend")
        if trend is None:
            return {"state": "sin_datos", "detail": "Estructura SMC propia todavia no disponible en vivo."}
        return {"state": "analizando", "detail": f"Esperando un ChoCH bajista recien confirmado (tendencia actual: {trend})."}


class SmcStructureFreshMitigationLong(_SmcStructureBase):
    direction = "long"
    title = "Estructura SMC propia -- Mitigacion fresca (Largo)"
    description = (
        "Entra en LARGO justo en la barra donde el precio ACABA de entrar "
        "(por primera vez, no una barra mas tarde) a una zona de Order "
        "Block alcista vigente, con la tendencia ya alineada hacia arriba, "
        "confirmado con una vela que cierra por encima de su apertura. "
        "Variante 3 de 3: a diferencia de la Confluencia (variante 1), "
        "dispara UNA sola vez por visita a la zona en vez de en cada barra "
        "que la condicion se sostiene -- la version mecanica de 'mitigacion "
        "de un Order Block fresco'."
    )

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        trend = ctx.indicators["htf_trend"]
        zone = ctx.indicators["htf_zone_signal"]
        prev_zone = self._prev_zone
        self._prev_trend, self._prev_zone = trend, zone
        if ctx.position.side != "flat":
            return None
        if trend != "up" or zone != "bullish" or prev_zone == "bullish":
            return None
        if ctx.bar.close <= ctx.bar.open:
            return None
        return self._target("long", ctx.bar.close, ctx.indicators["atr"])

    def live_status(self, snapshot: dict) -> dict:
        trend, zone = snapshot.get("htf_trend"), snapshot.get("htf_zone_signal")
        if trend is None and zone is None:
            return {"state": "sin_datos", "detail": "Estructura SMC propia todavia no disponible en vivo."}
        return {"state": "analizando", "detail": f"Esperando una mitigacion fresca de zona bullish con tendencia alcista (actual: trend={trend}, zona={zone})."}


class SmcStructureFreshMitigationShort(_SmcStructureBase):
    direction = "short"
    title = "Estructura SMC propia -- Mitigacion fresca (Corto)"
    description = (
        "Espejo de 'Estructura SMC propia -- Mitigacion fresca (Largo)': "
        "CORTO justo en la barra donde el precio entra por primera vez a "
        "una zona de Order Block bajista vigente, con la tendencia "
        "alineada hacia abajo."
    )

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        trend = ctx.indicators["htf_trend"]
        zone = ctx.indicators["htf_zone_signal"]
        prev_zone = self._prev_zone
        self._prev_trend, self._prev_zone = trend, zone
        if ctx.position.side != "flat":
            return None
        if trend != "down" or zone != "bearish" or prev_zone == "bearish":
            return None
        if ctx.bar.close >= ctx.bar.open:
            return None
        return self._target("short", ctx.bar.close, ctx.indicators["atr"])

    def live_status(self, snapshot: dict) -> dict:
        trend, zone = snapshot.get("htf_trend"), snapshot.get("htf_zone_signal")
        if trend is None and zone is None:
            return {"state": "sin_datos", "detail": "Estructura SMC propia todavia no disponible en vivo."}
        return {"state": "analizando", "detail": f"Esperando una mitigacion fresca de zona bearish con tendencia bajista (actual: trend={trend}, zona={zone})."}
