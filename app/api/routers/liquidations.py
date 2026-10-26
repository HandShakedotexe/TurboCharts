"""GET /api/real-liquidations -- alertas de liquidacion REALES (no
modeladas), leidas del grabador de Fase 0. Ver ../real_liquidations.py.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..real_liquidations import get_liquidations_since_cached, get_recent_liquidations_cached

router = APIRouter()


@router.get("/real-liquidations")
def get_real_liquidations(
    symbol: str = Query(...),
    # le=10000: LiquidationVolumePane.tsx pide hasta FETCH_LIMIT=8000 (ver
    # ese archivo, 2026-09-25) para su ventana de 3 dias -- el tope viejo
    # de 5000 rechazaba esa request entera con 422 en vez de truncar.
    limit: int = Query(50, le=10000),
    start: Optional[str] = Query(
        None,
        description=(
            "ISO 8601. Si se da, devuelve TODOS los eventos desde esta fecha "
            "(hasta `limit`), recorriendo cada hora archivada en disco -- no "
            "solo la cola en vivo de la hora actual/anterior que se usa sin "
            "este parametro. Ver LiquidationMarkers.tsx."
        ),
    ),
) -> dict:
    if start is not None:
        try:
            since = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(400, f"start invalido: {start}") from exc
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        events = get_liquidations_since_cached(symbol, since, limit=limit)
    else:
        events = get_recent_liquidations_cached(symbol, limit=limit)
    return {
        # Etiqueta explicita, a proposito distinta del "MODELED · UNCALIBRATED"
        # del heatmap -- "el heatmap es un modelo, no un dato". Esto SI es
        # un dato observado.
        "source": "binance_liquidation_stream",
        "label": "REAL",
        "symbol": symbol.upper(),
        "events": [
            {
                "event_time_ms": e.event_time,
                "side": e.side,
                "price": e.price,
                "price_kind": e.price_kind,
                "qty_base": e.qty_base,
                "quote_qty": e.quote_qty,
                "exchange": e.exchange,
            }
            for e in events
        ],
    }
