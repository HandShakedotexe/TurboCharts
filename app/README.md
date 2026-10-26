# app/ — Fase 1 en adelante

Todo lo que no es el grabador (Fase 0, en `recorder/`) vive aquí: ingesta
histórica, el futuro esquema de TimescaleDB, y más adelante el backtester y
la API. A diferencia de `recorder/`, este código sí puede tener dependencias
si hacen falta — no es el proceso que no puede caerse.

## Qué hay ahora

`ingest/binance_historical.py` — descarga los volcados públicos de Binance
USD-M (velas, funding rate, open interest / long-short ratio) desde
`data.binance.vision`, con verificación SHA256 de cada zip contra su
`.CHECKSUM` y un manifest que registra cada intento (éxito, no disponible,
checksum roto). Sin esto no hace falta esperar al grabador para tener datos
2020–2026 sobre los que trabajar en las fases siguientes.

```bash
cd app
python -m ingest.binance_historical --symbol BTCUSDT --start 2020-09 --end 2026-08 --klines-interval 1h
python -m unittest discover -s tests -t .   # sin red, ~0.1 s
```

Los CSV aterrizan en `../../data/historical/binance/{klines,fundingRate,metrics}/{symbol}/`,
junto a `data/raw/` del grabador — nunca dentro del repo.

`db/schema.sql` — point-in-time store en TimescaleDB. Tres timestamps por
fila (`event_time`, `available_at`, `ingested_at`) y por qué no son lo
mismo, explicado en la cabecera del propio fichero: ningún dato se lee sin
saber qué tan disponible estaba en ese momento (evita look-ahead).

`db/load_historical.py` — parsea los CSV de `ingest/` y los carga con
`INSERT ... ON CONFLICT DO NOTHING` (idempotente: correrlo dos veces sobre
el mismo fichero no duplica nada). Los lags de publicación hacia
`available_at` están documentados uno por uno en la cabecera del fichero.

```bash
cd app
cp .env.example .env        # y pon tu propia contraseña
docker compose up -d        # levanta TimescaleDB (aplica schema.sql solo)
pip install -r requirements.txt
python -m db.load_historical --data-dir ../../data/historical
```

`ingest/binance_brackets.py` — tramos de notional y maintenance margin rate
(MMR) por símbolo, desde el endpoint público que usa la propia web de
Binance (verificado con curl plano, sin API key — ver docstring del
fichero). Sin esto el modelo de heatmap habría tenido que inventar el MMR.

`api/heatmap_model.py` — el modelo del heatmap: `P_liq = Pe·(1 − 1/L + mmr)`
repartido sobre una cesta de apalancamientos **supuesta, no medida**
(10x/15x/25x/50x/100x, pesos iguales, nombre y versión visibles en la UI).
Etiquetado siempre `MODELED · UNCALIBRATED` — no calibrar contra Bybit hasta
Fase 4. Dos detalles que costaron encontrar y que vale la pena no repetir:
el MMR se busca con el notional de **una posición típica** (el tramo más
bajo de `margin_brackets`), nunca con el agregado del nivel completo de
apalancamiento (invierte la fórmula); y los niveles por encima del
`max_leverage` real del símbolo se saltan enteros (no todos llegan a
100x — TIAUSDT topa en 75x).

`api/routers/live.py` — precio, OI y velas EN VIVO directo a Binance (sin
pasar por la DB), porque los volcados históricos solo cubren meses ya
cerrados. `/api/live-klines?start=...` pagina hasta ahora sin importar
cuántas velas haga falta — necesario en temporalidades finas (un hueco de
un mes en 5m son miles de velas, no un puñado).

`api/` — FastAPI, solo lectura, sirve `/api/candles`, `/api/heatmap`,
`/api/live-klines`, `/api/symbols` e `/api/intervals` desde TimescaleDB:

```bash
cd app
uvicorn api.main:app --port 8000   # sin --reload: se vio colgado a medio recargar en pruebas
```

`web/` — chart + heatmap en React + Vite + TypeScript, con leyenda para
activar/desactivar cada nivel de apalancamiento. Ver `web/README.md`.

## Pendiente

* Ingesta de liquidaciones desde `../data/raw/` del grabador (Fase 0) una vez
  haya suficiente histórico grabado.
* Decaimiento del heatmap (consumir niveles al cruzar el precio, retirar
  proporcionalmente cuando cae el OI) y calibración contra Bybit — Fase 4,
  necesita 3-6 meses de datos del grabador.
* Datos de 15m/1d/otras temporalidades — solo 1h y 5m están cargados.
