"""GET /api/setups -- ver ../setups.py para la logica y el "que es esto,
que no es"."""
from __future__ import annotations

from fastapi import APIRouter, Query

from .. import setups as setups_mod

router = APIRouter()


@router.get("/setups")
def get_setups(
    symbol: str = Query(...),
    min_liquidation_usd: float = Query(setups_mod.DEFAULT_MIN_LIQUIDATION_USD, ge=0),
    min_pool_usd: float = Query(setups_mod.DEFAULT_MIN_POOL_USD, ge=0),
    min_correlation: int = Query(setups_mod.DEFAULT_MIN_CORRELATION, ge=1),
) -> dict:
    return setups_mod.find_setups(
        symbol,
        min_liquidation_usd=min_liquidation_usd,
        min_pool_usd=min_pool_usd,
        min_correlation=min_correlation,
    )
