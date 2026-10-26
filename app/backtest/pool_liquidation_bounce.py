"""Pool multi-simbolo para `liquidation_bounce` -- misma logica de
`stage2.run_pooled_backtest` (mismas reglas y parametros, corridas por
simbolo, trades juntados en una sola muestra), adaptada a esta estrategia
porque necesita `data.load_bars_live` (Binance REST) en vez de
`data.load_bars` (DB) para CUALQUIER simbolo -- `candles` esta rezagado
globalmente (volcado mensual), no es un problema exclusivo de BTCUSDT.

El grabador graba liquidaciones REALES de los ~20 simbolos del universo
ya usado en el resto del proyecto, no solo BTCUSDT. Pooling es la forma
mas directa de ampliar la muestra real sin esperar a que pase mas
tiempo."""
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


def run_pooled_liquidation_bounce(
    symbols: list[str], interval: str, start: datetime, end: datetime, strategy_key: str,
    commission_bps: float = 5.0, slippage_bps: float = 2.0,
) -> dict:
    strategy_cls = STRATEGIES[strategy_key]
    per_symbol: dict[str, dict] = {}
    all_trades: list[Trade] = []

    conn = data_mod.connect()
    try:
        for symbol in symbols:
            bars = data_mod.load_bars_live(symbol, interval, start, end)
            if not bars:
                per_symbol[symbol] = {"trades": 0, "note": "sin velas en vivo para este rango"}
                continue
            funding_rows = data_mod.load_funding(conn, symbol, start, end)
            oi_rows = data_mod.load_oi_metrics(conn, symbol, start, end)
            big_trades = data_mod.load_big_trades(conn, symbol, start, end)
            mmr_table = data_mod.load_mmr_table(conn, symbol, end)
            real_liquidations = data_mod.load_real_liquidations(symbol, start, end)

            strategy = strategy_cls()
            result = engine.run_backtest_on_series(
                symbol, interval, start, end, bars, funding_rows, oi_rows, big_trades, mmr_table,
                strategy, strategy_key, strategy.params(),
                commission_bps=commission_bps, slippage_bps=slippage_bps,
                real_liquidations=real_liquidations,
            )
            closed = [t for t in result.trades if t.pnl_usd is not None]
            per_symbol[symbol] = {
                "trades": len(closed),
                "real_liquidation_events": len(real_liquidations),
                "win_rate": stats_mod.win_rate(result.trades),
                "expectancy_r": stats_mod.expectancy_r(result.trades),
                "expectancy_usd": stats_mod.expectancy_usd(result.trades),
                "profit_factor": stats_mod.profit_factor(result.trades),
                "net_performance_usd": stats_mod.net_performance_usd(result.trades),
            }
            all_trades.extend(result.trades)
    finally:
        conn.close()

    return {
        "strategy": strategy_key, "symbols": symbols, "interval": interval,
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
    ap = argparse.ArgumentParser(prog="backtest.pool_liquidation_bounce")
    ap.add_argument("--symbols", default=",".join(s for s in ALL_SYMBOLS if s != "TONUSDT"))
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--strategy", required=True,
                     choices=["liquidation_bounce_long", "liquidation_bounce_short"])
    args = ap.parse_args(argv)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    start, end = _parse_date(args.start), _parse_date(args.end)
    out = run_pooled_liquidation_bounce(symbols, args.interval, start, end, args.strategy)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
