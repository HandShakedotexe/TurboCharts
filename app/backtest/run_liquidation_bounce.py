"""Runner dedicado para `strategies/liquidation_bounce.py` -- EXCEPCION
documentada al patron del resto de `backtest/` (todo lo demas corre por
`__main__.py`/`stage2.py`, que asumen `data.load_bars` desde la DB).

Esta estrategia solo tiene historia real en el rango donde el grabador ya
corrio: `candles` (volcado MENSUAL) no llega ahi -- asi que hace falta
`data.load_bars_live` (Binance REST) en vez del loader normal. Las 4
tablas de la DB (candles/funding/OI/big_trades) estan todas rezagadas
respecto al rango que de verdad importa aqui.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from . import data as data_mod
from . import engine
from . import journal
from . import statistics as stats_mod
from .strategies import STRATEGIES

PERIODS_PER_YEAR_5M = 365 * 24 * 12


def run(
    symbol: str, interval: str, start: datetime, end: datetime, strategy_key: str,
    commission_bps: float = 5.0, slippage_bps: float = 2.0,
) -> engine.BacktestResult:
    strategy_cls = STRATEGIES[strategy_key]
    strategy = strategy_cls()

    bars = data_mod.load_bars_live(symbol, interval, start, end)

    conn = data_mod.connect()
    try:
        # funding_rate/open_interest_metrics estan rezagados -- se piden
        # igual (pueden venir vacios o parciales, el motor ya maneja
        # series vacias con su calentamiento en 0.0/neutral) en vez de
        # asumir que estan vacios: si algun dia se restablece esa
        # ingesta, este runner los usa sin cambios.
        funding_rows = data_mod.load_funding(conn, symbol, start, end)
        oi_rows = data_mod.load_oi_metrics(conn, symbol, start, end)
        big_trades = data_mod.load_big_trades(conn, symbol, start, end)
        mmr_table = data_mod.load_mmr_table(conn, symbol, end)
        real_liquidations = data_mod.load_real_liquidations(symbol, start, end)
    finally:
        conn.close()

    return engine.run_backtest_on_series(
        symbol, interval, start, end, bars, funding_rows, oi_rows, big_trades, mmr_table,
        strategy, strategy_key, strategy.params(),
        commission_bps=commission_bps, slippage_bps=slippage_bps,
        real_liquidations=real_liquidations,
    )


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="backtest.run_liquidation_bounce")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--strategy", required=True,
                     choices=["liquidation_bounce_long", "liquidation_bounce_short"])
    ap.add_argument("--commission-bps", type=float, default=5.0)
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args(argv)

    start, end = _parse_date(args.start), _parse_date(args.end)
    result = run(args.symbol, args.interval, start, end, args.strategy,
                 args.commission_bps, args.slippage_bps)
    summary = stats_mod.summarize(result, PERIODS_PER_YEAR_5M)
    print(json.dumps(summary, indent=2, default=str))
    if args.save:
        conn = data_mod.connect()
        try:
            run_id = journal.save_run(conn, result)
        finally:
            conn.close()
        print(f"run_id={run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
