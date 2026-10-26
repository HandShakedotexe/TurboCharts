# TurboCharts

Plataforma de análisis de mercado de derivados cripto: grabador de
liquidaciones 24/7, ingesta histórica, chart con heatmap de liquidez,
indicadores de derivados y herramientas de dibujo.

## Qué incluye

**Grabador (`recorder/`)** — servicio autónomo que captura en tiempo real los
eventos de liquidación de futuros perpetuos (Binance, Bybit, OKX), algo que no
tiene histórico gratuito en ningún lado: solo existe el stream en vivo. Guarda
el mensaje crudo tal cual llega, sin dependencias externas (Python puro),
pensado para correr indefinidamente sin caerse.

**Ingesta histórica y API (`app/`)** — descarga de los volcados públicos de
Binance (velas, funding rate, open interest) desde 2020, cargados en
TimescaleDB con un esquema point-in-time (distingue cuándo ocurrió un dato de
cuándo estuvo disponible, para evitar look-ahead). API de solo lectura en
FastAPI que sirve candles, heatmap y datos en vivo.

**Chart (`app/web/`)** — interfaz en React + Vite + TypeScript:

* **Pools / heatmap de liquidez** — cuadrícula de precio × tiempo que modela
  dónde se concentran posibles liquidaciones según una cesta de apalancamientos
  (10x–100x), con perfil de volumen fijo (FRVP) y de sesión.
* **Indicadores** — paneles de Open Interest, CVD (delta de volumen acumulado)
  y Funding Rate, recortados para no tapar el precio.
* **Herramientas de dibujo** — líneas horizontales/verticales/diagonales,
  rectángulos, con grosor/color configurables, deshacer/rehacer y persistencia
  por símbolo.
* **Big Trades** — burbujas de operaciones grandes sobre el precio, coloreadas
  por lado (compra/venta), con umbral y ventana configurables.
* Marcadores de liquidaciones reales, panel lateral de mercado y color de
  velas personalizable.

## Arranque rápido

```bash
cd recorder
cp config.example.toml config.toml
python -m liqrec --config config.toml --health-port 8787   # Python 3.11+, sin dependencias
```

```bash
cd app
cp .env.example .env
docker compose up -d
pip install -r requirements.txt
python -m db.load_historical --data-dir ../../data/historical
uvicorn api.main:app --port 8000
```

```bash
cd app/web
npm install
npm run dev
```

Detalles de cada pieza en `recorder/README.md`, `app/README.md`,
`app/web/README.md`, el runbook en `docs/FASE-0.md` y, para grabar desde un PC
doméstico, `docs/WINDOWS.md`.

## Estructura

```
recorder/          Servicio de grabación. Solo stdlib, sin dependencias.
  liqrec/          Adapters por exchange, escritor, cobertura, normalización.
  tests/           Tests sin red, contra exchanges simulados.
app/
  ingest/          Descarga de volcados históricos de Binance.
  db/              Esquema TimescaleDB y carga de históricos.
  api/             FastAPI de solo lectura.
  web/             Chart, heatmap, indicadores y herramientas (React).
docs/              Runbooks de despliegue.
```
