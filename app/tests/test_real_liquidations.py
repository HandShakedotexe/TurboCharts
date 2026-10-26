import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from api.real_liquidations import fetch_recent_liquidations


def _force_order(symbol: str, order_side: str, price: float, qty: float, event_time_ms: int) -> dict:
    """Mismo shape que el payload real de Binance !forceOrder@arr, segun
    la muestra verificada contra disco."""
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


class TestFetchRecentLiquidations(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self._tmp.cleanup()

    def _hour_path(self, dt: datetime) -> Path:
        # Mismo layout que recorder/liqrec/writer.py:
        # {root}/raw/{exchange}/{stream}/{YYYY-MM-DD}/{HH}.jsonl
        return (self.data_dir / "raw" / "binance" / "liquidation"
                / dt.strftime("%Y-%m-%d") / f"{dt.strftime('%H')}.jsonl")

    def test_normalizes_and_filters_by_symbol(self):
        t = int(self.now.timestamp() * 1000)
        path = self._hour_path(self.now)
        _write_line(path, {"rx": t, "ex": "binance", "st": "liquidation",
                            "raw": _force_order("BTCUSDT", "SELL", 60000.0, 0.5, t)})
        _write_line(path, {"rx": t, "ex": "binance", "st": "liquidation",
                            "raw": _force_order("ETHUSDT", "BUY", 3000.0, 2.0, t)})
        events = fetch_recent_liquidations("BTCUSDT", data_dir=self.data_dir)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].symbol, "BTCUSDT")
        self.assertEqual(events[0].side, "long")  # SELL forzado = se liquido un long
        self.assertEqual(events[0].price, 60000.0)
        self.assertAlmostEqual(events[0].quote_qty, 30000.0)

    def test_most_recent_first(self):
        t1 = int(self.now.timestamp() * 1000) - 5000
        t2 = int(self.now.timestamp() * 1000)
        path = self._hour_path(self.now)
        _write_line(path, {"rx": t1, "ex": "binance", "st": "liquidation",
                            "raw": _force_order("BTCUSDT", "SELL", 60000.0, 0.5, t1)})
        _write_line(path, {"rx": t2, "ex": "binance", "st": "liquidation",
                            "raw": _force_order("BTCUSDT", "BUY", 61000.0, 0.3, t2)})
        events = fetch_recent_liquidations("BTCUSDT", data_dir=self.data_dir)
        self.assertEqual([e.event_time for e in events], [t2, t1])

    def test_skips_corrupt_lines_without_failing(self):
        """Una linea cortada a media escritura (ver writer.py) no debe
        tumbar toda la lectura -- se salta y sigue con las demas."""
        path = self._hour_path(self.now)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("not json at all\n")
        t = int(self.now.timestamp() * 1000)
        _write_line(path, {"rx": t, "ex": "binance", "st": "liquidation",
                            "raw": _force_order("BTCUSDT", "SELL", 60000.0, 0.5, t)})
        events = fetch_recent_liquidations("BTCUSDT", data_dir=self.data_dir)
        self.assertEqual(len(events), 1)

    def test_no_files_returns_empty(self):
        self.assertEqual(fetch_recent_liquidations("BTCUSDT", data_dir=self.data_dir), [])

    def test_reads_previous_hour_too(self):
        """Cubre el caso de justo despues de una rotacion de hora -- ver
        docstring de _hour_files."""
        prev = self.now - timedelta(hours=1)
        t = int(prev.timestamp() * 1000)
        _write_line(self._hour_path(prev), {"rx": t, "ex": "binance", "st": "liquidation",
                                             "raw": _force_order("BTCUSDT", "SELL", 60000.0, 0.5, t)})
        events = fetch_recent_liquidations("BTCUSDT", data_dir=self.data_dir)
        self.assertEqual(len(events), 1)

    def test_respects_limit(self):
        path = self._hour_path(self.now)
        base = int(self.now.timestamp() * 1000)
        for i in range(5):
            _write_line(path, {"rx": base + i, "ex": "binance", "st": "liquidation",
                                "raw": _force_order("BTCUSDT", "SELL", 60000.0 + i, 0.1, base + i)})
        events = fetch_recent_liquidations("BTCUSDT", limit=2, data_dir=self.data_dir)
        self.assertEqual(len(events), 2)


if __name__ == "__main__":
    unittest.main()
