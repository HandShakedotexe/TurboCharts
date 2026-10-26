"""Trades grandes recientes, directo a Binance -- NO persisten en TimescaleDB.

Sirven de "entradas" reales para el heatmap. La versión anterior repartía
todo el open interest desde el precio de AHORA, como si toda la posición se
hubiera abierto en este instante -- eso da 5 líneas limpias alrededor del
precio actual, no la textura densa y con muchos niveles de un heatmap
publicado (CoinGlass, etc.). Cada trade grande se trata en su lugar como una
posible apertura de posición en SU PROPIO precio: el open interest se fue
acumulando a distintos precios a lo largo del tiempo, y el heatmap debería
reflejar eso.

Umbral de "trade grande": $10,000 USD de notional. No hay una cifra
"correcta" publicada en ningún sitio -- es una elección, verificada con
datos reales el 2026-08-28 antes de fijarla (no adivinada):

    BTCUSDT  (1000 aggTrades, ventana de ~4.6 min): 7.5% >= $10,000
             (~23,000 trades grandes/día)
    TIAUSDT  (1000 aggTrades, ventana de ~69 min):  ~2.6 trades grandes/hora

Denso donde hay volumen real (los majors), presente pero disperso donde no
-- sin inventar actividad que no existe.

VENTANA: 12 horas, no 4. Con 4h se probó en un momento en que BTC apenas se
había movido, y los niveles de liquidación de todos los trades grandes
quedaron apretados en dos franjas de precio -- exactamente 2 líneas por
apalancamiento, no la textura de muchas franjas a distintos precios que se
buscaba. Una ventana más larga captura trades de cuando el precio estaba en
otros niveles, y por eso las liquidaciones aparecen repartidas en todo el
rango que se ve en un heatmap publicado. Se probó con 24h primero: dio
buena densidad pero disparó 429 (Too Many Requests) de Binance en el
backfill inicial de BTCUSDT -- 12h + espaciado entre páginas (ver
PAGE_PACE_SECONDS) se quedan dentro del límite real, verificado.

CACHE INCREMENTAL: 12h de BTCUSDT son miles de trades grandes -- pedirlos
enteros cada minuto (a como refresca el heatmap) tardaría más de un minuto
por si solo. En vez de eso se guarda un buffer por símbolo y cada refresco
solo pide, con `fromId`, lo que llegó desde el último trade ya visto. El
primer arranque por símbolo sigue siendo lento (un backfill completo de
12h, del orden de un minuto en BTCUSDT); todos los siguientes son casi
instantáneos.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

BINANCE_REST = "https://fapi.binance.com"
USER_AGENT = "liq-intel-api/0.1"

# Subido de $10,000 a $100,000 a peticion del usuario (2026-08-28): con
# $10k el heatmap salia demasiado saturado (miles de trades pequeños
# aportando ruido). Con datos reales de BTCUSDT verificados el 2026-08-28:
# ~1.1% de los trades superan $100k (~3,450/dia) -- mas limpio, y siguen
# siendo trades genuinamente grandes, no ruido de retail.
BIG_TRADE_THRESHOLD_USD = 100_000.0
# 24h daba una densidad muy buena pero disparaba 429 (Too Many Requests) de
# Binance en el backfill inicial de un simbolo con tanto volumen como
# BTCUSDT -- confirmado en pruebas reales, no una suposicion. 12h + el
# espaciado de abajo se quedan dentro del limite.
LOOKBACK_HOURS = 12.0
# Tope de seguridad por refresco: a 1000 trades/pagina, 150 paginas son
# 150,000 trades escaneados en una sola llamada -- de sobra para las 12h de
# BTCUSDT (~13,000 trades/hora en horas activas) incluso en el backfill
# inicial, y evita un bucle sin fin si la paginacion se descontrola.
MAX_PAGES = 150
# Espaciado entre paginas: sin esto, un backfill de BTCUSDT (decenas de
# paginas seguidas sin pausa) dispara 429 de Binance -- visto en pruebas
# reales, no algo evitado por precaucion. Mismo patron que ya usan los
# pollers del grabador de Fase 0 (recorder/liqrec/pollers.py) entre
# peticiones a un mismo exchange.
PAGE_PACE_SECONDS = 0.2
CACHE_TTL_SECONDS = 60.0


def _get_json(url: str, timeout: float = 8.0):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _paginate(symbol: str, *, start_ms: int | None = None, from_id: int | None = None) -> list[dict]:
    """Trae aggTrades crudos desde `start_ms` o desde `from_id` hasta
    alcanzar el presente. Exactamente uno de los dos debe darse.

    Pagina con `fromId`, no con `startTime`+`endTime`: ese par exige un
    rango de una hora o menos en la API de Binance, y aqui se quieren
    ventanas mas largas. Con solo `startTime` (o solo `fromId`) no hay esa
    restriccion -- verificado contra la API real antes de construir esto
    sobre un supuesto."""
    now_ms = int(time.time() * 1000)
    raw: list[dict] = []
    if from_id is not None:
        url = f"{BINANCE_REST}/fapi/v1/aggTrades?symbol={symbol}&fromId={from_id}&limit=1000"
    else:
        url = f"{BINANCE_REST}/fapi/v1/aggTrades?symbol={symbol}&startTime={start_ms}&limit=1000"
    for i in range(MAX_PAGES):
        if i > 0:
            time.sleep(PAGE_PACE_SECONDS)
        page = _get_json(url)
        if not page:
            break
        raw.extend(page)
        last = page[-1]
        if len(page) < 1000 or last["T"] >= now_ms:
            break
        url = f"{BINANCE_REST}/fapi/v1/aggTrades?symbol={symbol}&fromId={last['a'] + 1}&limit=1000"
    return raw


def fetch_recent_big_trades(
    symbol: str,
    threshold_usd: float = BIG_TRADE_THRESHOLD_USD,
    lookback_hours: float = LOOKBACK_HOURS,
) -> list[tuple[float, float, int]]:
    """Devuelve [(precio, notional_usd, trade_time_ms), ...] de trades >=
    threshold_usd en las ultimas `lookback_hours`. Backfill completo, sin
    cache -- para eso esta `get_recent_big_trades_cached`.

    `trade_time_ms` (campo `T` de Binance) viaja hasta
    `heatmap_model.compute_levels_from_trades` y de ahi a cada nivel
    generado -- lo necesita `filter_consumed_levels` para saber si el precio
    ya cruzo ese nivel DESPUES de que el trade ocurriera."""
    start_ms = int((time.time() - lookback_hours * 3600) * 1000)
    raw = _paginate(symbol, start_ms=start_ms)
    return [
        (float(t["p"]), float(t["p"]) * float(t["q"]), int(t["T"]))
        for t in raw
        if float(t["p"]) * float(t["q"]) >= threshold_usd
    ]


@dataclass
class _Buffer:
    # (aggId, time_ms, price, notional_usd, is_buyer_maker) -- solo trades ya
    # filtrados por threshold_usd, para que el buffer de un simbolo con mucho
    # volumen no crezca sin limite guardando trades pequeños que nunca hacen
    # falta. `is_buyer_maker` es el campo "m" de Binance ("Was the buyer the
    # maker?") -- True implica que el VENDEDOR fue el agresor (taker sell,
    # BigTradesOverlay.tsx lo pinta rojo), False que lo fue el comprador
    # (taker buy, verde). Guardado aca (2026-09-15, pedido del usuario:
    # "los vendedores que sean de color rojo y los verdes [compradores]")
    # para que `get_recent_big_trades_with_side_cached` no tenga que volver
    # a pedirle nada a Binance solo por el lado.
    trades: list[tuple[int, int, float, float, bool]] = field(default_factory=list)
    last_refresh: float = 0.0


_buffers: dict[str, _Buffer] = {}
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(symbol: str) -> threading.Lock:
    with _locks_guard:
        if symbol not in _locks:
            _locks[symbol] = threading.Lock()
        return _locks[symbol]


def get_recent_big_trades_cached(
    symbol: str,
    threshold_usd: float = BIG_TRADE_THRESHOLD_USD,
    lookback_hours: float = LOOKBACK_HOURS,
) -> list[tuple[float, float, int]]:
    """Como `fetch_recent_big_trades`, pero con un buffer incremental por
    simbolo: la primera vez hace un backfill completo de `lookback_hours`
    (lento -- para BTCUSDT, un dia entero son decenas de peticiones
    paginadas); los refrescos siguientes solo piden, con `fromId`, lo que
    llego desde el ultimo trade ya visto, y podan lo que ya salio de la
    ventana. Sin esto, refrescar cada minuto un dia entero de BTCUSDT
    tardaria mas de un minuto por si solo."""
    return [(p, n, t) for (_id, t, p, n, _m) in _refresh_buffer(symbol, threshold_usd, lookback_hours)]


def get_recent_big_trades_with_side_cached(
    symbol: str,
    threshold_usd: float = BIG_TRADE_THRESHOLD_USD,
    lookback_hours: float = LOOKBACK_HOURS,
) -> list[tuple[float, float, int, bool]]:
    """Igual que `get_recent_big_trades_cached` (mismo buffer compartido, un
    solo refresco por symbolo sirve a las dos), pero sin descartar el lado
    -- usado por `api/routers/big_trades.py` para las burbujas
    compra/venta de `BigTradesOverlay.tsx`. Separada de la version sin lado
    para no tocar el contrato de `heatmap_model.compute_levels_from_trades`
    (3-tupla, no le importa el lado) ni los tests/callers existentes."""
    return [(p, n, t, m) for (_id, t, p, n, m) in _refresh_buffer(symbol, threshold_usd, lookback_hours)]


def _refresh_buffer(
    symbol: str, threshold_usd: float, lookback_hours: float,
) -> list[tuple[int, int, float, float, bool]]:
    now = time.monotonic()
    buf = _buffers.get(symbol)
    if buf and now - buf.last_refresh < CACHE_TTL_SECONDS:
        return buf.trades

    with _lock_for(symbol):
        now = time.monotonic()
        buf = _buffers.get(symbol)
        if buf and now - buf.last_refresh < CACHE_TTL_SECONDS:
            return buf.trades

        cutoff_ms = int(time.time() * 1000) - int(lookback_hours * 3600 * 1000)
        if buf is None or not buf.trades:
            raw = _paginate(symbol, start_ms=cutoff_ms)
            existing: list[tuple[int, int, float, float, bool]] = []
        else:
            raw = _paginate(symbol, from_id=buf.trades[-1][0] + 1)
            existing = buf.trades

        new_big = [
            (t["a"], t["T"], float(t["p"]), float(t["p"]) * float(t["q"]), bool(t["m"]))
            for t in raw
            if float(t["p"]) * float(t["q"]) >= threshold_usd
        ]
        merged = [t for t in existing if t[1] >= cutoff_ms] + new_big
        _buffers[symbol] = _Buffer(trades=merged, last_refresh=time.monotonic())
        return merged


def combine_trades(historical: list[tuple], live: list[tuple], start_ms: int, limit: int) -> list[tuple]:
    """Junta trades historicos (DB, `api/routers/big_trades.py`) y en vivo
    (`get_recent_big_trades_cached`/`get_recent_big_trades_with_side_cached`),
    filtra por `start_ms`, ordena por tiempo y recorta a los `limit` MAS
    RECIENTES -- mismo criterio que `/api/candles` (DESC+LIMIT+reversed)
    para no cortar por el extremo equivocado si hay mas de `limit` trades
    en la ventana pedida. Generica sobre la forma de la tupla (3 o 4
    elementos, con o sin lado) -- solo le importa `t[2]` (tiempo en ms). No
    deduplica: el router ya evita que las dos fuentes se solapen (la
    consulta a la DB se corta justo donde empieza la ventana en vivo)."""
    combined = [t for t in (historical + live) if t[2] >= start_ms]
    combined.sort(key=lambda t: t[2])
    if len(combined) > limit:
        combined = combined[-limit:]
    return combined
