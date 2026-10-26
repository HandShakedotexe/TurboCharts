"""Configuración del recorder. TOML por stdlib (tomllib, Python 3.11+)."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ExchangeConfig:
    enabled: bool = True
    ws_url: str = ""
    rest_url: str = ""
    # Símbolos para los pollers REST (OI/funding). Vacío = usar top_symbols.
    poll_symbols: list[str] = field(default_factory=list)


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    data_dir: Path = Path("./data")
    fsync_seconds: float = 5.0
    compress_on_rotate: bool = True
    heartbeat_seconds: float = 60.0
    health_file: Path = Path("./data/health.json")

    reconnect_min_seconds: float = 1.0
    reconnect_max_seconds: float = 60.0
    # Binance cierra las conexiones a las 24 h; reconectamos antes por nuestra cuenta.
    proactive_reconnect_seconds: float = 23 * 3600

    # Interruptor de hombre muerto. Vacío = sin vigilancia externa.
    heartbeat_url: str = ""
    heartbeat_seconds_interval: float = 300.0
    heartbeat_require_all_feeds: bool = True

    oi_poll_seconds: float = 30.0
    funding_poll_seconds: float = 300.0
    reference_refresh_seconds: float = 6 * 3600

    top_symbols: list[str] = field(default_factory=list)
    binance: ExchangeConfig = field(default_factory=ExchangeConfig)
    bybit: ExchangeConfig = field(default_factory=ExchangeConfig)
    okx: ExchangeConfig = field(default_factory=ExchangeConfig)

    def validate(self) -> list[str]:
        """Problemas que harían que el grabador funcione mal en silencio.

        Todos los de esta lista son errores que ya han pasado de verdad, no
        casos teóricos. El más caro: pegar la URL de aviso en `health_file`,
        que es un campo de RUTA. El grabador arranca, escribe el estado en un
        fichero con nombre absurdo, deja el heartbeat apagado, y todo parece
        normal hasta el día que se para y no avisa nadie.
        """
        problems = []

        # OJO: health_file ya es un Path, y Path("https://x") se normaliza a
        # "https:/x" (una sola barra). str() en Windows devuelve barras
        # invertidas ("https:\x") y la regex no la reconoce: as_posix() la
        # deja siempre con barra normal, en cualquier plataforma.
        if re.match(r"^https?:/", self.health_file.as_posix(), re.IGNORECASE):
            problems.append(
                f"health_file tiene una URL ({self.health_file}).\n"
                f"     health_file es la RUTA del fichero de estado, no una dirección web.\n"
                f"     La URL de aviso va en heartbeat_url, más abajo en el mismo bloque."
            )

        url = self.heartbeat_url.strip()
        if url and not url.lower().startswith(("http://", "https://")):
            problems.append(
                f"heartbeat_url no parece una URL ({url!r}). Tiene que empezar por https://"
            )

        if not str(self.data_dir).strip():
            problems.append("data_dir está vacío: no hay dónde escribir.")

        for field in ("fsync_seconds", "heartbeat_seconds", "oi_poll_seconds",
                      "funding_poll_seconds", "heartbeat_seconds_interval"):
            if getattr(self, field) <= 0:
                problems.append(f"{field} tiene que ser mayor que cero.")

        if not any(getattr(self, x).enabled for x in ("binance", "bybit", "okx")):
            problems.append("no hay ningún exchange habilitado: no se grabaría nada.")

        return problems

    @staticmethod
    def load(path: str | Path) -> "Config":
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        return Config.from_dict(raw)

    @staticmethod
    def from_dict(raw: dict) -> "Config":
        g = raw.get("recorder", {})
        cfg = Config(
            data_dir=Path(g.get("data_dir", "./data")),
            fsync_seconds=float(g.get("fsync_seconds", 5.0)),
            compress_on_rotate=bool(g.get("compress_on_rotate", True)),
            heartbeat_seconds=float(g.get("heartbeat_seconds", 60.0)),
            health_file=Path(g.get("health_file", "./data/health.json")),
            reconnect_min_seconds=float(g.get("reconnect_min_seconds", 1.0)),
            reconnect_max_seconds=float(g.get("reconnect_max_seconds", 60.0)),
            proactive_reconnect_seconds=float(g.get("proactive_reconnect_seconds", 23 * 3600)),
            heartbeat_url=str(g.get("heartbeat_url", "")),
            heartbeat_seconds_interval=float(g.get("heartbeat_seconds_interval", 300.0)),
            heartbeat_require_all_feeds=bool(g.get("heartbeat_require_all_feeds", True)),
            oi_poll_seconds=float(g.get("oi_poll_seconds", 30.0)),
            funding_poll_seconds=float(g.get("funding_poll_seconds", 300.0)),
            reference_refresh_seconds=float(g.get("reference_refresh_seconds", 6 * 3600)),
            top_symbols=list(g.get("top_symbols", [])),
        )
        for name, default_ws, default_rest in (
            ("binance", "wss://fstream.binance.com/market/stream?streams=!forceOrder@arr", "https://fapi.binance.com"),
            ("bybit", "wss://stream.bybit.com/v5/public/linear", "https://api.bybit.com"),
            ("okx", "wss://ws.okx.com:8443/ws/v5/public", "https://www.okx.com"),
        ):
            sub = raw.get(name, {})
            setattr(
                cfg,
                name,
                ExchangeConfig(
                    enabled=bool(sub.get("enabled", True)),
                    ws_url=sub.get("ws_url", default_ws),
                    rest_url=sub.get("rest_url", default_rest),
                    poll_symbols=list(sub.get("poll_symbols", [])),
                ),
            )
        return cfg
