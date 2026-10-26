"""Solo `load_real_liquidations` -- el unico loader de `backtest/data.py`
que NO toca la DB (lee ficheros del grabador, ver su docstring). El resto
de loaders de ese modulo (`load_bars`, `load_funding`, etc.) necesitan una
conexion real y no se prueban aqui, mismo criterio que el resto del
proyecto (`python -m unittest discover`, sin red/DB)."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backtest.data import load_real_liquidations


def _force_order(symbol: str, order_side: str, price: float, qty: float, event_time_ms: int) -> dict:
    return {
        "stream": "!forceOrder@arr",
        "data": {
            "e": "forceOrder", "E": event_time_ms,
            "o": {
                "s": symbol, "S": order_side, "o": "LIMIT", "f": "IOC",
                "q": str(qty), "p": str(price), "ap": str(price),
                "X": "FILLED", "l": str(qty), "z": str(qty), "T": event_time_ms,
            },
        },
    }


def _write_line(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


class TestLoadRealLiquidations(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _hour_path(self, dt: datetime) -> Path:
        return (self.data_dir / "raw" / "binance" / "liquidation"
                / dt.strftime("%Y-%m-%d") / f"{dt.strftime('%H')}.jsonl")

    def test_normalizes_filters_by_symbol_and_side_is_the_liquidated_position(self):
        dt = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        t = int(dt.timestamp() * 1000)
        path = self._hour_path(dt)
        _write_line(path, {"rx": t, "ex": "binance", "st": "liquidation",
                            "raw": _force_order("BTCUSDT", "SELL", 60000.0, 0.5, t)})
        _write_line(path, {"rx": t, "ex": "binance", "st": "liquidation",
                            "raw": _force_order("ETHUSDT", "BUY", 3000.0, 2.0, t)})
        start = dt - timedelta(hours=1)
        end = dt + timedelta(hours=1)
        events = load_real_liquidations("BTCUSDT", start, end, data_dir=self.data_dir)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].side, "long")  # SELL forzado = se liquido un long
        self.assertEqual(events[0].price, 60000.0)
        self.assertAlmostEqual(events[0].quote_qty_usd, 30000.0)

    def test_sorted_ascending_by_time(self):
        dt = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        t1, t2 = int(dt.timestamp() * 1000), int(dt.timestamp() * 1000) + 5000
        path = self._hour_path(dt)
        _write_line(path, {"rx": t2, "ex": "binance", "st": "liquidation",
                            "raw": _force_order("BTCUSDT", "BUY", 61000.0, 0.3, t2)})
        _write_line(path, {"rx": t1, "ex": "binance", "st": "liquidation",
                            "raw": _force_order("BTCUSDT", "SELL", 60000.0, 0.5, t1)})
        events = load_real_liquidations(
            "BTCUSDT", dt - timedelta(minutes=1), dt + timedelta(minutes=1), data_dir=self.data_dir,
        )
        self.assertEqual([e.time_ms for e in events], [t1, t2])

    def test_excludes_events_after_end(self):
        dt = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        t_in = int(dt.timestamp() * 1000)
        t_after = int((dt + timedelta(hours=2)).timestamp() * 1000)
        path_in = self._hour_path(dt)
        path_after = self._hour_path(dt + timedelta(hours=2))
        _write_line(path_in, {"rx": t_in, "ex": "binance", "st": "liquidation",
                               "raw": _force_order("BTCUSDT", "SELL", 60000.0, 0.5, t_in)})
        _write_line(path_after, {"rx": t_after, "ex": "binance", "st": "liquidation",
                                  "raw": _force_order("BTCUSDT", "SELL", 60000.0, 0.5, t_after)})
        events = load_real_liquidations(
            "BTCUSDT", dt - timedelta(minutes=1), dt + timedelta(minutes=1), data_dir=self.data_dir,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].time_ms, t_in)


if __name__ == "__main__":
    unittest.main()
