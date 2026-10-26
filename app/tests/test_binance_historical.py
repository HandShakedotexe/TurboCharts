"""Sin red: un 'bucket' en memoria hace de data.binance.vision.

Las URLs y la cadencia (klines/fundingRate mensual, metrics diario) estan
verificadas contra el bucket real en el momento de escribir esto. Aqui
solo se prueba la logica: verificacion de checksum, manifest, idempotencia
y construccion de URLs.
"""
import hashlib
import io
import json
import unittest
import zipfile
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from ingest.binance_historical import (
    ChecksumMismatch,
    NotAvailable,
    days_between,
    download_verified,
    extract_single_csv,
    funding_target,
    kline_target,
    metrics_target,
    months_between,
    sync_funding,
    sync_klines,
    sync_metrics,
)


def _zip_with(inner_name: str, content: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(inner_name, content)
    return buf.getvalue()


def _checksum_line(data: bytes, fname: str) -> bytes:
    return f"{hashlib.sha256(data).hexdigest()}  {fname}\n".encode("ascii")


class FakeBucket:
    """url -> bytes, mas un contador de peticiones para probar idempotencia."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[str] = []

    def put_zip(self, url: str, fname: str, inner_name: str, content: bytes):
        zip_bytes = _zip_with(inner_name, content)
        self.objects[url] = zip_bytes
        self.objects[url + ".CHECKSUM"] = _checksum_line(zip_bytes, fname)

    def fetch(self, url: str) -> bytes:
        self.calls.append(url)
        if url not in self.objects:
            raise NotAvailable(url)
        return self.objects[url]


class TestUrlConstruction(unittest.TestCase):
    """Verificadas contra data.binance.vision real (ver docstring del modulo)."""

    def test_kline_url(self):
        url, fname = kline_target("BTCUSDT", "1h", date(2024, 1, 1))
        self.assertEqual(fname, "BTCUSDT-1h-2024-01.zip")
        self.assertEqual(
            url,
            "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2024-01.zip",
        )

    def test_funding_url(self):
        url, fname = funding_target("BTCUSDT", date(2024, 1, 1))
        self.assertEqual(fname, "BTCUSDT-fundingRate-2024-01.zip")
        self.assertIn("/monthly/fundingRate/BTCUSDT/", url)

    def test_metrics_url_is_daily_not_monthly(self):
        """metrics NO tiene version mensual: si esto vuelve a monthly, algo se rompio."""
        url, fname = metrics_target("BTCUSDT", date(2024, 1, 15))
        self.assertEqual(fname, "BTCUSDT-metrics-2024-01-15.zip")
        self.assertIn("/daily/metrics/BTCUSDT/", url)


class TestPeriods(unittest.TestCase):
    def test_months_between_same_year(self):
        months = list(months_between(date(2024, 1, 1), date(2024, 3, 1)))
        self.assertEqual([d.month for d in months], [1, 2, 3])

    def test_months_between_crosses_year(self):
        months = list(months_between(date(2023, 11, 1), date(2024, 2, 1)))
        self.assertEqual([(d.year, d.month) for d in months],
                          [(2023, 11), (2023, 12), (2024, 1), (2024, 2)])

    def test_days_between_inclusive(self):
        days = list(days_between(date(2024, 1, 30), date(2024, 2, 2)))
        self.assertEqual(days, [date(2024, 1, 30), date(2024, 1, 31), date(2024, 2, 1), date(2024, 2, 2)])


class TestDownloadVerified(unittest.TestCase):
    def test_good_checksum_passes(self):
        bucket = FakeBucket()
        bucket.put_zip("http://x/f.zip", "f.zip", "f.csv", b"hola")
        body = download_verified("http://x/f.zip", fetch=bucket.fetch)
        name, content = extract_single_csv(body)
        self.assertEqual(name, "f.csv")
        self.assertEqual(content, b"hola")

    def test_corrupted_download_is_rejected(self):
        """Una descarga cortada da bytes distintos a los del zip original:
        el checksum no puede coincidir y no se debe aceptar el fichero."""
        bucket = FakeBucket()
        bucket.put_zip("http://x/f.zip", "f.zip", "f.csv", b"hola")
        bucket.objects["http://x/f.zip"] = bucket.objects["http://x/f.zip"][:-2]  # truncado
        with self.assertRaises(ChecksumMismatch):
            download_verified("http://x/f.zip", fetch=bucket.fetch)

    def test_missing_checksum_file_is_an_error_not_a_silent_pass(self):
        bucket = FakeBucket()
        bucket.objects["http://x/f.zip"] = _zip_with("f.csv", b"hola")
        # sin .CHECKSUM
        with self.assertRaises(RuntimeError):
            download_verified("http://x/f.zip", fetch=bucket.fetch)


class TestExtractSingleCsv(unittest.TestCase):
    def test_rejects_zip_with_more_than_one_file(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("a.csv", b"a")
            z.writestr("b.csv", b"b")
        with self.assertRaises(RuntimeError):
            extract_single_csv(buf.getvalue())


class TestSyncEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_klines_lands_files_and_records_manifest(self):
        bucket = FakeBucket()
        for period in (date(2024, 1, 1), date(2024, 2, 1)):
            url, fname = kline_target("BTCUSDT", "1h", period)
            bucket.put_zip(url, fname, fname[:-4] + ".csv", f"data-{period.month}".encode())

        result = sync_klines("BTCUSDT", "1h", date(2024, 1, 1), date(2024, 2, 1),
                              self.root, fetch=bucket.fetch, pace_seconds=0)

        self.assertEqual(result.ok, 2)
        self.assertEqual(result.not_available, 0)
        dest = self.root / "binance" / "klines" / "BTCUSDT" / "1h"
        self.assertEqual((dest / "BTCUSDT-1h-2024-01.csv").read_bytes(), b"data-1")
        self.assertEqual((dest / "BTCUSDT-1h-2024-02.csv").read_bytes(), b"data-2")

        manifest_lines = (dest / "manifest.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(manifest_lines), 2)
        statuses = {json.loads(l)["status"] for l in manifest_lines}
        self.assertEqual(statuses, {"ok"})

    def test_period_not_yet_published_is_recorded_not_silently_skipped(self):
        """El mes en curso o una fecha anterior al inicio del dataset da 404.
        Eso tiene que quedar en el manifest como 'not_available', no como nada."""
        bucket = FakeBucket()  # bucket vacio: todo da NotAvailable
        result = sync_funding("BTCUSDT", date(2024, 1, 1), date(2024, 1, 1),
                               self.root, fetch=bucket.fetch, pace_seconds=0)
        self.assertEqual(result.not_available, 1)
        dest = self.root / "binance" / "fundingRate" / "BTCUSDT"
        manifest = json.loads((dest / "manifest.jsonl").read_text().strip())
        self.assertEqual(manifest["status"], "not_available")

    def test_already_landed_file_is_not_refetched(self):
        bucket = FakeBucket()
        url, fname = kline_target("BTCUSDT", "1h", date(2024, 1, 1))
        bucket.put_zip(url, fname, fname[:-4] + ".csv", b"data")

        sync_klines("BTCUSDT", "1h", date(2024, 1, 1), date(2024, 1, 1),
                    self.root, fetch=bucket.fetch, pace_seconds=0)
        calls_after_first = len(bucket.calls)

        result = sync_klines("BTCUSDT", "1h", date(2024, 1, 1), date(2024, 1, 1),
                              self.root, fetch=bucket.fetch, pace_seconds=0)

        self.assertEqual(len(bucket.calls), calls_after_first, "no debio volver a pedir nada por red")
        self.assertEqual(result.ok, 1)

    def test_metrics_uses_daily_cadence(self):
        bucket = FakeBucket()
        for d in (date(2024, 1, 15), date(2024, 1, 16)):
            url, fname = metrics_target("BTCUSDT", d)
            bucket.put_zip(url, fname, fname[:-4] + ".csv", b"m")

        result = sync_metrics("BTCUSDT", date(2024, 1, 15), date(2024, 1, 16),
                               self.root, fetch=bucket.fetch, pace_seconds=0)
        self.assertEqual(result.ok, 2)
        dest = self.root / "binance" / "metrics" / "BTCUSDT"
        self.assertTrue((dest / "BTCUSDT-metrics-2024-01-15.csv").exists())
        self.assertTrue((dest / "BTCUSDT-metrics-2024-01-16.csv").exists())


if __name__ == "__main__":
    unittest.main()
