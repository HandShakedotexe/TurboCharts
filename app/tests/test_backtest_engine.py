import unittest

from backtest.data import Bar, FundingPoint, RealLiquidation
from backtest.engine import TargetPosition, run_backtest_on_series
from backtest.regime import atr as atr_fn


def _bar(i, open_, close, t=None):
    t = i * 3_600_000 if t is None else t
    return Bar(t, open_, max(open_, close) + 1, min(open_, close) - 1, close, 100.0, 50.0)


class ScriptedStrategy:
    """Estrategia de prueba: devuelve un TargetPosition fijo por indice de
    barra, para poder afirmar EXACTAMENTE en que barra se llena cada orden."""

    def __init__(self, script):
        self.script = script  # {bar_index: TargetPosition}

    def on_bar(self, ctx):
        return self.script.get(ctx.bar_index)


class TestFillTiming(unittest.TestCase):
    def test_fills_at_next_bar_open_not_signal_bar_close(self):
        """El bug de look-ahead clasico: llenar en el close de la propia
        barra de señal usaria un precio que en el mercado real todavia no
        existia en el momento de decidir."""
        bars = [_bar(0, 100.0, 101.0), _bar(1, 102.0, 103.0), _bar(2, 104.0, 105.0)]
        strat = ScriptedStrategy({0: TargetPosition("long", 1000.0)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.entry_price, 102.0)   # open de la barra 1
        self.assertEqual(trade.entry_time_ms, bars[1].time_ms)


class TestCommissionAndSlippage(unittest.TestCase):
    def test_entry_price_includes_slippage_and_commission_on_notional(self):
        bars = [_bar(0, 100.0, 100.0), _bar(1, 100.0, 100.0), _bar(2, 100.0, 100.0)]
        strat = ScriptedStrategy({0: TargetPosition("long", 1000.0)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=50.0, slippage_bps=100.0,  # 0.5% / 1%
        )
        trade = result.trades[0]
        self.assertAlmostEqual(trade.entry_price, 101.0)  # 100 * 1.01, adverso a un largo
        # comision de entrada + la de salida forzosa al final de la corrida
        self.assertAlmostEqual(trade.commission_usd, 1000.0 * 0.005 * 2)

    def test_short_entry_slips_to_a_worse_lower_price(self):
        bars = [_bar(0, 100.0, 100.0), _bar(1, 100.0, 100.0), _bar(2, 100.0, 100.0)]
        strat = ScriptedStrategy({0: TargetPosition("short", 1000.0)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=100.0,
        )
        trade = result.trades[0]
        self.assertAlmostEqual(trade.entry_price, 99.0)  # vender para abrir corto: peor precio es mas bajo


class TestFundingAccrual(unittest.TestCase):
    def test_long_pays_positive_funding(self):
        bars = [_bar(0, 100.0, 100.0), _bar(1, 100.0, 100.0), _bar(2, 100.0, 100.0), _bar(3, 100.0, 100.0)]
        funding = [FundingPoint(bars[2].time_ms, 0.001)]  # visible recien en la barra 2
        strat = ScriptedStrategy({0: TargetPosition("long", 1000.0)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, funding, [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        self.assertAlmostEqual(result.trades[0].funding_paid_usd, 0.001 * 1000.0)

    def test_short_receives_positive_funding(self):
        bars = [_bar(0, 100.0, 100.0), _bar(1, 100.0, 100.0), _bar(2, 100.0, 100.0), _bar(3, 100.0, 100.0)]
        funding = [FundingPoint(bars[2].time_ms, 0.001)]
        strat = ScriptedStrategy({0: TargetPosition("short", 1000.0)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, funding, [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        self.assertAlmostEqual(result.trades[0].funding_paid_usd, -0.001 * 1000.0)


class TestFlatExit(unittest.TestCase):
    def test_explicit_flat_target_closes_position(self):
        bars = [_bar(0, 100.0, 100.0), _bar(1, 100.0, 100.0), _bar(2, 150.0, 150.0), _bar(3, 150.0, 150.0)]
        strat = ScriptedStrategy({0: TargetPosition("long", 1000.0), 1: TargetPosition("flat", 0.0)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.entry_price, 100.0)
        self.assertEqual(trade.exit_price, 150.0)   # open de la barra 2
        self.assertAlmostEqual(trade.pnl_usd, 1000.0 * (150.0 / 100.0 - 1))


class TestEndOfRunForcedClose(unittest.TestCase):
    def test_open_position_closes_at_last_bar_close(self):
        bars = [_bar(0, 100.0, 100.0), _bar(1, 100.0, 100.0), _bar(2, 100.0, 120.0)]
        strat = ScriptedStrategy({0: TargetPosition("long", 1000.0)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertEqual(trade.exit_price, 120.0)  # close de la ultima barra, no su open
        self.assertEqual(trade.exit_time_ms, bars[-1].time_ms)

    def test_forced_close_is_labeled_end_of_window_with_no_exit_snapshot(self):
        """Sin esto, un trade cerrado porque la corrida se quedo sin datos
        se veia identico en el journal a uno cerrado por una señal real de
        la estrategia -- ver docstring de Trade.exit_reason."""
        bars = [_bar(0, 100.0, 100.0), _bar(1, 100.0, 100.0), _bar(2, 100.0, 120.0)]
        strat = ScriptedStrategy({0: TargetPosition("long", 1000.0)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "end_of_window")
        self.assertIsNone(trade.exit_signal_snapshot)


class TestSignalExit(unittest.TestCase):
    def test_explicit_flat_exit_is_labeled_signal_with_an_exit_snapshot(self):
        bars = [_bar(0, 100.0, 100.0), _bar(1, 100.0, 100.0), _bar(2, 150.0, 150.0), _bar(3, 150.0, 150.0)]
        strat = ScriptedStrategy({0: TargetPosition("long", 1000.0), 1: TargetPosition("flat", 0.0)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "signal")
        self.assertIsNotNone(trade.exit_signal_snapshot)


class TestSlippageBreakdown(unittest.TestCase):
    def test_slippage_usd_is_the_exact_cost_the_slipped_fill_price_implies(self):
        """slippage_usd NO es una resta nueva sobre pnl_usd (el slip ya esta
        horneado en entry_price/exit_price) -- es solo el desglose de cuanto
        de ese costo ya incluido es slippage, para el "Costos de Ejecucion"
        del trade journal. Debe ser EXACTAMENTE size_usd * slippage_bps/1e4
        por pata (misma formula que `_slip` usa para mover el precio), no
        una aproximacion."""
        bars = [_bar(0, 100.0, 100.0), _bar(1, 100.0, 100.0), _bar(2, 100.0, 100.0)]
        strat = ScriptedStrategy({0: TargetPosition("long", 1000.0)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=100.0,  # 1%
        )
        trade = result.trades[0]
        # entrada (barra 1) + cierre forzoso (barra 2): dos patas de 1% cada una
        self.assertAlmostEqual(trade.slippage_usd, 1000.0 * 0.01 * 2)


class TestMfeMae(unittest.TestCase):
    def test_tracks_max_favorable_and_adverse_excursion_using_bar_high_low(self):
        """MFE/MAE tienen que usar el high/low de CADA barra mientras la
        posicion sigue abierta, no solo el close -- si solo se mirara el
        close se perderia toda excursion intrabar, que es justo lo que esto
        mide (util para juzgar donde habria quedado bien un SL/TP)."""
        bars = [
            _bar(0, 100.0, 100.0),   # señal
            _bar(1, 100.0, 100.0),   # entra aqui: open=100, high=101, low=99
            _bar(2, 100.0, 130.0),   # high=131, low=99 -- excursion favorable grande
            _bar(3, 130.0, 80.0),    # high=131, low=79 -- excursion adversa grande
        ]
        strat = ScriptedStrategy({0: TargetPosition("long", 1000.0)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertAlmostEqual(trade.entry_price, 100.0)
        # favorable maxima: high=131 en la barra 2 (o 3) -> (131/100 - 1) * 1000
        self.assertAlmostEqual(trade.mfe_usd, 310.0)
        # adversa maxima: low=79 en la barra 3 -> (1 - 79/100) * 1000
        self.assertAlmostEqual(trade.mae_usd, 210.0)

    def test_short_favorable_excursion_is_a_price_drop_not_a_rise(self):
        bars = [
            _bar(0, 100.0, 100.0),
            _bar(1, 100.0, 100.0),   # entra corto aqui: open=100
            _bar(2, 100.0, 70.0),    # high=101, low=69 -- el precio bajando es FAVORABLE para un corto
        ]
        strat = ScriptedStrategy({0: TargetPosition("short", 1000.0)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        # favorable: low=69 -> (1 - 69/100) * 1000 = 310
        self.assertAlmostEqual(trade.mfe_usd, 310.0)
        # adversa: high=101 (barra 1, la de entrada) -> (101/100 - 1) * 1000 = 10
        self.assertAlmostEqual(trade.mae_usd, 10.0)


class TestStopLossTakeProfit(unittest.TestCase):
    def test_long_stop_loss_closes_at_the_stop_price_when_low_touches_it(self):
        bars = [
            _bar(0, 100.0, 100.0),
            _bar(1, 100.0, 100.0),   # entra aqui: open=100
            _bar(2, 95.0, 92.0),     # low = min(95,92)-1 = 91 -- toca un SL de 95
        ]
        strat = ScriptedStrategy({0: TargetPosition("long", 1000.0, stop_loss_price=95.0)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "stop_loss")
        self.assertAlmostEqual(trade.exit_price, 95.0)
        self.assertEqual(trade.exit_time_ms, bars[2].time_ms)

    def test_long_take_profit_closes_at_the_target_price_when_high_touches_it(self):
        bars = [
            _bar(0, 100.0, 100.0),
            _bar(1, 100.0, 100.0),
            _bar(2, 105.0, 108.0),   # high = max(105,108)+1 = 109 -- toca un TP de 105
        ]
        strat = ScriptedStrategy({0: TargetPosition("long", 1000.0, take_profit_price=105.0)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "take_profit")
        self.assertAlmostEqual(trade.exit_price, 105.0)

    def test_short_stop_loss_triggers_on_a_price_rise_not_a_drop(self):
        bars = [
            _bar(0, 100.0, 100.0),
            _bar(1, 100.0, 100.0),   # entra corto aqui
            _bar(2, 105.0, 108.0),   # high=109 -- toca un SL de 105 (el precio subiendo es adverso a un corto)
        ]
        strat = ScriptedStrategy({0: TargetPosition("short", 1000.0, stop_loss_price=105.0)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "stop_loss")
        self.assertAlmostEqual(trade.exit_price, 105.0)

    def test_can_trigger_on_the_same_bar_as_entry(self):
        """Un SL muy ajustado puede tocarse en la MISMA barra en la que se
        abrio el trade -- el motor no espera a la barra siguiente para
        empezar a vigilarlo (ver el comentario del chequeo en el motor)."""
        bars = [
            _bar(0, 100.0, 100.0),
            _bar(1, 100.0, 90.0),   # entra en open=100; low=min(100,90)-1=89 -- toca un SL de 95 EN ESTA MISMA BARRA
            _bar(2, 90.0, 90.0),
        ]
        strat = ScriptedStrategy({0: TargetPosition("long", 1000.0, stop_loss_price=95.0)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertEqual(trade.exit_time_ms, bars[1].time_ms)   # misma barra que la entrada
        self.assertAlmostEqual(trade.exit_price, 95.0)

    def test_stop_and_target_in_the_same_bar_assumes_the_stop_hit_first(self):
        """Sin datos intrabar mas finos no se sabe el orden real -- asumir
        que el SL gano es la eleccion conservadora (no inventa un resultado
        mejor del que se puede defender)."""
        bars = [
            _bar(0, 100.0, 100.0),
            _bar(1, 100.0, 100.0),   # entra en open=100
            _bar(2, 100.0, 100.0),   # high=101, low=99 -- toca TANTO un SL de 99.5 como un TP de 100.5
        ]
        strat = ScriptedStrategy({
            0: TargetPosition("long", 1000.0, stop_loss_price=99.5, take_profit_price=100.5),
        })
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "stop_loss")
        self.assertAlmostEqual(trade.exit_price, 99.5)

    def test_r_multiple_uses_risk_measured_at_the_slipped_entry_price(self):
        bars = [
            _bar(0, 100.0, 100.0),
            _bar(1, 100.0, 100.0),    # entra con 1% de slip adverso -> entry_price=101
            _bar(2, 103.0, 106.0),    # high=107 -- toca un TP de 103
        ]
        strat = ScriptedStrategy({
            0: TargetPosition("long", 1000.0, stop_loss_price=99.0, take_profit_price=103.0),
        })
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=100.0,  # 1%
        )
        trade = result.trades[0]
        self.assertAlmostEqual(trade.entry_price, 101.0)
        # riesgo: 1000 * |101 - 99| / 101
        self.assertAlmostEqual(trade.risk_usd, 1000.0 * 2.0 / 101.0)
        self.assertIsNotNone(trade.r_multiple)
        self.assertAlmostEqual(trade.r_multiple, trade.pnl_usd / trade.risk_usd)

    def test_no_stop_or_target_leaves_risk_and_r_multiple_unset(self):
        """Las estrategias de referencia de Fase 2 (funding_squeeze_*) no
        dan SL/TP -- deben seguir sin risk_usd/r_multiple, no un 0 o un
        numero inventado."""
        bars = [_bar(0, 100.0, 100.0), _bar(1, 100.0, 100.0), _bar(2, 150.0, 150.0), _bar(3, 150.0, 150.0)]
        strat = ScriptedStrategy({0: TargetPosition("long", 1000.0), 1: TargetPosition("flat", 0.0)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertIsNone(trade.risk_usd)
        self.assertIsNone(trade.r_multiple)


class TestTrailingStopAndAdaptiveTarget(unittest.TestCase):
    """`TargetPosition.trail_atr_mult`/`target_atr_mult` (2026-09-14, goal
    original del usuario: SL y TP adaptables a la volatilidad). El motor,
    no la estrategia, reajusta stop_loss_price/take_profit_price en cada
    barra siguiente usando el ATR de ESE momento -- ver el docstring de
    TargetPosition."""

    def test_trailing_stop_ratchets_up_and_never_loosens_for_a_long(self):
        bars = [
            _bar(0, 100.0, 100.0),
            _bar(1, 100.0, 100.0),   # entra en open=100
            _bar(2, 100.0, 110.0),   # close sube -- el trail deberia subir
            _bar(3, 108.0, 108.0),   # retrocede un poco -- el trail NO deberia bajar
            _bar(4, 108.0, 108.0),
        ]
        expected_atr = atr_fn(bars)
        trail_mult = 1.0
        candidate_after_bar2 = 110.0 - trail_mult * expected_atr[2]
        candidate_after_bar3 = 108.0 - trail_mult * expected_atr[3]
        # El escenario solo prueba algo si el candidato de la barra 3 es
        # de verdad mas bajo que el de la barra 2 -- si no, el ratchet
        # nunca se ejercita.
        self.assertGreater(candidate_after_bar2, candidate_after_bar3)

        strat = ScriptedStrategy({0: TargetPosition("long", 1000.0, trail_atr_mult=trail_mult)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "end_of_window")  # nunca lo toco el propio SL
        self.assertAlmostEqual(trade.stop_loss_price, candidate_after_bar2)

    def test_trailing_stop_closes_the_trade_once_price_pulls_back_far_enough(self):
        bars = [
            _bar(0, 100.0, 100.0),
            _bar(1, 100.0, 100.0),    # entra en open=100
            _bar(2, 100.0, 110.0),    # sube -- el trail sube detras
            _bar(3, 100.0, 90.0),     # retrocede fuerte -- deberia tocar el trail que quedo arriba
        ]
        strat = ScriptedStrategy({0: TargetPosition("long", 1000.0, trail_atr_mult=1.0)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "stop_loss")
        self.assertEqual(trade.exit_time_ms, bars[3].time_ms)

    def test_adaptive_target_widens_after_a_volatility_spike(self):
        """Una barra con rango grande (un unico wick, sin cruzar el TP
        vigente en ese momento -- no se puede evitar un toque con datos de
        la MISMA barra que todavia no paso) sube el ATR; el TP de las
        barras SIGUIENTES debe reflejar ese ATR mas grande, no el de la
        entrada."""
        H = 3_600_000
        bars = [
            Bar(0 * H, 100.0, 101.0, 99.0, 100.0, 100.0, 50.0),
            Bar(1 * H, 100.0, 101.0, 99.0, 100.0, 100.0, 50.0),   # entra en open=100
            Bar(2 * H, 100.0, 101.0, 99.0, 100.0, 100.0, 50.0),
            Bar(3 * H, 100.0, 101.9, 70.0, 100.0, 100.0, 50.0),   # wick grande hacia abajo, high se queda bajo el TP vigente
            Bar(4 * H, 100.0, 101.0, 99.0, 100.0, 100.0, 50.0),   # salida por señal aqui
            Bar(5 * H, 100.0, 101.0, 99.0, 100.0, 100.0, 50.0),
        ]
        expected_atr = atr_fn(bars)
        target_mult = 1.0
        strat = ScriptedStrategy({
            0: TargetPosition("long", 1000.0, target_atr_mult=target_mult),
            4: TargetPosition("flat", 0.0),
        })
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "signal")  # sobrevivio al wick, no lo toco el TP viejo
        tp_using_entry_atr = trade.entry_price + target_mult * expected_atr[1]
        self.assertGreater(trade.take_profit_price, tp_using_entry_atr)

    def test_risk_usd_stays_based_on_the_initial_stop_not_the_trailed_one(self):
        """'R' es el riesgo INICIAL -- que el stop se haya movido despues
        no debe cambiar la unidad con la que se mide el resultado."""
        bars = [
            _bar(0, 100.0, 100.0),
            _bar(1, 100.0, 100.0),   # entra en open=100, SL inicial=95
            _bar(2, 100.0, 110.0),   # el trail deberia subir el stop bien por encima de 95
            _bar(3, 108.0, 108.0),
        ]
        strat = ScriptedStrategy({
            0: TargetPosition("long", 1000.0, stop_loss_price=95.0, trail_atr_mult=1.0),
        })
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertAlmostEqual(trade.risk_usd, 1000.0 * 5.0 / 100.0)
        self.assertGreater(trade.stop_loss_price, 95.0)   # confirma que el stop de verdad se movio

    def test_a_short_with_no_trail_or_target_mult_behaves_exactly_as_before(self):
        """Compatibilidad hacia atras: sin estos campos, el motor no toca
        stop_loss_price/take_profit_price despues de la entrada."""
        bars = [
            _bar(0, 100.0, 100.0),
            _bar(1, 100.0, 100.0),
            _bar(2, 100.0, 100.0),
            _bar(3, 100.0, 100.0),
        ]
        strat = ScriptedStrategy({0: TargetPosition("short", 1000.0, stop_loss_price=105.0)})
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertEqual(trade.stop_loss_price, 105.0)


class TestBreakevenStop(unittest.TestCase):
    """`TargetPosition.breakeven_at_r` (2026-09-19, pedido del usuario:
    analizar los trades OOS ya guardados de `cvd_divergence` mostro que
    una fraccion grande de los que terminan perdiendo TODO el riesgo
    primero habian alcanzado 0.2-0.3R de excursion favorable real). Salto
    UNICO a `entry_price` una vez que
    `mfe_usd/risk_usd` cruza el umbral -- nunca se afloja."""

    def test_snaps_stop_to_entry_once_mfe_threshold_reached_for_a_long(self):
        bars = [
            _bar(0, 100.0, 100.0),
            _bar(1, 100.0, 100.0),   # entra en open=100, SL=95 (risk=5/unidad)
            _bar(2, 100.0, 103.0),   # high=104 -> MFE=4, 4/5=0.8R >= 0.5R: breakeven se activa
            _bar(3, 100.0, 99.0),    # low=98 -> deberia tocar el breakeven (100), no el SL original (95)
        ]
        strat = ScriptedStrategy({
            0: TargetPosition("long", 1000.0, stop_loss_price=95.0, breakeven_at_r=0.5),
        })
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "stop_loss")
        self.assertEqual(trade.exit_time_ms, bars[3].time_ms)
        self.assertAlmostEqual(trade.stop_loss_price, 100.0)  # entry_price, no 95.0
        self.assertAlmostEqual(trade.pnl_usd, 0.0)  # rescatado en breakeven, no en -1R

    def test_does_not_trigger_before_the_mfe_threshold(self):
        """Compatibilidad: si el precio nunca alcanza el umbral, el stop
        se queda en el original -- mismo comportamiento que sin
        `breakeven_at_r`."""
        bars = [
            _bar(0, 100.0, 100.0),
            _bar(1, 100.0, 100.0),   # entra en open=100, SL=95
            _bar(2, 100.0, 101.0),   # high=102 -> MFE=2, 2/5=0.4R < 0.5R: NO se activa
            _bar(3, 100.0, 90.0),    # low=89 -> toca el SL original de 95
        ]
        strat = ScriptedStrategy({
            0: TargetPosition("long", 1000.0, stop_loss_price=95.0, breakeven_at_r=0.5),
        })
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "stop_loss")
        self.assertAlmostEqual(trade.stop_loss_price, 95.0)

    def test_is_a_one_time_snap_not_continuous_trailing(self):
        """A diferencia de `trail_atr_mult`, el stop NO sigue trepando mas
        alla de `entry_price` aunque el precio siga subiendo -- se queda
        exactamente en breakeven."""
        bars = [
            _bar(0, 100.0, 100.0),
            _bar(1, 100.0, 100.0),    # entra en open=100, SL=95
            _bar(2, 100.0, 103.0),    # activa breakeven (ver test de arriba)
            _bar(3, 105.0, 130.0),    # sigue subiendo mucho mas (low=104, por encima del breakeven) -- el stop NO deberia subir de 100
            _bar(4, 102.0, 102.0),    # low=101, se mantiene por encima del breakeven (100)
        ]
        strat = ScriptedStrategy({
            0: TargetPosition("long", 1000.0, stop_loss_price=95.0, breakeven_at_r=0.5),
        })
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "end_of_window")  # nunca lo toco el stop
        self.assertAlmostEqual(trade.stop_loss_price, 100.0)

    def test_mirrors_for_a_short(self):
        bars = [
            _bar(0, 100.0, 100.0),
            _bar(1, 100.0, 100.0),   # entra en open=100, SL=105 (risk=5/unidad)
            _bar(2, 100.0, 97.0),    # low=96 -> MFE=4, 4/5=0.8R >= 0.5R: breakeven se activa
            _bar(3, 100.0, 101.0),   # high=102 -> deberia tocar el breakeven (100), no el SL original (105)
        ]
        strat = ScriptedStrategy({
            0: TargetPosition("short", 1000.0, stop_loss_price=105.0, breakeven_at_r=0.5),
        })
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "stop_loss")
        self.assertAlmostEqual(trade.stop_loss_price, 100.0)
        self.assertAlmostEqual(trade.pnl_usd, 0.0)

    def test_risk_usd_stays_based_on_the_initial_stop(self):
        """Igual que con `trail_atr_mult` -- 'R' es el riesgo INICIAL, el
        breakeven no lo recalcula."""
        bars = [
            _bar(0, 100.0, 100.0),
            _bar(1, 100.0, 100.0),
            _bar(2, 100.0, 103.0),
            _bar(3, 100.0, 99.0),
        ]
        strat = ScriptedStrategy({
            0: TargetPosition("long", 1000.0, stop_loss_price=95.0, breakeven_at_r=0.5),
        })
        result = run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "scripted", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        trade = result.trades[0]
        self.assertAlmostEqual(trade.risk_usd, 1000.0 * 5.0 / 100.0)


class RecordingStrategy:
    """Guarda `ctx.indicators` por indice de barra en vez de operar --
    para inspeccionar exactamente que ve la estrategia en cada barra."""

    def __init__(self):
        self.seen: dict = {}

    def on_bar(self, ctx):
        self.seen[ctx.bar_index] = ctx.indicators["real_liquidations_this_bar"]
        return None


class TestRealLiquidationsPerBar(unittest.TestCase):
    """`real_liquidations_this_bar` (el trigger de `strategies/liquidation_bounce.py`)
    -- eventos que ocurrieron DENTRO de
    la ventana de esa barra (desde el cierre de la anterior, exclusivo,
    hasta el cierre de esta, inclusive), nunca en una barra distinta."""

    def test_event_attaches_to_the_bar_whose_window_contains_it(self):
        bars = [_bar(0, 100.0, 100.0, t=0), _bar(1, 100.0, 100.0, t=3_600_000), _bar(2, 100.0, 100.0, t=7_200_000)]
        events = [RealLiquidation(time_ms=1_800_000, side="long", price=100.0, quote_qty_usd=500_000.0)]
        strat = RecordingStrategy()
        run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "recording", {}, commission_bps=0.0, slippage_bps=0.0,
            real_liquidations=events,
        )
        self.assertEqual(strat.seen[0], [])
        self.assertEqual(strat.seen[1], events)
        self.assertEqual(strat.seen[2], [])

    def test_event_exactly_at_bar_close_belongs_to_that_bar_not_the_next(self):
        bars = [_bar(0, 100.0, 100.0, t=0), _bar(1, 100.0, 100.0, t=3_600_000), _bar(2, 100.0, 100.0, t=7_200_000)]
        events = [RealLiquidation(time_ms=3_600_000, side="short", price=100.0, quote_qty_usd=500_000.0)]
        strat = RecordingStrategy()
        run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "recording", {}, commission_bps=0.0, slippage_bps=0.0,
            real_liquidations=events,
        )
        self.assertEqual(strat.seen[1], events)
        self.assertEqual(strat.seen[2], [])

    def test_no_real_liquidations_passed_gives_empty_lists_not_a_crash(self):
        bars = [_bar(0, 100.0, 100.0)]
        strat = RecordingStrategy()
        run_backtest_on_series(
            "TEST", "1h", None, None, bars, [], [], [], None,
            strat, "recording", {}, commission_bps=0.0, slippage_bps=0.0,
        )
        self.assertEqual(strat.seen[0], [])


class RecordingHtfStrategy:
    """Igual que `RecordingStrategy` pero para `htf_trend`/`htf_zone_signal`
    (ver TestHtfIndicators mas abajo)."""

    def __init__(self):
        self.seen: dict = {}

    def on_bar(self, ctx):
        self.seen[ctx.bar_index] = (ctx.indicators["htf_trend"], ctx.indicators["htf_zone_signal"])
        return None


class TestHtfIndicators(unittest.TestCase):
    """`htf_trend`/`htf_zone_signal` (ver htf_context.py) -- confirma que el
    motor los alinea por TIEMPO contra `htf_bars` (nunca contra una vela
    mayor todavia en curso), no solo que `htf_context.py` en si funcione
    (eso ya lo cubre test_backtest_htf_context.py)."""

    def _htf_bars(self):
        # Mismo fixture que test_backtest_smc.py::TestOrderBlocks
        # (test_bullish_order_block_before_bos_up_with_mitigation_and_break),
        # con tiempos reales de 1h: BOS confirmado en la barra htf index=5
        # (cierra en t=18_000_000), roto en la barra htf index=6 (cierra en
        # t=21_600_000).
        raw = [
            (9.8, 10, 9.0, 9.5), (9.6, 12, 9.2, 11), (11, 11, 8.0, 9), (9, 13, 8.5, 12),
            (12, 12, 9.3, 10), (10, 14, 9.4, 13), (13, 13, 8.8, 8),
        ]
        return [Bar(i * 3_600_000, o, h, l, c, 0.0, 0.0) for i, (o, h, l, c) in enumerate(raw)]

    def test_none_before_any_htf_bar_has_closed(self):
        htf_bars = self._htf_bars()
        bars = [_bar(0, 100.0, 100.0, t=-500)]
        strat = RecordingHtfStrategy()
        run_backtest_on_series(
            "TEST", "5m", None, None, bars, [], [], [], None,
            strat, "recording", {}, commission_bps=0.0, slippage_bps=0.0,
            htf_bars=htf_bars, htf_lookback=1,
        )
        self.assertEqual(strat.seen[0], (None, None))

    def test_bullish_zone_and_up_trend_once_htf_bos_bar_has_closed(self):
        htf_bars = self._htf_bars()
        # 5 minutos despues del cierre de la barra htf index=5 (t=18_000_000)
        # -- el BOS ya esta confirmado, el Order Block (zona [9.5,9.8])
        # todavia no esta roto (eso pasa recien en la barra htf index=6).
        bars = [_bar(0, 9.6, 9.6, t=18_300_000)]
        strat = RecordingHtfStrategy()
        run_backtest_on_series(
            "TEST", "5m", None, None, bars, [], [], [], None,
            strat, "recording", {}, commission_bps=0.0, slippage_bps=0.0,
            htf_bars=htf_bars, htf_lookback=1,
        )
        self.assertEqual(strat.seen[0], ("up", "bullish"))

    def test_zone_signal_clears_once_htf_break_bar_has_closed(self):
        htf_bars = self._htf_bars()
        # 5 minutos despues del cierre de la barra htf index=6
        # (t=21_600_000, donde el bloque se rompe) -- la tendencia sigue
        # "up" (ningun evento nuevo la cambio), pero la zona ya no cuenta.
        bars = [_bar(0, 9.6, 9.6, t=21_900_000)]
        strat = RecordingHtfStrategy()
        run_backtest_on_series(
            "TEST", "5m", None, None, bars, [], [], [], None,
            strat, "recording", {}, commission_bps=0.0, slippage_bps=0.0,
            htf_bars=htf_bars, htf_lookback=1,
        )
        self.assertEqual(strat.seen[0], ("up", None))


if __name__ == "__main__":
    unittest.main()
