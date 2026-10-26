import unittest

from api.heatmap_model import (
    DEFAULT_BASKET,
    LeverageBasket,
    LiquidationLevel,
    MmrTable,
    MmrTier,
    bin_levels,
    bin_levels_by_leverage,
    compute_levels,
    compute_levels_from_trades,
    compute_liquidity_intervals,
    filter_consumed_levels,
    liq_price,
)


class TestLiqPrice(unittest.TestCase):
    def test_long_liquidates_below_entry(self):
        p = liq_price(100.0, leverage=10, mmr=0.005, side="long")
        self.assertAlmostEqual(p, 100 * (1 - 0.1 + 0.005))
        self.assertLess(p, 100.0)

    def test_short_liquidates_above_entry(self):
        p = liq_price(100.0, leverage=10, mmr=0.005, side="short")
        self.assertAlmostEqual(p, 100 * (1 + 0.1 - 0.005))
        self.assertGreater(p, 100.0)

    def test_higher_leverage_liquidates_closer_to_entry(self):
        near = liq_price(100.0, leverage=100, mmr=0.004, side="long")
        far = liq_price(100.0, leverage=5, mmr=0.004, side="long")
        self.assertGreater(near, far)   # 100x se liquida mas cerca (arriba) que 5x

    def test_rejects_invalid_side(self):
        with self.assertRaises(ValueError):
            liq_price(100.0, 10, 0.005, "sideways")


class TestMmrTable(unittest.TestCase):
    def setUp(self):
        self.table = MmrTable([
            MmrTier(0, 300_000, 0.004),
            MmrTier(300_000, 800_000, 0.005),
            MmrTier(800_000, 3_000_000, 0.0065),
        ])

    def test_picks_the_tier_containing_the_notional(self):
        self.assertEqual(self.table.mmr_for_notional(100_000), 0.004)
        self.assertEqual(self.table.mmr_for_notional(300_000), 0.005)   # floor inclusivo
        self.assertEqual(self.table.mmr_for_notional(1_000_000), 0.0065)

    def test_above_highest_tier_falls_back_to_the_last_known_mmr(self):
        """No falla ni inventa un numero: usa la mejor estimacion disponible."""
        self.assertEqual(self.table.mmr_for_notional(999_999_999), 0.0065)

    def test_empty_table_returns_zero_instead_of_raising(self):
        self.assertEqual(MmrTable([]).mmr_for_notional(1000), 0.0)


class TestLeverageBasket(unittest.TestCase):
    def test_weights_must_sum_to_one(self):
        with self.assertRaises(ValueError):
            LeverageBasket(name="bad", version="v0", weights={10: 0.5, 20: 0.4})

    def test_default_basket_is_valid(self):
        self.assertAlmostEqual(sum(DEFAULT_BASKET.weights.values()), 1.0)
        self.assertTrue(DEFAULT_BASKET.name)
        self.assertTrue(DEFAULT_BASKET.version)


