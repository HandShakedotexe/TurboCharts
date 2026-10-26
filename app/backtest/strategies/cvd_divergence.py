"""Familia deliberadamente NUEVA y sin relacion con las familias
anteriores (RSI/regimen, funding, liquidaciones reales, volumen+ruptura,
SMC) -- diversidad de hipotesis en vez de una cuarta variante sobre la
misma idea.

Premisa: DIVERGENCIA entre precio y flujo de ordenes real. `volume_breakout`
es una apuesta de CONTINUACION -- un quiebre de rango CON volumen alto
tiene mas probabilidad de seguir. Esta es la apuesta opuesta, de
REVERSION: un quiebre de rango que el flujo de ordenes NO respalda (CVD,
compra taker menos venta taker, ver `indicators.cvd`) es sospechoso de
ser un movimiento sin participacion real detras -- precio empujado por
pocas ordenes agresivas contra un libro fino, no por conviccion
genuina, con mas probabilidad de revertir que de continuar. Ejemplo
concreto (largo): el precio hace un MINIMO nuevo de 20 barras (parece
bajista) pero el CVD de esas mismas 20 barras es POSITIVO (los
compradores estuvieron netamente mas activos que los vendedores mientras
el precio caia) -- los vendedores agresivos no tuvieron el respaldo de
flujo real, apuesta a que el "minimo" no se sostiene.

No hizo falta un indicador nuevo desde cero: `indicators.cvd` ya existia
para exactamente este uso ("divergencia entre CVD y precio") pero nunca
se habia conectado a una regla de entrada -- solo `indicators.cvd_change`
es nuevo (el CAMBIO de CVD en una ventana fija, porque `cvd` en si es una
suma acumulada desde el inicio de la serie, no comparable entre
ventanas).

Reusa `prior_max_high_20`/`prior_min_low_20` (ya calculados en el motor
para `volume_breakout`, misma ventana de 20 para que "extremo de precio"
y "cambio de CVD" describan exactamente el mismo tramo) para el extremo
de precio, y el mismo patron ATR de SL/TP (`stop_atr_mult`/`target_r`)
que el resto de las estrategias de este motor."""
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


class CvdDivergenceLong:
    direction = "long"
    title = "Divergencia CVD (Largo)"
    description = (
        "Entra en LARGO cuando el cierre hace un MINIMO nuevo de 20 barras "
        "(parece bajista) pero el CVD (flujo de ordenes real, compra taker "
        "menos venta taker) de esas mismas 20 barras es POSITIVO -- el "
        "movimiento bajista no tiene respaldo de flujo real detras, "
        "apuesta a la reversion. Premisa opuesta a 'Ruptura con Volumen': "
        "aca la FALTA de confirmacion es la señal, no su presencia."
    )

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD, stop_atr_mult: float = DEFAULT_STOP_ATR_MULT,
        target_r: float = DEFAULT_TARGET_R, risk_usd_target: Optional[float] = None,
        max_size_usd: float = DEFAULT_MAX_SIZE_USD,
        trail_atr_mult: Optional[float] = None, target_atr_mult: Optional[float] = None,
        breakeven_at_r: Optional[float] = None,
    ):
        self.size_usd = size_usd
        self.stop_atr_mult = stop_atr_mult
        self.target_r = target_r
        self.risk_usd_target = risk_usd_target
        self.max_size_usd = max_size_usd
        # SL/TP adaptables -- ver docstring de TargetPosition en engine.py.
        # Opcionales -- None dejan el comportamiento identico al de antes
        # (SL/TP fijos desde la entrada).
        self.trail_atr_mult = trail_atr_mult
        self.target_atr_mult = target_atr_mult
        # Breakeven-stop -- ver `cvd_divergence_breakeven_long` en
        # __main__.py y TargetPosition.breakeven_at_r en engine.py.
        # Opcional, None deja el comportamiento identico al de antes.
        self.breakeven_at_r = breakeven_at_r

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "stop_atr_mult": self.stop_atr_mult,
            "target_r": self.target_r, "risk_usd_target": self.risk_usd_target,
            "max_size_usd": self.max_size_usd,
            "trail_atr_mult": self.trail_atr_mult, "target_atr_mult": self.target_atr_mult,
            "breakeven_at_r": self.breakeven_at_r,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        if ctx.bar.close >= ctx.indicators["prior_min_low_20"]:
            return None
        if ctx.indicators["cvd_change_20"] <= 0:
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
            trail_atr_mult=self.trail_atr_mult, target_atr_mult=self.target_atr_mult,
            breakeven_at_r=self.breakeven_at_r,
        )

    def live_status(self, snapshot: dict) -> dict:
        cvd_chg = snapshot.get("cvd_change_20")
        breakdown = snapshot.get("broke_prior_low_20")
        if cvd_chg is None or breakdown is None:
            return {"state": "sin_datos", "detail": "Sin CVD/rango previo suficiente todavia."}
        if breakdown and cvd_chg > 0:
            return {
                "state": "condiciones_cumplidas",
                "detail": f"Minimo nuevo de 20 barras sin respaldo de flujo (CVD 20b={cvd_chg:+.0f}).",
            }
        return {
            "state": "analizando",
            "detail": "Esperando minimo nuevo de 20 barras con CVD de esa ventana positivo.",
        }


