"""Conexion a TimescaleDB para la API.

Una conexion por peticion: a esta escala (dev local, un usuario) un pool es
complejidad que todavia no hace falta. Reusa `_connect` de db/load_historical
en vez de duplicar la logica del DSN -- el mismo LIQINTEL_DSN o las mismas
POSTGRES_* deben apuntar siempre al mismo sitio para ingest, el loader y la API.
"""
from __future__ import annotations

from contextlib import contextmanager

from db.load_historical import _connect


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()
