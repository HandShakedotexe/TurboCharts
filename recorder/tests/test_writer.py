"""El escritor es lo único entre un mensaje y el disco. Se testea la rotación,
la compresión al rotar, y que un reinicio no machaque lo ya grabado.
"""
import gzip
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from liqrec.writer import RawWriter


def ms(y, mo, d, h, mi=0, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp() * 1000)


def read_any(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class TestRawWriter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_envelope_with_receive_time(self):
        w = RawWriter(self.root, "binance", "liquidation", compress_on_rotate=False)
        t = ms(2026, 8, 24, 13, 5)
        w.write({"e": "forceOrder"}, received_at=t)
        w.close()
        p = self.root / "raw/binance/liquidation/2026-08-24/13.jsonl"
        recs = read_any(p)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["rx"], t)
        self.assertEqual(recs[0]["ex"], "binance")
        self.assertEqual(recs[0]["st"], "liquidation")
        self.assertEqual(recs[0]["raw"], {"e": "forceOrder"})

    def test_rotates_by_hour_and_compresses_the_closed_file(self):
        w = RawWriter(self.root, "bybit", "liquidation", compress_on_rotate=True)
        w.write({"n": 1}, received_at=ms(2026, 8, 24, 13, 59))
        w.write({"n": 2}, received_at=ms(2026, 8, 24, 14, 0))   # cruza la hora
        w.write({"n": 3}, received_at=ms(2026, 8, 24, 14, 30))
        w.close()
        base = self.root / "raw/bybit/liquidation/2026-08-24"
        self.assertTrue((base / "13.jsonl.gz").exists(), "la hora cerrada debe quedar comprimida")
        self.assertFalse((base / "13.jsonl").exists(), "el crudo se borra tras comprimir")
        self.assertTrue((base / "14.jsonl.gz").exists())
        self.assertEqual([r["raw"]["n"] for r in read_any(base / "13.jsonl.gz")], [1])
        self.assertEqual([r["raw"]["n"] for r in read_any(base / "14.jsonl.gz")], [2, 3])

    def test_current_hour_is_plain_text_until_rotation(self):
        w = RawWriter(self.root, "okx", "liquidation", compress_on_rotate=True)
        w.write({"n": 1}, received_at=ms(2026, 8, 24, 9, 10))
        w.flush()
        p = self.root / "raw/okx/liquidation/2026-08-24/09.jsonl"
        # Sin comprimir mientras está abierto: un corte de luz aquí pierde una
        # línea, no el fichero entero.
        self.assertTrue(p.exists())
        self.assertEqual(len(read_any(p)), 1)
        w.close()

    def test_restart_within_same_hour_appends_instead_of_truncating(self):
        t = ms(2026, 8, 24, 11, 0)
        w1 = RawWriter(self.root, "binance", "liquidation", compress_on_rotate=False)
        w1.write({"n": 1}, received_at=t)
        w1.close()
        w2 = RawWriter(self.root, "binance", "liquidation", compress_on_rotate=False)
        w2.write({"n": 2}, received_at=t + 1000)
        w2.close()
        recs = read_any(self.root / "raw/binance/liquidation/2026-08-24/11.jsonl")
        self.assertEqual([r["raw"]["n"] for r in recs], [1, 2])

    def test_restart_after_compression_does_not_clobber_the_gz(self):
        t = ms(2026, 8, 24, 12, 0)
        w1 = RawWriter(self.root, "binance", "liquidation", compress_on_rotate=True)
        w1.write({"n": 1}, received_at=t)
        w1.close()   # deja 12.jsonl.gz
        w2 = RawWriter(self.root, "binance", "liquidation", compress_on_rotate=True)
        w2.write({"n": 2}, received_at=t + 1000)
        w2.close()
        base = self.root / "raw/binance/liquidation/2026-08-24"
        files = sorted(p.name for p in base.iterdir())
        self.assertIn("12.jsonl.gz", files)
        # el segundo arranque no puede haber destruido el primero
        all_n = []
        for p in base.iterdir():
            all_n += [r["raw"]["n"] for r in read_any(p)]
        self.assertEqual(sorted(all_n), [1, 2])

    def test_counters(self):
        w = RawWriter(self.root, "binance", "liquidation", compress_on_rotate=False)
        for i in range(50):
            w.write({"i": i}, received_at=ms(2026, 8, 24, 8, 0, 0) + i)
        w.close()
        self.assertEqual(w.lines_written, 50)
        self.assertGreater(w.bytes_written, 0)

    def test_unicode_survives_roundtrip(self):
        w = RawWriter(self.root, "binance", "liquidation", compress_on_rotate=False)
        w.write({"s": "BTC/USDT ✓ ñ"}, received_at=ms(2026, 8, 24, 7, 0))
        w.close()
        recs = read_any(self.root / "raw/binance/liquidation/2026-08-24/07.jsonl")
        self.assertEqual(recs[0]["raw"]["s"], "BTC/USDT ✓ ñ")


if __name__ == "__main__":
    unittest.main()