class TestComputeLevels(unittest.TestCase):
    def test_two_levels_per_leverage_in_the_basket(self):
        table = MmrTable([MmrTier(0, 10**12, 0.004)])
        levels = compute_levels(100.0, 1_000_000, table)
        self.assertEqual(len(levels), 2 * len(DEFAULT_BASKET.weights))

    def test_notional_conserved_across_all_levels(self):
        """La suma de todo el notional repartido debe ser el OI de entrada
        (los pesos de la cesta suman 1, y cada apalancamiento se parte 50/50)."""
        table = MmrTable([MmrTier(0, 10**12, 0.004)])
        oi = 2_500_000.0
        levels = compute_levels(100.0, oi, table)
        self.assertAlmostEqual(sum(l.notional_usd for l in levels), oi)

    def test_zero_or_negative_inputs_yield_no_levels(self):
        table = MmrTable([MmrTier(0, 10**12, 0.004)])
        self.assertEqual(compute_levels(0, 1000, table), [])
        self.assertEqual(compute_levels(100, 0, table), [])
        self.assertEqual(compute_levels(100, -5, table), [])

    def test_custom_basket_is_respected(self):
        table = MmrTable([MmrTier(0, 10**12, 0.004)])
        basket = LeverageBasket(name="solo_10x", version="test", weights={10: 1.0})
        levels = compute_levels(100.0, 1_000_000, table, basket=basket)
        self.assertEqual({l.leverage for l in levels}, {10})

    def test_mmr_uses_the_typical_position_tier_not_the_aggregate_tier(self):
        """Bug real: buscar el MMR con el notional AGREGADO del tramo de
        apalancamiento (potencialmente miles de millones) caia en un tramo
        de MMR altisimo (BTCUSDT real: 50% sobre $1.2B), y con
        mmr > 1/leverage la formula se invertia -- la liquidacion "long"
        quedaba por ENCIMA del precio de entrada. Debe usarse siempre el
        tramo mas bajo (posicion tipica), sin importar que tan grande sea
        el open interest total."""
        table = MmrTable([
            MmrTier(0, 300_000, 0.004),
            MmrTier(300_000, 800_000, 0.005),
            MmrTier(800_000, 1_200_000_000, 0.25),
            MmrTier(1_200_000_000, 1_800_000_000, 0.5),
        ])
        # OI enorme: bucket_notional por apalancamiento (20% de 8.25B) cae
        # de lleno en el tramo de 50% si se usara el agregado.
        huge_oi = 8_259_370_681.5
        levels = compute_levels(77_375.5, huge_oi, table)

        long_levels = [l for l in levels if l.side == "long"]
        self.assertTrue(all(l.price < 77_375.5 for l in long_levels),
                         "una liquidacion long por encima del precio de entrada no tiene sentido")
        short_levels = [l for l in levels if l.side == "short"]
        self.assertTrue(all(l.price > 77_375.5 for l in short_levels))

    def test_leverage_above_the_symbol_max_is_skipped_not_clamped(self):
        """TIAUSDT real: tramo mas bajo topa en 75x. Modelar un nivel de
        100x ahi seria un apalancamiento que ningun trader puede tener de
        verdad en ese simbolo -- se salta entero, no se recorta a 75x."""
        table = MmrTable([MmrTier(0, 10_000, 0.01, max_leverage=75)])
        levels = compute_levels(100.0, 1_000_000, table)
        leverages = {l.leverage for l in levels}
        self.assertNotIn(100, leverages)
        self.assertIn(50, leverages)  # 50x si sigue disponible (<=75)

    def test_max_leverage_property_reads_the_lowest_tier(self):
        table = MmrTable([
            MmrTier(0, 1000, 0.01, max_leverage=75),
            MmrTier(1000, 5000, 0.02, max_leverage=25),
        ])
        self.assertEqual(table.max_leverage, 75)

    def test_max_leverage_defaults_generous_when_table_is_empty(self):
        self.assertGreaterEqual(MmrTable([]).max_leverage, 100)


class TestComputeLevelsFromTrades(unittest.TestCase):
    def test_each_trade_liquidates_from_its_own_entry_price_not_a_shared_one(self):
        """El punto entero de esta funcion: dos trades a precios distintos
        dan clusters de liquidacion en sitios distintos, no las mismas 5
        lineas repetidas alrededor de un unico precio."""
        table = MmrTable([MmrTier(0, 10**12, 0.004)])
        basket = LeverageBasket(name="solo_10x", version="test", weights={10: 1.0})
        levels = compute_levels_from_trades(
            [(100.0, 50_000.0, 1000), (200.0, 50_000.0, 1000)], table, basket=basket
        )
        prices = {round(l.price, 4) for l in levels}
        # 10x desde 100 y desde 200 dan cuatro precios de liquidacion distintos
        self.assertEqual(len(prices), 4)

    def test_notional_conserved_per_trade(self):
        table = MmrTable([MmrTier(0, 10**12, 0.004)])
        levels = compute_levels_from_trades([(100.0, 10_000.0, 1000)], table)
        self.assertAlmostEqual(sum(l.notional_usd for l in levels), 10_000.0)

    def test_empty_trades_yield_no_levels(self):
        table = MmrTable([MmrTier(0, 10**12, 0.004)])
        self.assertEqual(compute_levels_from_trades([], table), [])

    def test_skips_invalid_trades_without_raising(self):
        table = MmrTable([MmrTier(0, 10**12, 0.004)])
        levels = compute_levels_from_trades(
            [(0.0, 10_000.0, 1000), (100.0, 0.0, 1000), (100.0, -5.0, 1000)], table
        )
        self.assertEqual(levels, [])

    def test_respects_max_leverage_per_trade(self):
        table = MmrTable([MmrTier(0, 10_000, 0.01, max_leverage=75)])
        levels = compute_levels_from_trades([(100.0, 50_000.0, 1000)], table)
        self.assertNotIn(100, {l.leverage for l in levels})

    def test_each_level_carries_the_trade_time(self):
        table = MmrTable([MmrTier(0, 10**12, 0.004)])
        levels = compute_levels_from_trades([(100.0, 10_000.0, 12345)], table)
        self.assertTrue(levels)
        self.assertTrue(all(l.trade_time_ms == 12345 for l in levels))


