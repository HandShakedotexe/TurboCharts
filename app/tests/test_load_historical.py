"""Sin DB real: un cursor falso basta para probar el SQL y el conteo de
filas. La deduplicacion de verdad (ON CONFLICT DO NOTHING contra la
PRIMARY KEY) es semantica estandar de Postgres, no logica propia -- lo que
se prueba aqui es que se pide, y que el parseo no se inventa ni descarta
filas por su cuenta.

Los CSV de muestra son fragmentos REALES descargados de data.binance.vision
(ver docstring de db/load_historical.py): klines de 2020-09 sin cabecera,
funding y metrics con cabecera, incluida la fila duplicada de fabrica que
trae metrics.
"""
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from db.load_historical import (
    AGGTRADES_PUBLISH_LAG,
    CANDLES_COLS,
    FUNDING_PUBLISH_LAG,
    KLINE_PUBLISH_LAG,
    METRICS_PUBLISH_LAG,
    _upsert,
    find_klines_files,
    find_symbol_files,
    load_all,
    parse_aggtrades_csv,
    parse_funding_csv,
    parse_klines_csv,
    parse_metrics_csv,
)

KLINES_NO_HEADER = (
    "1598918400000,11658.11,11675.00,11531.34,11618.27,15015.299,1598921999999,"
    "174112062.33123,48351,6666.854,77327235.25097,0\n"
    "1598922000000,11618.27,11643.75,11593.00,11634.31,4410.780,1598925599999,"
    "51261715.25539,18795,2151.537,25008941.58982,0\n"
)

KLINES_WITH_HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
    "taker_buy_volume,taker_buy_quote_volume,ignore\n"
    "1704067200000,42314.00,42603.20,42289.60,42503.50,8459.477,1704070799999,"
    "359196345.08716,88278,4687.976,199033806.82405,0\n"
)

FUNDING_CSV = (
    "calc_time,funding_interval_hours,last_funding_rate\n"
    "1598918400000,8,0.00010000\n"
    "1598947200000,8,0.00022961\n"
)

# Fragmento real de BTCUSDT-aggTrades-2020-01-01.zip (descargado y
# verificado contra data.binance.vision, sin cabecera -- igual que klines,
# los dumps de 2019-2020 no la traen). Ninguna de estas filas reales supera
# los $100k de notional (BTC a ~7189, cantidades chicas); se agrega una fila
# mas con la MISMA forma pero cantidad ajustada a proposito para poder
# probar el filtro de umbral.
AGGTRADES_NO_HEADER = (
    "18374167,7189.43,0.030,25247504,25247504,1577836801481,true\n"
    "18374168,7189.42,1.470,25247505,25247505,1577836801481,true\n"
    "18374169,7189.42,20.000,25247506,25247506,1577836801708,true\n"
    "18374170,7189.00,0.778,25247507,25247507,1577836806290,false\n"
)

# Fragmento real de BTCUSDT-aggTrades-2025-06-01.zip, CON cabecera
# (verificado: los dumps recientes sí la traen).
AGGTRADES_WITH_HEADER = (
    "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"
    "2741984702,104544.6,0.106,6360532016,6360532017,1748736000018,true\n"
    "2741984703,104544.6,0.009,6360532018,6360532018,1748736004742,true\n"
)

METRICS_CSV_WITH_DUPLICATE_ROW = (
    "create_time,symbol,sum_open_interest,sum_open_interest_value,"
    "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
    "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
    "2020-09-01 00:00:00,BTCUSDT,39080.23100000,456144339.23360443,"
    "1.17547937,1.23012681,1.35731217,0.78373373\n"
    "2020-09-01 00:00:00,BTCUSDT,39080.23100000,456144339.23360443,"
    "1.17547937,1.23012681,1.35731217,0.78373373\n"
    "2020-09-01 00:05:00,BTCUSDT,39106.41300000,455693833.60853225,"
    "1.17905012,1.22928510,1.35924435,0.85063183\n"
)


class FakeCursor:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def executemany(self, sql, batch):
        self.sink.append((sql, list(batch)))


class FakeConn:
    def __init__(self):
        self.batches = []
        self.committed = False

    def cursor(self):
        return FakeCursor(self.batches)

    def commit(self):
        self.committed = True


