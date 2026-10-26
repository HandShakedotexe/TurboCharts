"""Un lote de peticiones que falla entero no es un dato.

Distinción que importa dentro de seis meses: un registro de open interest con
todos los símbolos en error ocupa sitio en raw/ y parece una observación. Un
lote parcial sí se graba, porque la parte buena es real.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from liqrec import pollers
from liqrec.writer import RawWriter


class TestFailureHandling(unittest.TestCase):
    def test_all_failed_raises(self):
        with self.assertRaises(pollers.AllFetchesFailed):
            pollers._require_some_success([{"_error": "a"}, {"_error": "b"}])

    def test_partial_failure_is_kept(self):
        items = [{"symbol": "BTCUSDT", "openInterest": "1"}, {"_error": "boom"}]
        self.assertIs(pollers._require_some_success(items), items)

    def test_empty_batch_is_not_an_error(self):
        self.assertEqual(pollers._require_some_success([]), [])


class TestPollerLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, fetch, cycles=2):
        w = RawWriter(self.root, "test", "poll", compress_on_rotate=False)
        p = pollers.Poller("test/poll", w, fetch, interval=0.05)

        async def scenario():
            task = asyncio.create_task(p.run())
            for _ in range(60):
                if p.ok + p.errors >= cycles:
                    break
                await asyncio.sleep(0.02)
            p.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        asyncio.run(asyncio.wait_for(scenario(), timeout=15))
        w.close()
        return p

    def test_success_is_written(self):
        async def fetch():
            return {"value": 1}
        p = self._run(fetch)
        self.assertGreaterEqual(p.ok, 2)
        self.assertEqual(p.errors, 0)
        self.assertTrue(list((self.root / "raw/test/poll").rglob("*.jsonl")))

    def test_failure_writes_nothing_but_is_counted(self):
        async def fetch():
            raise pollers.AllFetchesFailed("todo falló")
        p = self._run(fetch)
        self.assertGreaterEqual(p.errors, 2)
        self.assertEqual(p.ok, 0)
        self.assertIn("AllFetchesFailed", p.last_error)
        self.assertEqual(list((self.root / "raw").rglob("*.jsonl")), [],
                         "un lote fallido entero no debe dejar rastro en raw/")

    def test_poller_survives_exceptions_and_keeps_going(self):
        state = {"n": 0}

        async def fetch():
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("primer intento revienta")
            return {"n": state["n"]}
        p = self._run(fetch, cycles=3)
        self.assertGreaterEqual(p.ok, 1)
        self.assertGreaterEqual(p.errors, 1)


if __name__ == "__main__":
    unittest.main()