class TestFilterConsumedLevels(unittest.TestCase):
    def test_long_level_disappears_once_price_trades_below_it_after_the_trade(self):
        lvl = LiquidationLevel(price=90.0, side="long", leverage=10, notional_usd=500, trade_time_ms=1000)
        # vela DESPUES del trade (close_time 2000) con low que ya toco 90
        history = [(2000, 100.0, 85.0)]
        self.assertEqual(filter_consumed_levels([lvl], history), [])

    def test_short_level_disappears_once_price_trades_above_it_after_the_trade(self):
        lvl = LiquidationLevel(price=110.0, side="short", leverage=10, notional_usd=500, trade_time_ms=1000)
        history = [(2000, 115.0, 100.0)]
        self.assertEqual(filter_consumed_levels([lvl], history), [])

    def test_level_survives_when_price_never_reaches_it(self):
        lvl = LiquidationLevel(price=90.0, side="long", leverage=10, notional_usd=500, trade_time_ms=1000)
        history = [(2000, 105.0, 95.0)]
        self.assertEqual(filter_consumed_levels([lvl], history), [lvl])

    def test_a_touch_before_the_trade_happened_does_not_count(self):
        """El cruce tiene que ser DESPUES de que el trade abriera la posicion
        hipotetica -- un precio que ya habia estado ahi antes no dice nada
        sobre esta posicion en concreto."""
        lvl = LiquidationLevel(price=90.0, side="long", leverage=10, notional_usd=500, trade_time_ms=5000)
        history = [(1000, 100.0, 85.0), (6000, 100.0, 95.0)]
        self.assertEqual(filter_consumed_levels([lvl], history), [lvl])

    def test_levels_without_trade_time_pass_through_unfiltered(self):
        """La ruta de open interest agregado (compute_levels) no tiene un
        trade real detras -- no hay forma de saber desde cuando existe esa
        posicion hipotetica, asi que no se evalua."""
        lvl = LiquidationLevel(price=1.0, side="long", leverage=10, notional_usd=500, trade_time_ms=None)
        history = [(2000, 100.0, 0.5)]
        self.assertEqual(filter_consumed_levels([lvl], history), [lvl])

    def test_empty_history_leaves_levels_untouched(self):
        lvl = LiquidationLevel(price=90.0, side="long", leverage=10, notional_usd=500, trade_time_ms=1000)
        self.assertEqual(filter_consumed_levels([lvl], []), [lvl])


