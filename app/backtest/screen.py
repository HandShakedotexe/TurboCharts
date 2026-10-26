"""Screener Etapa 1 de un proceso progresivo de descubrimiento de
estrategias -- nunca una explosion combinatoria sin control.

Compara, SIN correr el motor de backtest, un feature causal en `t` contra el
retorno futuro sobre N barras via el information coefficient (Spearman) que
ya define `statistics.information_coefficient`. Es la unica forma barata de
descartar la mayoria de las ideas: un walk-forward completo de una sola
estrategia tarda horas; correr eso por cada combinacion de feature/simbolo/
horizonte antes de saber si el feature correlaciona con algo seria
exactamente la "explosion combinatoria" que este proceso progresivo evita.

Un IC alto aqui es NECESARIO, no SUFICIENTE: todavia falta convertir el
feature en reglas de entrada/salida explicitas, correr el motor con costos
reales, y pasar walk-forward (Etapa 2: backtest simple; Etapa 3:
walk-forward completo, ya existente en `validation.py`, solo para los
sobrevivientes de aqui).
"""
from __future__ import annotations

import argparse
import bisect
import json
from datetime import datetime, timezone
from typing import NamedTuple, Optional

from . import data as data_mod
from . import features as features_mod
from . import htf_context as htf_context_mod
from . import indicators as indicators_mod
from . import regime as regime_mod
from . import ta as ta_mod
from .statistics import information_coefficient

DEFAULT_HORIZONS_BARS = (4, 12, 24)

# Los 20 simbolos con backfill de candles/funding/OI -- 1h, no 5m: mismo
# cubrimiento estadistico con ~12x menos filas, y esto es un filtro
# barato, no la validacion final.
ALL_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "TRXUSDT",
    "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT", "TIAUSDT",
    "TONUSDT", "INJUSDT",
]


class FeatureResult(NamedTuple):
    feature: str
    symbol: str
    interval: str
    horizon_bars: int
    n: int
    ic: float


def _align_to_bars(bars: list[data_mod.Bar], source_times_ms: list[int], series: list[float]) -> list[float]:
    """Mismo patron de `engine.run_backtest_on_series` (bisect al ULTIMO
    valor conocido en o antes de cada barra, nunca al mas cercano en el
    tiempo sin importar direccion) -- reusar exactamente esa regla evita
    reintroducir aqui un desajuste de point-in-time: replicar la logica
    original en vez de consultar por ventana acotada puede dar un
    resultado distinto por unos pocos trades."""
    out = []
    for b in bars:
        idx = bisect.bisect_right(source_times_ms, b.time_ms) - 1
        out.append(series[idx] if idx >= 0 else 0.0)
    return out


def forward_return(bars: list[data_mod.Bar], horizon: int) -> list[Optional[float]]:
    """Retorno simple desde el CLOSE de la barra `i` hasta el close de la
    barra `i+horizon`. Es la unica pieza de este modulo que mira al futuro a
    proposito -- es el TARGET de una comparacion de investigacion offline,
    nunca una entrada de una estrategia en vivo (ver docstring del modulo)."""
    closes = [b.close for b in bars]
    n = len(closes)
    out: list[Optional[float]] = []
    for i in range(n):
        if i + horizon >= n or closes[i] == 0:
            out.append(None)
        else:
            out.append(closes[i + horizon] / closes[i] - 1.0)
    return out


def build_feature_set(
    bars: list[data_mod.Bar],
    funding_rows: list[data_mod.FundingPoint],
    oi_rows: list[data_mod.OiPoint],
) -> dict[str, list[float]]:
    """Todos los features causales disponibles HOY -- `indicators.py`,
    `ta.py`, `regime.py` y `features.py` -- alineados 1:1 con `bars`. Un
    solo lugar donde agregar un feature nuevo lo hace visible al screener
    automaticamente, sin tocar `run_screen`."""
    oi_times = [o.time_ms for o in oi_rows]
    funding_times = [f.time_ms for f in funding_rows]

    realized_vol_20 = features_mod.realized_vol(bars, 20)
    atr_series = regime_mod.atr(bars)
    volumes = [b.volume for b in bars]

    return {
        "log_return_1bar": features_mod.log_returns(bars),
        "momentum_20": features_mod.momentum(bars, 20),
        "momentum_50": features_mod.momentum(bars, 50),
        "realized_vol_20": realized_vol_20,
        "volatility_zscore_60": features_mod.rolling_zscore(realized_vol_20, 60),
        "candle_body_ratio": features_mod.candle_body_ratio(bars),
        "taker_buy_ratio": features_mod.taker_buy_ratio(bars),
        "volume_zscore_60": features_mod.rolling_zscore(volumes, 60),
        "rsi_14": ta_mod.rsi(bars),
        "atr_pct": [a / b.close if b.close else 0.0 for a, b in zip(atr_series, bars)],
        "oi_trend": _align_to_bars(bars, oi_times, indicators_mod.oi_trend(oi_rows)),
        "oi_trend_zscore": _align_to_bars(bars, oi_times, indicators_mod.oi_trend_zscore(oi_rows)),
        "long_short_ratio_trend": _align_to_bars(bars, oi_times, indicators_mod.long_short_ratio_trend(oi_rows)),
        "funding_zscore": _align_to_bars(bars, funding_times, indicators_mod.funding_zscore(funding_rows)),
    }


