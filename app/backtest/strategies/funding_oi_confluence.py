"""CONFLUENCIA DE DERIVADOS en 1h: combina al menos DOS de {funding rate,
open interest, CVD} en una sola premisa de entrada, buscando el caso
donde coinciden apuntando en la misma direccion o divergen de forma
especifica -- DISTINTO de usar cada senal SOLA (`funding_squeeze` usa
funding+cluster de liquidacion modelado, `oi_trend_continuation` usa
OI+regimen, `cvd_divergence` usa CVD+extremo de precio -- ninguna de las
tres es "funding+OI", la premisa de aqui).

Una conjuncion booleana de extremos (funding Y OI extremos A LA VEZ)
describe mejor como se usaria de verdad una confluencia que un producto
continuo de z-scores sobre todo el rango -- dos z-scores ruidosos
multiplicados no amplifican la señal, la diluyen en los tramos donde
ninguno de los dos esta extremo.

**Premisa (asimetrica a proposito entre los dos lados):**
- LARGO: funding extremo NEGATIVO (cortos apretados) + OI cayendo de forma
  inusual (deleveraging del lado corto, el squeeze YA esta en progreso) ->
  apuesta a que el squeeze continua empujando el precio hacia arriba.
- CORTO: funding extremo POSITIVO (largos apretados) + OI subiendo de forma
  inusual (apalancamiento fresco en el mismo lado ya sobre-extendido) ->
  apuesta a la reversion/correccion del blow-off.

Ninguna de las dos reusa la premisa de `funding_squeeze` (que exige un
CLUSTER DE LIQUIDACION modelado como confirmacion, no OI) ni la de
`oi_trend_continuation` (que exige REGIMEN de tendencia, no funding, y
apuesta a CONTINUACION de OI con el precio, no a reversion de funding).

SL/TP: mismo patron ATR que el resto de las estrategias
(`stop_atr_mult`/`target_r`), `risk_usd_target` fijo en 5.0 (5% de $100,
default ya establecido, NO barrido)."""
from __future__ import annotations

from typing import Optional

from ..engine import BacktestContext, TargetPosition

DEFAULT_SIZE_USD = 500.0
DEFAULT_FUNDING_Z_THRESHOLD = 2.0
DEFAULT_OI_Z_THRESHOLD = 1.0
DEFAULT_STOP_ATR_MULT = 2.0
DEFAULT_TARGET_R = 1.5
DEFAULT_RISK_USD_TARGET = 5.0
# $100 de capital a 100x (techo real de apalancamiento de Binance para casi
# todos estos simbolos) = $10,000 de notional -- mismo techo que ya usan
# volume_breakout/cvd_divergence, no un numero nuevo elegido para esta.
DEFAULT_MAX_SIZE_USD = 10_000.0


def _sized_position(price: float, stop_dist: float, size_usd: float,
                     risk_usd_target: Optional[float], max_size_usd: float) -> float:
    if risk_usd_target is None:
        return size_usd
    if stop_dist <= 0:
        return 0.0
    return min(risk_usd_target * price / stop_dist, max_size_usd)


class FundingOiConfluenceLong:
    """Squeeze de cortos EN PROGRESO: funding extremo negativo (cortos
    pagando premium, crowded) Y open interest cayendo de forma inusual
    (posiciones cortas cerrandose/siendo liquidadas, deleveraging real, no
    solo posicionamiento) -> apuesta a que el squeeze continua."""

    direction = "long"
    title = "Confluencia Funding+OI -- Squeeze en Progreso (Largo)"
    description = (
        "Entra en LARGO cuando el funding esta anormalmente negativo (cortos "
        "crowded, pagando premium a los largos) Y el open interest esta "
        "cayendo de forma inusual para este simbolo ahora mismo (z-score, no "
        "un nivel fijo) -- deleveraging real del lado apretado, un short "
        "squeeze ya en marcha, no solo posicionamiento. Distinto de "
        "'Squeeze de Funding' (que exige un cluster de liquidacion modelado, "
        "no OI) y de 'Continuacion por OI' (que exige regimen de tendencia, "
        "no funding). SL/TP en multiplos de ATR, sizing por riesgo fijo en "
        "USD (volatility-adjusted)."
    )

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD,
        funding_z_threshold: float = DEFAULT_FUNDING_Z_THRESHOLD,
        oi_z_threshold: float = DEFAULT_OI_Z_THRESHOLD,
        stop_atr_mult: float = DEFAULT_STOP_ATR_MULT, target_r: float = DEFAULT_TARGET_R,
        risk_usd_target: Optional[float] = DEFAULT_RISK_USD_TARGET,
        max_size_usd: float = DEFAULT_MAX_SIZE_USD,
    ):
        self.size_usd = size_usd
        self.funding_z_threshold = funding_z_threshold
        self.oi_z_threshold = oi_z_threshold
        self.stop_atr_mult = stop_atr_mult
        self.target_r = target_r
        self.risk_usd_target = risk_usd_target
        self.max_size_usd = max_size_usd

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "funding_z_threshold": self.funding_z_threshold,
            "oi_z_threshold": self.oi_z_threshold, "stop_atr_mult": self.stop_atr_mult,
            "target_r": self.target_r, "risk_usd_target": self.risk_usd_target,
            "max_size_usd": self.max_size_usd,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        if ctx.indicators["funding_zscore"] > -self.funding_z_threshold:
            return None
        if ctx.indicators["oi_trend_zscore"] > -self.oi_z_threshold:
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
        funding_z = snapshot.get("funding_zscore")
        oi_z = snapshot.get("oi_trend_zscore")
        if funding_z is None or oi_z is None:
            return {"state": "sin_datos", "detail": "Sin funding/OI en vivo suficiente todavia."}
        f_ok = funding_z <= -self.funding_z_threshold
        oi_ok = oi_z <= -self.oi_z_threshold
        if f_ok and oi_ok:
            return {
                "state": "condiciones_cumplidas",
                "detail": (
                    f"Funding z-score {funding_z:.2f} (<= -{self.funding_z_threshold}) con "
                    f"OI trend z-score {oi_z:.2f} (<= -{self.oi_z_threshold}) -- squeeze de "
                    f"cortos en progreso, condiciones de entrada LARGO cumplidas."
                ),
            }
        return {
            "state": "analizando",
            "detail": (
                f"Esperando funding z-score <= -{self.funding_z_threshold} (actual: "
                f"{funding_z:.2f}) con OI trend z-score <= -{self.oi_z_threshold} (actual: "
                f"{oi_z:.2f})."
            ),
        }