class TestComputeLiquidityIntervals(unittest.TestCase):
    """`compute_liquidity_intervals` reconstruye el heatmap con precision
    EXACTA -- reemplaza a la version anterior (`compute_liquidity_grid`,
    bucket a bucket) que en dos intentos sucesivos tuvo que resamplear
    `price_history` a resoluciones cada vez mas gruesas para no explotar
    en costo, dejando
    el heatmap "en bloques" (el ancho del bucket, no el costo de computo,
    era lo que limitaba que tan pegado al precio quedaba el decaimiento).
    Cada celda ahora dura EXACTAMENTE lo que el nivel estuvo activo, sin
    redondear a ningun bucket."""

    # 10 barras espaciadas 100 unidades. Altos/bajos neutros (no tocan 90)
    # salvo donde el test lo dice explicitamente.
    TIMES = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]

    def _history(self, low_at: dict[int, float] | None = None) -> list[tuple[int, float, float]]:
        low_at = low_at or {}
        return [(t, 105.0, low_at.get(t, 95.0)) for t in self.TIMES]

    def test_level_never_crossed_spans_a_single_interval_to_the_window_end(self):
        lvl = LiquidationLevel(price=90.0, side="long", leverage=10, notional_usd=500, trade_time_ms=0)
        cells = compute_liquidity_intervals([lvl], self._history())
        self.assertEqual(len(cells), 1)  # una sola celda para toda la vida del nivel
        self.assertEqual(cells[0].notional_usd, 500)
        self.assertEqual(cells[0].time_ms, 100)  # primera barra >= trade_time_ms=0
        self.assertEqual(cells[0].bucket_ms, 900)  # hasta la ultima barra (1000)

    def test_level_stops_exactly_at_the_crossing_bar_not_a_rounded_bucket(self):
        """El precio toca 90 en t=700 -- el intervalo debe terminar EN ESE
        INSTANTE exacto, no en un bucket redondeado (el bug que reporto el
        usuario con capturas: bloques de horas/dias en vez de seguir la
        vela)."""
        lvl = LiquidationLevel(price=90.0, side="long", leverage=10, notional_usd=500, trade_time_ms=0)
        history = self._history(low_at={700: 80.0})
        cells = compute_liquidity_intervals([lvl], history)
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0].time_ms, 100)
        self.assertEqual(cells[0].time_ms + cells[0].bucket_ms, 700)  # termina justo en la barra del cruce

    def test_short_side_crossing_uses_the_high_not_the_low(self):
        lvl = LiquidationLevel(price=110.0, side="short", leverage=10, notional_usd=500, trade_time_ms=0)
        history = [(t, 120.0 if t == 700 else 105.0, 95.0) for t in self.TIMES]
        cells = compute_liquidity_intervals([lvl], history)
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0].time_ms + cells[0].bucket_ms, 700)

    def test_two_levels_starting_at_different_times_produce_a_step_function(self):
        """Dos niveles en el mismo bin de precio que empiezan en instantes
        distintos deben producir DOS celdas -- una con el notional de solo
        el primero, otra (desde que el segundo arranca) con la suma -- en
        vez de una celda por bucket de tiempo transcurrido."""
        lvls = [
            LiquidationLevel(price=90.0, side="long", leverage=10, notional_usd=500, trade_time_ms=0),
            LiquidationLevel(price=90.0, side="long", leverage=10, notional_usd=300, trade_time_ms=250),
        ]
        cells = sorted(compute_liquidity_intervals(lvls, self._history()), key=lambda c: c.time_ms)
        self.assertEqual(len(cells), 2)
        self.assertEqual((cells[0].time_ms, cells[0].notional_usd), (100, 500))
        # El segundo nivel arranca en su trade_time_ms EXACTO (250), no
        # redondeado a la barra siguiente (300).
        self.assertEqual((cells[1].time_ms, cells[1].notional_usd), (250, 800))

    def test_level_without_trade_time_is_active_from_the_first_bar(self):
        """La ruta de OI agregado (compute_levels) no tiene un trade real
        detras -- se trata como si existiera desde siempre en la ventana."""
        lvl = LiquidationLevel(price=90.0, side="long", leverage=10, notional_usd=500, trade_time_ms=None)
        cells = compute_liquidity_intervals([lvl], self._history())
        self.assertEqual(cells[0].time_ms, 100)

    def test_trade_newer_than_all_available_price_history_yields_no_cells(self):
        lvl = LiquidationLevel(price=90.0, side="long", leverage=10, notional_usd=500, trade_time_ms=5000)
        cells = compute_liquidity_intervals([lvl], self._history())
        self.assertEqual(cells, [])

    def test_empty_levels_or_empty_history_yields_no_cells(self):
        self.assertEqual(compute_liquidity_intervals([], self._history()), [])
        lvl = LiquidationLevel(price=90.0, side="long", leverage=10, notional_usd=500, trade_time_ms=0)
        self.assertEqual(compute_liquidity_intervals([lvl], []), [])

    def test_precision_survives_irregular_bar_spacing(self):
        """A diferencia de la version por buckets, esta no depende de que
        las barras esten espaciadas uniformemente -- el cruce se ubica en
        la barra EXACTA sea cual sea su posicion en el tiempo (mezcla de
        barras anchas y finas, como pasa de verdad al concatenar velas
        extendidas de 5m con recientes de 1m en heatmap.py)."""
        history = [
            (0, 105.0, 95.0), (1000, 105.0, 95.0), (2000, 105.0, 95.0),
            (2050, 105.0, 80.0), (2400, 105.0, 95.0),
        ]
        lvl = LiquidationLevel(price=90.0, side="long", leverage=10, notional_usd=500, trade_time_ms=-1)
        cells = compute_liquidity_intervals([lvl], history)
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0].time_ms, 0)
        self.assertEqual(cells[0].time_ms + cells[0].bucket_ms, 2050)

    def test_a_fully_closed_group_leaves_no_phantom_cell_from_float_rounding(self):
        """Bug real (2026-09-04), encontrado construyendo api/setups.py --
        no en este archivo. `running` en compute_liquidity_intervals suma
        y resta cientos de valores de millones de dolares por grupo
        (mismo bin de precio + apalancamiento + lado); cuando TODOS los
        niveles de un grupo ya se cruzaron, `running` deberia volver
        exactamente a donde estaba -- pero por redondeo de punto flotante
        (float64, ~15-17 digitos significativos) el residuo real no es
        0.0 exacto, es del orden de 1e-8 a 1e-6 para sumas en este rango.
        El umbral anterior (`> 1e-9`) era mas chico que ese residuo real
        -- verificado contra /api/heatmap: celdas "fantasma" con
        notional_usd de 6.5e-09, 1.65e-08, etc., pasaban el filtro como si
        fueran pools genuinos y se ofrecian como pool real al buscar
        setups sobre una liquidacion real. Este test reproduce el MISMO
        mecanismo (200 niveles al mismo precio/apalancamiento, todos
        abren en t=0 y se cruzan juntos en la MISMA barra) con una
        semilla que da un residuo positivo (1.7e-06, medido aparte antes
        de fijar la semilla) -- justo el caso donde el bug se manifestaba:
        positivo y mas chico que $1, pero mas grande que el umbral viejo."""
        import random
        rng = random.Random(5)  # semilla elegida: da un residuo positivo real, no adivinado
        values = [rng.uniform(1_000_000, 50_000_000) for _ in range(200)]
        levels = [
            LiquidationLevel(price=90.0, side="long", leverage=10, notional_usd=v, trade_time_ms=0)
            for v in values
        ]
        # 10 barras; todas cruzan juntas en t=600 (indice 5) -- despues de
        # eso el precio se queda plano, para que el residuo (si sobrevive)
        # se extienda como una celda "fantasma" hasta el final de la
        # ventana, que es exactamente el sintoma reportado.
        history = self._history(low_at={600: 80.0, 700: 80.0, 800: 80.0, 900: 80.0, 1000: 80.0})
        cells = compute_liquidity_intervals(levels, history)
        # Una sola celda real: el pool completo, activo desde t=100 (la
        # primera barra) hasta el cruce en t=600. Nada mas alla de eso.
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0].time_ms, 100)
        self.assertEqual(cells[0].time_ms + cells[0].bucket_ms, 600)
        self.assertAlmostEqual(cells[0].notional_usd, sum(values))


