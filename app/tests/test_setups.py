"""Sin red/DB: solo la logica pura de setups.py. find_setups en si toca
DB/red (real_liquidations, heatmap, funding/OI en vivo) -- mismo criterio
que el resto del proyecto para los endpoints que combinan varias fuentes
en vivo (heatmap.py, indicators.py): se verifica contra datos reales con
curl, no con mocks aqui."""
import unittest

from api.setups import _pool_near


def _tier(leverage, cells):
    """cells: lista de (price, notional_usd, time_ms, bucket_ms)."""
    return {
        "leverage": leverage,
        "cells": [
            {"price": p, "notional_usd": n, "time_ms": t, "bucket_ms": b}
            for p, n, t, b in cells
        ],
    }


class TestPoolNear(unittest.TestCase):
    def test_sums_notional_of_cells_active_at_the_given_time(self):
        tiers = [
            _tier(10, [(100.0, 500.0, 1000, 500), (105.0, 300.0, 1000, 500)]),
            _tier(25, [(102.0, 200.0, 900, 300)]),
        ]
        result = _pool_near(tiers, price=100.0, at_time_ms=1200, tolerance_usd=5.0)
        self.assertEqual(result, {10: 800.0})  # el de 25x ya termino en t=1200 (900+300)

    def test_bug_real_encontrado_2026_09_04_un_pool_ya_consumido_no_cuenta_como_activo(self):
        """El hallazgo real que motivo este diseño: si el precio YA cruzo
        el nivel (la celda termino ANTES del instante de la liquidacion),
        ese pool ya se "comio" -- no debe contar como evidencia de un pool
        que la liquidacion real acaba de consumir, porque el snapshot
        decaido nunca lo tendria de todos modos. Verificado contra una
        liquidacion real de BTCUSDT: con `levels` (snapshot ya decaido) el
        resultado siempre daba vacio, exactamente por esto."""
        tiers = [_tier(10, [(100.0, 500.0, 1000, 200)])]  # activo de t=1000 a t=1200
        # la liquidacion ocurre en t=1300, DESPUES de que la celda ya termino
        result = _pool_near(tiers, price=100.0, at_time_ms=1300, tolerance_usd=5.0)
        self.assertEqual(result, {})

    def test_cell_not_yet_started_does_not_count(self):
        tiers = [_tier(10, [(100.0, 500.0, 2000, 500)])]  # empieza en t=2000
        result = _pool_near(tiers, price=100.0, at_time_ms=1500, tolerance_usd=5.0)
        self.assertEqual(result, {})

    def test_ignores_cells_outside_price_tolerance(self):
        tiers = [_tier(10, [(100.0, 500.0, 1000, 500), (200.0, 999.0, 1000, 500)])]
        result = _pool_near(tiers, price=100.0, at_time_ms=1200, tolerance_usd=5.0)
        self.assertEqual(result, {10: 500.0})

    def test_leverage_with_nothing_active_nearby_is_absent_not_zero(self):
        """Un apalancamiento sin nada activo cerca no cuenta para la
        'correlacion' (cuantos apalancamientos DISTINTOS convergen) -- un
        0 explicito lo contaria igual que uno real si no se excluyera."""
        tiers = [
            _tier(10, [(100.0, 500.0, 1000, 500)]),
            _tier(25, [(500.0, 999.0, 1000, 500)]),
        ]
        result = _pool_near(tiers, price=100.0, at_time_ms=1200, tolerance_usd=5.0)
        self.assertEqual(result, {10: 500.0})
        self.assertNotIn(25, result)

    def test_empty_tiers_yields_empty_dict(self):
        self.assertEqual(_pool_near([], price=100.0, at_time_ms=1000, tolerance_usd=5.0), {})


if __name__ == "__main__":
    unittest.main()
