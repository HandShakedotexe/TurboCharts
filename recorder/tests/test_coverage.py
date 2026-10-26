"""Los huecos son datos. Este test comprueba que se registran y se reconstruyen."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from liqrec.coverage import CoverageLog, scan_gaps, scan_gaps_across_files


class TestCoverage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _log(self):
        return CoverageLog(self.root, "binance", "liquidation")

    def test_up_down_cycle_produces_a_gap(self):
        c = self._log()
        c.mark_up(url="ws://x")
        time.sleep(0.02)
        c.mark_down(reason="server_close")
        time.sleep(0.02)
        c.mark_up(url="ws://x")
        c.close()
        path = next((self.root / "coverage/binance/liquidation").glob("*.jsonl"))
        gaps = scan_gaps(path)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["reason"], "server_close")
        self.assertIsNotNone(gaps[0]["end"])
        self.assertGreaterEqual(gaps[0]["end"] - gaps[0]["start"], 0)

    def test_open_gap_is_reported_as_unterminated(self):
        c = self._log()
        c.mark_up()
        c.mark_down(reason="idle_timeout")
        c.close()
        path = next((self.root / "coverage/binance/liquidation").glob("*.jsonl"))
        gaps = scan_gaps(path)
        self.assertEqual(len(gaps), 1)
        self.assertIsNone(gaps[0]["end"], "un hueco todavía abierto no puede darse por cerrado")

    def test_heartbeat_records_zero_messages_as_information(self):
        c = self._log()
        c.mark_up()
        c.heartbeat(messages=0, bytes_in=0)
        c.close()
        path = next((self.root / "coverage/binance/liquidation").glob("*.jsonl"))
        lines = [l for l in path.read_text().splitlines() if l.strip()]
        beat = [l for l in lines if '"minute"' in l]
        self.assertEqual(len(beat), 1)
        # "0 mensajes con el socket arriba" es distinto de "no hay registro"
        self.assertIn('"messages":0', beat[0])
        self.assertIn('"state":"up"', beat[0])

    def test_counters_track_gaps(self):
        c = self._log()
        c.mark_up(); c.mark_down(reason="a"); c.mark_up(); c.mark_down(reason="b")
        c.close()
        self.assertEqual(c.gaps, 2)


class TestImplicitGapFromProcessRestart(unittest.TestCase):
    """Bug real encontrado el 2026-09-14: un apagon mata al proceso sin
    darle chance de escribir su propio evento "down" -- al reconectar solo
    aparece un "up" suelto. `scan_gaps` tenia que reconocer eso tambien,
    no solo pares down/up emparejados (ver docstring de scan_gaps)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "coverage.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, *events):
        with open(self.path, "w", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e) + "\n")

    def test_up_after_long_silence_with_no_prior_down_is_a_gap(self):
        # Un latido normal a los 1000ms, y el siguiente evento (un "up" de
        # reconexion tras reiniciar el proceso) 200s despues -- sin ningun
        # "down" de por medio. Simula EXACTAMENTE un apagon: nada escribio
        # nada durante el corte porque el proceso no existia.
        self._write(
            {"t": 0, "event": "up"},
            {"t": 1000, "event": "minute", "messages": 5, "state": "up"},
            {"t": 201_000, "event": "up"},
        )
        gaps = scan_gaps(self.path)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["reason"], "process_restart_implicit")
        self.assertEqual(gaps[0]["start"], 1000)
        self.assertEqual(gaps[0]["end"], 201_000)

    def test_normal_heartbeat_spacing_is_not_a_gap(self):
        # Latidos cada 60s (el default de heartbeat_seconds) repetidos --
        # jitter normal, nunca deberia sintetizar un hueco.
        events = [{"t": i * 60_000, "event": "minute", "messages": 1, "state": "up"} for i in range(5)]
        self._write(*events)
        self.assertEqual(scan_gaps(self.path), [])

    def test_explicit_long_down_up_pair_is_not_double_counted(self):
        # Un hueco EXPLICITO (down...up) de mas de 180s no debe generar
        # TAMBIEN un hueco implicito duplicado por el mismo salto de tiempo.
        self._write(
            {"t": 0, "event": "up"},
            {"t": 1000, "event": "down", "reason": "server_close"},
            {"t": 500_000, "event": "up"},
        )
        gaps = scan_gaps(self.path)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["reason"], "server_close")
        self.assertEqual(gaps[0]["start"], 1000)
        self.assertEqual(gaps[0]["end"], 500_000)


class TestScanGapsAcrossFiles(unittest.TestCase):
    """Bug real encontrado el 2026-09-17: un apagon de
    ~30h cruzo la medianoche del 16 al 17 y `doctor` reporto "sin huecos"
    para los DOS ficheros de dia (16 y 17) por separado -- ni el ultimo
    evento del 16 ni el primero del 17 tenian, dentro de SU PROPIO
    fichero, algo con que compararse. `scan_gaps_across_files` junta los
    eventos de varios ficheros en una sola linea de tiempo antes de
    buscar huecos, exactamente para este caso."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.day1 = Path(self.tmp.name) / "2026-09-16.jsonl"
        self.day2 = Path(self.tmp.name) / "2026-09-17.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, path, *events):
        with open(path, "w", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e) + "\n")

    def test_gap_spanning_midnight_is_invisible_file_by_file_but_not_merged(self):
        # Ultimo evento real del dia 1 a las 11:12 (t=0 de referencia);
        # primer evento real del dia 2 mas de 30h despues -- exactamente
        # el apagon real (los otros ficheros de este mismo test no tienen
        # NADA que comparar en su propio borde).
        self._write(self.day1, {"t": 0, "event": "minute", "messages": 3, "state": "up"})
        gap_ms = 30 * 3600 * 1000
        self._write(self.day2, {"t": gap_ms, "event": "up"})

        self.assertEqual(scan_gaps(self.day1), [], "el fichero del dia 1, solo, no ve nada raro")
        self.assertEqual(scan_gaps(self.day2), [], "el fichero del dia 2, solo, tampoco")

        merged = scan_gaps_across_files([self.day1, self.day2])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["reason"], "process_restart_implicit")
        self.assertEqual(merged[0]["start"], 0)
        self.assertEqual(merged[0]["end"], gap_ms)

    def test_order_of_paths_does_not_matter(self):
        """Los ficheros se ordenan por TIEMPO de sus propios eventos, no
        por el orden en que se pasan -- doctor.py ya los pasa ordenados
        por nombre, pero la funcion no deberia depender de eso."""
        self._write(self.day1, {"t": 0, "event": "minute", "messages": 1, "state": "up"})
        gap_ms = 200_000
        self._write(self.day2, {"t": gap_ms, "event": "up"})
        self.assertEqual(
            scan_gaps_across_files([self.day2, self.day1]),
            scan_gaps_across_files([self.day1, self.day2]),
        )

    def test_no_real_gap_across_files_when_spacing_is_normal(self):
        self._write(self.day1, {"t": 0, "event": "minute", "messages": 1, "state": "up"})
        self._write(self.day2, {"t": 60_000, "event": "minute", "messages": 1, "state": "up"})
        self.assertEqual(scan_gaps_across_files([self.day1, self.day2]), [])


if __name__ == "__main__":
    unittest.main()