def screen_symbol(
    bars: list[data_mod.Bar],
    funding_rows: list[data_mod.FundingPoint],
    oi_rows: list[data_mod.OiPoint],
    symbol: str,
    interval: str,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS_BARS,
    min_n: int = 500,
) -> list[FeatureResult]:
    """IC de cada feature contra cada horizonte de retorno futuro, para UN
    simbolo ya cargado. `min_n`: un simbolo con poco historial (ej. TONUSDT,
    listado 2024) puede no tener suficientes barras para un horizonte largo
    -- se omite esa combinacion en vez de reportar un IC calculado sobre una
    muestra demasiado chica para significar algo."""
    if len(bars) < min_n:
        return []
    features = build_feature_set(bars, funding_rows, oi_rows)
    out = []
    for horizon in horizons:
        fwd = forward_return(bars, horizon)
        # Recortar al tramo con target definido (sin los ultimos `horizon`
        # NULOS) -- information_coefficient exige listas del mismo largo.
        valid_n = len(fwd) - horizon
        if valid_n < min_n:
            continue
        fwd_valid = fwd[:valid_n]
        assert all(v is not None for v in fwd_valid)
        for name, series in features.items():
            ic = information_coefficient(series[:valid_n], fwd_valid)
            out.append(FeatureResult(name, symbol, interval, horizon, valid_n, ic))
    return out


def run_screen(
    conn,
    symbols: list[str],
    interval: str,
    start: datetime,
    end: datetime,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS_BARS,
) -> list[FeatureResult]:
    out: list[FeatureResult] = []
    for symbol in symbols:
        bars = data_mod.load_bars(conn, symbol, interval, start, end)
        funding_rows = data_mod.load_funding(conn, symbol, start, end)
        oi_rows = data_mod.load_oi_metrics(conn, symbol, start, end)
        out.extend(screen_symbol(bars, funding_rows, oi_rows, symbol, interval, horizons))
    return out


def build_liquidation_cluster_features(
    bars: list[data_mod.Bar],
    big_trades: list[data_mod.BigTrade],
    mmr_table,
    lookback_hours: float = 24.0,
) -> dict[str, list[float]]:
    """Evalua la hipotesis de "pool de liquidaciones" -- no
    `indicators.oi_trend` ni los otros features de derivados genericos ya
    screeneados, sino la MISMA funcion que ya usa `funding_squeeze` en
    produccion: `engine._liquidation_cluster` (ventana movil de 24h de
    `big_trades` reales, niveles modelados via `compute_levels_from_trades`
    -- la textura de un heatmap publicado, no una unica linea sintetica
    desde el OI agregado de ahora mismo). Se reusa esa funcion tal cual,
    no una version "equivalente" reescrita aqui -- replicar el metodo
    exacto evita reintroducir un desajuste ya visto en otra parte del
    proyecto.

    Requiere `big_trades` reales -- solo BTCUSDT tiene esta tabla
    cargada, asi que a diferencia de `build_feature_set` (20 simbolos,
    historico largo) esta funcion solo puede correr sobre esa ventana
    angosta. No se aproxima con otro simbolo ni otro rango: sin
    `big_trades` reales esto es `DATA UNAVAILABLE`, no una version peor
    del mismo feature."""
    from .engine import _liquidation_cluster

    trade_times = [t.time_ms for t in big_trades]
    lookback_ms = int(lookback_hours * 3_600_000)
    long_dist, short_dist, long_notional, short_notional = [], [], [], []
    for bar in bars:
        cluster = _liquidation_cluster(bar, big_trades, trade_times, lookback_ms, mmr_table)
        below, above = cluster["long_cluster_below"], cluster["short_cluster_above"]
        # Sin cluster de verdad en ese lado (ventana de 24h sin trades
        # todavia, solo puede pasar en las primeras horas del rango) --
        # 1.0 (100% de distancia) como centinela de "no hay nada cerca",
        # nunca 0.0 (que significaria "el precio ESTA sobre el cluster").
        long_dist.append(below["distance_pct"] if below["distance_pct"] is not None else 1.0)
        short_dist.append(above["distance_pct"] if above["distance_pct"] is not None else 1.0)
        long_notional.append(below["notional_usd"])
        short_notional.append(above["notional_usd"])

    imbalance = []
    for ln, sn in zip(long_notional, short_notional):
        total = ln + sn
        # Positivo: mas liquidez modelada arriba (lado corto) que abajo --
        # si la hipotesis de "iman" fuera cierta, un valor positivo grande
        # deberia anticipar un retorno futuro tambien positivo (el precio
        # atraido hacia el cluster mas denso). Si en cambio domina la
        # hipotesis de "rechazo" (soporte/resistencia), el signo saldria
        # invertido. El IC de abajo responde cual, si alguna, se sostiene
        # en los datos -- no se asume ninguna de las dos de entrada.
        imbalance.append((sn - ln) / total if total > 0 else 0.0)

    return {
        "liq_cluster_imbalance": imbalance,
        "liq_long_cluster_notional_usd": long_notional,
        "liq_short_cluster_notional_usd": short_notional,
        "liq_long_cluster_distance_pct": long_dist,
        "liq_short_cluster_distance_pct": short_dist,
    }


