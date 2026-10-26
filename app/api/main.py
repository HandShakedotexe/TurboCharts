"""API de Liquidation Intelligence (Fase 1).

Solo lectura: sirve datos de TimescaleDB al frontend. No escribe nada -- eso
lo hacen ingest/ y db/load_historical.py.

    uvicorn api.main:app --reload --port 8000
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routers import backtest, big_trades, candles, heatmap, indicators, live, liquidations, paper, setups

app = FastAPI(title="Liquidation Intelligence API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST", "PUT", "PATCH"],
    allow_headers=["*"],
)

app.include_router(candles.router, prefix="/api")
app.include_router(heatmap.router, prefix="/api")
app.include_router(live.router, prefix="/api")
app.include_router(indicators.router, prefix="/api")
app.include_router(liquidations.router, prefix="/api")
app.include_router(big_trades.router, prefix="/api")
app.include_router(backtest.router, prefix="/api")
app.include_router(setups.router, prefix="/api")
app.include_router(paper.router, prefix="/api")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}
