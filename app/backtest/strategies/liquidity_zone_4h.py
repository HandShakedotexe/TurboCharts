"""LIQUIDEZ REAL como estructura de mercado en 4h -- temporalidad de
TRADING por derecho propio, no solo 5m. BTCUSDT y ETHUSDT (las unicas dos
con 4h real disponible, resampleado de 1h via Binance REST).

Zona candidata: confluencia de DOS estructuras ya construidas y validadas
en el proyecto, nunca reinventadas aqui:

1. **Heatmap de liquidez modelado** (`api/heatmap_model.compute_levels_from_trades`,
   la MISMA funcion que ya usa `engine._liquidation_cluster` -- expuesta
   sin tocar engine.py en `ctx.indicators["liquidation_cluster"]`): el
   precio esta a `cluster_proximity_pct` o menos de un cluster de
   liquidacion LARGA modelada por debajo (entrada larga) o CORTA por
   encima (entrada corta), con notional modelado >= `min_cluster_notional_usd`
   (filtra ruido de clusters chicos sin relevancia real).
2. **Perfil de volumen de la sesion anterior** (`backtest/volume_profile.py`,
   `ctx.indicators["session_profile"]`): el mismo precio esta tambien a
   `vp_proximity_pct` o menos de un Nodo de Alto Volumen (HVN) del dia
   calendario UTC anterior -- confirmacion estructural INDEPENDIENTE del
   heatmap (volumen real transado, no liquidaciones modeladas).

Diferencia REAL con otras dos familias que usan ingredientes parecidos:

* `liquidation_bounce`: trigger = EVENTOS REALES de liquidacion del
  grabador (5m, historia corta), reaccion INMEDIATA (la cascada de la
  barra actual dispara la entrada en la barra siguiente). Esta estrategia
  usa el heatmap MODELADO (big_trades + MMR, no eventos reales), en 4h, y
  EXIGE que el precio lleve VARIAS velas de 4h consolidando en la zona
  antes de disparar -- lo opuesto de "inmediato".
* `frvp_absorption`: trigger = UNA sola vela de 5m con mecha de rechazo +
  Delta extremo tocando POC/VAL/VAH. Esta estrategia no usa Delta de vela
  en absoluto -- usa la PENDIENTE DE OPEN INTEREST (`oi_trend`, ya
  calculado por el motor) de la vela de disparo como confirmacion de que
  el apalancamiento se esta desarmando (deleveraging) justo cuando el
  precio ya lleva varias velas basando en la zona, no una unica mecha de
  absorcion.

Mecanismo de entrada (largo, espejado para corto):
    1. Zona: cluster largo modelado Y HVN de ayer, ambos dentro de su
       umbral de proximidad -- `_in_long_zone`.
    2. Confirmacion: la zona se sostiene durante >= `confirm_bars` velas
       de 4h CONSECUTIVAS (contador propio de la instancia, se resetea en
       cuanto la zona se rompe) -- "varias velas confirmando" en vez de
       una reaccion de una sola vela.
    3. Disparo: en la vela que completa `confirm_bars`, el open interest
       ya esta en pendiente NEGATIVA mas alla de `oi_decline_threshold`
       (apalancamiento desarmandose, no acumulandose -- lo opuesto de lo
       que predeciria una cascada en marcha) -- entra en la SIGUIENTE
       barra (disciplina causal del motor, igual que el resto de la Fase
       3). El contador se resetea tras disparar, para exigir una zona
       NUEVA antes de volver a entrar.

SL/TP: ATR (`stop_atr_mult`/`target_r`), mismo patron que
`liquidation_bounce`/`rsi_pullback`/`oi_trend_continuation` -- la premisa
bajo prueba es la ENTRADA (zona + confirmacion + OI), no un mecanismo de
salida nuevo sin validar.

Las velas reales de 4h vienen de Binance REST (`data.load_bars_live` --
`candles` archivado no llega hasta el presente, mismo hueco ya
documentado para `liquidation_bounce`), sobre la ventana donde
`big_trades` existe para AMBOS simbolos. Se construyen y corren IGUAL
los dos lados (mismo criterio del resto del proyecto: no se descarta un
lado sin corrida real)."""
from __future__ import annotations

from typing import Optional

from ..engine import BacktestContext, TargetPosition

DEFAULT_SIZE_USD = 500.0
DEFAULT_CLUSTER_PROXIMITY_PCT = 0.01
DEFAULT_VP_PROXIMITY_PCT = 0.01
DEFAULT_MIN_CLUSTER_NOTIONAL_USD = 2_000_000.0
DEFAULT_CONFIRM_BARS = 2
DEFAULT_OI_DECLINE_THRESHOLD = 0.01
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