class CvdDivergenceShort:
    direction = "short"
    title = "Divergencia CVD (Corto)"
    description = (
        "Espejo de 'Divergencia CVD (Largo)': entra en CORTO cuando el "
        "cierre hace un MAXIMO nuevo de 20 barras pero el CVD de esas "
        "mismas 20 barras es NEGATIVO -- el movimiento alcista no tiene "
        "respaldo de flujo real detras."
    )

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD, stop_atr_mult: float = DEFAULT_STOP_ATR_MULT,
        target_r: float = DEFAULT_TARGET_R, risk_usd_target: Optional[float] = None,
        max_size_usd: float = DEFAULT_MAX_SIZE_USD,
        trail_atr_mult: Optional[float] = None, target_atr_mult: Optional[float] = None,
        breakeven_at_r: Optional[float] = None,
    ):
        self.size_usd = size_usd
        self.stop_atr_mult = stop_atr_mult
        self.target_r = target_r
        self.risk_usd_target = risk_usd_target
        self.max_size_usd = max_size_usd
        self.trail_atr_mult = trail_atr_mult
        self.target_atr_mult = target_atr_mult
        self.breakeven_at_r = breakeven_at_r

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "stop_atr_mult": self.stop_atr_mult,
            "target_r": self.target_r, "risk_usd_target": self.risk_usd_target,
            "max_size_usd": self.max_size_usd,
            "trail_atr_mult": self.trail_atr_mult, "target_atr_mult": self.target_atr_mult,
            "breakeven_at_r": self.breakeven_at_r,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        if ctx.bar.close <= ctx.indicators["prior_max_high_20"]:
            return None
        if ctx.indicators["cvd_change_20"] >= 0:
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
            trail_atr_mult=self.trail_atr_mult, target_atr_mult=self.target_atr_mult,
            breakeven_at_r=self.breakeven_at_r,
        )

    def live_status(self, snapshot: dict) -> dict:
        cvd_chg = snapshot.get("cvd_change_20")
        breakout = snapshot.get("broke_prior_high_20")
        if cvd_chg is None or breakout is None:
            return {"state": "sin_datos", "detail": "Sin CVD/rango previo suficiente todavia."}
        if breakout and cvd_chg < 0:
            return {
                "state": "condiciones_cumplidas",
                "detail": f"Maximo nuevo de 20 barras sin respaldo de flujo (CVD 20b={cvd_chg:+.0f}).",
            }
        return {
            "state": "analizando",
            "detail": "Esperando maximo nuevo de 20 barras con CVD de esa ventana negativo.",
        }