class FundingOiConfluenceShort:
    """Espejo conceptual, NO simetrico en la premisa: funding extremo
    positivo (largos crowded) Y OI
    subiendo de forma inusual (apalancamiento FRESCO entrando en el MISMO
    lado ya sobre-extendido) -> posicionamiento + capital nuevo juntos,
    senal de sobre-extension/blow-off -> apuesta a la correccion/reversion,
    no a la continuacion."""

    direction = "short"
    title = "Confluencia Funding+OI -- Sobre-extension (Corto)"
    description = (
        "Entra en CORTO cuando el funding esta anormalmente positivo (largos "
        "crowded, pagando premium a los cortos) Y el open interest esta "
        "SUBIENDO de forma inusual para este simbolo ahora mismo -- "
        "apalancamiento fresco entrando en el mismo lado ya sobre-extendido "
        "(posicionamiento + capital nuevo juntos), senal de blow-off/"
        "sobre-extension, no de continuacion con conviccion. A diferencia del "
        "lado largo de esta misma familia (funding negativo + OI CAYENDO, "
        "squeeze en progreso), aqui el OI va en la MISMA direccion que la "
        "crowd, no en contra -- son dos mecanismos distintos, no un espejo "
        "mecanico. SL/TP en multiplos de ATR, sizing por riesgo fijo en USD."
    )

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD,
        funding_z_threshold: float = DEFAULT_FUNDING_Z_THRESHOLD,
        oi_z_threshold: float = DEFAULT_OI_Z_THRESHOLD,
        stop_atr_mult: float = DEFAULT_STOP_ATR_MULT, target_r: float = DEFAULT_TARGET_R,
        risk_usd_target: Optional[float] = DEFAULT_RISK_USD_TARGET,
        max_size_usd: float = DEFAULT_MAX_SIZE_USD,
    ):
        self.size_usd = size_usd
        self.funding_z_threshold = funding_z_threshold
        self.oi_z_threshold = oi_z_threshold
        self.stop_atr_mult = stop_atr_mult
        self.target_r = target_r
        self.risk_usd_target = risk_usd_target
        self.max_size_usd = max_size_usd

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "funding_z_threshold": self.funding_z_threshold,
            "oi_z_threshold": self.oi_z_threshold, "stop_atr_mult": self.stop_atr_mult,
            "target_r": self.target_r, "risk_usd_target": self.risk_usd_target,
            "max_size_usd": self.max_size_usd,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        if ctx.indicators["funding_zscore"] < self.funding_z_threshold:
            return None
        if ctx.indicators["oi_trend_zscore"] < self.oi_z_threshold:
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
        funding_z = snapshot.get("funding_zscore")
        oi_z = snapshot.get("oi_trend_zscore")
        if funding_z is None or oi_z is None:
            return {"state": "sin_datos", "detail": "Sin funding/OI en vivo suficiente todavia."}
        f_ok = funding_z >= self.funding_z_threshold
        oi_ok = oi_z >= self.oi_z_threshold
        if f_ok and oi_ok:
            return {
                "state": "condiciones_cumplidas",
                "detail": (
                    f"Funding z-score {funding_z:.2f} (>= {self.funding_z_threshold}) con "
                    f"OI trend z-score {oi_z:.2f} (>= {self.oi_z_threshold}) -- "
                    f"sobre-extension con capital fresco, condiciones de entrada CORTO "
                    f"cumplidas."
                ),
            }
        return {
            "state": "analizando",
            "detail": (
                f"Esperando funding z-score >= {self.funding_z_threshold} (actual: "
                f"{funding_z:.2f}) con OI trend z-score >= {self.oi_z_threshold} (actual: "
                f"{oi_z:.2f})."
            ),
        }
