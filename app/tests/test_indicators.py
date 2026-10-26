"""Bug real (2026-09-03): OI y funding venian de Binance alineados a la
marca redonda (`...:05:00`), pero el `time` de una vela es SIEMPRE
`closeTime // 1000`, que cae 1 segundo antes (`...:04:59`) -- verificado
contra datos reales de BTCUSDT: 0 de 629,107 puntos de OI y 0 de 6,580 de
funding coincidian con algun `time` de vela. Eso le inyectaba a la escala de
tiempo COMPARTIDA del chart un indice logico nuevo por cada punto, con
densidad no uniforme en el tiempo (OI es 5 min archivado, 1h en la cola en
vivo) -- rompia la interpolacion lineal precio/tiempo->pixel de
HeatmapOverlay.tsx (`computeMapping`), que asume espaciado uniforme entre
indices. Ver el comentario de `_align_to_candle_grid` en
api/routers/indicators.py para el detalle completo, incluida la verificacion
de que `timeToCoordinate` de lightweight-charts no interpola (exige
coincidencia exacta)."""
import unittest

from api.routers.indicators import _align_to_candle_grid, _resample_to_bar_grid


class TestAlignToCandleGrid(unittest.TestCase):
    def test_shifts_back_one_second(self):
        self.assertEqual(_align_to_candle_grid(1788418800), 1788418799)

    def test_matches_the_real_candle_time_convention(self):
        """closeTime de una vela de 5m que cierra a las 07:00:00 es
        07:00:00 - 1ms; `time` de la vela (`closeTime // 1000`, ver
        live.py._parse_rows) es por tanto 06:59:59, no 07:00:00 -- el mismo
        numero que produce _align_to_candle_grid a partir del `:00` limpio
        que entrega Binance para OI/funding."""
        oi_raw_seconds = 1788418800  # 07:00:00, tal cual lo entrega Binance
        candle_time_at_that_hour = 1788418799  # 06:59:59, closeTime // 1000
        self.assertEqual(_align_to_candle_grid(oi_raw_seconds), candle_time_at_that_hour)


class TestResampleToBarGrid(unittest.TestCase):
    """Bug real, mismo dia (2026-09-03): la alineacion de -1s por si sola
    solo dejaba a OI/funding EXACTOS con la grilla de velas cuando la
    cadencia nativa de OI (5 min archivado) coincidia con la temporalidad
    vista -- a 1h, 11 de cada 12 puntos seguian sin coincidir con ninguna
    vela, y el usuario reporto el mismo patron de bloques negros otra vez.
    `_resample_to_bar_grid` reasigna cada punto al `time` EXACTO de la
    vela mas reciente, para cualquier temporalidad."""

    def test_each_bar_gets_the_most_recent_point_at_or_before_it(self):
        points = [{"time": 100, "value": 1.0}, {"time": 205, "value": 2.0}, {"time": 400, "value": 3.0}]
        bar_times = [100, 200, 300, 400]
        result = _resample_to_bar_grid(points, bar_times)
        self.assertEqual(result, [
            {"time": 100, "value": 1.0},
            {"time": 200, "value": 1.0},   # el punto de t=205 todavia no paso
            {"time": 300, "value": 2.0},
            {"time": 400, "value": 3.0},
        ])

    def test_output_times_are_exactly_the_bar_times_never_the_points_own(self):
        """El punto entero del arreglo: el `time` de salida tiene que ser
        el de la vela, no el timestamp nativo del punto original -- eso es
        lo que garantiza cero indices logicos nuevos en la escala de
        tiempo del chart, sea cual sea la cadencia nativa de OI/funding."""
        points = [{"time": 12345, "value": 7.0}]
        bar_times = [12300, 12400]
        result = _resample_to_bar_grid(points, bar_times)
        self.assertEqual([r["time"] for r in result], [12400])

    def test_bars_before_the_first_point_are_skipped_not_zero_filled(self):
        """Sin dato real todavia para ese instante -- se omite la barra en
        vez de inventar un valor (0.0 se veria como un dato real, no como
        "no habia informacion")."""
        points = [{"time": 500, "value": 9.0}]
        bar_times = [100, 200, 500, 600]
        result = _resample_to_bar_grid(points, bar_times)
        self.assertEqual(result, [{"time": 500, "value": 9.0}, {"time": 600, "value": 9.0}])

    def test_empty_points_or_bar_times_yields_empty_list(self):
        self.assertEqual(_resample_to_bar_grid([], [100, 200]), [])
        self.assertEqual(_resample_to_bar_grid([{"time": 1, "value": 1.0}], []), [])


if __name__ == "__main__":
    unittest.main()