class TestBinLevels(unittest.TestCase):
    def test_close_levels_land_in_the_same_bin(self):
        levels = [
            LiquidationLevel(price=100.00, side="long", leverage=10, notional_usd=500),
            LiquidationLevel(price=100.01, side="long", leverage=20, notional_usd=300),
        ]
        bins = bin_levels(levels, bin_size=1.0)
        self.assertEqual(len(bins), 1)
        self.assertAlmostEqual(sum(bins.values()), 800)

    def test_far_apart_levels_land_in_different_bins(self):
        levels = [
            LiquidationLevel(price=100.0, side="long", leverage=10, notional_usd=500),
            LiquidationLevel(price=200.0, side="short", leverage=10, notional_usd=500),
        ]
        bins = bin_levels(levels, bin_size=0.15)
        self.assertEqual(len(bins), 2)

    def test_no_levels_yields_empty_bins(self):
        self.assertEqual(bin_levels([]), {})

    def test_zero_bin_size_yields_empty_bins_instead_of_dividing_by_zero(self):
        levels = [LiquidationLevel(price=100.0, side="long", leverage=10, notional_usd=500)]
        self.assertEqual(bin_levels(levels, bin_size=0.0), {})


class TestBinLevelsByLeverage(unittest.TestCase):
    def test_same_price_different_leverage_stay_in_separate_groups(self):
        """Si se mezclaran, activar/desactivar un apalancamiento en la UI
        movería el notional de otro -- por eso van agrupados aparte."""
        levels = [
            LiquidationLevel(price=100.0, side="long", leverage=10, notional_usd=500),
            LiquidationLevel(price=100.0, side="long", leverage=25, notional_usd=300),
        ]
        grouped = bin_levels_by_leverage(levels)
        self.assertEqual(set(grouped.keys()), {10, 25})
        self.assertAlmostEqual(sum(grouped[10].values()), 500)
        self.assertAlmostEqual(sum(grouped[25].values()), 300)

    def test_empty_levels_yields_empty_dict(self):
        self.assertEqual(bin_levels_by_leverage([]), {})


if __name__ == "__main__":
    unittest.main()
