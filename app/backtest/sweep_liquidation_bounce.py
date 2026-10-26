"""Barrido de `min_quote_qty_usd` para `liquidation_bounce` -- explora
umbrales menores y mayores en vez de fijar uno solo a ojo.

Carga las series UNA vez (evita repetir 14 veces la misma llamada de red a
Binance y las mismas consultas a la DB) y corre cada umbral sobre la MISMA
serie de datos -- misma disciplina que `stage2.py`, adaptada a un barrido
de un solo parametro en vez de un pool de simbolos.

Reporta TODAS las configuraciones, nunca solo la que mejor salio: con una
muestra tan chica (12 dias de trigger real), la correccion formal de
Benjamini-Hochberg que ya usa `validation.py` casi no tendria poder
estadistico -- la transparencia total del barrido es la salvaguarda real
contra elegir el umbral que "gano" por puro azar de muestra chica."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from . import data as data_mod
from . import engine
from . import statistics as stats_mod
from .strategies import STRATEGIES

DEFAULT_THRESHOLDS_USD = (
    50_000.0, 100_000.0, 150_000.0, 200_000.0, 300_000.0, 500_000.0, 1_000_000.0,
)


def run_sweep(
    symbol: str, interval: str, start: datetime, end: datetime, strategy_key: str,
    thresholds: tuple[float, ...] = DEFAULT_THRESHOLDS_USD,
    commission_bps: float = 5.0, slippage_bps: float = 2.0,
) -> list[dict]:
    strategy_cls = STRATEGIES[strategy_key]

    bars = data_mod.load_bars_live(symbol, interval, start, end)
    conn = data_mod.connect()
    try:
        funding_rows = data_mod.load_funding(conn, symbol, start, end)
        oi_rows = data_mod.load_oi_metrics(conn, symbol, start, end)
        big_trades = data_mod.load_big_trades(conn, symbol, start, end)
        mmr_table = data_mod.load_mmr_table(conn, symbol, end)
        real_liquidations = data_mod.load_real_liquidations(symbol, start, end)
    finally:
        conn.close()

    out = []
    for threshold in thresholds:
        strategy = strategy_cls(min_quote_qty_usd=threshold)
        result = engine.run_backtest_on_series(
            symbol, interval, start, end, bars, funding_rows, oi_rows, big_trades, mmr_table,
            strategy, strategy_key, strategy.params(),
            commission_bps=commission_bps, slippage_bps=slippage_bps,
            real_liquidations=real_liquidations,
        )
        out.append({
            "min_quote_qty_usd": threshold,
            "trades": sum(1 for t in result.trades if t.pnl_usd is not None),
            "win_rate": stats_mod.win_rate(result.trades),
            "win_rate_ci_95": stats_mod.win_rate_ci(result.trades, seed=0),
            "expectancy_r": stats_mod.expectancy_r(result.trades),
            "expectancy_r_ci_95": stats_mod.expectancy_r_ci(result.trades, seed=0),
            "expectancy_usd": stats_mod.expectancy_usd(result.trades),
            "profit_factor": stats_mod.profit_factor(result.trades),
            "net_performance_usd": stats_mod.net_performance_usd(result.trades),
            "sample_size": stats_mod.sample_size_flag(result.trades),
        })
    return out


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="backtest.sweep_liquidation_bounce")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--strategy", required=True,
                     choices=["liquidation_bounce_long", "liquidation_bounce_short"])
    ap.add_argument("--thresholds", default=",".join(str(t) for t in DEFAULT_THRESHOLDS_USD))
    args = ap.parse_args(argv)

    start, end = _parse_date(args.start), _parse_date(args.end)
    thresholds = tuple(float(t) for t in args.thresholds.split(","))
    results = run_sweep(args.symbol, args.interval, start, end, args.strategy, thresholds)
    print(json.dumps(results, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
