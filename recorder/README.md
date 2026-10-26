# Recorder — Fase 0

Graba lo que no se puede recuperar después: **eventos de liquidación**.

Las velas, el funding y el open interest a 5 minutos existen en los volcados
públicos de Binance desde 2020 y se pueden descargar cuando quieras. Los
eventos de liquidación de perpetuos USD-M **no tienen histórico gratuito**:
solo el stream en vivo. Cada hora sin grabar es una hora que nunca vas a poder
backtestear.

Por eso este servicio es lo primero que se pone en marcha y lo último que se
toca.

## Qué graba

| Fuente | Stream | Cadencia | Nota |
|---|---|---|---|
| Binance USD-M | `!forceOrder@arr` | tiempo real | **1 orden por símbolo por segundo**: es una muestra, no el total |
| Bybit v5 linear | `allLiquidation.{symbol}` | 500 ms | todas las liquidaciones — la única fuente de verdad completa |
| OKX v5 | `liquidation-orders` (SWAP) | tiempo real | máx. 1 update por contrato por segundo |
| Binance | `premiumIndex` (bulk) | 5 min | mark price, index, funding de todo el mercado |
| Binance | `openInterest` | 30 s | por símbolo — Binance no tiene endpoint masivo |
| Bybit | `tickers` (bulk) | 30 s | OI + funding + mark de todo el mercado en una llamada |
| OKX | `open-interest`, `mark-price` (bulk) | 30 s / 5 min | |
| los tres | especificaciones de contrato | 6 h | el `ctVal` de OKX cambia y tampoco se recupera |

## Instalación

Sin dependencias. Solo Python 3.11 o superior.

```bash
cd recorder
cp config.example.toml config.toml
python -m liqrec --config config.toml --health-port 8787
```

En Docker:

```bash
cp config.example.toml config.toml
docker compose -f deploy/docker-compose.yml up -d --build
```

En un VPS con systemd: copia `deploy/liq-recorder.service` a
`/etc/systemd/system/`, ajusta las rutas y `systemctl enable --now liq-recorder`.

## Antes de arrancar: preflight

```bash
python -m liqrec.preflight --data-dir ./data
```

Comprueba, desde la máquina donde va a correr, lo único que no se puede
comprobar desde ningún otro sitio: si los exchanges responden **desde esa
conexión**, si llegan mensajes de verdad por WebSocket (abrir el socket no
demuestra nada) y si hay disco para el primer año. Usa el mismo cliente
WebSocket que el grabador, así que si el preflight pasa, el grabador conecta.

Un `HTTP 451` significa que esa IP está geobloqueada y hay que grabar desde
otro sitio.

## Comprobar que está grabando

```bash
python -m liqrec.doctor --data-dir ./data
curl -s localhost:8787/health | python -m json.tool
```

`doctor` cuenta registros por feed y día, y lista los tramos sin cobertura.
`/health` devuelve 503 si algún feed está caído, para que un monitor externo
te avise sin montar nada más.

## Cómo quedan los datos

```
data/
  raw/{exchange}/{stream}/{YYYY-MM-DD}/{HH}.jsonl.gz
  coverage/{exchange}/{stream}/{YYYY-MM-DD}.jsonl
  health.json
```

Cada línea de `raw` es el mensaje **tal cual llegó**, con el instante en que lo
recibimos:

```json
{"rx":1756123456789,"ex":"bybit","st":"liquidation","raw":{...}}
```

No se normaliza al grabar a propósito. La normalización se puede rehacer sobre
el fichero crudo el día que descubramos que nos equivocamos; el mensaje perdido
no vuelve.

`coverage` contesta la pregunta que se hace seis meses después: *¿este tramo
vacío es mercado tranquilo o estábamos desconectados?* Registra conexiones,
desconexiones con su motivo, y un latido por minuto con el número de mensajes.
Cero mensajes con el socket arriba es un dato; la ausencia de registro no.

## Que no se caiga en silencio

El peor fallo no es pararse: es pararse sin que te enteres. Pon una URL de
ping en `heartbeat_url` (healthchecks.io, Better Stack, Cronitor — todos con
plan gratuito suficiente). El grabador la llama cada cinco minutos **mientras
todos los feeds están arriba**, y deja de llamarla en cuanto algo falla.

La alarma es el silencio, no un mensaje. Por eso funciona también cuando se
muere el proceso, se apaga la máquina o se va la luz, que es justo cuando un
sistema de alertas convencional tampoco podría avisarte.

## Tres decisiones que parecen detalles y no lo son

**El lado de la liquidación.** Binance y Bybit usan la palabra **opuesta** para
el mismo suceso. En Binance una orden `SELL` cierra un largo; en Bybit `Buy`
significa que se liquidó un largo. Mapearlos igual invierte el heatmap entero y
ningún test de integración lo detecta, porque los datos siguen pareciendo
razonables. `liqrec/normalize.py` lo resuelve en un solo sitio, con la cita de
cada documentación, y `tests/test_normalize.py` lo fija.

**La hora en curso va sin comprimir.** Un `.gz` cortado por un corte de luz es
basura; un JSONL cortado pierde como mucho la última línea. Se comprime al
rotar.

**`sz` de OKX viene en contratos.** Sin el `ctVal` del instrumento no hay
notional. Por eso se graba un snapshot de las especificaciones de contrato cada
6 horas: también cambian, y tampoco son recuperables hacia atrás.

## Tests

```bash
python -m unittest discover -s tests -t .
```

44 tests, sin red: los adapters se ejercitan contra un servidor WebSocket
simulado que implementa el framing por su cuenta, incluyendo caídas abruptas a
mitad de stream para comprobar la reconexión y el registro del hueco.
