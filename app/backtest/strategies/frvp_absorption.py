"""Familia "Absorcion en Niveles Clave de FRVP" -- dos variantes (larga y
corta) que describen el MISMO mecanismo espejado: una orden pasiva
institucional absorbiendo la agresion del mercado en un nivel clave del
perfil de volumen, mirada desde cada lado.

* Lado CORTO: el precio rompe un maximo local/VAH pero el Delta no lo
  respalda -- un pico de Volume Delta POSITIVO (compra agresiva) en la
  vela que rompe el nivel, que sin embargo cierra RECHAZANDO hacia
  adentro del rango otra vez (vela de mecha larga). Logica del bot: venta
  corta con TP en el POC y SL justo por encima del mechazo de absorcion.
* Lado LARGO (espejo exacto del corto): el precio toca el POC/VAL con
  Delta NEGATIVO fuerte (venta agresiva) pero el precio se niega a bajar
  -- cierra plano o positivo a pesar de la presion vendedora. Compra al
  cierre de la vela de absorcion, SL justo por debajo del minimo de esa
  vela.

Niveles usados: POC y VAL para el lado largo, POC y VAH para el corto --
los dos niveles donde el mecanismo de absorcion tiene una regla concreta
y verificable. El TP es SIEMPRE el nivel "hacia el centro" del que
disparo la entrada: VAL->POC / POC->VAH para el largo (espejado VAH->POC
/ POC->VAL para el corto) -- "TP en el POC" cuando la entrada es en el
borde del rango, extendido de forma simetrica a POC->borde-opuesto
cuando la entrada ya es en el propio POC (la unica extension consistente
con "el TP es el proximo nivel estructural hacia el resto del rango").

Diferencia real de infraestructura con `lvn_breakout`: aca el SL Y el TP
son 100% ESTRUCTURALES (low/high de la propia vela de absorcion, y el
nivel opuesto del perfil) -- CERO parametros de volatilidad (ATR) que
barrer. El unico grado de libertad real es el umbral de Delta
("pico de Volume Delta" / "Delta por debajo de un umbral negativo"), aca
expresado como z-score contra la ventana movil de 60 barras del propio
Delta de cada vela (`bar_delta_zscore`, indicators.bar_delta +
features.rolling_zscore, ver engine.py) -- mismo patron que
`volume_zscore_60` ya usa `lvn_breakout`/`volume_breakout`, pero sobre el
Delta de UNA vela, no el volumen total ni un cambio acumulado de varias
(`cvd_change_20`)."""
from __future__ import annotations

from typing import Optional

from ..engine import BacktestContext, TargetPosition

DEFAULT_SIZE_USD = 500.0
DEFAULT_DELTA_ZSCORE_THRESHOLD = 2.0
DEFAULT_MAX_SIZE_USD = 10_000.0


def _sized_position(price: float, stop_dist: float, size_usd: float,
                     risk_usd_target: Optional[float], max_size_usd: float) -> float:
    if risk_usd_target is None:
        return size_usd
    if stop_dist <= 0:
        return 0.0
    return min(risk_usd_target * price / stop_dist, max_size_usd)


def _absorption_long_level(bar, profile, delta_zscore: float, threshold: float) -> Optional[float]:
    """`None` si ninguno de los dos niveles (VAL, POC) tuvo una vela de
    absorcion compradora esta barra. Si no, el precio OBJETIVO (TP) del
    nivel que disparo -- VAL dispara hacia POC, POC dispara hacia VAH (ver
    docstring del modulo)."""
    for level, target in ((profile.val, profile.poc), (profile.poc, profile.vah)):
        # La mecha toco el nivel (o quedo por debajo) pero el cierre se
        # mantuvo en o por encima de el -- "el precio se niega a bajar",
        # rechazo clasico de mecha larga hacia abajo.
        if bar.low <= level and bar.close >= level and delta_zscore <= -threshold:
            return target
    return None


def _absorption_short_level(bar, profile, delta_zscore: float, threshold: float) -> Optional[float]:
    for level, target in ((profile.vah, profile.poc), (profile.poc, profile.val)):
        if bar.high >= level and bar.close <= level and delta_zscore >= threshold:
            return target
    return None