def _in_long_zone(
    price: float, cluster: dict, profile, cluster_proximity_pct: float,
    vp_proximity_pct: float, min_cluster_notional_usd: float,
) -> bool:
    below = cluster["long_cluster_below"]
    cluster_ok = (
        below["distance_pct"] is not None
        and below["distance_pct"] <= cluster_proximity_pct
        and below["notional_usd"] >= min_cluster_notional_usd
    )
    if not cluster_ok or profile is None:
        return False
    hvn_below = profile.nearest_hvn_below(price)
    return hvn_below is not None and (price - hvn_below) / price <= vp_proximity_pct


def _in_short_zone(
    price: float, cluster: dict, profile, cluster_proximity_pct: float,
    vp_proximity_pct: float, min_cluster_notional_usd: float,
) -> bool:
    above = cluster["short_cluster_above"]
    cluster_ok = (
        above["distance_pct"] is not None
        and above["distance_pct"] <= cluster_proximity_pct
        and above["notional_usd"] >= min_cluster_notional_usd
    )
    if not cluster_ok or profile is None:
        return False
    hvn_above = profile.nearest_hvn_above(price)
    return hvn_above is not None and (hvn_above - price) / price <= vp_proximity_pct


class LiquidityZone4hLong:
    direction = "long"
    title = "Zona de Liquidez 4h con confirmacion multi-vela + OI (Largo)"
    description = (
        "Entra en LARGO cuando el precio confluye con un cluster de "
        "liquidacion LARGA modelada (heatmap, big_trades reales) Y un HVN "
        "del perfil de volumen de la sesion anterior, sostenido durante "
        "varias velas de 4h consecutivas, con el open interest ya en "
        "pendiente negativa (deleveraging) en la vela de disparo."
    )
    needs_real_liquidations = False
    needs_htf_context = False

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD,
        cluster_proximity_pct: float = DEFAULT_CLUSTER_PROXIMITY_PCT,
        vp_proximity_pct: float = DEFAULT_VP_PROXIMITY_PCT,
        min_cluster_notional_usd: float = DEFAULT_MIN_CLUSTER_NOTIONAL_USD,
        confirm_bars: int = DEFAULT_CONFIRM_BARS,
        oi_decline_threshold: float = DEFAULT_OI_DECLINE_THRESHOLD,
        stop_atr_mult: float = DEFAULT_STOP_ATR_MULT,
        target_r: float = DEFAULT_TARGET_R,
        risk_usd_target: Optional[float] = None, max_size_usd: float = DEFAULT_MAX_SIZE_USD,
    ):
        self.size_usd = size_usd
        self.cluster_proximity_pct = cluster_proximity_pct
        self.vp_proximity_pct = vp_proximity_pct
        self.min_cluster_notional_usd = min_cluster_notional_usd
        self.confirm_bars = confirm_bars
        self.oi_decline_threshold = oi_decline_threshold
        self.stop_atr_mult = stop_atr_mult
        self.target_r = target_r
        self.risk_usd_target = risk_usd_target
        self.max_size_usd = max_size_usd
        # Estado propio de la instancia -- seguro entre corridas porque
        # `sweep_in_sample`/`walk_forward_validate`/`multi_symbol_walk_
        # forward_validate` SIEMPRE crean una instancia NUEVA
        # (`strategy_factory(**params)`) por combinacion de parametros y
        # por tramo/simbolo, nunca reusan una ya usada -- ver validation.py.
        self._consecutive_in_zone = 0

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "cluster_proximity_pct": self.cluster_proximity_pct,
            "vp_proximity_pct": self.vp_proximity_pct,
            "min_cluster_notional_usd": self.min_cluster_notional_usd,
            "confirm_bars": self.confirm_bars, "oi_decline_threshold": self.oi_decline_threshold,
            "stop_atr_mult": self.stop_atr_mult, "target_r": self.target_r,
            "risk_usd_target": self.risk_usd_target, "max_size_usd": self.max_size_usd,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        price = ctx.bar.close
        in_zone = _in_long_zone(
            price, ctx.indicators["liquidation_cluster"], ctx.indicators["session_profile"],
            self.cluster_proximity_pct, self.vp_proximity_pct, self.min_cluster_notional_usd,
        )
        self._consecutive_in_zone = self._consecutive_in_zone + 1 if in_zone else 0
        if self._consecutive_in_zone < self.confirm_bars:
            return None
        if ctx.indicators["oi_trend"] > -self.oi_decline_threshold:
            return None
        atr = ctx.indicators["atr"]
        if atr <= 0:
            return None
        self._consecutive_in_zone = 0   # exige una zona NUEVA antes de volver a disparar
        stop_dist = self.stop_atr_mult * atr
        size = _sized_position(price, stop_dist, self.size_usd, self.risk_usd_target, self.max_size_usd)
        if size <= 0:
            return None
        return TargetPosition(
            "long", size,
            stop_loss_price=price - stop_dist,
            take_profit_price=price + self.target_r * stop_dist,
        )

    def live_status(self, snapshot: dict) -> dict:
        # `liquidation_cluster`/`session_profile`/`oi_trend` no estan en el
        # `live_snapshot` fijo de api/routers/backtest.py -- mismo hueco ya
        # aceptado para otras estrategias (ver docstring de
        # list_strategies). Ademas, esta estrategia tiene estado propio
        # entre barras (`_consecutive_in_zone`) que el snapshot de un solo
        # instante no puede reconstruir -- "sin_datos" es la respuesta
        # honesta, no una aproximacion.
        return {"state": "sin_datos", "detail": "Zona de liquidez 4h (cluster+HVN+OI) no disponible en la vista en vivo todavia."}


