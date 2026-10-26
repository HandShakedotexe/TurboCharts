<#
  Registra el grabador como tarea programada de Windows.

      powershell -ExecutionPolicy Bypass -File deploy\windows\install-task.ps1

  Por que una tarea programada y no un servicio: un servicio de Windows de
  verdad exige un envoltorio externo (NSSM o similar). La tarea programada
  hace lo mismo para este caso, viene incluida en el sistema, y sobrevive a
  reinicios y a cierres de sesion sin depender de nada que haya que descargar.

  La tarea queda configurada para:
    - arrancar con el sistema, sin que nadie inicie sesion
    - reintentar cada minuto si el proceso muere, indefinidamente
    - no tener limite de tiempo de ejecucion
    - no detenerse por pasar a bateria ni por inactividad

  DataDir NO tiene un valor por defecto fijo a proposito: si lo tuviera y
  alguien lo dejara sin tocar, el grabador arrancaria escribiendo en un
  directorio nuevo y vacio en vez del que ya tiene el historial, y nadie se
  enteraria hasta buscar datos que no estan. Sin -DataDir, se lee data_dir
  del propio config.toml, que es la fuente de verdad.
#>
#Requires -RunAsAdministrator
param(
  [string]$TaskName = "LiqIntelRecorder",
  [string]$DataDir  = "",
  [string]$Config   = "",
  [int]   $HealthPort = 8787
)
$ErrorActionPreference = "Stop"

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not $Config) { $Config = Join-Path $repo "config.toml" }

# --- comprobaciones previas ----------------------------------------------
$python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "No se encontro python.exe en el PATH. Instala Python 3.11 o superior." }

$ver = & $python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$ver -lt [version]"3.11") { throw "Se necesita Python 3.11 o superior (encontrado $ver)." }

if (-not (Test-Path $Config)) {
  $example = Join-Path $repo "config.example.toml"
  if (-not (Test-Path $example)) { throw "No existe $Config ni config.example.toml." }
  Copy-Item $example $Config
  Write-Host "Creado $Config a partir del ejemplo." -ForegroundColor Yellow
}

if (-not $DataDir) {
  $DataDir = & $python -c "import tomllib; print(tomllib.load(open(r'$Config','rb')).get('recorder', {}).get('data_dir', './data'))"
  if (-not $DataDir) { throw "No se pudo leer data_dir de $Config." }
}
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

Write-Host "`nPython : $python ($ver)"
Write-Host "Repo   : $repo"
Write-Host "Config : $Config"
Write-Host "Datos  : $DataDir`n"

# --- la tarea -------------------------------------------------------------
$args = "-m liqrec --config `"$Config`" --data-dir `"$DataDir`" --health-port $HealthPort"
$action = New-ScheduledTaskAction -Execute $python -Argument $args -WorkingDirectory $repo

$trigger = New-ScheduledTaskTrigger -AtStartup

# SYSTEM: arranca sin sesion iniciada y sin guardar contrasena de nadie.
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -RestartCount 999 `
  -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
  -MultipleInstances IgnoreNew
$settings.DisallowStartIfOnBatteries = $false
$settings.StopIfGoingOnBatteries     = $false
$settings.IdleSettings.StopOnIdleEnd = $false

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
  Write-Host "Ya existia una tarea '$TaskName': se reemplaza." -ForegroundColor Yellow
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Principal $principal -Settings $settings `
  -Description "Grabador de liquidaciones (Fase 0, Liquidation Intelligence)" | Out-Null

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 6

$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "Tarea registrada y arrancada." -ForegroundColor Green
Write-Host "  Ultimo resultado: $($info.LastTaskResult)  -  Estado: $((Get-ScheduledTask -TaskName $TaskName).State)"

Write-Host @"

Comprobar que esta grabando:

    curl http://localhost:$HealthPort/health
    python -m liqrec.doctor --data-dir "$DataDir"

Parar / arrancar / quitar:

    Stop-ScheduledTask  -TaskName $TaskName
    Start-ScheduledTask -TaskName $TaskName
    powershell -File deploy\windows\uninstall-task.ps1

AVISO sobre el apagado: Windows no manda SIGTERM como lo hace Linux, asi que
al detener la tarea el proceso puede morir sin comprimir la hora en curso. El
fichero .jsonl que quede suelto es valido y se comprime a mano; no se pierde
nada. Si quieres un apagado limpio, para el proceso con Ctrl+C en una consola
en lugar de con Stop-ScheduledTask.
"@
