"""La validacion existe por errores que ya ocurrieron, no por casos teoricos."""
import unittest

from liqrec.config import Config

BASE = {"recorder": {"data_dir": "./data"}, "binance": {}, "bybit": {}, "okx": {}}


def cfg(**recorder):
    return Config.from_dict({**BASE, "recorder": {**BASE["recorder"], **recorder}})


class TestValidation(unittest.TestCase):
    def test_clean_config_has_no_problems(self):
        self.assertEqual(cfg().validate(), [])

    def test_ping_url_pasted_into_health_file_is_caught(self):
        """El error real: la URL de aviso acabó en health_file, que es una RUTA.

        El grabador arrancaba, escribía el estado en un fichero con nombre
        absurdo, dejaba el heartbeat apagado, y todo parecía normal.
        """
        problems = cfg(health_file="https://hc-ping.com/07c0e59b-2a80-4a6e").validate()
        self.assertEqual(len(problems), 1)
        self.assertIn("health_file", problems[0])
        self.assertIn("heartbeat_url", problems[0])

    def test_http_url_also_caught(self):
        self.assertTrue(cfg(health_file="http://example.com/x").validate())

    def test_a_normal_windows_path_is_fine(self):
        self.assertEqual(cfg(health_file="C:/Users/x/data/health.json").validate(), [])

    def test_heartbeat_url_must_look_like_a_url(self):
        problems = cfg(heartbeat_url="pegar-aqui-la-url").validate()
        self.assertEqual(len(problems), 1)
        self.assertIn("heartbeat_url", problems[0])

    def test_empty_heartbeat_url_is_allowed(self):
        self.assertEqual(cfg(heartbeat_url="").validate(), [])

    def test_valid_heartbeat_url_passes(self):
        self.assertEqual(cfg(heartbeat_url="https://hc-ping.com/abc").validate(), [])

    def test_zero_intervals_are_rejected(self):
        self.assertTrue(cfg(oi_poll_seconds=0).validate())
        self.assertTrue(cfg(fsync_seconds=-1).validate())

    def test_empty_data_dir_is_rejected(self):
        self.assertTrue(cfg(data_dir="  ").validate())

    def test_all_exchanges_disabled_is_rejected(self):
        c = Config.from_dict({**BASE,
                              "binance": {"enabled": False},
                              "bybit": {"enabled": False},
                              "okx": {"enabled": False}})
        problems = c.validate()
        self.assertTrue(any("exchange" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
