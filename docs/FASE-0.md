# Fase 0 — Runbook del recorder

## Antes de arrancar

Decide **dónde** va a correr. La respuesta correcta casi siempre es un VPS de
5–10 $/mes, no el portátil: si la máquina se suspende, esas horas se pierden y
no se recuperan. Arrancarlo en Windows para probar está bien; dejarlo ahí como
sistema de grabación definitivo, no.

Requisitos: Python 3.11 o superior. Nada más — el recorder no tiene
dependencias externas a propósito.

## Arranque

```bash
git clone <repo> && cd liq-intel/recorder
cp config.example.toml config.toml
python -m liqrec --config config.toml --data-dir ./data --health-port 8787
```

Deberías ver, en menos de diez segundos:

```
recorder en marcha · 3 feeds · 6 pollers · datos en /.../data
[binance] conectado y suscrito
[bybit] suscribiendo 512 símbolos
[okx] conectado y suscrito
```

Si Bybit dice `no se pudo listar instrumentos`, el recorder sigue funcionando
con la lista de `top_symbols`, pero estarás grabando una fracción del mercado:
revisa la conectividad REST antes de darlo por bueno.

## Verificación el primer día

```bash
python -m liqrec.doctor --data-dir ./data
```

A las pocas horas debe haber registros en los tres feeds. Un feed a cero
después de una hora de mercado activo significa que algo no está suscrito, no
que no haya habido liquidaciones.

Comprueba también que el volumen tiene sentido: Bybit debería producir
**bastantes más** eventos que Binance pese a mover menos volumen. Si ves lo
contrario, el throttling de Binance no es la explicación y hay un bug.

## Rutina semanal

1. `python -m liqrec.doctor` — mira los huecos. Unos segundos por reconexión
   son normales; minutos repetidos a la misma hora apuntan a la red del VPS.
2. `curl -s localhost:8787/health | python -m json.tool` — `reconnects` que
   sube sin parar indica un problema de keepalive, no de red.
3. `du -sh data/` — dimensiona el disco. Ver abajo.
4. Copia los datos fuera de la máquina. Un recorder sin backup es un único
   punto de fallo para el activo más caro del proyecto.

## Espacio en disco

Los eventos de liquidación son pequeños y comprimen muy bien; los pollers de
open interest ocupan más de lo que parece porque devuelven el mercado entero
cada 30 segundos. Mide una semana real antes de dimensionar: la cifra depende
mucho de la volatilidad del periodo. Si aprieta, sube `oi_poll_seconds` antes
de tocar nada de las liquidaciones — el OI se puede rellenar desde los volcados
históricos, las liquidaciones no.

## Qué hacer si…

**El proceso se muere y no reinicia.** El servicio de systemd tiene
`Restart=always` y `TimeoutStopSec=45` para que le dé tiempo a cerrar y
comprimir. Si lo estás corriendo a mano, no uses `kill -9`: SIGTERM cierra
limpio, SIGKILL deja la hora en curso sin comprimir (recuperable, pero sucio).

**Hay un `.jsonl` sin comprimir de una hora pasada.** Es el rastro de un cierre
brusco. El fichero es válido; se puede comprimir a mano con `gzip`.

**Un exchange cambia el formato del mensaje.** No pasa nada inmediato: el
recorder graba lo que llega sin interpretarlo. Lo que hay que actualizar es
`normalize.py`, y se puede hacer después, sobre los datos ya grabados. Esa es
exactamente la razón de guardar el crudo.

**Quieres añadir un exchange.** Hereda de `Adapter`, implementa
`subscribe_messages`, `keepalive_message` y `handle`. Todo lo demás
—reconexión, cobertura, latido, rotación— ya está.

## Lo que este servicio NO hace

No normaliza, no calcula, no sirve nada a nadie. Solo escribe. Cualquier
funcionalidad que se le añada es una forma nueva de que se caiga, y si se cae
se pierden datos irrecuperables. Todo lo demás va en otro proceso que lee estos
ficheros.
