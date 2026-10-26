"""Estrategias de REFERENCIA -- existen solo para ejercitar el motor de
punta a punta (comisiones, slippage, funding, journal, indicadores de
derivados) con un setup real, no como estrategias validadas. Validar
estrategias de verdad (walk-forward, robustez, IC sobre datos out-of-sample)
es trabajo de `validation.py`.

Setup: funding extremo = posicionamiento de mercado apretado = vulnerable
a un squeeze -- se apuesta CONTRA el lado apretado, confirmado por un
cluster de liquidacion denso del lado que confirmaria el squeeze.

Dos clases, no una -- cada estrategia atiende una sola direccion, nunca
mezclada. Una version anterior entraba LARGO o CORTO segun el signo del
z-score en la MISMA clase; eso hacia imposible responder "esta estrategia
es alcista o bajista" con una sola palabra, y mezclaba dos setups (largo
apretado vs corto apretado) que no tienen por que compartir performance.
`FundingSqueezeShort` y `FundingSqueezeLong` son la misma logica partida
por lado, cada una con su propio `direction` fijo."""
from __future__ import annotations

from typing import Optional

from ..engine import BacktestContext, TargetPosition

# Tamano de orden por defecto -- $500. El motor liquida en notional USD
# (`TargetPosition.size_usd`), no en contratos -- convertir a contratos
# fijos cambiaria el riesgo por trade con el precio, en vez de mantener
# un tamano de orden ESTABLE.
DEFAULT_SIZE_USD = 500.0


class FundingSqueezeShort:
    """Funding z-score MUY positivo (largos pagando, apretados) + cluster
    de liquidacion LARGO denso POR DEBAJO del precio -> entra CORTO
    (apuesta a que el precio baja y liquida esos largos)."""

    direction = "short"
    title = "Squeeze de Funding (Corto)"
    description = (
        "Entra en CORTO cuando el funding esta anormalmente positivo (largos "
        "pagando a cortos, senal de posicionamiento largo apretado/sobreextendido) "
        "Y hay un cluster denso de liquidaciones LARGAS modeladas por debajo del "
        "precio actual (confirma que, si el precio cae, hay liquidez real que "
        "empujaria la caida -- un short-squeeze inverso). Sale cuando el funding "
        "vuelve a una banda neutral -- mean reversion simple, sin objetivo de "
        "precio ni stop."
    )

    def __init__(
        self,
        size_usd: float = DEFAULT_SIZE_USD,
        entry_z: float = 2.0,
        exit_z: float = 0.5,
        min_cluster_notional_usd: float = 0.0,
    ):
        self.size_usd = size_usd
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.min_cluster_notional_usd = min_cluster_notional_usd

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "entry_z": self.entry_z,
            "exit_z": self.exit_z, "min_cluster_notional_usd": self.min_cluster_notional_usd,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        z = ctx.indicators["funding_zscore"]
        cluster = ctx.indicators["liquidation_cluster"]
        long_below_usd = cluster["long_cluster_below"]["notional_usd"]
        pos = ctx.position

        if pos.side == "flat":
            if z >= self.entry_z and long_below_usd > self.min_cluster_notional_usd:
                return TargetPosition("short", self.size_usd)
            return None
        if pos.side == "short" and z <= self.exit_z:
            return TargetPosition("flat", 0.0)
        return None

    def live_status(self, snapshot: dict) -> dict:
        """Que estaria mirando esta estrategia AHORA MISMO si estuviera
        plana -- esto NO implica una posicion realmente abierta; describe la
        condicion de entrada evaluada contra el dato mas reciente
        disponible. `snapshot` es el mismo diccionario para TODAS las
        estrategias registradas (funding z-score, cluster de liquidacion,
        oi_trend, regimen -- ver api/routers/backtest.py); cada una lee
        solo las claves que necesita, con `.get()` porque una pieza puede
        faltar (fail-soft) sin tumbar el estado de las demas."""
        funding_zscore = snapshot.get("funding_zscore")
        cluster = snapshot.get("cluster") or {}
        if funding_zscore is None:
            return {"state": "sin_datos", "detail": "Sin funding en vivo suficiente todavia."}
        long_below_usd = cluster.get("long_cluster_below", {}).get("notional_usd", 0.0)
        z_ok = funding_zscore >= self.entry_z
        cluster_ok = long_below_usd > self.min_cluster_notional_usd
        if z_ok and cluster_ok:
            return {
                "state": "condiciones_cumplidas",
                "detail": (
                    f"Funding z-score {funding_zscore:.2f} (>= {self.entry_z}) con "
                    f"cluster largo de ${long_below_usd:,.0f} por debajo del precio -- "
                    f"condiciones de entrada CORTO cumplidas."
                ),
            }
        return {
            "state": "analizando",
            "detail": (
                f"Esperando funding z-score >= {self.entry_z} (actual: {funding_zscore:.2f}) "
                f"confirmado con un cluster de liquidacion largo > "
                f"${self.min_cluster_notional_usd:,.0f} por debajo del precio "
                f"(actual: ${long_below_usd:,.0f})."
            ),
        }


