"""Estrategia CANDIDATA a validar de verdad con el arnes de
`validation.py`, no una referencia para ejercitar el motor (a diferencia
de funding_squeeze_*, ver sus docstrings). Usa UNICAMENTE indicadores con
historia profunda -- candles, funding y open interest con cobertura de
varios años segun el simbolo -- deliberadamente SIN `liquidation_cluster`,
que solo existe para BTCUSDT en una ventana corta. Una estrategia que
necesite el cluster no puede juntar la muestra que exige un veredicto con
algo de poder estadistico.

Setup: apalancamiento NUEVO entrando (`oi_trend` inusualmente alto para
este simbolo AHORA, no un numero absoluto fijo) mientras el precio ya esta
en una tendencia establecida (`regime.classify_regime`) -- conviccion real
detras de la tendencia, no solo cobertura de posiciones contrarias
cerrandose (que tambien sube el precio pero NO deja OI nuevo). SL/TP en
multiplos de ATR -- necesario para poder expresar el resultado en R.

Umbral en z-score, no en nivel crudo: un umbral fijo sobre `oi_trend`
tiene el mismo problema que el funding crudo (ver
`indicators.funding_zscore`) -- la escala tipica del indicador no es
estable en el tiempo, asi que un numero fijo puede cumplirse demasiado
poco o demasiado seguido segun el periodo. Se usa `oi_trend_zscore`
(indicators.py), con un umbral natural en unidades de desviacion
estandar en vez de un numero arbitrario en el nivel crudo.

Dos clases, no una -- mismo criterio que funding_squeeze.py: cada
estrategia atiende una sola direccion, nunca mezclada."""
from __future__ import annotations

from typing import Optional

from ..engine import BacktestContext, TargetPosition

DEFAULT_SIZE_USD = 500.0


class OiTrendContinuationLong:
    direction = "long"
    title = "Continuacion por OI (Largo)"
    description = (
        "Entra en LARGO cuando el precio ya esta en tendencia alcista "
        "(regimen 'trending_up') Y el open interest esta subiendo de forma "
        "INUSUAL para este simbolo ahora mismo (z-score, no un nivel fijo) -- "
        "apalancamiento nuevo entrando, no solo cobertura de cortos cerrandose. "
        "Stop y objetivo en multiplos de ATR (volatilidad real reciente del "
        "simbolo). Sale por SL o TP, nunca por señal de la estrategia -- el "
        "riesgo queda definido desde la entrada."
    )

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD, oi_trend_zscore_threshold: float = 1.5,
        stop_atr_mult: float = 2.0, target_r: float = 1.5,
    ):
        self.size_usd = size_usd
        self.oi_trend_zscore_threshold = oi_trend_zscore_threshold
        self.stop_atr_mult = stop_atr_mult
        self.target_r = target_r

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "oi_trend_zscore_threshold": self.oi_trend_zscore_threshold,
            "stop_atr_mult": self.stop_atr_mult, "target_r": self.target_r,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        # Solo abre desde plano -- el cierre lo maneja el motor solo via
        # SL/TP (ver el chequeo intrabar en engine.run_backtest_on_series),
        # nunca una señal explicita de "flat" de esta estrategia.
        if ctx.position.side != "flat":
            return None
        if ctx.indicators["regime"] != "trending_up":
            return None
        if ctx.indicators["oi_trend_zscore"] <= self.oi_trend_zscore_threshold:
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
        """Ver el docstring gemelo en FundingSqueezeShort.live_status
        (mismo patron: `snapshot` compartido por todas las estrategias
        registradas, se leen solo las claves que hacen falta)."""
        oi_z = snapshot.get("oi_trend_zscore")
        regime = snapshot.get("regime")
        if oi_z is None or regime is None:
            return {"state": "sin_datos", "detail": "Sin OI/regimen en vivo suficiente todavia."}
        trend_ok = regime == "trending_up"
        oi_ok = oi_z > self.oi_trend_zscore_threshold
        if trend_ok and oi_ok:
            return {
                "state": "condiciones_cumplidas",
                "detail": (
                    f"Regimen trending_up con OI trend z-score {oi_z:.2f} "
                    f"(> {self.oi_trend_zscore_threshold}) -- condiciones de entrada LARGO cumplidas."
                ),
            }
        return {
            "state": "analizando",
            "detail": (
                f"Esperando regimen trending_up (actual: {regime}) con OI trend z-score "
                f"> {self.oi_trend_zscore_threshold} (actual: {oi_z:.2f})."
            ),
        }


class OiTrendContinuationShort:
    direction = "short"
    title = "Continuacion por OI (Corto)"
    description = (
        "Espejo de 'Continuacion por OI (Largo)': entra en CORTO cuando el precio "
        "ya esta en tendencia bajista (regimen 'trending_down') Y el open interest "
        "esta subiendo de forma inusual (z-score) para este simbolo ahora mismo -- "
        "apalancamiento nuevo del lado corto, no solo largos cerrando posicion. "
        "Mismo SL/TP en multiplos de ATR."
    )

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD, oi_trend_zscore_threshold: float = 1.5,
        stop_atr_mult: float = 2.0, target_r: float = 1.5,
    ):
        self.size_usd = size_usd
        self.oi_trend_zscore_threshold = oi_trend_zscore_threshold
        self.stop_atr_mult = stop_atr_mult
        self.target_r = target_r

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "oi_trend_zscore_threshold": self.oi_trend_zscore_threshold,
            "stop_atr_mult": self.stop_atr_mult, "target_r": self.target_r,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        if ctx.indicators["regime"] != "trending_down":
            return None
        if ctx.indicators["oi_trend_zscore"] <= self.oi_trend_zscore_threshold:
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
        oi_z = snapshot.get("oi_trend_zscore")
        regime = snapshot.get("regime")
        if oi_z is None or regime is None:
            return {"state": "sin_datos", "detail": "Sin OI/regimen en vivo suficiente todavia."}
        trend_ok = regime == "trending_down"
        oi_ok = oi_z > self.oi_trend_zscore_threshold
        if trend_ok and oi_ok:
            return {
                "state": "condiciones_cumplidas",
                "detail": (
                    f"Regimen trending_down con OI trend z-score {oi_z:.2f} "
                    f"(> {self.oi_trend_zscore_threshold}) -- condiciones de entrada CORTO cumplidas."
                ),
            }
        return {
            "state": "analizando",
            "detail": (
                f"Esperando regimen trending_down (actual: {regime}) con OI trend z-score "
                f"> {self.oi_trend_zscore_threshold} (actual: {oi_z:.2f})."
            ),
        }