def screen_liquidation_cluster(
    bars: list[data_mod.Bar],
    big_trades: list[data_mod.BigTrade],
    mmr_table,
    symbol: str,
    interval: str,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS_BARS,
    lookback_hours: float = 24.0,
    min_n: int = 200,
) -> list[FeatureResult]:
    if len(bars) < min_n:
        return []
    features = build_liquidation_cluster_features(bars, big_trades, mmr_table, lookback_hours)
    out = []
    for horizon in horizons:
        fwd = forward_return(bars, horizon)
        valid_n = len(fwd) - horizon
        if valid_n < min_n:
            continue
        fwd_valid = fwd[:valid_n]
        for name, series in features.items():
            ic = information_coefficient(series[:valid_n], fwd_valid)
            out.append(FeatureResult(name, symbol, interval, horizon, valid_n, ic))
    return out


def build_smc_features(bars: list[data_mod.Bar], lookback: int = 2) -> dict[str, list[float]]:
    """Features de estructura SMC (`smc.py` via `htf_context.py`) usada
    como señal de ENTRADA en su PROPIA temporalidad, no como contexto de
    una menor -- ver docstring de `strategies/smc_structure.py`. Se
    reusan las MISMAS
    consultas point-in-time que ya usa el motor (`trend_at`/
    `zone_signal_at`), llamadas con `htf_idx = i` (la propia barra) para
    cada `i` -- exactamente lo que `engine.run_backtest_on_series` hace
    cuando `htf_bars` es la MISMA serie que `bars` (mismo `time_ms`,
    `bisect_right` cae siempre en la barra actual).

    Cuatro features, point-in-time por construccion (ninguno mira mas
    alla de `i`):
    - `smc_trend_signed`: +1 "up" / -1 "down" / 0 sin estructura todavia.
    - `smc_zone_signed`: +1 zona Order Block bullish activa / -1 bearish
      / 0 sin zona.
    - `smc_choch_signed`: +1 SOLO en la barra donde `trend` CAMBIA a "up"
      (ChoCH real, nunca un BOS de continuacion -- ver docstring del
      modulo de estrategias), -1 simetrico, 0 en cualquier otra barra.
    - `smc_fresh_zone_signed`: +1 SOLO en la barra donde `zone` CAMBIA a
      "bullish" (el precio ACABA de entrar a la zona), -1 simetrico, 0 en
      cualquier otra barra.

    Deliberadamente NO se calcula un feature de Fair Value Gap: el motor
    (`engine.py`, que este cambio no toca) no expone `fair_value_gaps` en
    `ctx.indicators` todavia -- cualquier feature de FVG aqui mediria algo
    que ninguna estrategia real podria usar hoy sin antes ampliar el
    motor, lo que violaria la regla de no tocar engine.py sin un bug
    real."""
    ctx = htf_context_mod.build_htf_context(bars, lookback)
    trend_signed, zone_signed, choch_signed, fresh_zone_signed = [], [], [], []
    prev_trend: Optional[str] = None
    prev_zone: Optional[str] = None
    for i, bar in enumerate(bars):
        trend = htf_context_mod.trend_at(ctx, i)
        zone = htf_context_mod.zone_signal_at(ctx, i, bar.close)
        trend_signed.append(1.0 if trend == "up" else (-1.0 if trend == "down" else 0.0))
        zone_signed.append(1.0 if zone == "bullish" else (-1.0 if zone == "bearish" else 0.0))
        choch_signed.append(
            1.0 if (trend == "up" and prev_trend != "up")
            else (-1.0 if (trend == "down" and prev_trend != "down") else 0.0)
        )
        fresh_zone_signed.append(
            1.0 if (zone == "bullish" and prev_zone != "bullish")
            else (-1.0 if (zone == "bearish" and prev_zone != "bearish") else 0.0)
        )
        prev_trend, prev_zone = trend, zone
    return {
        "smc_trend_signed": trend_signed,
        "smc_zone_signed": zone_signed,
        "smc_choch_signed": choch_signed,
        "smc_fresh_zone_signed": fresh_zone_signed,
    }


