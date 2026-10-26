"""CLI para `multi_symbol_validation.py` -- walk-forward real con los
simbolos pooled DENTRO de cada tramo, no `stage2.py` (pooled sin split)
ni `validation.walk_forward_validate` (un solo simbolo). Reusa
`PARAM_GRIDS` de `__main__.py` -- una sola fuente de verdad para que
grilla le corresponde a cada estrategia, sea el walk-forward de un
simbolo o de varios."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from . import data as data_mod
from . import journal
from .__main__ import PARAM_GRIDS
from .multi_symbol_validation import multi_symbol_walk_forward_validate
from .screen import ALL_SYMBOLS
from .strategies import STRATEGIES
from .validation import report_to_dict, verdict


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="backtest.run_multi_symbol_validate")
    ap.add_argument("--symbols", default=",".join(ALL_SYMBOLS))
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--strategy", required=True, choices=sorted(STRATEGIES))
    ap.add_argument("--min-train-days", type=int, default=180)
    ap.add_argument("--test-days", type=int, default=30)
    ap.add_argument("--commission-bps", type=float, default=5.0)
    ap.add_argument("--slippage-bps", type=float, default=2.0)
    ap.add_argument("--checkpoint", default=None,
                    help=("archivo JSON donde se guarda cada tramo al terminar; si ya existe "
                          "con la misma configuracion, la corrida retoma desde el ultimo tramo "
                          "guardado en vez de empezar de cero"))
    args = ap.parse_args(argv)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    start, end = _parse_date(args.start), _parse_date(args.end)
    strategy_cls = STRATEGIES[args.strategy]
    grid = PARAM_GRIDS.get(args.strategy, [{}])

    conn = data_mod.connect()
    try:
        data_by_symbol = {}
        for symbol in symbols:
            data_by_symbol[symbol] = {
                "bars": data_mod.load_bars(conn, symbol, args.interval, start, end),
                "funding": data_mod.load_funding(conn, symbol, start, end),
                "oi": data_mod.load_oi_metrics(conn, symbol, start, end),
                "big_trades": data_mod.load_big_trades(conn, symbol, start, end),
                "mmr_table": data_mod.load_mmr_table(conn, symbol, end),
            }
    finally:
        conn.close()

    report = multi_symbol_walk_forward_validate(
        strategy_cls, grid, symbols, args.interval, data_by_symbol,
        start, end, args.min_train_days, args.test_days,
        commission_bps=args.commission_bps, slippage_bps=args.slippage_bps,
        strategy_key=args.strategy, checkpoint_path=args.checkpoint,
    )
    out = report_to_dict(report)
    print(json.dumps(out, indent=2, default=str))

    conn = data_mod.connect()
    try:
        validation_id = journal.save_validation(conn, report, args.min_train_days, args.test_days)
        print(f"validation_id={validation_id}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
