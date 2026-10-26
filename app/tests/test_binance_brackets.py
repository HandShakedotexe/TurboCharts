"""Sin red: el fixture es la respuesta REAL (recortada) del endpoint
publico de brackets, capturada el 2026-08-28 (ver docstring de
ingest/binance_brackets.py para como se verifico que es publico)."""
import unittest
from datetime import datetime, timezone

from ingest.binance_brackets import parse_brackets

BTCUSDT_RAW = {
    "symbol": "BTCUSDT",
    "updateTime": 1755585384254,
    "notionalLimit": 100,
    "riskBrackets": [
        {"bracketSeq": 1, "bracketNotionalFloor": 0, "bracketNotionalCap": 300000,
         "bracketMaintenanceMarginRate": 0.004, "cumFastMaintenanceAmount": 0,
         "minOpenPosLeverage": 101, "maxOpenPosLeverage": 150},
        {"bracketSeq": 2, "bracketNotionalFloor": 300000, "bracketNotionalCap": 800000,
         "bracketMaintenanceMarginRate": 0.005, "cumFastMaintenanceAmount": 300,
         "minOpenPosLeverage": 76, "maxOpenPosLeverage": 100},
    ],
}


class TestParseBrackets(unittest.TestCase):
    def test_parses_all_tiers_of_every_symbol(self):
        rows = list(parse_brackets([BTCUSDT_RAW]))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["symbol"], "BTCUSDT")
        self.assertEqual(rows[0]["maintenance_margin_rate"], 0.004)
        self.assertEqual(rows[0]["max_leverage"], 150)
        self.assertEqual(rows[1]["bracket_seq"], 2)

    def test_fetched_at_is_shared_across_the_whole_snapshot(self):
        ts = datetime(2026, 8, 28, tzinfo=timezone.utc)
        rows = list(parse_brackets([BTCUSDT_RAW], fetched_at=ts))
        self.assertTrue(all(r["fetched_at"] == ts for r in rows))

    def test_defaults_fetched_at_to_now_when_not_given(self):
        before = datetime.now(timezone.utc)
        rows = list(parse_brackets([BTCUSDT_RAW]))
        self.assertGreaterEqual(rows[0]["fetched_at"], before)


if __name__ == "__main__":
    unittest.main()