def screen_smc(
    bars: list[data_mod.Bar],
    symbol: str,
    interval: str,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS_BARS,
    lookback: int = 2,
    min_n: int = 500,
) -> list[FeatureResult]:
    if len(bars) < min_n:
        return []
    features = build_smc_features(bars, lookback)
    out = []
    for horizon in horizons:
        fwd = forward_return(bars, horizon)
        valid_n = len(fwd) - horizon
        if valid_n < min_n:
            continue
        fwd_valid = fwd[:valid_n]
        for name, series in features.items():
            ic = information_coefficient(series[:valid_n], fwd_valid)
            out.append(FeatureResult(name, symbol, interval, horizon, valid_n, ic))
    return out


def summarize_by_feature(results: list[FeatureResult]) -> list[dict]:
    """Un feature que solo funciona en UN simbolo es sospechoso de azar --
    se prefiere estabilidad sobre un pico aislado, asi que se agrupa por
    (feature, horizonte) y se reporta la mediana del IC entre simbolos
    junto al peor y mejor caso, no solo el mejor simbolo individual."""
    import statistics as pystats
    from collections import defaultdict

    buckets: dict[tuple[str, int], list[float]] = defaultdict(list)
    for r in results:
        buckets[(r.feature, r.horizon_bars)].append(r.ic)
    out = []
    for (feature, horizon), ics in buckets.items():
        out.append({
            "feature": feature,
            "horizon_bars": horizon,
            "n_symbols": len(ics),
            "median_ic": pystats.median(ics),
            "min_ic": min(ics),
            "max_ic": max(ics),
            "mean_abs_ic": pystats.fmean(abs(x) for x in ics),
        })
    out.sort(key=lambda d: abs(d["median_ic"]), reverse=True)
    return out


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="backtest.screen")
    ap.add_argument("--symbols", default=",".join(ALL_SYMBOLS),
                     help="lista separada por comas, default: los 20 simbolos con backfill")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--horizons", default=",".join(str(h) for h in DEFAULT_HORIZONS_BARS),
                     help="barras hacia adelante, separadas por comas")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--liquidation", action="store_true",
                     help=("en vez del screen de features generales, evalua la hipotesis de "
                           "pool de liquidaciones (engine._liquidation_cluster sobre big_trades "
                           "reales) -- solo BTCUSDT tiene esa tabla, --symbols se ignora en este modo."))
    ap.add_argument("--smc", action="store_true",
                     help=("en vez del screen de features generales, screenea estructura SMC "
                           "calculada en su PROPIA temporalidad (trend/zona/ChoCH/mitigacion "
                           "fresca, ver screen.build_smc_features) -- usa --symbols/--interval normal."))
    args = ap.parse_args(argv)

    horizons = tuple(int(h) for h in args.horizons.split(","))
    start, end = _parse_date(args.start), _parse_date(args.end)

    conn = data_mod.connect()
    try:
        if args.liquidation:
            symbols = ["BTCUSDT"]
            bars = data_mod.load_bars(conn, symbols[0], args.interval, start, end)
            big_trades = data_mod.load_big_trades(conn, symbols[0], start, end)
            mmr_table = data_mod.load_mmr_table(conn, symbols[0], end)
            results = screen_liquidation_cluster(bars, big_trades, mmr_table, symbols[0], args.interval, horizons)
        elif args.smc:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
            results = []
            for symbol in symbols:
                bars = data_mod.load_bars(conn, symbol, args.interval, start, end)
                results.extend(screen_smc(bars, symbol, args.interval, horizons))
        else:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
            results = run_screen(conn, symbols, args.interval, start, end, horizons)
    finally:
        conn.close()

    per_symbol_top = sorted(results, key=lambda r: abs(r.ic), reverse=True)[:args.top]
    by_feature = summarize_by_feature(results)

    print(json.dumps({
        "liquidation_mode": args.liquidation,
        "n_symbols_screened": len(symbols),
        "n_feature_symbol_horizon_combos": len(results),
        "by_feature_ranked": by_feature,
        "top_individual_combos": [r._asdict() for r in per_symbol_top],
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
