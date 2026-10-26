import json
import unittest
from datetime import datetime, timezone

from backtest.data import Bar
from backtest.volume_profile import compute_profile, session_profile_series


def _bar(time_ms, open_, high, low, close, volume) -> Bar:
    return Bar(time_ms=time_ms, open=open_, high=high, low=low, close=close, volume=volume, taker_buy_volume=0.0)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class TestComputeProfile(unittest.TestCase):
    def test_empty_bars_returns_none(self):
        self.assertIsNone(compute_profile([]))

    def test_degenerate_price_range_returns_none(self):
        bars = [_bar(0, 100, 100, 100, 100, 10), _bar(1, 100, 100, 100, 100, 5)]
        self.assertIsNone(compute_profile(bars))

    def test_poc_vah_val_hvn_hand_verified(self):
        """Rango de precio 100-124 (24 buckets de tamaño exacto 1.0, sin
        fracciones que compliquen la cuenta a mano):

        - Bar A: [100,124], volumen 24 -- span=24=NUM_BUCKETS, asi que
          cada uno de los 24 buckets recibe exactamente 24/24=1.0.
        - Bar B: [110,112], volumen 20 -- span=2, se reparte entre los DOS
          buckets [110,111) y [111,112): 20*1/2=10.0 cada uno.

        Volumenes finales: bucket 10 y 11 = 1.0+10.0 = 11.0; el resto = 1.0.
        POC = bucket 10 (primer maximo, empatado con el 11). Area de Valor
        (70% de 44=30.8): crece desde el 10 siempre hacia arriba en cada
        empate (below==above==1.0 -- el criterio `above_vol >= below_vol`
        favorece arriba) hasta juntar 31 en el bucket 20 -- ver el calculo
        completo en la conversacion/commit que introdujo este test."""
        bars = [
            _bar(0, 100, 124, 100, 100, 24.0),
            _bar(1, 111, 112, 110, 111, 20.0),
        ]
        profile = compute_profile(bars)
        self.assertIsNotNone(profile)
        self.assertEqual(len(profile.buckets), 24)
        self.assertAlmostEqual(profile.buckets[10].volume, 11.0)
        self.assertAlmostEqual(profile.buckets[11].volume, 11.0)
        self.assertAlmostEqual(profile.buckets[0].volume, 1.0)
        self.assertEqual(profile.poc_idx, 10)
        self.assertAlmostEqual(profile.poc, 110.5)
        self.assertAlmostEqual(profile.val, 110.0)
        self.assertAlmostEqual(profile.vah, 121.0)

    def test_hvn_prices_and_nearest_above_below(self):
        bars = [
            _bar(0, 100, 124, 100, 100, 24.0),
            _bar(1, 111, 112, 110, 111, 20.0),
        ]
        profile = compute_profile(bars)
        hvn = profile.hvn_prices()
        self.assertEqual(sorted(hvn), [110.5, 111.5])
        self.assertIsNone(profile.nearest_hvn_above(115.0))
        self.assertAlmostEqual(profile.nearest_hvn_above(105.0), 110.5)
        self.assertAlmostEqual(profile.nearest_hvn_below(115.0), 111.5)
        self.assertIsNone(profile.nearest_hvn_below(100.0))

    def test_single_bar_spreads_volume_evenly_across_all_buckets(self):
        """Una sola vela cuyo rango cubre TODO el perfil -- cada bucket
        queda completamente contenido en [low, high], asi que el
        `overlap` es el tamaño entero del bucket para los 24: el volumen
        se reparte en partes EXACTAMENTE iguales."""
        bars = [_bar(0, 100, 124, 100, 100, 240.0)]
        profile = compute_profile(bars)
        for b in profile.buckets:
            self.assertAlmostEqual(b.volume, 10.0)

    def test_profile_is_json_serializable(self):
        """Bug real (2026-09-17, ver docstring del modulo): `ctx.indicators`
        completo -- incluido `session_profile` -- se guarda como
        `signal_snapshot` en el trade journal via `json.dumps`
        (`psycopg.types.json.Jsonb`). Un `@dataclass` revienta ese
        `json.dumps` con `TypeError: Object of type SessionProfile is not
        JSON serializable` -- CONFIRMADO en una corrida real (el backtest
        entero corria bien, incluidos los trades; fallaba recien al
        guardar), no un caso hipotetico. `SessionProfile`/`Bucket` son
        `NamedTuple` especificamente por esto: `json.dumps` SI sabe
        serializar una tupla."""
        bars = [
            _bar(0, 100, 124, 100, 100, 24.0),
            _bar(1, 111, 112, 110, 111, 20.0),
        ]
        profile = compute_profile(bars)
        snapshot = {"session_profile": profile, "cvd_change_20": 5.0}
        json.dumps(snapshot)  # no debe lanzar TypeError


class TestSessionProfileSeriesCausality(unittest.TestCase):
    def test_first_day_has_no_profile(self):
        day1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = [_bar(_ms(day1) + i * 60_000, 100, 101, 99, 100, 1.0) for i in range(5)]
        series = session_profile_series(bars)
        self.assertEqual(series, [None] * 5)

    def test_second_day_gets_the_first_days_profile(self):
        day1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        day2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
        day1_bars = [_bar(_ms(day1) + i * 60_000, 100, 124, 100, 110, 1.0) for i in range(3)]
        day2_bars = [_bar(_ms(day2) + i * 60_000, 200, 224, 200, 210, 1.0) for i in range(3)]
        series = session_profile_series(day1_bars + day2_bars)
        self.assertEqual(series[:3], [None, None, None])
        expected = compute_profile(day1_bars)
        for profile in series[3:]:
            self.assertIsNotNone(profile)
            self.assertAlmostEqual(profile.poc, expected.poc)
        # Nunca el perfil de la sesion EN CURSO (dia 2) -- verificado
        # comparando contra el perfil que SI tendria dia 2 si (por error)
        # se usara su propia sesion.
        wrong = compute_profile(day2_bars)
        self.assertNotAlmostEqual(series[3].poc, wrong.poc)

    def test_a_third_day_gets_the_second_days_profile_not_the_firsts(self):
        day1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        day2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
        day3 = datetime(2026, 1, 3, tzinfo=timezone.utc)
        day1_bars = [_bar(_ms(day1), 100, 124, 100, 110, 1.0)]
        day2_bars = [_bar(_ms(day2), 200, 224, 200, 210, 1.0)]
        day3_bars = [_bar(_ms(day3), 300, 324, 300, 310, 1.0)]
        series = session_profile_series(day1_bars + day2_bars + day3_bars)
        expected_day2_profile = compute_profile(day2_bars)
        self.assertAlmostEqual(series[2].poc, expected_day2_profile.poc)

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(session_profile_series([]), [])


if __name__ == "__main__":
    unittest.main()
