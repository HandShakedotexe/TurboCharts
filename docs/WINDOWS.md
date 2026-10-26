# Grabar desde un PC con Windows

Sí se puede, y para empezar hoy es mejor que cualquier alternativa: el reloj
corre desde que enciendes el grabador, y un PC encendido lo enciendes ahora.
México no está en ninguna lista de restricción de Binance, Bybit ni OKX, así
que no hay ningún impedimento de acceso.

Lo que sigue es lo que hay que ajustar para que un equipo doméstico se
comporte como un servidor, y —al final— lo que ningún ajuste puede arreglar.

## Los tres pasos

Todo lo siguiente se ejecuta desde `recorder/` (`cd recorder` primero).

```powershell
# 1. Arranque guiado: valida config, red y datos, y hace una prueba real
python -m liqrec.setup

# 2. Preparar el equipo (una sola vez, como administrador)
powershell -ExecutionPolicy Bypass -File deploy\windows\harden.ps1

# 3. Registrar el grabador para que arranque con el sistema
powershell -ExecutionPolicy Bypass -File deploy\windows\install-task.ps1

# 4. Comprobar en cualquier momento
python -m liqrec.doctor
```

### Por qué los .ps1 no llevan acentos

Windows PowerShell 5.1 lee los `.ps1` sin BOM como Windows-1252. Un guión largo
en UTF-8 se decodifica entonces como tres caracteres, y uno de ellos es la
comilla tipográfica de cierre, que PowerShell trata como delimitador de cadena.
Las comillas quedan impares y el parser falla cincuenta líneas más abajo, en un
sitio que no tiene nada que ver con la causa.

Por eso los scripts de PowerShell son ASCII puro y no hacen más que llamar a
Python, donde toda la lógica se puede probar antes de entregarla. Hay un test
(`tests/test_scripts_ascii.py`) que falla si alguien vuelve a meter un acento.

`harden.ps1` desactiva la suspensión, la hibernación, el inicio rápido, el
ahorro de energía de la tarjeta de red y la suspensión selectiva de USB, y
ajusta Windows Update para que no reinicie mientras hay sesión iniciada.

`install-task.ps1` registra una tarea programada que arranca con el sistema
aunque nadie inicie sesión, sin límite de tiempo, y que reintenta cada minuto
si el proceso muere.

## Lo que se arregla con configuración

| Riesgo | Solución |
|---|---|
| Windows suspende por inactividad | `harden.ps1` |
| Windows apaga la tarjeta de red para ahorrar | `harden.ps1` |
| El proceso muere y no vuelve | tarea programada con reintento |
| El equipo reinicia y nadie arranca el grabador | disparador «al iniciar el sistema» |
| El equipo no enciende solo tras un apagón | BIOS/UEFI → *Restore on AC Power Loss = Power On* |
| Microcortes de luz | un no-break (UPS) de 600 VA |

## Lo que NO se arregla con configuración

**Windows Update va a reiniciar la máquina.** Las horas activas topan en 18 h,
no 24, y las actualizaciones acumuladas acaban forzando el reinicio. Vas a
perder minutos u horas cada mes, y no vas a elegir cuándo.

**Tu proveedor de internet va a cortar.** Los reinicios de módem de madrugada
y las ventanas de mantenimiento son normales en cualquier conexión doméstica.

**Vas a usar el PC.** Instalar algo, reiniciar, mover el equipo, desconectar
un cable. Es tu máquina, y está bien que lo sea.

**El disco puede morir.** Con un solo grabador, eso son todos los datos.

Ninguna de estas cuatro es un problema de configuración. Son la diferencia
entre un equipo doméstico y un servidor, y no desaparecen por mucho que
prometas no apagarlo.

## Por eso: dos grabadores, no uno

La solución no es elegir entre tu PC y un VPS. Es **poner el mismo grabador en
los dos**. Graban lo mismo de forma independiente, y como cada evento trae el
timestamp del exchange, unir los dos ficheros después y quitar duplicados es
trivial.

Lo que consigues:

* Los huecos de tu PC los tapa el VPS, y al revés.
* Ninguno de los dos necesita ser perfecto, que es bueno, porque ninguno lo va
  a ser.
* Si el disco de uno muere, tienes el otro.

La cobertura combinada de dos sitios mediocres e independientes es mucho mejor
que la de uno solo por bueno que sea. Y para el único dato del proyecto que no
se puede volver a comprar, esa es la propiedad que importa.

## Y en cualquier caso: el interruptor de hombre muerto

El peor fallo de un grabador doméstico no es pararse. Es pararse **sin que te
enteres**: descubrir el jueves que llevabas sin grabar desde el lunes, cuando
ya no hay nada que hacer.

Pon una URL en `heartbeat_url` (healthchecks.io y equivalentes tienen plan
gratuito de sobra). El grabador manda una señal cada cinco minutos mientras
todos los feeds están arriba, y **deja de mandarla** en cuanto algo falla. Es
el silencio lo que dispara el aviso, así que funciona aunque se muera el
proceso, se apague la máquina o se caiga la luz — precisamente los casos en
los que un sistema de alertas normal tampoco podría avisarte.

Configura el plazo en el servicio al doble del intervalo (10 minutos) para que
una reconexión normal no te despierte de madrugada.
