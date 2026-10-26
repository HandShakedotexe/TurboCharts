"""El informe del doctor es lo que se mira dentro de seis meses."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from liqrec.coverage import CoverageLog
from liqrec.doctor import count_raw, report
from liqrec.writer import RawWriter


def ms(h, mi=0):
    return int(datetime(2026, 8, 24, h, mi, tzinfo=timezone.utc).timestamp() * 1000)


class TestDoctor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self):
        w = RawWriter(self.root, "binance", "liquidation", compress_on_rotate=True)
        for i in range(7):
            w.write({"i": i}, received_at=ms(10) + i)
        w.write({"i": 99}, received_at=ms(11))   # fuerza rotación + gz
        w.close()

        c = CoverageLog(self.root, "binance", "liquidation")
        c.mark_up()
        c.mark_down(reason="server_close")
        c.mark_up()
        c.close()

    def test_counts_across_plain_and_compressed_files(self):
        self._seed()
        counts = count_raw(self.root)
        self.assertEqual(sum(counts["binance/liquidation"].values()), 8)

    def test_report_mentions_the_gap(self):
        self._seed()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = report(self.root)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("binance/liquidation", out)
        self.assertIn("COBERTURA", out)

    def test_a_connected_feed_with_zero_records_is_shouted_about(self):
        """El fallo que costó dos días de datos de Binance.

        El feed conectaba, respondía al keepalive y no grababa nada. El informe
        antiguo simplemente no lo listaba, porque solo contaba lo que tenía
        ficheros — así que la ausencia total parecía normalidad.
        """
        self._seed()                                  # binance con datos
        c = CoverageLog(self.root, "bybit", "liquidation")   # bybit conectado...
        c.mark_up()
        c.close()                                     # ...pero sin un solo dato

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = report(self.root)
        out = buf.getvalue()
        self.assertIn("bybit/liquidation", out)
        self.assertIn("CONECTADO PERO SIN DATOS", out)
        self.assertIn("preflight", out)
        self.assertEqual(rc, 1, "un feed mudo tiene que devolver código de fallo")

    def test_all_feeds_with_data_is_a_clean_report(self):
        self._seed()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = report(self.root)
        self.assertEqual(rc, 0)
        self.assertNotIn("SIN DATOS", buf.getvalue())

    def test_missing_data_dir_is_reported_not_crashed(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = report(self.root / "nope")
        self.assertEqual(rc, 1)
        self.assertIn("no existe", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