class LiquidityZone4hShort:
    direction = "short"
    title = "Zona de Liquidez 4h con confirmacion multi-vela + OI (Corto)"
    description = (
        "Espejo de 'Zona de Liquidez 4h (Largo)': vende en CORTO cuando el "
        "precio confluye con un cluster de liquidacion CORTA modelada Y un "
        "HVN del perfil de volumen de la sesion anterior, sostenido varias "
        "velas de 4h, con el open interest en pendiente negativa en la "
        "vela de disparo."
    )
    needs_real_liquidations = False
    needs_htf_context = False

    def __init__(
        self, size_usd: float = DEFAULT_SIZE_USD,
        cluster_proximity_pct: float = DEFAULT_CLUSTER_PROXIMITY_PCT,
        vp_proximity_pct: float = DEFAULT_VP_PROXIMITY_PCT,
        min_cluster_notional_usd: float = DEFAULT_MIN_CLUSTER_NOTIONAL_USD,
        confirm_bars: int = DEFAULT_CONFIRM_BARS,
        oi_decline_threshold: float = DEFAULT_OI_DECLINE_THRESHOLD,
        stop_atr_mult: float = DEFAULT_STOP_ATR_MULT,
        target_r: float = DEFAULT_TARGET_R,
        risk_usd_target: Optional[float] = None, max_size_usd: float = DEFAULT_MAX_SIZE_USD,
    ):
        self.size_usd = size_usd
        self.cluster_proximity_pct = cluster_proximity_pct
        self.vp_proximity_pct = vp_proximity_pct
        self.min_cluster_notional_usd = min_cluster_notional_usd
        self.confirm_bars = confirm_bars
        self.oi_decline_threshold = oi_decline_threshold
        self.stop_atr_mult = stop_atr_mult
        self.target_r = target_r
        self.risk_usd_target = risk_usd_target
        self.max_size_usd = max_size_usd
        self._consecutive_in_zone = 0

    def params(self) -> dict:
        return {
            "size_usd": self.size_usd, "cluster_proximity_pct": self.cluster_proximity_pct,
            "vp_proximity_pct": self.vp_proximity_pct,
            "min_cluster_notional_usd": self.min_cluster_notional_usd,
            "confirm_bars": self.confirm_bars, "oi_decline_threshold": self.oi_decline_threshold,
            "stop_atr_mult": self.stop_atr_mult, "target_r": self.target_r,
            "risk_usd_target": self.risk_usd_target, "max_size_usd": self.max_size_usd,
        }

    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        if ctx.position.side != "flat":
            return None
        price = ctx.bar.close
        in_zone = _in_short_zone(
            price, ctx.indicators["liquidation_cluster"], ctx.indicators["session_profile"],
            self.cluster_proximity_pct, self.vp_proximity_pct, self.min_cluster_notional_usd,
        )
        self._consecutive_in_zone = self._consecutive_in_zone + 1 if in_zone else 0
        if self._consecutive_in_zone < self.confirm_bars:
            return None
        if ctx.indicators["oi_trend"] > -self.oi_decline_threshold:
            return None
        atr = ctx.indicators["atr"]
        if atr <= 0:
            return None
        self._consecutive_in_zone = 0
        stop_dist = self.stop_atr_mult * atr
        size = _sized_position(price, stop_dist, self.size_usd, self.risk_usd_target, self.max_size_usd)
        if size <= 0:
            return None
        return TargetPosition(
            "short", size,
            stop_loss_price=price + stop_dist,
            take_profit_price=price - self.target_r * stop_dist,
        )

    def live_status(self, snapshot: dict) -> dict:
        return {"state": "sin_datos", "detail": "Zona de liquidez 4h (cluster+HVN+OI) no disponible en la vista en vivo todavia."}
