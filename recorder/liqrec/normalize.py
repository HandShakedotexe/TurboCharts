"""Normalización de eventos de liquidación entre exchanges.

ESTE FICHERO ES EL MÁS FÁCIL DE EQUIVOCAR DE TODO EL PROYECTO.

Los tres exchanges publican el "lado" de una liquidación con convenciones
distintas, y dos de ellos usan literalmente la palabra opuesta para el mismo
evento. Si esto se mapea mal, el heatmap sale invertido y no hay ningún test de
integración que lo detecte, porque los datos siguen pareciendo razonables.

    Binance  o.S = "SELL"  -> la orden que cierra la posición es una venta
                              -> se liquidó un LONG
             o.S = "BUY"   -> se liquidó un SHORT
             (docs: "Liquidation Order Streams", campo S = Side de la orden)

    Bybit    S   = "Buy"   -> se liquidó un LONG
             S   = "Sell"  -> se liquidó un SHORT
             (docs, textual: "Position side. Buy,Sell. When you receive a Buy
              update, this means that a long position has been liquidated")

    OKX      posSide = "long" | "short" -> directo, es el lado de la posición
             posSide = "net"            -> hay que derivarlo de side:
                                           side="sell" -> LONG liquidado

Es decir: Binance dice SELL y Bybit dice Buy PARA EL MISMO EVENTO.

Convención interna del proyecto: `side` es SIEMPRE el lado de la posición
liquidada, "long" o "short". Nunca el lado de la orden.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Iterable, Optional

LONG, SHORT = "long", "short"


class NormalizationError(ValueError):
    pass


@dataclass(frozen=True)
class Liquidation:
    """Un evento de liquidación en el esquema común del proyecto."""

    event_time: int          # ms UTC, tiempo del exchange
    ingested_at: int         # ms UTC, cuándo lo recibimos nosotros
    exchange: str
    symbol: str              # símbolo normalizado, p.ej. BTCUSDT
    side: str                # "long" | "short" — LADO DE LA POSICIÓN LIQUIDADA
    price: float             # ver price_kind
    price_kind: str          # "avg_fill" | "bankruptcy" — NO son lo mismo
    qty_base: float          # tamaño en moneda base
    quote_qty: float         # notional aproximado en quote
    is_throttled: bool       # True si la fuente limita mensajes (Binance, OKX)
    raw_side: str            # lo que dijo el exchange, para poder auditar

    def as_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------- Binance
def from_binance(msg: dict, ingested_at: int) -> Liquidation:
    """`!forceOrder@arr` de USD-M futures.

    El payload puede venir envuelto por el endpoint combinado
    ({"stream": ..., "data": {...}}) o directo.
    """
    if "data" in msg and "stream" in msg:
        msg = msg["data"]
    o = msg.get("o")
    if not o:
        raise NormalizationError("forceOrder sin objeto 'o'")

    order_side = str(o["S"]).upper()
    if order_side == "SELL":
        side = LONG      # una venta forzada cierra un largo
    elif order_side == "BUY":
        side = SHORT
    else:
        raise NormalizationError(f"lado de orden desconocido en Binance: {order_side!r}")

    # 'ap' es el precio medio de ejecución; 'z' la cantidad acumulada ejecutada.
    # Si 'z' viene a cero (orden aún no llenada) caemos a 'q'.
    price = float(o.get("ap") or o["p"])
    qty = float(o.get("z") or 0.0) or float(o["q"])
    return Liquidation(
        event_time=int(o.get("T") or msg["E"]),
        ingested_at=ingested_at,
        exchange="binance",
        symbol=str(o["s"]).upper(),
        side=side,
        price=price,
        price_kind="avg_fill",
        qty_base=qty,
        quote_qty=price * qty,
        is_throttled=True,   # 1 orden por símbolo por segundo, siempre
        raw_side=order_side,
    )


# ----------------------------------------------------------------------- Bybit
def from_bybit(entry: dict, ingested_at: int) -> Liquidation:
    """Una entrada del array `data` del topic `allLiquidation.{symbol}`."""
    pos_side = str(entry["S"]).lower()
    if pos_side == "buy":
        side = LONG      # documentado explícitamente por Bybit
    elif pos_side == "sell":
        side = SHORT
    else:
        raise NormalizationError(f"lado de posición desconocido en Bybit: {pos_side!r}")

    # OJO: 'p' es el precio de bancarrota, NO el precio de ejecución. El notional
    # que sale de aquí es una aproximación y así se etiqueta.
    price = float(entry["p"])
    qty = float(entry["v"])
    return Liquidation(
        event_time=int(entry["T"]),
        ingested_at=ingested_at,
        exchange="bybit",
        symbol=str(entry["s"]).upper(),
        side=side,
        price=price,
        price_kind="bankruptcy",
        qty_base=qty,
        quote_qty=price * qty,
        is_throttled=False,  # allLiquidation publica todos los eventos
        raw_side=entry["S"],
    )


# ------------------------------------------------------------------------- OKX
def from_okx(inst_id: str, detail: dict, ingested_at: int, ct_val: Optional[float] = None) -> Liquidation:
    """Una entrada de `details` del canal `liquidation-orders`.

    `sz` viene en CONTRATOS, no en moneda base. Para convertir hace falta el
    `ctVal` del instrumento (BTC-USDT-SWAP = 0.01 BTC por contrato). Si no se
    pasa ct_val no inventamos un factor: dejamos qty_base a 0 y quote_qty a 0,
    y el consumidor sabe que le falta la referencia.
    """
    pos_side = str(detail.get("posSide", "")).lower()
    order_side = str(detail.get("side", "")).lower()
    if pos_side in (LONG, SHORT):
        side = pos_side
    elif pos_side == "net" or not pos_side:
        if order_side == "sell":
            side = LONG
        elif order_side == "buy":
            side = SHORT
        else:
            raise NormalizationError(f"OKX sin posSide utilizable y side={order_side!r}")
    else:
        raise NormalizationError(f"posSide desconocido en OKX: {pos_side!r}")

    price = float(detail["bkPx"])
    contracts = float(detail["sz"])
    qty = contracts * ct_val if ct_val else 0.0
    return Liquidation(
        event_time=int(detail["ts"]),
        ingested_at=ingested_at,
        exchange="okx",
        symbol=inst_id.upper(),
        side=side,
        price=price,
        price_kind="bankruptcy",
        qty_base=qty,
        quote_qty=price * qty,
        is_throttled=True,   # máx. 1 update por contrato por segundo
        raw_side=f"{order_side}/{pos_side}",
    )


def okx_ct_vals(instruments: Iterable[dict]) -> dict:
    """Extrae {instId: ctVal} de la respuesta de /api/v5/public/instruments."""
    out = {}
    for inst in instruments:
        try:
            out[inst["instId"].upper()] = float(inst["ctVal"])
        except (KeyError, TypeError, ValueError):
            continue
    return out