class FrvpAbsorptionLong:
    direction = "long"
    title = "Absorcion en Niveles Clave de FRVP (Largo)"
    description = (
        "Compra cuando el precio toca el POC o el VAL (Area de Valor) del "
        "perfil de volumen de la sesion CALENDARIO ANTERIOR con una mecha "
        "que perfora el nivel, Delta de la vela fuertemente NEGATIVO "
        "(venta agresiva, z-score contra su ventana movil de 60 barras), "
        "pero el cierre se mantiene en o por encima del nivel -- absorcion "
        "compradora. SL justo por debajo del minimo de esa vela; TP en el "
        "siguiente nivel estructural hacia el centro del rango (POC si "
        "disparo en VAL, VAH si disparo en el propio POC)."
    )

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD,
        delta_zscore_threshold: float = DEFAULT_DELTA_ZSCORE_THRESHOLD,
        risk_usd_target: Optional[float] = None, max_size_usd: float = DEFAULT_MAX_SIZE_USD,
    ):
        self.size_usd = size_usd
        self.delta_zscore_threshold = delta_zscore_threshold
        self.risk_usd_target = risk_usd_target
        self.max_size_usd = max_size_usd

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "delta_zscore_threshold": self.delta_zscore_threshold,
            "risk_usd_target": self.risk_usd_target, "max_size_usd": self.max_size_usd,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        profile = ctx.indicators["session_profile"]
        if profile is None:
            return None
        bar = ctx.bar
        target_price = _absorption_long_level(bar, profile, ctx.indicators["bar_delta_zscore"], self.delta_zscore_threshold)
        if target_price is None:
            return None
        price = bar.close
        if target_price <= price:
            return None
        stop_price = bar.low
        stop_dist = price - stop_price
        if stop_dist <= 0:
            return None
        size = _sized_position(price, stop_dist, self.size_usd, self.risk_usd_target, self.max_size_usd)
        if size <= 0:
            return None
        return TargetPosition(
            "long", size,
            stop_loss_price=stop_price,
            take_profit_price=target_price,
        )

    def live_status(self, snapshot: dict) -> dict:
        # `session_profile`/`bar_delta_zscore` no estan en el
        # `live_snapshot` fijo de api/routers/backtest.py (mismo hueco ya
        # aceptado para `lvn_breakout`/`volume_breakout`/`smc_confluence`/
        # `cvd_divergence`, ver docstring de list_strategies) --
        # "sin_datos" es la respuesta honesta hasta que alguien lo
        # conecte.
        return {"state": "sin_datos", "detail": "Perfil de volumen por sesion no disponible en la vista en vivo todavia."}


class FrvpAbsorptionShort:
    direction = "short"
    title = "Absorcion en Niveles Clave de FRVP (Corto)"
    description = (
        "Espejo de 'Absorcion en Niveles Clave de FRVP (Largo)': vende en "
        "corto cuando el precio toca el POC o el VAH con una mecha que "
        "perfora el nivel, Delta de la vela fuertemente POSITIVO (compra "
        "agresiva) pero el cierre se mantiene en o por debajo del nivel -- "
        "absorcion vendedora. SL justo por encima del maximo de esa vela; "
        "TP en el siguiente nivel estructural hacia el centro del rango."
    )

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD,
        delta_zscore_threshold: float = DEFAULT_DELTA_ZSCORE_THRESHOLD,
        risk_usd_target: Optional[float] = None, max_size_usd: float = DEFAULT_MAX_SIZE_USD,
    ):
        self.size_usd = size_usd
        self.delta_zscore_threshold = delta_zscore_threshold
        self.risk_usd_target = risk_usd_target
        self.max_size_usd = max_size_usd

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "delta_zscore_threshold": self.delta_zscore_threshold,
            "risk_usd_target": self.risk_usd_target, "max_size_usd": self.max_size_usd,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        profile = ctx.indicators["session_profile"]
        if profile is None:
            return None
        bar = ctx.bar
        target_price = _absorption_short_level(bar, profile, ctx.indicators["bar_delta_zscore"], self.delta_zscore_threshold)
        if target_price is None:
            return None
        price = bar.close
        if target_price >= price:
            return None
        stop_price = bar.high
        stop_dist = stop_price - price
        if stop_dist <= 0:
            return None
        size = _sized_position(price, stop_dist, self.size_usd, self.risk_usd_target, self.max_size_usd)
        if size <= 0:
            return None
        return TargetPosition(
            "short", size,
            stop_loss_price=stop_price,
            take_profit_price=target_price,
        )

    def live_status(self, snapshot: dict) -> dict:
        return {"state": "sin_datos", "detail": "Perfil de volumen por sesion no disponible en la vista en vivo todavia."}
