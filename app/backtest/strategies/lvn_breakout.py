"""Ruptura de LVN (Low Volume Node) con confirmacion de CVD.

Deliberadamente una premisa de CONTINUACION, no de reversion -- el
diferencial real de esta version contra `volume_breakout` (que YA prueba
"ruptura + volumen alto") no es el volumen: es que el TP aca es
ESTRUCTURAL (el siguiente HVN del perfil de volumen de la sesion
anterior), no un multiplo fijo de ATR.

Premisa: el precio rompe el borde del Area de Valor (VAH para el lado
largo) de la sesion CALENDARIO ANTERIOR ya cerrada (ver volume_profile.py,
point-in-time por construccion -- nunca el perfil de la sesion en curso)
hacia una zona de bajo volumen (LVN, "vacio de liquidez" donde casi no hay
ordenes limite acumuladas) -- el precio deberia atravesarla rapido, sin
resistencia real, si de verdad hay flujo de ordenes agresivo detras (CVD
sincronizado con la direccion de la ruptura + un pico de volumen >= 2
desviaciones estandar contra su propia ventana movil de 60 barras,
`volume_zscore_60`, ya calculado por el motor para `volume_breakout`).
TP en el HVN (Nodo de Alto Volumen) mas cercano por delante -- la proxima
zona con liquidez real acumulada, donde es razonable esperar que el
movimiento pierda impulso. Sin HVN por delante en el perfil (el precio ya
esta mas alla de toda la estructura conocida), no hay objetivo valido:
la señal se descarta, no se inventa un TP de respaldo.

SL: unico parametro sin anclar a la estructura del perfil -- mismo
patron ATR ya usado en el resto de las estrategias (`stop_atr_mult`,
default 2.0), para no mezclar un mecanismo nuevo sin validar (un stop
tambien basado en el perfil) con el que de verdad se esta probando aca
(el TP estructural)."""
from __future__ import annotations

from typing import Optional

from ..engine import BacktestContext, TargetPosition

DEFAULT_SIZE_USD = 500.0
DEFAULT_VOLUME_ZSCORE_THRESHOLD = 2.0
DEFAULT_STOP_ATR_MULT = 2.0
DEFAULT_MAX_SIZE_USD = 10_000.0


def _sized_position(price: float, stop_dist: float, size_usd: float,
                     risk_usd_target: Optional[float], max_size_usd: float) -> float:
    if risk_usd_target is None:
        return size_usd
    if stop_dist <= 0:
        return 0.0
    return min(risk_usd_target * price / stop_dist, max_size_usd)


class LvnBreakoutLong:
    direction = "long"
    title = "Ruptura de LVN con CVD (Largo)"
    description = (
        "Entra en LARGO cuando el cierre rompe el VAH (borde superior del "
        "Area de Valor) del perfil de volumen de la sesion CALENDARIO "
        "ANTERIOR, con el CVD acompañando la ruptura (compra neta en la "
        "ventana de 20 barras) y un pico de volumen (z-score >= 2 contra "
        "su ventana movil de 60 barras). TP en el siguiente Nodo de Alto "
        "Volumen (HVN) por delante -- estructural, no un multiplo de ATR."
    )

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD,
        volume_zscore_threshold: float = DEFAULT_VOLUME_ZSCORE_THRESHOLD,
        stop_atr_mult: float = DEFAULT_STOP_ATR_MULT,
        risk_usd_target: Optional[float] = None, max_size_usd: float = DEFAULT_MAX_SIZE_USD,
    ):
        self.size_usd = size_usd
        self.volume_zscore_threshold = volume_zscore_threshold
        self.stop_atr_mult = stop_atr_mult
        self.risk_usd_target = risk_usd_target
        self.max_size_usd = max_size_usd

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "volume_zscore_threshold": self.volume_zscore_threshold,
            "stop_atr_mult": self.stop_atr_mult, "risk_usd_target": self.risk_usd_target,
            "max_size_usd": self.max_size_usd,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        profile = ctx.indicators["session_profile"]
        if profile is None:
            return None
        price = ctx.bar.close
        if price <= profile.vah:
            return None
        if ctx.indicators["cvd_change_20"] <= 0:
            return None
        if ctx.indicators["volume_zscore_60"] < self.volume_zscore_threshold:
            return None
        target_price = profile.nearest_hvn_above(price)
        if target_price is None:
            return None
        atr = ctx.indicators["atr"]
        if atr <= 0:
            return None
        stop_dist = self.stop_atr_mult * atr
        size = _sized_position(price, stop_dist, self.size_usd, self.risk_usd_target, self.max_size_usd)
        if size <= 0:
            return None
        return TargetPosition(
            "long", size,
            stop_loss_price=price - stop_dist,
            take_profit_price=target_price,
        )

    def live_status(self, snapshot: dict) -> dict:
        # `session_profile` no esta en el `live_snapshot` fijo de
        # api/routers/backtest.py (mismo hueco ya aceptado para
        # volume_breakout/smc_confluence/cvd_divergence, ver docstring de
        # list_strategies) -- "sin_datos" es la respuesta honesta hasta
        # que alguien lo conecte.
        return {"state": "sin_datos", "detail": "Perfil de volumen por sesion no disponible en la vista en vivo todavia."}


class LvnBreakoutShort:
    direction = "short"
    title = "Ruptura de LVN con CVD (Corto)"
    description = (
        "Espejo de 'Ruptura de LVN con CVD (Largo)': entra en CORTO cuando "
        "el cierre rompe el VAL (borde inferior del Area de Valor) de la "
        "sesion anterior, con CVD acompañando (venta neta) y pico de "
        "volumen. TP en el siguiente HVN por debajo."
    )

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD,
        volume_zscore_threshold: float = DEFAULT_VOLUME_ZSCORE_THRESHOLD,
        stop_atr_mult: float = DEFAULT_STOP_ATR_MULT,
        risk_usd_target: Optional[float] = None, max_size_usd: float = DEFAULT_MAX_SIZE_USD,
    ):
        self.size_usd = size_usd
        self.volume_zscore_threshold = volume_zscore_threshold
        self.stop_atr_mult = stop_atr_mult
        self.risk_usd_target = risk_usd_target
        self.max_size_usd = max_size_usd

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "volume_zscore_threshold": self.volume_zscore_threshold,
            "stop_atr_mult": self.stop_atr_mult, "risk_usd_target": self.risk_usd_target,
            "max_size_usd": self.max_size_usd,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        profile = ctx.indicators["session_profile"]
        if profile is None:
            return None
        price = ctx.bar.close
        if price >= profile.val:
            return None
        if ctx.indicators["cvd_change_20"] >= 0:
            return None
        if ctx.indicators["volume_zscore_60"] < self.volume_zscore_threshold:
            return None
        target_price = profile.nearest_hvn_below(price)
        if target_price is None:
            return None
        atr = ctx.indicators["atr"]
        if atr <= 0:
            return None
        stop_dist = self.stop_atr_mult * atr
        size = _sized_position(price, stop_dist, self.size_usd, self.risk_usd_target, self.max_size_usd)
        if size <= 0:
            return None
        return TargetPosition(
            "short", size,
            stop_loss_price=price + stop_dist,
            take_profit_price=target_price,
        )

    def live_status(self, snapshot: dict) -> dict:
        return {"state": "sin_datos", "detail": "Perfil de volumen por sesion no disponible en la vista en vivo todavia."}
