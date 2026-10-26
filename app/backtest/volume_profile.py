"""Perfil de Volumen de Rango Fijo (FRVP) por SESION, point-in-time, para
el backtester -- puerto a Python de la misma logica ya construida en el
frontend (`FixedRangeVolumeProfile.tsx`, herramienta MANUAL del usuario:
dos clics sobre el grafico). Aca hace falta un perfil AUTOMATICO, uno por
sesion, para que una estrategia pueda consultarlo barra a barra.

Sesion = dia calendario UTC, no una ventana movil de N barras: es el uso
profesional estandar ("perfil del dia anterior" como referencia estatica
para el dia en curso) y, mas importante para este motor, el perfil de una
sesion ya CERRADA nunca cambia mientras la sesion siguiente esta en curso
-- una ventana movil recalculada cada barra dejaria que el mismo nivel
(POC/VAH/VAL) se corriera bajo los pies de una posicion ya abierta, sin
ninguna razon de mercado real detras de ese movimiento.

Causal por construccion (mismo contrato que `htf_context.py`): el perfil
de la sesion EN CURSO nunca se expone -- `session_profile_series` solo
asigna a cada barra el perfil de la sesion ANTERIOR, ya cerrada por
completo. No hace falta bisect ni indices point-in-time como en
`htf_context.py` porque el perfil cambia una sola vez por dia (no en
cualquier punto arbitrario), así que un simple mapa dia->perfil alcanza.

`NamedTuple`, no `@dataclass` (bug real): `ctx.indicators`
completo se guarda como `signal_snapshot` en el trade journal
(`journal.save_run`/`save_validation`, JSONB via `psycopg.types.json.Jsonb`)
-- un `@dataclass` normal no es serializable por `json.dumps` y revienta
la corrida entera recien al guardar, no al calcular (confirmado con un
smoke test real: el backtest completo, incluidos los trades, corrio bien;
fallo call `TypeError: Object of type SessionProfile is not JSON
serializable`). El resto del motor ya resuelve esto mismo con
`NamedTuple` (`TargetPosition`/`Position`/`Bar`/`HtfContext`, etc.):
`json.dumps` SI sabe serializar una tupla (la trata como lista), asi que
heredar de `NamedTuple` en vez de `@dataclass` alcanza sin logica extra
-- mismo patron que ya usa `data.RealLiquidation` dentro de
`real_liquidations_this_bar`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import NamedTuple, Optional, Sequence

from .data import Bar

# Mismos valores que FixedRangeVolumeProfile.tsx -- consistencia entre el
# perfil que el usuario ve a mano en el dashboard y el que usa el
# backtester, no dos convenciones distintas para la misma idea.
NUM_BUCKETS = 24
VALUE_AREA_FRACTION = 0.7


class Bucket(NamedTuple):
    price_low: float
    price_high: float
    volume: float

    @property
    def mid(self) -> float:
        return (self.price_low + self.price_high) / 2


class SessionProfile(NamedTuple):
    buckets: tuple[Bucket, ...]
    poc_idx: int
    va_low_idx: int
    va_high_idx: int

    @property
    def poc(self) -> float:
        return self.buckets[self.poc_idx].mid

    @property
    def vah(self) -> float:
        return self.buckets[self.va_high_idx].price_high

    @property
    def val(self) -> float:
        return self.buckets[self.va_low_idx].price_low

    def hvn_prices(self) -> list[float]:
        """Nodos de Alto Volumen: buckets con volumen >= el promedio del
        propio perfil. Umbral relativo al perfil (no un numero fijo en
        USD/BTC que no tendria sentido de un simbolo o rango a otro) --
        con 24 buckets y un Area de Valor que por definicion concentra el
        70% del volumen en unos pocos buckets, "por encima del promedio"
        recupera aproximadamente esa misma zona sin inventar un segundo
        umbral arbitrario distinto de `VALUE_AREA_FRACTION`."""
        mean_vol = sum(b.volume for b in self.buckets) / len(self.buckets)
        return [b.mid for b in self.buckets if b.volume >= mean_vol]

    def nearest_hvn_above(self, price: float) -> Optional[float]:
        """El HVN mas cercano por ENCIMA de `price` -- objetivo de TP de
        `lvn_breakout_long` (ver docstring de la estrategia). `None` si no
        hay ningun HVN por encima dentro de este perfil (el precio ya esta
        mas alla de toda la estructura conocida de la sesion anterior)."""
        candidates = [p for p in self.hvn_prices() if p > price]
        return min(candidates) if candidates else None

    def nearest_hvn_below(self, price: float) -> Optional[float]:
        candidates = [p for p in self.hvn_prices() if p < price]
        return max(candidates) if candidates else None


def compute_profile(bars: Sequence[Bar]) -> Optional[SessionProfile]:
    """Igual que `computeProfile`/`computeValueArea` de
    FixedRangeVolumeProfile.tsx: reparte el volumen de CADA vela
    PROPORCIONALMENTE entre los buckets de precio que su rango
    [low, high] atraviesa (no todo al cierre, que exagerar un pico
    artificial ahi), y el Area de Valor crece desde el bucket de mayor
    volumen (POC) sumando el vecino mas grande de los dos hasta juntar
    `VALUE_AREA_FRACTION` del volumen total. `None` si `bars` esta vacio o
    el rango de precio de la sesion es degenerado (todas las velas
    perfectamente planas, en la practica solo posible con datos
    sinteticos de test)."""
    if not bars:
        return None
    price_min = min(b.low for b in bars)
    price_max = max(b.high for b in bars)
    if not price_max > price_min:
        return None

    bucket_size = (price_max - price_min) / NUM_BUCKETS
    volumes = [0.0] * NUM_BUCKETS
    for b in bars:
        span = b.high - b.low
        if span <= 0:
            idx = min(NUM_BUCKETS - 1, max(0, int((b.close - price_min) / bucket_size)))
            volumes[idx] += b.volume
            continue
        for i in range(NUM_BUCKETS):
            bucket_low = price_min + i * bucket_size
            bucket_high = bucket_low + bucket_size
            overlap = min(b.high, bucket_high) - max(b.low, bucket_low)
            if overlap > 0:
                volumes[i] += b.volume * (overlap / span)

    buckets = tuple(
        Bucket(price_min + i * bucket_size, price_min + (i + 1) * bucket_size, volumes[i])
        for i in range(NUM_BUCKETS)
    )
    poc_idx = max(range(NUM_BUCKETS), key=lambda i: buckets[i].volume)
    va_low_idx, va_high_idx = _value_area(buckets, poc_idx)
    return SessionProfile(buckets=buckets, poc_idx=poc_idx, va_low_idx=va_low_idx, va_high_idx=va_high_idx)


def _value_area(buckets: tuple[Bucket, ...], poc_idx: int) -> tuple[int, int]:
    """Algoritmo estandar de volume profile (Peter Steidlmayer): arranca
    en el POC y suma, un paso a la vez, el bucket inmediato de arriba o de
    abajo -- el que tenga mas volumen de los dos -- hasta acumular
    `VALUE_AREA_FRACTION` del volumen total. Mismo algoritmo que
    `computeValueArea` en FixedRangeVolumeProfile.tsx."""
    total = sum(b.volume for b in buckets)
    target = total * VALUE_AREA_FRACTION
    lo = hi = poc_idx
    accumulated = buckets[poc_idx].volume
    n = len(buckets)
    while accumulated < target and (lo > 0 or hi < n - 1):
        below_vol = buckets[lo - 1].volume if lo > 0 else -1.0
        above_vol = buckets[hi + 1].volume if hi < n - 1 else -1.0
        if above_vol >= below_vol:
            hi += 1
            accumulated += above_vol
        else:
            lo -= 1
            accumulated += below_vol
    return lo, hi


def _day_key(time_ms: int) -> str:
    return datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def session_profile_series(bars: Sequence[Bar]) -> list[Optional[SessionProfile]]:
    """`series[i]` es el perfil del dia calendario UTC ANTERIOR al de
    `bars[i]` -- NUNCA el de la propia sesion en curso (ver "Causal por
    construccion" en el docstring del modulo). `None` para toda barra del
    primer dia de la serie cargada (no hay sesion anterior que perfilar)
    y para cualquier dia cuya sesion anterior resulto degenerada (ver
    `compute_profile`).

    Se recalcula UNA vez por dia, no por barra -- `bars` ya viene ordenado
    por tiempo (contrato de `data.load_bars`), asi que agrupar por dia en
    una sola pasada basta, sin necesidad de bisect."""
    if not bars:
        return []
    day_keys = [_day_key(b.time_ms) for b in bars]
    day_bars: dict[str, list[Bar]] = {}
    day_order: list[str] = []
    for b, k in zip(bars, day_keys):
        if k not in day_bars:
            day_bars[k] = []
            day_order.append(k)
        day_bars[k].append(b)

    profile_by_day = {k: compute_profile(v) for k, v in day_bars.items()}
    prev_profile_by_day: dict[str, Optional[SessionProfile]] = {day_order[0]: None}
    for idx in range(1, len(day_order)):
        prev_profile_by_day[day_order[idx]] = profile_by_day[day_order[idx - 1]]

    return [prev_profile_by_day[k] for k in day_keys]