class TestParseKlines(unittest.TestCase):
    def test_dump_without_header_is_parsed_from_the_first_row(self):
        """Los dumps de klines anteriores a 2022 no llevan cabecera -- si el
        parser esperara una y se la comiera siempre, perderia la primera vela
        de cada mes en silencio."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "BTCUSDT-1h-2020-09.csv"
            path.write_text(KLINES_NO_HEADER)
            rows = list(parse_klines_csv(path, "binance", "BTCUSDT", "1h"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["open"], 11658.11)
        close_time = datetime.fromtimestamp(1598921999999 / 1000, tz=timezone.utc)
        self.assertEqual(rows[0]["event_time"], close_time)
        self.assertEqual(rows[0]["available_at"], close_time + KLINE_PUBLISH_LAG)

    def test_dump_with_header_skips_it(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "BTCUSDT-1h-2024-01.csv"
            path.write_text(KLINES_WITH_HEADER)
            rows = list(parse_klines_csv(path, "binance", "BTCUSDT", "1h"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["close"], 42503.50)
        self.assertEqual(rows[0]["trades"], 88278)


class TestParseFunding(unittest.TestCase):
    def test_available_at_equals_event_time_plus_lag(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "BTCUSDT-fundingRate-2020-09.csv"
            path.write_text(FUNDING_CSV)
            rows = list(parse_funding_csv(path, "binance", "BTCUSDT"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["funding_rate"], 0.0001)
        self.assertEqual(rows[0]["available_at"] - rows[0]["event_time"], FUNDING_PUBLISH_LAG)


class TestParseMetrics(unittest.TestCase):
    def test_symbol_comes_from_the_csv_column_not_the_path(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "BTCUSDT-metrics-2020-09-01.csv"
            path.write_text(METRICS_CSV_WITH_DUPLICATE_ROW)
            rows = list(parse_metrics_csv(path, "binance"))
        self.assertEqual(rows[0]["symbol"], "BTCUSDT")

    def test_factory_duplicate_row_is_not_dropped_by_the_parser(self):
        """El parseo no deduplica: eso es responsabilidad de la PRIMARY KEY
        en la base de datos (ON CONFLICT DO NOTHING), no del parser. Si el
        parser empezara a descartar filas 'raras' por su cuenta, un futuro
        cambio real de Binance podria perderse en silencio."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "BTCUSDT-metrics-2020-09-01.csv"
            path.write_text(METRICS_CSV_WITH_DUPLICATE_ROW)
            rows = list(parse_metrics_csv(path, "binance"))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], rows[1])

    def test_available_at_lag_is_five_minutes(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "BTCUSDT-metrics-2020-09-01.csv"
            path.write_text(METRICS_CSV_WITH_DUPLICATE_ROW)
            rows = list(parse_metrics_csv(path, "binance"))
        self.assertEqual(rows[2]["available_at"] - rows[2]["event_time"], METRICS_PUBLISH_LAG)
        self.assertEqual(METRICS_PUBLISH_LAG, timedelta(minutes=5))


class TestParseAggTrades(unittest.TestCase):
    def test_dump_without_header_is_parsed_from_the_first_row(self):
        """Igual que klines: los dumps de aggTrades de 2019-2020 no llevan
        cabecera. threshold_usd=0 para aislar esto del filtro de umbral,
        que se prueba aparte."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "BTCUSDT-aggTrades-2020-01-01.csv"
            path.write_text(AGGTRADES_NO_HEADER)
            rows = list(parse_aggtrades_csv(path, "binance", "BTCUSDT", threshold_usd=0))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["agg_trade_id"], 18374167)
        self.assertAlmostEqual(rows[0]["price"], 7189.43)
        event_time = datetime.fromtimestamp(1577836801481 / 1000, tz=timezone.utc)
        self.assertEqual(rows[0]["event_time"], event_time)

    def test_dump_with_header_skips_it(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "BTCUSDT-aggTrades-2025-06-01.csv"
            path.write_text(AGGTRADES_WITH_HEADER)
            rows = list(parse_aggtrades_csv(path, "binance", "BTCUSDT", threshold_usd=0))
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["price"], 104544.6)

    def test_only_trades_at_or_above_the_threshold_survive(self):
        """El punto entero de esta tabla: guardar el tape completo de un
        simbolo liquido son cientos de miles de filas por dia. Con el
        umbral de $100k solo sobrevive la fila 20.000 BTC * 7189.42 =
        ~$143,788, no las otras tres (todas por debajo)."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "BTCUSDT-aggTrades-2020-01-01.csv"
            path.write_text(AGGTRADES_NO_HEADER)
            rows = list(parse_aggtrades_csv(path, "binance", "BTCUSDT"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agg_trade_id"], 18374169)
        self.assertAlmostEqual(rows[0]["notional_usd"], 7189.42 * 20.000)

    def test_parses_is_buyer_maker_as_a_real_boolean(self):
        """Agregada 2026-09-15 (burbujas rojo/verde de BigTradesOverlay.tsx,
        ver db/schema.sql) -- "true"/"false" en minuscula tal cual vienen en
        el CSV, no "True"/"1"."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "BTCUSDT-aggTrades-2020-01-01.csv"
            path.write_text(AGGTRADES_NO_HEADER)
            rows = list(parse_aggtrades_csv(path, "binance", "BTCUSDT", threshold_usd=0))
        self.assertEqual(rows[0]["is_buyer_maker"], True)
        self.assertEqual(rows[3]["is_buyer_maker"], False)

    def test_available_at_equals_event_time(self):
        """Un trade impreso es publico al instante -- sin el lag de
        publicacion que si tienen candles/funding/metrics."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "BTCUSDT-aggTrades-2020-01-01.csv"
            path.write_text(AGGTRADES_NO_HEADER)
            rows = list(parse_aggtrades_csv(path, "binance", "BTCUSDT", threshold_usd=0))
        self.assertEqual(rows[0]["available_at"], rows[0]["event_time"])
        self.assertEqual(AGGTRADES_PUBLISH_LAG, timedelta(0))


class TestUpsert(unittest.TestCase):
    def test_sql_requests_on_conflict_do_nothing(self):
        conn = FakeConn()
        rows = [{c: None for c in CANDLES_COLS}]
        _upsert(conn, "candles", CANDLES_COLS, rows)
        sql, batch = conn.batches[0]
        self.assertIn("ON CONFLICT DO NOTHING", sql)
        self.assertEqual(len(batch), 1)
        self.assertTrue(conn.committed)

    def test_batches_are_paged(self):
        conn = FakeConn()
        rows = [{c: None for c in CANDLES_COLS} for _ in range(3)]
        n = _upsert(conn, "candles", CANDLES_COLS, rows, page_size=2)
        self.assertEqual(n, 3)
        self.assertEqual([len(b) for _, b in conn.batches], [2, 1])


class TestLoadAllDatasetFilter(unittest.TestCase):
    def test_datasets_filter_skips_the_rest(self):
        """Bug real evitado, no solo hipotetico: sin este filtro, cargar un
        puñado de CSV de aggTrades nuevos obligaba a re-recorrer TODO
        klines/funding/metrics ya cargados de los 20 simbolos de Fase 1 --
        `ON CONFLICT DO NOTHING` lo vuelve inofensivo pero no gratis (cada
        fila se manda a Postgres igual). Medido en pruebas reales
        (2026-08-30): tardaba mas de 5 minutos solo para llegar a los
        aggTrades. Aqui se prueba que `datasets=("aggTrades",)` ni siquiera
        recorre el directorio de klines."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            klines_dir = root / "binance" / "klines" / "BTCUSDT" / "1h"
            klines_dir.mkdir(parents=True)
            (klines_dir / "BTCUSDT-1h-2024-01.csv").write_text(KLINES_WITH_HEADER)
            agg_dir = root / "binance" / "aggTrades" / "BTCUSDT"
            agg_dir.mkdir(parents=True)
            (agg_dir / "BTCUSDT-aggTrades-2020-01-01.csv").write_text(AGGTRADES_NO_HEADER)

            conn = FakeConn()
            counts = load_all(root, conn, datasets=("aggTrades",))

        self.assertEqual(counts["candles"], 0)
        self.assertEqual(counts["big_trades"], 1)  # solo la fila que supera el umbral


class TestIncrementalLoadState(unittest.TestCase):
    """Bug real de rendimiento (2026-09-17): sin estado
    incremental, pedir "los aggTrades nuevos de BTCUSDT" releia los 9GB de
    ficheros YA cargados de meses anteriores -- casi una hora para lo que
    deberian ser un par de minutos. `load_all` ahora salta un fichero que
    no cambio (mismo tamaño+mtime) desde la ultima corrida."""

    def _agg_file(self, root: Path, name: str = "BTCUSDT-aggTrades-2020-01-01.csv") -> Path:
        agg_dir = root / "binance" / "aggTrades" / "BTCUSDT"
        agg_dir.mkdir(parents=True, exist_ok=True)
        path = agg_dir / name
        path.write_text(AGGTRADES_NO_HEADER)
        return path

    def test_second_call_skips_an_unchanged_file(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._agg_file(root)
            conn = FakeConn()
            first = load_all(root, conn, datasets=("aggTrades",))
            second = load_all(root, conn, datasets=("aggTrades",))
        self.assertEqual(first["big_trades"], 1)
        self.assertEqual(second["big_trades"], 0, "el fichero no cambio -- no deberia releerse")

    def test_a_modified_file_is_reloaded(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._agg_file(root)
            conn = FakeConn()
            load_all(root, conn, datasets=("aggTrades",))
            # Reescribir con contenido distinto (tamaño distinto) simula un
            # re-descarga real -- el fingerprint tiene que notarlo.
            path.write_text(AGGTRADES_NO_HEADER + "18374171,7189.42,50.000,25247508,25247508,1577836806290,false\n")
            second = load_all(root, conn, datasets=("aggTrades",))
        self.assertGreater(second["big_trades"], 0, "el fichero SI cambio -- se debe releer")

    def test_force_reload_ignores_the_state(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._agg_file(root)
            conn = FakeConn()
            load_all(root, conn, datasets=("aggTrades",))
            second = load_all(root, conn, datasets=("aggTrades",), force=True)
        self.assertEqual(second["big_trades"], 1, "--force-reload debe releer aunque no haya cambiado")

    def test_state_persists_to_disk_between_separate_processes(self):
        """Simula dos invocaciones SEPARADAS del CLI (dos procesos python
        distintos, no dos llamadas en el mismo test) -- el estado tiene que
        sobrevivir en disco, no solo en memoria dentro de una corrida."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._agg_file(root)
            load_all(root, FakeConn(), datasets=("aggTrades",))
            self.assertTrue((root / ".load_state.json").exists())
            second = load_all(root, FakeConn(), datasets=("aggTrades",))
        self.assertEqual(second["big_trades"], 0)

    def test_corrupt_state_file_does_not_crash_and_just_reloads(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._agg_file(root)
            (root / ".load_state.json").write_text("{ esto no es json valido")
            counts = load_all(root, FakeConn(), datasets=("aggTrades",))
        self.assertEqual(counts["big_trades"], 1)

    def test_state_is_scoped_per_file_not_global(self):
        """Un fichero nuevo se carga aunque otro, ya visto antes, se salte."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._agg_file(root, "BTCUSDT-aggTrades-2020-01-01.csv")
            conn = FakeConn()
            load_all(root, conn, datasets=("aggTrades",))
            self._agg_file(root, "BTCUSDT-aggTrades-2020-01-02.csv")
            second = load_all(root, conn, datasets=("aggTrades",))
        self.assertEqual(second["big_trades"], 1, "el fichero NUEVO si se debe cargar")


class TestDiscovery(unittest.TestCase):
    def test_find_klines_files_walks_symbol_and_interval_dirs(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = root / "binance" / "klines" / "BTCUSDT" / "1h"
            d.mkdir(parents=True)
            (d / "BTCUSDT-1h-2024-01.csv").write_text(KLINES_WITH_HEADER)
            found = list(find_klines_files(root))
        self.assertEqual(len(found), 1)
        path, symbol, interval = found[0]
        self.assertEqual((symbol, interval), ("BTCUSDT", "1h"))

    def test_find_symbol_files_missing_dataset_yields_nothing(self):
        with TemporaryDirectory() as tmp:
            found = list(find_symbol_files(Path(tmp), "fundingRate"))
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
