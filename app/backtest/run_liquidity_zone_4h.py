"""Runner dedicado para `strategies/liquidity_zone_4h.py` -- misma
EXCEPCION ya documentada en `run_liquidation_bounce.py`: la ventana real
donde `big_trades` existe para BTCUSDT y ETHUSDT cae casi enteramente
DESPUES de donde llega `candles` archivado -- asi que las velas de 4h se
piden a Binance REST (`data.load_bars_live`, que ya soporta `4h` nativo)
en vez del loader normal de la DB.

Dos modos:
    --mode single   Etapa 2 -- una corrida simple por simbolo, SIN
                     walk-forward, para confirmar que la señal dispara y
                     el SL/TP/sizing funcionan antes de pagar el costo de
                     un walk-forward completo (igual criterio que
                     `stage2.py` para el resto del proyecto).
    --mode validate  Etapa 3 -- walk-forward anchored con los DOS simbolos
                     POOLED dentro de cada tramo (`multi_symbol_validation.
                     multi_symbol_walk_forward_validate`, ya construido y
                     usado por otras familias con el mismo problema de
                     muestra chica en un solo simbolo) -- se persiste
                     SIEMPRE en `backtest_validations`, pase o no el
                     veredicto, igual que `--validate` en `__main__.py`.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from . import data as data_mod
from . import engine
from . import journal
from . import statistics as stats_mod
from .__main__ import PARAM_GRIDS
from .multi_symbol_validation import multi_symbol_walk_forward_validate
from .strategies import STRATEGIES
from .validation import report_to_dict

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
PERIODS_PER_YEAR_4H = 365 * 6


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _load_symbol_data(symbol: str, start: datetime, end: datetime) -> dict:
    bars = data_mod.load_bars_live(symbol, "4h", start, end)
    conn = data_mod.connect()
    try:
        funding = data_mod.load_funding(conn, symbol, start, end)
        oi = data_mod.load_oi_metrics(conn, symbol, start, end)
        big_trades = data_mod.load_big_trades(conn, symbol, start, end)
        mmr_table = data_mod.load_mmr_table(conn, symbol, end)
    finally:
        conn.close()
    return {"bars": bars, "funding": funding, "oi": oi, "big_trades": big_trades, "mmr_table": mmr_table}


def run_single(symbol: str, start: datetime, end: datetime, strategy_key: str,
                commission_bps: float, slippage_bps: float) -> dict:
    strategy_cls = STRATEGIES[strategy_key]
    strategy = strategy_cls()
    d = _load_symbol_data(symbol, start, end)
    result = engine.run_backtest_on_series(
        symbol, "4h", start, end, d["bars"], d["funding"], d["oi"], d["big_trades"], d["mmr_table"],
        strategy, strategy_key, strategy.params(),
        commission_bps=commission_bps, slippage_bps=slippage_bps,
    )
    return stats_mod.summarize(result, PERIODS_PER_YEAR_4H)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="backtest.run_liquidity_zone_4h")
    ap.add_argument("--mode", choices=["single", "validate"], required=True)
    ap.add_argument("--symbol", default="BTCUSDT", help="solo para --mode single")
    ap.add_argument("--strategy", required=True,
                     choices=["liquidity_zone_4h_long", "liquidity_zone_4h_short"])
    ap.add_argument("--start", default="2026-06-24", help="YYYY-MM-DD")
    ap.add_argument("--end", default="2026-09-19", help="YYYY-MM-DD")
    ap.add_argument("--min-train-days", type=int, default=40)
    ap.add_argument("--test-days", type=int, default=15)
    ap.add_argument("--commission-bps", type=float, default=5.0)
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    args = ap.parse_args(argv)

    start, end = _parse_date(args.start), _parse_date(args.end)

    if args.mode == "single":
        summary = run_single(args.symbol, start, end, args.strategy, args.commission_bps, args.slippage_bps)
        print(json.dumps(summary, indent=2, default=str))
        return 0

    data_by_symbol = {symbol: _load_symbol_data(symbol, start, end) for symbol in SYMBOLS}
    grid = PARAM_GRIDS.get(args.strategy, [{}])
    strategy_cls = STRATEGIES[args.strategy]
    report = multi_symbol_walk_forward_validate(
        strategy_cls, grid, SYMBOLS, "4h", data_by_symbol,
        start, end, args.min_train_days, args.test_days,
        commission_bps=args.commission_bps, slippage_bps=args.slippage_bps,
        strategy_key=args.strategy,
    )
    out = report_to_dict(report)
    print(json.dumps(out, indent=2, default=str))
    conn = data_mod.connect()
    try:
        validation_id = journal.save_validation(conn, report, args.min_train_days, args.test_days)
    finally:
        conn.close()
    print(f"validation_id={validation_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
