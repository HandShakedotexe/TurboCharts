"""Contexto de temporalidad mayor (higher-timeframe, HTF) para estrategias
de temporalidad menor -- expone "tendencia" + Smart Money Concepts de una
temporalidad mayor como CONTEXTO de las entradas de 5m, nunca una
estrategia de 1h en si misma. Capa de consulta point-in-time sobre
`smc.py` (que solo detecta estructuras, sin opinion de para que se usan)
-- separacion analoga a `heatmap_model.compute_levels` (calcula niveles)
vs. `filter_consumed_levels` (dice si un nivel sigue vigente en un
instante dado).

Point-in-time por diseño: todo lo que expone aqui solo mira estructuras
con `index <= htf_idx` -- un caller que consulte `trend_at(ctx, htf_idx)`
nunca puede ver un evento formado con barras futuras a `htf_idx`, mismo
contrato que el resto del motor (ver docstring de `smc.py`).
"""
from __future__ import annotations

import bisect
from typing import NamedTuple, Optional

from .data import Bar
from .smc import OrderBlock, StructureEvent, order_blocks, structure_events


class HtfContext(NamedTuple):
    events: list[StructureEvent]        # ordenados por `.index` (smc.structure_events ya los arma asi)
    event_indices: list[int]            # paralelo a `events`, para bisect
    blocks: list[OrderBlock]            # ordenados por `.index` (smc.order_blocks preserva el orden de `events`)
    block_indices: list[int]            # paralelo a `blocks`, para bisect


def build_htf_context(bars: list[Bar], lookback: int = 2) -> HtfContext:
    events = structure_events(bars, lookback)
    blocks = order_blocks(bars, lookback)
    # `block_indices` usa `impulse_event_index` (cuando el BOS/ChoCH que lo
    # origina CONFIRMA la zona como Order Block), NUNCA `ob.index` (la
    # vela del bloque en si, que es ANTERIOR al impulso por definicion --
    # ver docstring de `order_blocks`). Bisectar por `ob.index` dejaria ver
    # el bloque antes de que el rompimiento que lo confirma haya ocurrido
    # -- exactamente el look-ahead que el modulo entero existe para
    # evitar. `order_blocks` itera `events` (ya ordenados por indice) y
    # agrega como mucho un bloque por evento, asi que `impulse_event_index`
    # ya sale ordenado sin necesidad de un sort explicito.
    return HtfContext(
        events=events, event_indices=[e.index for e in events],
        blocks=blocks, block_indices=[b.impulse_event_index for b in blocks],
    )


def trend_at(ctx: HtfContext, htf_idx: int) -> Optional[str]:
    """Direccion (`"up"`/`"down"`) del ULTIMO `StructureEvent` visible en
    `htf_idx` (BOS o ChoCH, no se distinguen aca -- lo unico que importa
    para "tendencia" es hacia donde rompio la ultima vez). `None` si
    todavia no hubo ningun evento en esta serie."""
    pos = bisect.bisect_right(ctx.event_indices, htf_idx) - 1
    return ctx.events[pos].direction if pos >= 0 else None


def zone_signal_at(ctx: HtfContext, htf_idx: int, price: float) -> Optional[str]:
    """`"bullish"`/`"bearish"` si `price` cae dentro de un Order Block ya
    CONFIRMADO (`impulse_event_index <= htf_idx`, ver `build_htf_context`)
    y todavia no ROTO en `htf_idx` (`broken_at_index` None o > htf_idx) --
    deliberadamente NO excluye bloques ya MITIGADOS (`mitigated_at_index`):
    el regreso del precio a mitigar la zona es, en la practica de ICT, a
    menudo el propio gatillo de entrada, no una invalidacion (ver
    docstring de `smc.py`, "Mitigation Block"). `None` si no hay ningun
    bloque activo ahi.

    Si varios bloques activos se superponen en precio, gana el
    CONFIRMADO MAS RECIENTE (`impulse_event_index` mayor) -- una zona
    vieja que nunca se rompio pero quedo "tapada" por estructura mas
    nueva es menos relevante que la ultima, mismo criterio que
    `structure_events` al descartar swings ya superados."""
    hi = bisect.bisect_right(ctx.block_indices, htf_idx)
    best: Optional[OrderBlock] = None
    for ob in ctx.blocks[:hi]:
        if ob.broken_at_index is not None and ob.broken_at_index <= htf_idx:
            continue
        if ob.bottom <= price <= ob.top:
            if best is None or ob.impulse_event_index > best.impulse_event_index:
                best = ob
    return best.direction if best is not None else None