class FundingSqueezeLong:
    """Espejo de `FundingSqueezeShort`: funding z-score MUY negativo (cortos
    pagando, apretados) + cluster de liquidacion CORTO denso POR ENCIMA del
    precio -> entra LARGO."""

    direction = "long"
    title = "Squeeze de Funding (Largo)"
    description = (
        "Entra en LARGO cuando el funding esta anormalmente negativo (cortos "
        "pagando a largos, senal de posicionamiento corto apretado/sobreextendido) "
        "Y hay un cluster denso de liquidaciones CORTAS modeladas por encima del "
        "precio actual (confirma que, si el precio sube, hay liquidez real que "
        "empujaria la subida). Sale cuando el funding vuelve a una banda neutral."
    )

    def __init__(
        self,
        size_usd: float = DEFAULT_SIZE_USD,
        entry_z: float = 2.0,
        exit_z: float = 0.5,
        min_cluster_notional_usd: float = 0.0,
    ):
        self.size_usd = size_usd
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.min_cluster_notional_usd = min_cluster_notional_usd

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "entry_z": self.entry_z,
            "exit_z": self.exit_z, "min_cluster_notional_usd": self.min_cluster_notional_usd,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        z = ctx.indicators["funding_zscore"]
        cluster = ctx.indicators["liquidation_cluster"]
        short_above_usd = cluster["short_cluster_above"]["notional_usd"]
        pos = ctx.position

        if pos.side == "flat":
            if z <= -self.entry_z and short_above_usd > self.min_cluster_notional_usd:
                return TargetPosition("long", self.size_usd)
            return None
        if pos.side == "long" and z >= -self.exit_z:
            return TargetPosition("flat", 0.0)
        return None

    def live_status(self, snapshot: dict) -> dict:
        """Ver el docstring gemelo en FundingSqueezeShort.live_status."""
        funding_zscore = snapshot.get("funding_zscore")
        cluster = snapshot.get("cluster") or {}
        if funding_zscore is None:
            return {"state": "sin_datos", "detail": "Sin funding en vivo suficiente todavia."}
        short_above_usd = cluster.get("short_cluster_above", {}).get("notional_usd", 0.0)
        z_ok = funding_zscore <= -self.entry_z
        cluster_ok = short_above_usd > self.min_cluster_notional_usd
        if z_ok and cluster_ok:
            return {
                "state": "condiciones_cumplidas",
                "detail": (
                    f"Funding z-score {funding_zscore:.2f} (<= -{self.entry_z}) con "
                    f"cluster corto de ${short_above_usd:,.0f} por encima del precio -- "
                    f"condiciones de entrada LARGO cumplidas."
                ),
            }
        return {
            "state": "analizando",
            "detail": (
                f"Esperando funding z-score <= -{self.entry_z} (actual: {funding_zscore:.2f}) "
                f"confirmado con un cluster de liquidacion corto > "
                f"${self.min_cluster_notional_usd:,.0f} por encima del precio "
                f"(actual: ${short_above_usd:,.0f})."
            ),
        }
