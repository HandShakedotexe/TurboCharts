"""Etapa 2 del funnel de descubrimiento: backtest real -- motor completo,
costos reales, SL/TP -- de una estrategia candidata, SIN barrido de
parametros y SIN walk-forward, sobre VARIOS simbolos a la vez. El filtro
barato entre el screen de features (Etapa 1, `screen.py`) y el compromiso
de un walk-forward completo (Etapa 3, `validation.py`, horas de computo)
-- un candidato que no muestra nada util aqui no merece ese gasto.

Tambien responde de la forma mas directa posible la pregunta de pool
multi-simbolo: mismas reglas y mismos parametros, corridas en cada
simbolo por separado (el motor no mezcla simbolos en una sola corrida) y
los trades resultantes juntados en una sola muestra estadistica. Sin
parametros elegidos DESPUES de ver el resultado (serian los mismos
defaults para cualquier simbolo), no hay overfitting que corregir con
Benjamini-Hochberg todavia -- esa correccion es para cuando SI se barre un
grid, en Etapa 3."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Callable

from . import data as data_mod
from . import engine
from . import statistics as stats_mod
from .engine import Strategy, Trade
from .screen import ALL_SYMBOLS
from .strategies import STRATEGIES


def run_pooled_backtest(
    conn,
    strategy_factory: Callable[[], Strategy],
    strategy_name: str,
    symbols: list[str],
    interval: str,
    start: datetime,
    end: datetime,
    commission_bps: float = 5.0,
    slippage_bps: float = 2.0,
) -> dict:
    per_symbol: dict[str, dict] = {}
    all_trades: list[Trade] = []
    for symbol in symbols:
        strat = strategy_factory()
        result = engine.run_backtest(
            conn, symbol, interval, start, end, strat,
            strategy_name=strategy_name, strategy_params=strat.params(),
            commission_bps=commission_bps, slippage_bps=slippage_bps,
        )
        closed = [t for t in result.trades if t.pnl_usd is not None]
        per_symbol[symbol] = {
            "trades": len(closed),
            "win_rate": stats_mod.win_rate(result.trades),
            "expectancy_r": stats_mod.expectancy_r(result.trades),
            "expectancy_usd": stats_mod.expectancy_usd(result.trades),
            "profit_factor": stats_mod.profit_factor(result.trades),
            "net_performance_usd": stats_mod.net_performance_usd(result.trades),
        }
        all_trades.extend(result.trades)

    return {
        "strategy": strategy_name, "symbols": symbols, "interval": interval,
        "start": start.isoformat(), "end": end.isoformat(),
        "per_symbol": per_symbol,
        "pooled": {
            "trades": sum(1 for t in all_trades if t.pnl_usd is not None),
            "win_rate": stats_mod.win_rate(all_trades),
            "win_rate_ci_95": stats_mod.win_rate_ci(all_trades, seed=0),
            "expectancy_r": stats_mod.expectancy_r(all_trades),
            "expectancy_r_ci_95": stats_mod.expectancy_r_ci(all_trades, seed=0),
            "expectancy_usd": stats_mod.expectancy_usd(all_trades),
            "profit_factor": stats_mod.profit_factor(all_trades),
            "net_performance_usd": stats_mod.net_performance_usd(all_trades),
            "sample_size": stats_mod.sample_size_flag(all_trades),
        },
    }


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="backtest.stage2")
    ap.add_argument("--strategy", required=True, choices=sorted(STRATEGIES))
    ap.add_argument("--symbols", default=",".join(ALL_SYMBOLS))
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--commission-bps", type=float, default=5.0)
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    ap.add_argument("--risk-usd-target", type=float, default=None,
                     help=("sizing por riesgo fijo en USD en vez de notional fijo -- solo "
                           "estrategias que acepten `risk_usd_target` en su constructor "
                           "(hoy: volume_breakout_*)."))
    args = ap.parse_args(argv)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    start, end = _parse_date(args.start), _parse_date(args.end)
    strategy_cls = STRATEGIES[args.strategy]
    if args.risk_usd_target is not None:
        strategy_cls = lambda: STRATEGIES[args.strategy](risk_usd_target=args.risk_usd_target)  # noqa: E731

    conn = data_mod.connect()
    try:
        out = run_pooled_backtest(
            conn, strategy_cls, args.strategy, symbols, args.interval, start, end,
            commission_bps=args.commission_bps, slippage_bps=args.slippage_bps,
        )
    finally:
        conn.close()

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
