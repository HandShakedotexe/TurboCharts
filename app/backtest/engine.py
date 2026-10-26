"""Motor event-driven del backtester.

Guardia contra look-ahead: `Strategy.on_bar(ctx)` solo ve datos hasta E
INCLUYENDO la barra actual -- los indicadores se calculan una vez sobre la
serie completa PORQUE `indicators.py`/`regime.py` son causales por
construccion (el valor en el indice i solo lee datos de `0..i`, ver sus
docstrings); lo que de verdad podria filtrar futuro es mirar el indicador
en un indice posterior al de la barra actual, y eso el motor lo evita
buscando siempre el indice mas reciente con `time_ms <= bar.time_ms` (ver
`bisect` mas abajo) -- nunca el ultimo del array completo.

Las ordenes de la barra i se llenan en el OPEN de la barra i+1, nunca en el
close de la propia barra de señal -- el bug de look-ahead clasico: en la
barra i todavia no se conoce el open de i+1 en el mercado real.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime
from typing import NamedTuple, Optional, Protocol

from api.heatmap_model import DEFAULT_BASKET, compute_levels_from_trades

from . import data as data_mod
from . import features as features_mod
from . import htf_context as htf_context_mod
from . import indicators
from . import regime as regime_mod
from . import ta as ta_mod
from . import volume_profile as volume_profile_mod

LIQUIDATION_LOOKBACK_HOURS_DEFAULT = 24.0


class TargetPosition(NamedTuple):
    """`side`: "long", "short" o "flat". `size_usd` se ignora si side ==
    "flat". LIMITACION deliberada: no se soporta cambiar el tamaño de una
    posicion ya abierta del MISMO lado (una TargetPosition con side igual
    al actual y size_usd distinto se ignora silenciosamente en el motor).
    Si una estrategia futura lo necesita, hay que ampliar
    `run_backtest_on_series` primero, no asumir que ya funciona.

    `stop_loss_price`/`take_profit_price` son OPCIONALES: una estrategia
    puede salir solo por señal (`None` en los dos) o declarar niveles de
    riesgo explicitos. Cuando se dan, el motor gana un cierre automatico
    intrabar cuando el precio los toca (ver el chequeo de stop/target en
    `run_backtest_on_series`) -- sin esto no hay forma honesta de expresar
    el resultado en R (unidades de riesgo).

    `trail_atr_mult`/`target_atr_mult`: OPCIONALES, se leen solo del
    TargetPosition que ABRE el trade (los de barras posteriores, mientras
    la posicion sigue del mismo lado, se ignoran -- ver la nota de
    `size_usd` arriba, misma limitacion). Si se dan, el motor mismo (no la
    estrategia) reajusta `stop_loss_price`/`take_profit_price` en CADA
    barra siguiente en funcion del ATR de ESE momento, no el de la
    entrada -- ver el "trailing" en `run_backtest_on_series`.
    `trail_atr_mult`: el stop sigue al precio a esa distancia en ATR, SOLO
    endureciendo (jamas se afloja -- un trailing stop que se afloja no
    protege nada); existe porque un SL fijo desde la entrada no se
    beneficia de que el precio ya se movio a favor. `target_atr_mult`: el
    TP se recalcula como `entry_price +/- target_atr_mult * atr_actual` --
    se ensancha si la volatilidad sube despues de entrar (mas lugar para
    que el movimiento se desarrolle) y se achica si baja (asegura
    ganancia antes de que el impulso se apague), en vez de quedarse fijo
    en el ATR del momento de la entrada.

    `breakeven_at_r`: OPCIONAL, mismo patron que `trail_atr_mult` (solo se
    lee del TargetPosition que ABRE el trade, el motor lo reajusta el
    solo en cada barra siguiente). Una vez que `mfe_usd/risk_usd` (la
    excursion favorable maxima alcanzada, en unidades de R) llega a
    `breakeven_at_r`, el stop salta UNA VEZ a `entry_price` y se queda ahi
    -- nunca se afloja, y nunca sigue trepando mas alla de breakeven (a
    diferencia de `trail_atr_mult`, que seguiria acercandose al precio en
    cada barra). Existe para proteger una excursion favorable ya alcanzada
    en vez de dejarla ir hasta el stop completo, sin tocar la señal de
    entrada."""
    side: str
    size_usd: float
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    trail_atr_mult: Optional[float] = None
    target_atr_mult: Optional[float] = None
    breakeven_at_r: Optional[float] = None


class Position(NamedTuple):
    side: str
    size_usd: float


FLAT = Position("flat", 0.0)


@dataclass
class Trade:
    trade_seq: int
    side: str
    entry_time_ms: int
    entry_price: float
    size_usd: float
    signal_snapshot: dict
    commission_usd: float = 0.0
    funding_paid_usd: float = 0.0
    exit_time_ms: Optional[int] = None
    exit_price: Optional[float] = None
    pnl_usd: Optional[float] = None
    # Slippage en dolares -- YA esta implicito en pnl_usd (entry_price/exit_price
    # son precios CON slip, ver `_slip`), este campo no se resta de nuevo: es
    # solo el desglose de cuanto de ese costo total es slippage vs comision,
    # para la seccion de "Costos de Ejecucion" del trade journal.
    # size_usd * slippage_bps / 1e4 es exacto, no una estimacion -- es la
    # formula que `_slip` ya usa para mover el precio.
    slippage_usd: float = 0.0
    # Excursion favorable/adversa maxima, en dolares (sobre `size_usd`),
    # medida con el high/low de CADA barra mientras la posicion sigue
    # abierta -- no solo con el close. Sirve para juzgar si el SL/TP de una
    # estrategia futura estarian bien puestos (MFE alto y MAE bajo = habia
    # margen para un TP mas ambicioso; MAE alto = el trade estuvo cerca de
    # saltar un stop que hoy no existe). Estas estrategias de referencia no
    # tienen SL/TP todavia (ver docstring de FundingSqueezeShort/Long) -- el
    # dato queda listo para cuando lo tengan.
    mfe_usd: float = 0.0
    mae_usd: float = 0.0
    # Indicadores en el momento en que se DECIDIO la salida (la barra ANTERIOR
    # a exit_time_ms -- la orden se llena en el open de la siguiente, igual
    # que la entrada) -- None si el trade sigue abierto o si se cerro de forma
    # forzosa al final de la ventana (ver `exit_reason`), donde no hubo una
    # decision de la estrategia que registrar.
    exit_signal_snapshot: Optional[dict] = None
    # "signal": la estrategia pidio flat/cambio de lado. "end_of_window": no
    # hubo señal -- se liquido porque la corrida se termino con la posicion
    # abierta (ver el cierre de fin de corrida en run_backtest_on_series).
    # "stop_loss"/"take_profit": el precio toco uno de los niveles
    # de TargetPosition intrabar, sin que la estrategia decidiera nada ese
    # tick -- el motor cierra solo. Sin esto, un trade cerrado por quedarse
    # sin datos (o por un stop automatico) se veia identico a uno cerrado
    # por una señal real de la estrategia.
    exit_reason: Optional[str] = None
    # Nivel de SL/TP con el que se abrio el trade (None si la estrategia no
    # los dio, ver TargetPosition). `risk_usd` es cuanto se perderia si el
    # SL se toca EXACTO (sin slippage) -- la unidad de "R" usada para medir
    # resultados. `r_multiple` = pnl_usd / risk_usd, calculado en `_close`
    # una vez que el trade ya tiene resultado.
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    risk_usd: Optional[float] = None
    r_multiple: Optional[float] = None
    # Ver TargetPosition.trail_atr_mult/target_atr_mult -- copiados del
    # TargetPosition de apertura, el motor los usa en cada barra siguiente
    # para reajustar stop_loss_price/take_profit_price solo. `risk_usd`
    # (arriba) NO se recalcula cuando el stop se mueve -- sigue siendo el
    # riesgo INICIAL, que es la definicion estandar de "R" (el resultado
    # se mide en multiplos del riesgo con el que se entro, no del riesgo
    # ya reducido a mitad de camino).
    trail_atr_mult: Optional[float] = None
    target_atr_mult: Optional[float] = None
    # Ver TargetPosition.breakeven_at_r -- copiado igual que los dos de
    # arriba, el motor lo lee en cada barra siguiente comparando contra
    # `mfe_usd`/`risk_usd` (ya en este mismo Trade).
    breakeven_at_r: Optional[float] = None


@dataclass
class BacktestContext:
    bar: data_mod.Bar
    bar_index: int
    equity: float
    position: Position
    indicators: dict = field(default_factory=dict)


class Strategy(Protocol):
    def on_bar(self, ctx: BacktestContext) -> Optional[TargetPosition]:
        ...


@dataclass
class BacktestResult:
    symbol: str
    interval: str
    strategy_name: str
    params: dict
    data_start: datetime
    data_end: datetime
    commission_bps: float
    slippage_bps: float
    initial_equity: float
    trades: list[Trade]
    equity_curve: list[tuple[int, float]]   # (time_ms, equity con no realizado)
    regime_labels: list[str]
    bar_times_ms: list[int]


def _slip(price: float, bps: float, direction: int) -> float:
    """`direction` = +1 si estamos COMPRANDO (el precio se mueve en contra
    hacia arriba), -1 si estamos VENDIENDO (en contra hacia abajo)."""
    return price * (1 + direction * bps / 1e4)


def _entry_direction(side: str) -> int:
    return 1 if side == "long" else -1  # abrir largo = comprar; abrir corto = vender


def _exit_direction(side: str) -> int:
    return -1 if side == "long" else 1  # cerrar largo = vender; cerrar corto = comprar


def run_backtest(
    conn,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    strategy: Strategy,
    strategy_name: str,
    strategy_params: dict,
    commission_bps: float = 5.0,
    slippage_bps: float = 2.0,
    initial_equity: float = 10_000.0,
    liquidation_lookback_hours: float = LIQUIDATION_LOOKBACK_HOURS_DEFAULT,
    exchange: str = "binance",
    htf_interval: Optional[str] = None,
) -> BacktestResult:
    bars = data_mod.load_bars(conn, symbol, interval, start, end, exchange)
    funding_rows = data_mod.load_funding(conn, symbol, start, end, exchange)
    oi_rows = data_mod.load_oi_metrics(conn, symbol, start, end, exchange)
    big_trades = data_mod.load_big_trades(conn, symbol, start, end, exchange)
    mmr_table = data_mod.load_mmr_table(conn, symbol, end, exchange)
    # `real_liquidations` es la UNICA serie de este motor que no viene de la
    # DB (lee ficheros del grabador, ver data.load_real_liquidations) -- se
    # carga solo si la estrategia la declara necesaria
    # (`needs_real_liquidations = True`, ver strategies/liquidation_bounce.py)
    # para que ninguna estrategia existente pague ese costo de I/O sin usarla.
    real_liquidations = (
        data_mod.load_real_liquidations(symbol, start, end)
        if getattr(strategy, "needs_real_liquidations", False) else None
    )
    # Temporalidad mayor, usada como contexto de tendencia/estructura para
    # las entradas de la temporalidad base -- solo se pide/carga si la
    # estrategia lo declara (`needs_htf_context = True`, ver
    # strategies/smc_confluence.py), mismo criterio que `real_liquidations`
    # arriba: ninguna estrategia existente paga este I/O extra sin usarlo.
    # `htf_interval` explicito (no derivado de `interval`) porque no hay una
    # regla unica de "la temporalidad mayor" -- el caller decide, igual que
    # el resto del proyecto evita mapeos implicitos "5m -> 1h" sin pedirlo.
    htf_bars = (
        data_mod.load_bars(conn, symbol, htf_interval, start, end, exchange)
        if htf_interval and getattr(strategy, "needs_htf_context", False) else None
    )

    return run_backtest_on_series(
        symbol, interval, start, end, bars, funding_rows, oi_rows, big_trades, mmr_table,
        strategy, strategy_name, strategy_params, commission_bps, slippage_bps,
        initial_equity, liquidation_lookback_hours,
        real_liquidations=real_liquidations, htf_bars=htf_bars,
    )


def run_backtest_on_series(
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    bars: list[data_mod.Bar],
    funding_rows: list[data_mod.FundingPoint],
    oi_rows: list[data_mod.OiPoint],
    big_trades: list[data_mod.BigTrade],
    mmr_table,
    strategy: Strategy,
    strategy_name: str,
    strategy_params: dict,
    commission_bps: float = 5.0,
    slippage_bps: float = 2.0,
    initial_equity: float = 10_000.0,
    liquidation_lookback_hours: float = LIQUIDATION_LOOKBACK_HOURS_DEFAULT,
    real_liquidations: Optional[list[data_mod.RealLiquidation]] = None,
    htf_bars: Optional[list[data_mod.Bar]] = None,
    htf_lookback: int = 2,
) -> BacktestResult:
    """Separado de `run_backtest` para que los tests puedan pasar series
    sinteticas directamente, sin DB (mismo motivo que `_build_heatmap_response`
    esta separado de `get_heatmap` en api/routers/heatmap.py).

    `real_liquidations`: keyword-only DE HECHO (tiene default `None` y va al
    final) para no romper ningun caller posicional existente -- agregado
    para `strategies/liquidation_bounce.py`, el unico consumidor hoy.

    `htf_bars`: barras de una temporalidad MAYOR (ej. 1h mientras `bars` es
    5m), mismo criterio keyword-only. Cuando se da, expone `htf_trend`/
    `htf_zone_signal` en `ctx.indicators` (ver `htf_context.py`) -- point-in-
    time contra el CIERRE de la barra mayor mas reciente ya conocida en el
    momento de cada barra de `bars`, nunca contra una vela mayor todavia en
    curso."""
    if not bars:
        return BacktestResult(
            symbol, interval, strategy_name, strategy_params, start, end,
            commission_bps, slippage_bps, initial_equity, [], [], [], [],
        )

    # Series causales precalculadas UNA vez sobre los datos completos --
    # seguro (no es look-ahead) porque cada indice de estas series solo lee
    # datos hasta ese mismo indice, ver docstrings de indicators.py/regime.py.
    # El unico que de verdad necesita recalcularse por barra es la
    # proximidad a clusters de liquidacion: depende del PRECIO actual, no
    # solo de un indice que avanza.
    cvd_series = indicators.cvd(bars)
    # `cvd` en si es acumulado desde el inicio de la serie (no comparable
    # entre ventanas), asi que se expone tambien su CAMBIO en una ventana
    # fija (ver docstring de `indicators.cvd_change`), util para comparar
    # divergencias entre precio y flujo de ordenes. 20 barras, mismo tamaño
    # que `prior_max_high_20`/`prior_min_low_20` de mas abajo -- la
    # comparacion "¿el flujo de ordenes confirma el extremo de precio de
    # esta misma ventana?" solo tiene sentido si las dos ventanas
    # coinciden.
    cvd_change_20_series = indicators.cvd_change(bars, 20)
    oi_trend_series = indicators.oi_trend(oi_rows)
    oi_trend_zscore_series = indicators.oi_trend_zscore(oi_rows)
    lsr_trend_series = indicators.long_short_ratio_trend(oi_rows)
    funding_z_series = indicators.funding_zscore(funding_rows)
    regime_labels = regime_mod.classify_regime(bars)
    # ATR: expuesto a las estrategias para poder poner un SL/TP basado en
    # volatilidad real del simbolo en vez de un numero fijo en dolares o
    # porcentaje -- mismo suavizado de Wilder que ya usa
    # regime.classify_regime, no una segunda implementacion.
    atr_series = regime_mod.atr(bars)
    # RSI (ver strategies/rsi_pullback.py): mismo criterio que ATR arriba,
    # periodo fijo (14, estandar) precalculado una vez; lo que las
    # estrategias barren es el UMBRAL de sobreventa/sobrecompra, no la
    # ventana del indicador -- igual que ya se hace con
    # oi_trend_zscore_threshold sobre oi_trend_zscore_series.
    rsi_series = ta_mod.rsi(bars)
    # Volumen/breakout (ver strategies/volume_breakout.py): familia basada
    # en volumen, independiente de las de RSI/regimen. `volume_zscore_60`
    # reusa `features.rolling_zscore` (ya generico, sin duplicar la
    # formula); el rango de 20 barras para el breakout es el default
    # clasico de un canal de Donchian, no un numero ajustado a resultados.
    volume_zscore_series = features_mod.rolling_zscore([b.volume for b in bars], 60)
    prior_max_high_series = features_mod.prior_rolling_max_high(bars, 20)
    prior_min_low_series = features_mod.prior_rolling_min_low(bars, 20)
    # SMC de temporalidad mayor (ver htf_context.py): se calcula UNA vez
    # sobre `htf_bars` completo, igual que el resto de las series de
    # arriba -- point-in-time lo garantiza
    # `htf_context.build_htf_context`/`zone_signal_at` (gatean por
    # `impulse_event_index`, no por la posicion de la vela), no el motor.
    htf_ctx = htf_context_mod.build_htf_context(htf_bars, htf_lookback) if htf_bars else None
    htf_times = [b.time_ms for b in htf_bars] if htf_bars else []
    # Perfil de volumen por sesion: a diferencia de htf_bars/
    # real_liquidations, no necesita I/O adicional (sale de `bars` mismo),
    # asi que se calcula siempre, sin flag `needs_*` -- el costo es
    # O(barras x NUM_BUCKETS), barato (ver volume_profile.py).
    session_profile_series = volume_profile_mod.session_profile_series(bars)
    # Delta de volumen por barra, z-scoreado contra su propia ventana movil
    # (ver strategies/frvp_absorption.py): mismo patron que
    # `volume_zscore_60` de arriba (`features.rolling_zscore`, generico,
    # sin duplicar la formula), pero sobre `indicators.bar_delta` (compra
    # menos venta taker de CADA barra) en vez de sobre el volumen total --
    # util para saber si el Delta de LA VELA que toca un nivel es un pico
    # inusual, no si el volumen total lo es (una vela puede tener volumen
    # normal y Delta extremo, o viceversa).
    bar_delta_zscore_series = features_mod.rolling_zscore(indicators.bar_delta(bars), 60)

    bar_times = [b.time_ms for b in bars]
    oi_times = [o.time_ms for o in oi_rows]
    funding_times = [f.time_ms for f in funding_rows]
    trade_times = [t.time_ms for t in big_trades]
    lookback_ms = int(liquidation_lookback_hours * 3_600_000)
    real_liqs = real_liquidations or []
    real_liq_times = [e.time_ms for e in real_liqs]
    # Duracion de una barra, para saber donde empieza la ventana "durante
    # esta barra" de la primera barra (no hay una barra anterior de la que
    # restar) -- mismo criterio simple que ya usa el resto del motor: si
    # solo hay una barra, su propia duracion no importa (no hay bisect que
    # hacer con una serie vacia de liquidaciones reales en la practica).
    _bar_duration_ms = (bars[1].time_ms - bars[0].time_ms) if len(bars) > 1 else 300_000

    equity = initial_equity
    position: Position = FLAT
    open_trade: Optional[Trade] = None
    trades: list[Trade] = []
    equity_curve: list[tuple[int, float]] = []
    pending: Optional[tuple[TargetPosition, dict]] = None
    # Trailing stop / take-profit adaptable (ver TargetPosition.trail_atr_mult/
    # target_atr_mult): {"sl": float|None, "tp": float|None} calculado al
    # CIERRE de la barra anterior, aplicado aqui al ABRIR esta -- misma
    # disciplina causal que `pending` de arriba (decidir con datos de la
    # barra i, actuar en la i+1), nunca con el propio high/low de la barra
    # que se esta por chequear contra el stop.
    pending_trail: Optional[dict] = None
    last_funding_idx = -1
    trade_seq = 0

    for i, bar in enumerate(bars):
        # 0. Aplicar el trailing decidido al cerrar la barra anterior --
        # ANTES del chequeo de stop/target de esta barra (paso 1.5), para
        # que ese chequeo ya use el nivel actualizado. Solo puede afectar
        # a un trade que YA estaba abierto viniendo hacia esta barra (uno
        # recien abierto en el paso 1 de abajo, en esta misma iteracion,
        # todavia no tiene trailing calculado -- su primer ajuste sale al
        # final de ESTA barra y se aplica en la siguiente).
        if pending_trail is not None and open_trade is not None:
            if pending_trail.get("sl") is not None:
                open_trade.stop_loss_price = pending_trail["sl"]
            if pending_trail.get("tp") is not None:
                open_trade.take_profit_price = pending_trail["tp"]
        pending_trail = None

        # 1. Ejecutar, en el OPEN de esta barra, la decision tomada en la
        # barra anterior (si hubo una) -- nunca en la propia barra de señal.
        if pending is not None:
            target, snapshot = pending
            pending = None
            if position.side != "flat" and (target.side != position.side or target.size_usd <= 0):
                equity = _close(
                    open_trade, bar, position, commission_bps, slippage_bps, equity,
                    exit_snapshot=snapshot, reason="signal",
                )
                trades.append(open_trade)
                open_trade = None
                position = FLAT
            if target.side != "flat" and target.size_usd > 0 and position.side == "flat":
                trade_seq += 1
                open_trade, equity = _open(
                    trade_seq, target, bar, commission_bps, slippage_bps, equity, snapshot,
                )
                position = Position(target.side, target.size_usd)

        # 1.5. Stop loss / take profit: si el trade abierto tiene
        # alguno definido (TargetPosition.stop_loss_price/take_profit_price,
        # ver docstring) y el high/low de ESTA barra lo toca, se cierra aqui
        # mismo -- sin esperar a que la estrategia decida nada. Esto SI
        # incluye la barra en la que el trade acaba de abrirse en el paso 1
        # de arriba: es la misma barra cuyo high/low ya se uso para el MFE/
        # MAE, y un stop real reacciona al precio intrabar, no a que la
        # estrategia lo note un tick despues -- no es look-ahead porque solo
        # usa datos de la barra YA transcurrida, igual que el resto del
        # motor. Si el low toca el SL y el high toca el TP en la MISMA
        # barra (posible con velas de 1h+), se asume que el SL se ejecuto
        # PRIMERO -- sin datos intrabar mas finos no hay forma de saber el
        # orden real, y asumir lo peor es la eleccion conservadora, no la
        # optimista.
        if open_trade is not None and (open_trade.stop_loss_price is not None
                                        or open_trade.take_profit_price is not None):
            sl, tp = open_trade.stop_loss_price, open_trade.take_profit_price
            hit_price: Optional[float] = None
            hit_reason = ""
            if position.side == "long":
                if sl is not None and bar.low <= sl:
                    hit_price, hit_reason = sl, "stop_loss"
                elif tp is not None and bar.high >= tp:
                    hit_price, hit_reason = tp, "take_profit"
            else:
                if sl is not None and bar.high >= sl:
                    hit_price, hit_reason = sl, "stop_loss"
                elif tp is not None and bar.low <= tp:
                    hit_price, hit_reason = tp, "take_profit"
            if hit_price is not None:
                equity = _close(
                    open_trade, bar, position, commission_bps, slippage_bps, equity,
                    override_price=hit_price, reason=hit_reason,
                )
                trades.append(open_trade)
                open_trade = None
                position = FLAT

        # 2. Funding: cobrar/pagar por cualquier evento nuevo desde la
        # ultima barra, sobre la posicion (si hay una) ABIERTA en este punto.
        funding_idx = bisect.bisect_right(funding_times, bar.time_ms) - 1
        if funding_idx > last_funding_idx and position.side != "flat" and open_trade is not None:
            new_rates = [funding_rows[j].rate for j in range(last_funding_idx + 1, funding_idx + 1)]
            sign = 1 if position.side == "long" else -1
            cost = sum(new_rates) * position.size_usd * sign
            open_trade.funding_paid_usd += cost
            equity -= cost
        if funding_idx > last_funding_idx:
            last_funding_idx = funding_idx

        # 3. Marca a mercado (no realizado) para la curva de equity, y
        # excursion favorable/adversa maxima con el high/low de ESTA barra
        # (no solo el close) -- incluye la barra en la que el trade se abrio,
        # porque `open_trade` ya quedo asignado en el paso 1 de arriba si
        # hubo un fill este mismo tick.
        unrealized = 0.0
        if open_trade is not None:
            direction = 1 if position.side == "long" else -1
            unrealized = position.size_usd * (bar.close / open_trade.entry_price - 1) * direction
            if position.side == "long":
                fav_pct = max(0.0, bar.high / open_trade.entry_price - 1)
                adv_pct = max(0.0, 1 - bar.low / open_trade.entry_price)
            else:
                fav_pct = max(0.0, 1 - bar.low / open_trade.entry_price)
                adv_pct = max(0.0, bar.high / open_trade.entry_price - 1)
            open_trade.mfe_usd = max(open_trade.mfe_usd, position.size_usd * fav_pct)
            open_trade.mae_usd = max(open_trade.mae_usd, position.size_usd * adv_pct)
        equity_curve.append((bar.time_ms, equity + unrealized))

        # 4. Construir el contexto (solo con datos hasta esta barra) y
        # preguntarle a la estrategia el proximo objetivo -- se llenara en
        # el open de la barra i+1, no aqui.
        oi_idx = bisect.bisect_right(oi_times, bar.time_ms) - 1
        cluster = _liquidation_cluster(bar, big_trades, trade_times, lookback_ms, mmr_table)
        # Eventos de liquidacion REAL ocurridos DENTRO de esta barra (desde
        # el cierre de la anterior, exclusivo, hasta el cierre de esta,
        # inclusive) -- se conocen al cerrar la barra, la señal que generen
        # se ejecuta en el open de la siguiente, mismo timing que el resto
        # del motor (ver docstring del modulo).
        bar_start_ms = bars[i - 1].time_ms if i > 0 else bar.time_ms - _bar_duration_ms
        liq_lo = bisect.bisect_right(real_liq_times, bar_start_ms)
        liq_hi = bisect.bisect_right(real_liq_times, bar.time_ms)
        # `htf_idx`: la ultima barra de `htf_bars` cuyo CIERRE ya paso al
        # momento de esta barra de 5m -- nunca la vela mayor todavia en
        # curso (ver docstring de `run_backtest_on_series`).
        htf_idx = bisect.bisect_right(htf_times, bar.time_ms) - 1 if htf_ctx is not None else -1
        ind = {
            "cvd": cvd_series[i],
            "cvd_change_20": cvd_change_20_series[i],
            "oi_trend": oi_trend_series[oi_idx] if oi_idx >= 0 else 0.0,
            "oi_trend_zscore": oi_trend_zscore_series[oi_idx] if oi_idx >= 0 else 0.0,
            "long_short_ratio_trend": lsr_trend_series[oi_idx] if oi_idx >= 0 else 0.0,
            "funding_zscore": funding_z_series[funding_idx] if funding_idx >= 0 else 0.0,
            "liquidation_cluster": cluster,
            "regime": regime_labels[i],
            "atr": atr_series[i],
            "rsi": rsi_series[i],
            "real_liquidations_this_bar": real_liqs[liq_lo:liq_hi],
            "volume_zscore_60": volume_zscore_series[i],
            "prior_max_high_20": prior_max_high_series[i],
            "prior_min_low_20": prior_min_low_series[i],
            "htf_trend": htf_context_mod.trend_at(htf_ctx, htf_idx) if htf_idx >= 0 else None,
            "htf_zone_signal": (
                htf_context_mod.zone_signal_at(htf_ctx, htf_idx, bar.close) if htf_idx >= 0 else None
            ),
            "session_profile": session_profile_series[i],
            "bar_delta_zscore": bar_delta_zscore_series[i],
        }
        ctx = BacktestContext(bar=bar, bar_index=i, equity=equity + unrealized, position=position, indicators=ind)
        target = strategy.on_bar(ctx)
        if target is not None and i + 1 < len(bars):
            pending = (target, ind)

        # 5. Trailing stop / take-profit adaptable: si el trade que sigue
        # abierto AL CIERRE de esta barra (el de siempre, o uno recien
        # abierto en el paso 1 de esta misma iteracion) pidio
        # trail_atr_mult/target_atr_mult al entrar, calcular aqui el nuevo
        # nivel con el ATR de ESTA barra -- se aplica recien al abrir la
        # siguiente (arriba, paso 0), nunca contra el propio high/low de
        # esta barra (evita look-ahead, misma disciplina que el resto del
        # motor).
        if open_trade is not None and (open_trade.trail_atr_mult is not None
                                        or open_trade.target_atr_mult is not None
                                        or open_trade.breakeven_at_r is not None):
            atr_i = ind["atr"]
            trail: dict = {}
            if open_trade.trail_atr_mult is not None and atr_i > 0:
                if position.side == "long":
                    candidate = bar.close - open_trade.trail_atr_mult * atr_i
                    current = open_trade.stop_loss_price
                    trail["sl"] = candidate if current is None else max(current, candidate)
                else:
                    candidate = bar.close + open_trade.trail_atr_mult * atr_i
                    current = open_trade.stop_loss_price
                    trail["sl"] = candidate if current is None else min(current, candidate)
            # Breakeven-stop (ver TargetPosition.breakeven_at_r): salto UNICO
            # a `entry_price` una vez que `mfe_usd` (ya actualizado en el
            # paso 3 de ESTA misma barra) cruza el umbral -- se combina con
            # `trail["sl"]` de arriba si trail_atr_mult tambien esta activo
            # (el candidato ya calculado por ese mecanismo, no
            # `stop_loss_price` crudo, para no destrailing un stop que ya
            # esta mas ajustado que breakeven).
            if (open_trade.breakeven_at_r is not None and open_trade.risk_usd
                    and open_trade.risk_usd > 0
                    and open_trade.mfe_usd / open_trade.risk_usd >= open_trade.breakeven_at_r):
                current = trail.get("sl", open_trade.stop_loss_price)
                if position.side == "long":
                    trail["sl"] = open_trade.entry_price if current is None else max(current, open_trade.entry_price)
                else:
                    trail["sl"] = open_trade.entry_price if current is None else min(current, open_trade.entry_price)
            if open_trade.target_atr_mult is not None and atr_i > 0:
                if position.side == "long":
                    trail["tp"] = open_trade.entry_price + open_trade.target_atr_mult * atr_i
                else:
                    trail["tp"] = open_trade.entry_price - open_trade.target_atr_mult * atr_i
            if trail:
                pending_trail = trail

    # Cierre de fin de corrida: una posicion que sigue abierta en la
    # ultima barra se liquida al close de esa barra (no queda "abierta para
    # siempre"), para que las estadisticas no ignoren su resultado.
    if open_trade is not None:
        equity = _close(
            open_trade, bars[-1], position, commission_bps, slippage_bps, equity,
            at_close=True, reason="end_of_window",
        )
        trades.append(open_trade)
        equity_curve[-1] = (bars[-1].time_ms, equity)

    return BacktestResult(
        symbol=symbol, interval=interval, strategy_name=strategy_name, params=strategy_params,
        data_start=start, data_end=end, commission_bps=commission_bps, slippage_bps=slippage_bps,
        initial_equity=initial_equity, trades=trades, equity_curve=equity_curve,
        regime_labels=regime_labels, bar_times_ms=bar_times,
    )


def _open(trade_seq: int, target: TargetPosition, fill_bar: data_mod.Bar,
          commission_bps: float, slippage_bps: float, equity: float, snapshot: dict) -> tuple[Trade, float]:
    fill_price = _slip(fill_bar.open, slippage_bps, _entry_direction(target.side))
    commission = target.size_usd * commission_bps / 1e4
    slip_cost = target.size_usd * slippage_bps / 1e4
    # risk_usd: lo que se pierde si el SL se toca EXACTO, sin slippage --
    # la unidad de "R" usada para medir resultados. Se calcula sobre el precio de
    # entrada YA con slip (`fill_price`), no el `fill_bar.open` crudo: el
    # riesgo real de la posicion es desde donde de verdad se entro.
    risk_usd = (
        target.size_usd * abs(fill_price - target.stop_loss_price) / fill_price
        if target.stop_loss_price is not None else None
    )
    trade = Trade(
        trade_seq=trade_seq, side=target.side, entry_time_ms=fill_bar.time_ms,
        entry_price=fill_price, size_usd=target.size_usd, signal_snapshot=snapshot,
        commission_usd=commission, slippage_usd=slip_cost,
        stop_loss_price=target.stop_loss_price, take_profit_price=target.take_profit_price,
        risk_usd=risk_usd,
        trail_atr_mult=target.trail_atr_mult, target_atr_mult=target.target_atr_mult,
        breakeven_at_r=target.breakeven_at_r,
    )
    equity -= commission
    return trade, equity


def _close(trade: Trade, fill_bar: data_mod.Bar, position: Position,
           commission_bps: float, slippage_bps: float, equity: float,
           at_close: bool = False, exit_snapshot: Optional[dict] = None,
           reason: str = "signal", override_price: Optional[float] = None) -> float:
    """`at_close=True` solo para el cierre forzoso de fin de corrida (no
    hay "barra siguiente" cuyo open usar, asi que se liquida al close de
    la ultima barra disponible). El cierre normal, a mitad de corrida,
    siempre llena en el OPEN de `fill_bar` -- la barra que sigue a la de
    la señal, nunca su propio close (ver docstring del modulo).

    `override_price`: para un cierre por SL/TP, que no llena en el
    open NI en el close de la barra sino en el nivel exacto que se toco --
    ver el chequeo de stop/target en run_backtest_on_series. Sigue
    llevando slippage encima: un stop real en el mercado tambien puede
    ejecutarse peor que su precio de disparo."""
    raw_price = override_price if override_price is not None else (fill_bar.close if at_close else fill_bar.open)
    fill_price = _slip(raw_price, slippage_bps, _exit_direction(position.side))
    commission = position.size_usd * commission_bps / 1e4
    slip_cost = position.size_usd * slippage_bps / 1e4
    direction = 1 if position.side == "long" else -1
    price_pnl = position.size_usd * (fill_price / trade.entry_price - 1) * direction
    trade.exit_time_ms = fill_bar.time_ms
    trade.exit_price = fill_price
    trade.commission_usd += commission   # ahora es entrada + salida
    trade.slippage_usd += slip_cost      # idem -- desglose informativo, ver docstring de Trade
    trade.exit_signal_snapshot = exit_snapshot
    trade.exit_reason = reason
    # pnl_usd es el resultado economico TOTAL del trade -- resta la
    # comision de las DOS patas (trade.commission_usd, ya acumulada arriba),
    # no solo la de cierre, para que las estadisticas por trade (win_rate,
    # Sharpe sobre retornos por trade) reflejen el costo real completo.
    trade.pnl_usd = price_pnl - trade.commission_usd - trade.funding_paid_usd
    if trade.risk_usd:   # None o 0 (SL igual al precio de entrada, degenerado): no hay R que calcular
        trade.r_multiple = trade.pnl_usd / trade.risk_usd
    equity += price_pnl - commission
    return equity


def _liquidation_cluster(bar: data_mod.Bar, big_trades: list[data_mod.BigTrade],
                          trade_times: list[int], lookback_ms: int, mmr_table) -> dict:
    """Recalculado por barra a proposito (depende del precio actual, no
    solo de un indice que avanza) -- costo O(barras x trades en la
    ventana), aceptable para dias/semanas de barras horarias en el uso
    real. Si se mide lento con
    ventanas mas largas, se optimiza entonces -- igual que el cache de
    respuesta de /api/heatmap se añadio DESPUES de medir, no antes."""
    lo = bisect.bisect_left(trade_times, bar.time_ms - lookback_ms)
    hi = bisect.bisect_right(trade_times, bar.time_ms)
    window = big_trades[lo:hi]
    if not window:
        return indicators.liquidation_cluster_proximity(bar.close, [])
    trade_tuples = [(t.price, t.notional_usd, t.time_ms) for t in window]
    levels = compute_levels_from_trades(trade_tuples, mmr_table, basket=DEFAULT_BASKET)
    return indicators.liquidation_cluster_proximity(bar.close, levels)
