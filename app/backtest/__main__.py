"""CLI del backtester. Mismo estilo `argparse` que
`db.load_historical`.

    python -m backtest --symbol BTCUSDT --interval 1h --start 2026-07-01 --end 2026-08-01
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from . import data as data_mod
from . import engine
from . import journal
from . import statistics as stats_mod
from . import validation as validation_mod
from .strategies import STRATEGIES
from .strategies.funding_squeeze import DEFAULT_SIZE_USD

# Barras por año, para anualizar Sharpe/Sortino -- coincide con los
# intervalos que sirve la API (ver app/api/routers/candles.py).
PERIODS_PER_YEAR = {
    "1m": 365 * 24 * 60, "5m": 365 * 24 * 12, "15m": 365 * 24 * 4,
    "1h": 365 * 24, "4h": 365 * 6, "1d": 365,
}

# Grillas de parametros para `--validate` -- una por estrategia, aqui y
# no repartidas en cada archivo de strategies/, para que quede en un solo
# sitio cuantas configuraciones se prueban de verdad por cada una (el
# `trials_count` que exige el criterio 2 de validation.py sale de
# `len(grid)`, literal). Estrategias sin grid propio (funding_squeeze_*,
# de referencia, no candidatas a validar) usan una lista de un solo
# elemento con sus valores por defecto -- `--validate` sigue funcionando,
# solo que sin barrido real detras.
_OI_TREND_GRID = [
    {"oi_trend_zscore_threshold": t, "stop_atr_mult": s, "target_r": r}
    for t in (1.0, 1.5, 2.0)
    for s in (1.5, 2.0, 3.0)
    for r in (1.0, 1.5, 2.0)
]
# rsi_pullback_*: `max_funding_zscore` queda FUERA del barrido a
# proposito -- es una pregunta separada ("¿el filtro de derivados mejora
# un sistema de TA puro?", ver docstring de rsi_pullback.py), no una
# dimension mas para "encontrar la formula perfecta" en la misma pasada.
# Meterla aqui duplicaria trials_count sin necesidad para validar primero
# si la señal BASE (retroceso RSI dentro de una tendencia) tiene algo de
# edge. Mismo tamano de grilla (27) que _OI_TREND_GRID para que el
# `trials_count`/correccion de Benjamini-Hochberg no sea mas permisivo
# por casualidad.
_RSI_PULLBACK_LONG_GRID = [
    {"rsi_oversold": t, "stop_atr_mult": s, "target_r": r}
    for t in (30.0, 40.0, 50.0)
    for s in (1.5, 2.0, 3.0)
    for r in (1.0, 1.5, 2.0)
]
_RSI_PULLBACK_SHORT_GRID = [
    {"rsi_overbought": t, "stop_atr_mult": s, "target_r": r}
    for t in (50.0, 60.0, 70.0)
    for s in (1.5, 2.0, 3.0)
    for r in (1.0, 1.5, 2.0)
]
# volume_breakout_*: `risk_usd_target` fijo en 5.0 (5% de $100 de
# capital) en TODA la grilla, no barrido -- ya es una eleccion de diseño
# establecida (sizing volatility-adjusted), no una dimension mas para
# buscar. Lo que SI se barre es lo que ya se barria en las demas grillas:
# el umbral de entrada y el SL/TP -- mismo tamaño (27) que las otras,
# para que trials_count/Benjamini-Hochberg no sea mas permisivo por
# casualidad.
_VOLUME_BREAKOUT_SHORT_GRID = [
    {"volume_zscore_threshold": t, "stop_atr_mult": s, "target_r": r, "risk_usd_target": 5.0}
    for t in (1.5, 2.0, 2.5)
    for s in (1.5, 2.0, 3.0)
    for r in (1.0, 1.5, 2.0)
]
_VOLUME_BREAKOUT_LONG_GRID = [
    {"volume_zscore_threshold": t, "stop_atr_mult": s, "target_r": r, "risk_usd_target": 5.0}
    for t in (1.5, 2.0, 2.5)
    for s in (1.5, 2.0, 3.0)
    for r in (1.0, 1.5, 2.0)
]
# smc_confluence_*: sin umbral de entrada que barrer (la condicion de
# entrada es binaria -- tendencia + zona + vela de confirmacion, ver
# docstring de smc_confluence.py) -- solo SL/TP, mismo rango que el
# resto de las grillas. Grilla mas chica (9, no 27) a proposito: no hay
# un tercer eje real que barrer todavia, y rellenarla con un parametro
# artificial solo para igualar `trials_count` inflaria
# Benjamini-Hochberg sin agregar informacion.
_SMC_CONFLUENCE_GRID = [
    {"stop_atr_mult": s, "target_r": r, "risk_usd_target": 5.0}
    for s in (1.5, 2.0, 3.0)
    for r in (1.0, 1.5, 2.0)
]
# cvd_divergence_*: igual que smc_confluence, sin umbral de entrada que
# barrer (la condicion es binaria -- ruptura de 20 barras + CVD de esa
# ventana en contra, ver docstring de cvd_divergence.py) -- solo SL/TP.
#
# Se agregan 9 combos ADICIONALES con trailing stop + take-profit
# adaptable a la volatilidad (`trail_atr_mult`/`target_atr_mult`, ver
# TargetPosition en engine.py) para que el walk-forward compare
# honestamente los dos estilos de gestion de riesgo sobre la MISMA señal
# de entrada, tramo por tramo -- no se reemplaza la grilla estatica, se
# le suma: si el estilo adaptable de verdad ayuda, `pick_best` lo va a
# elegir solo en mas tramos sin que haga falta forzarlo. `stop_atr_mult`
# fijo en 2.0 para estos 9 (el riesgo INICIAL con el que se mide "R" no
# varia, ver docstring de Trade.trail_atr_mult) -- variar tambien esto
# multiplicaria la grilla sin agregar la pregunta que de verdad importa
# aca.
_CVD_DIVERGENCE_GRID = [
    {"stop_atr_mult": s, "target_r": r, "risk_usd_target": 5.0}
    for s in (1.5, 2.0, 3.0)
    for r in (1.0, 1.5, 2.0)
] + [
    {"stop_atr_mult": 2.0, "target_r": 1.5, "risk_usd_target": 5.0,
     "trail_atr_mult": tm, "target_atr_mult": gm}
    for tm in (1.0, 1.5, 2.0)
    for gm in (1.5, 2.0, 3.0)
]
# lvn_breakout_*: TP ESTRUCTURAL (el siguiente HVN del perfil de la
# sesion anterior, ver docstring de lvn_breakout.py) -- no hay `target_r`
# que barrer, a diferencia de las demas familias. Solo dos ejes reales:
# el umbral de volumen (unica condicion de entrada con un numero
# elegible) y el SL en ATR (unico parametro sin anclar a la estructura
# del perfil). Grilla de 9, igual que smc_confluence -- no se rellena
# con una tercera dimension artificial solo para igualar trials_count
# con las grillas de 27.
_LVN_BREAKOUT_GRID = [
    {"volume_zscore_threshold": t, "stop_atr_mult": s, "risk_usd_target": 5.0}
    for t in (1.5, 2.0, 2.5)
    for s in (1.5, 2.0, 3.0)
]
# frvp_absorption_*: SL y TP son 100% ESTRUCTURALES (low/high de la
# propia vela de absorcion, y el nivel opuesto del perfil -- ver
# docstring de frvp_absorption.py) -- CERO parametros de volatilidad que
# barrer, a diferencia de las demas familias. El unico eje real es el
# umbral de Delta. Grilla de 5, mas chica que las de 9 a proposito --
# inflarla con un segundo eje artificial (repitiendo el mismo umbral,
# por ejemplo) solo para "igualar" el tamaño de grilla de otras familias
# seria inventar grados de libertad que la señal no tiene.
_FRVP_ABSORPTION_GRID = [
    {"delta_zscore_threshold": t, "risk_usd_target": 5.0}
    for t in (1.0, 1.5, 2.0, 2.5, 3.0)
]
# cvd_divergence_breakeven_*: NO es una señal de entrada nueva -- es la
# MISMA señal de `cvd_divergence`, con un breakeven-stop agregado tras
# analizar los trades OOS ya guardados (`mfe_usd`/`risk_usd`, ver
# docstring de TargetPosition.breakeven_at_r en engine.py sobre la
# motivacion general del mecanismo). `stop_atr_mult`/`target_r` FIJOS en
# los defaults ya establecidos (2.0/1.5) -- el unico eje nuevo a barrer
# es `breakeven_at_r`, no reabrir ejes ya explorados en la version
# original.
_CVD_DIVERGENCE_BREAKEVEN_GRID = [
    {"breakeven_at_r": r, "risk_usd_target": 5.0}
    for r in (0.15, 0.2, 0.3, 0.5)
]
# smc_structure_*: las 3 variantes son condiciones binarias (sin umbral
# propio que barrer, mismo motivo que smc_confluence) -- solo SL/TP en
# ATR, misma grilla de 9 que smc_confluence para que
# trials_count/Benjamini-Hochberg no sea mas permisivo por casualidad
# entre familias comparables.
_SMC_STRUCTURE_GRID = [
    {"stop_atr_mult": s, "target_r": r, "risk_usd_target": 5.0}
    for s in (1.5, 2.0, 3.0)
    for r in (1.0, 1.5, 2.0)
]
# liquidity_zone_4h_*: dos ejes reales -- cuan cerca hay que estar del
# cluster+HVN (`cluster_proximity_pct`, el parametro mas incierto de la
# señal, ver docstring de la estrategia: un screen previo mostro que un
# umbral fijo puede ser demasiado laxo en algunos simbolos y razonable
# en otros, asi que se barre en vez de fijarlo a ojo) y el stop en ATR.
# `target_r`/`confirm_bars`/`vp_proximity_pct`/`oi_decline_threshold`/
# `min_cluster_notional_usd` quedan FIJOS en sus defaults -- no se abre un
# tercer eje sin necesidad (mismo criterio ya aplicado en smc_confluence/
# lvn_breakout/frvp_absorption). Grilla CHICA a proposito (4, no 18/27):
# `engine._liquidation_cluster` recalcula el heatmap completo desde
# `big_trades` CRUDOS en cada barra, un costo que NO depende de los
# parametros de la estrategia -- cada combo de la grilla paga ese costo
# de nuevo aunque el resultado del cluster en si sea identico entre
# combos, asi que una grilla mas grande multiplica tiempo de pared sin
# agregar informacion nueva sobre esta estrategia (ineficiencia del
# motor, no se "arregla" aca, ver la regla de no tocar engine.py sin
# preguntar).
_LIQUIDITY_ZONE_4H_GRID = [
    {"cluster_proximity_pct": cp, "stop_atr_mult": s, "risk_usd_target": 5.0}
    for cp in (0.005, 0.01)
    for s in (1.5, 2.5)
]
# funding_oi_confluence_*: grilla RECORTADA a 9 combos (no 36/81) por una
# razon de COSTO REAL medida, no una preferencia esteticia -- una corrida
# de calibracion con una grilla mas grande tardo mas de 2h de pared;
# extrapolar eso a un rango historico completo de varios años es
# inviable sin mas presupuesto de computo. `stop_atr_mult`/`target_r`
# quedan FIJOS en los defaults ya establecidos (2.0/1.5) -- no es la
# pregunta que esta hipotesis contesta (la pregunta real es CUAN
# extremos tienen que ser funding Y OI juntos, no el SL/TP) -- y se
# barren los DOS umbrales de entrada (3x3) que SI son el nucleo de la
# señal de confluencia. `risk_usd_target` fijo en 5.0 (default ya
# establecido, no se barre en silencio).
_FUNDING_OI_CONFLUENCE_GRID = [
    {
        "funding_z_threshold": fz, "oi_z_threshold": oz,
        "stop_atr_mult": 2.0, "target_r": 1.5, "risk_usd_target": 5.0,
    }
    for fz in (1.5, 2.0, 2.5)
    for oz in (0.5, 1.0, 1.5)
]
PARAM_GRIDS: dict[str, list[dict]] = {
    "oi_trend_continuation_long": _OI_TREND_GRID,
    "oi_trend_continuation_short": _OI_TREND_GRID,
    "rsi_pullback_long": _RSI_PULLBACK_LONG_GRID,
    "rsi_pullback_short": _RSI_PULLBACK_SHORT_GRID,
    "volume_breakout_long": _VOLUME_BREAKOUT_LONG_GRID,
    "volume_breakout_short": _VOLUME_BREAKOUT_SHORT_GRID,
    "smc_confluence_long": _SMC_CONFLUENCE_GRID,
    "smc_confluence_short": _SMC_CONFLUENCE_GRID,
    "cvd_divergence_long": _CVD_DIVERGENCE_GRID,
    "cvd_divergence_short": _CVD_DIVERGENCE_GRID,
    "lvn_breakout_long": _LVN_BREAKOUT_GRID,
    "lvn_breakout_short": _LVN_BREAKOUT_GRID,
    "frvp_absorption_long": _FRVP_ABSORPTION_GRID,
    "frvp_absorption_short": _FRVP_ABSORPTION_GRID,
    "cvd_divergence_breakeven_long": _CVD_DIVERGENCE_BREAKEVEN_GRID,
    "cvd_divergence_breakeven_short": _CVD_DIVERGENCE_BREAKEVEN_GRID,
    "smc_structure_trend_zone_long": _SMC_STRUCTURE_GRID,
    "smc_structure_trend_zone_short": _SMC_STRUCTURE_GRID,
    "smc_structure_choch_reversal_long": _SMC_STRUCTURE_GRID,
    "smc_structure_choch_reversal_short": _SMC_STRUCTURE_GRID,
    "smc_structure_fresh_mitigation_long": _SMC_STRUCTURE_GRID,
    "smc_structure_fresh_mitigation_short": _SMC_STRUCTURE_GRID,
    "liquidity_zone_4h_long": _LIQUIDITY_ZONE_4H_GRID,
    "liquidity_zone_4h_short": _LIQUIDITY_ZONE_4H_GRID,
    "funding_oi_confluence_long": _FUNDING_OI_CONFLUENCE_GRID,
    "funding_oi_confluence_short": _FUNDING_OI_CONFLUENCE_GRID,
}

# Temporalidad mayor por defecto para cualquier estrategia con
# `needs_htf_context = True` -- 1h, usada como contexto de tendencia.
# Constante del CLI, no de la estrategia (ver docstring de
# smc_confluence.py) -- expuesta igual como `--htf-interval` por si hace
# falta otra temporalidad mas adelante, sin tocar la estrategia.
DEFAULT_HTF_INTERVAL = "1h"


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="backtest")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--strategy", default="funding_squeeze_short", choices=sorted(STRATEGIES))
    ap.add_argument("--commission-bps", type=float, default=5.0)
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    ap.add_argument("--initial-equity", type=float, default=10_000.0)
    ap.add_argument("--size-usd", type=float, default=DEFAULT_SIZE_USD)
    ap.add_argument("--save", action="store_true",
                     help="guarda la corrida en backtest_runs/backtest_trades")
    ap.add_argument("--validate", action="store_true",
                     help=("en vez de una corrida unica, aplica el criterio completo de "
                           "validation.py: walk-forward anchored + barrido de "
                           "parametros por tramo + Benjamini-Hochberg + resultado "
                           "out-of-sample con su IC. Se guarda SIEMPRE en "
                           "backtest_validations (--save no aplica aqui, ignorado -- ese "
                           "flag es solo para una corrida unica, ver mas arriba), pase o "
                           "no el veredicto: un resultado que no pasa el criterio es un "
                           "resultado honesto que la vista \"Bots\" debe poder mostrar, no "
                           "un error que se descarta."))
    ap.add_argument("--min-train-days", type=int, default=180,
                     help="solo con --validate: dias minimos de la ventana de entrenamiento inicial")
    ap.add_argument("--test-days", type=int, default=30,
                     help="solo con --validate: dias de cada ventana de prueba out-of-sample")
    ap.add_argument("--htf-interval", default=DEFAULT_HTF_INTERVAL,
                     help=("temporalidad mayor para estrategias con needs_htf_context=True "
                           "(ej. smc_confluence_*, ver htf_context.py) -- ignorado por "
                           "cualquier otra estrategia."))
    args = ap.parse_args(argv)

    start = _parse_date(args.start)
    end = _parse_date(args.end)

    conn = data_mod.connect()
    try:
        strategy_cls = STRATEGIES[args.strategy]
        # Se carga UNA sola vez, antes de ramificar a --validate o a la
        # corrida unica -- las dos rutas la necesitan igual si la
        # estrategia la pide (mismo criterio que `real_liquidations` en
        # engine.run_backtest: ninguna estrategia que no la declare paga
        # este I/O extra).
        htf_bars = (
            data_mod.load_bars(conn, args.symbol, args.htf_interval, start, end)
            if getattr(strategy_cls, "needs_htf_context", False) else None
        )

        if args.validate:
            grid = PARAM_GRIDS.get(args.strategy, [{"size_usd": args.size_usd}])
            bars = data_mod.load_bars(conn, args.symbol, args.interval, start, end)
            funding_rows = data_mod.load_funding(conn, args.symbol, start, end)
            oi_rows = data_mod.load_oi_metrics(conn, args.symbol, start, end)
            big_trades = data_mod.load_big_trades(conn, args.symbol, start, end)
            mmr_table = data_mod.load_mmr_table(conn, args.symbol, end)
            report = validation_mod.walk_forward_validate(
                strategy_cls, grid, args.symbol, args.interval,
                bars, funding_rows, oi_rows, big_trades, mmr_table,
                start, end, args.min_train_days, args.test_days,
                commission_bps=args.commission_bps, slippage_bps=args.slippage_bps,
                strategy_key=args.strategy, htf_bars=htf_bars,
            )
            out = validation_mod.report_to_dict(report)
            print(json.dumps(out, indent=2, default=str))
            # Se guarda SIEMPRE, sin importar el veredicto -- un
            # resultado que no pasa el criterio es un resultado honesto
            # que se quiere poder ver en la vista "Bots", no un error
            # que se descarta. Antes `--validate` ni admitia `--save`
            # porque no habia donde guardar un reporte de N tramos --
            # ver backtest_validations en db/schema.sql.
            validation_id = journal.save_validation(
                conn, report, args.min_train_days, args.test_days,
            )
            print(f"validation_id={validation_id}")
            return 0

        strategy = strategy_cls(size_usd=args.size_usd)
        result = engine.run_backtest(
            conn, args.symbol, args.interval, start, end, strategy,
            strategy_name=args.strategy, strategy_params=strategy.params(),
            commission_bps=args.commission_bps, slippage_bps=args.slippage_bps,
            initial_equity=args.initial_equity, htf_interval=args.htf_interval,
        )
        periods_per_year = PERIODS_PER_YEAR.get(args.interval, 365 * 24)
        summary = stats_mod.summarize(result, periods_per_year)
        print(json.dumps(summary, indent=2, default=str))
        if args.save:
            run_id = journal.save_run(conn, result)
            print(f"run_id={run_id}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
