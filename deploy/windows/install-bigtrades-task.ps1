<#
  Registra el backfill diario de big_trades como tarea programada de
  Windows -- para que deje de ser un paso manual que alguien tiene que
  acordarse de correr (ver run-bigtrades-backfill.ps1 para el por que:
  esto ya causo un hueco real de datos en el heatmap/pools DOS VECES,
  2026-09-11 y 2026-09-17, porque nadie lo corria por varios dias
  seguidos).

      powershell -ExecutionPolicy Bypass -File deploy\windows\install-bigtrades-task.ps1

  Disparador DIARIO, no "al iniciar sesion" como LiqIntelAPI/LiqIntelWeb:
  esto es un trabajo por lotes que debe TERMINAR, no un servicio de larga
  duracion -- no hace falta que sobreviva reinicios de Task Scheduler de
  la misma forma, y correr una vez al dia a una hora fija es justo lo que
  se necesita. Igual que la API, necesita Postgres (Docker Desktop) y
  la sesion de usuario -- LogonType Interactive, no SYSTEM.

  -StartWhenAvailable hace que, si el PC estaba apagado/dormido a la hora
  programada, la tarea corra en cuanto vuelva a estar disponible -- no se
  pierde el dia solo porque no coincidio con la hora exacta.
#>
#Requires -RunAsAdministrator
param(
  [string]$TaskName = "LiqIntelBigTradesBackfill",
  [string]$Symbol = "BTCUSDT",
  [int]$Days = 3,
  [string]$At = "04:00"
)
$ErrorActionPreference = "Stop"

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$powershell = (Get-Command powershell.exe).Source
$user = "$env:USERDOMAIN\$env:USERNAME"
$script = Join-Path $PSScriptRoot "run-bigtrades-backfill.ps1"

Write-Host "`nRepo   : $repo"
Write-Host "Usuario: $user"
Write-Host "Hora   : todos los dias a las $At"
Write-Host "Simbolo: $Symbol (ventana de $Days dias por corrida)`n"

$action = New-ScheduledTaskAction -Execute $powershell `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -Symbol $Symbol -Days $Days" `
  -WorkingDirectory $repo

# LogonType Interactive SIN credenciales explicitas -- mismo motivo que
# install-webapp-tasks.ps1: corre en la sesion de escritorio ya abierta,
# sin pedir ni guardar contrasena.
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -Daily -At $At

$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -RestartInterval (New-TimeSpan -Minutes 15) `
  -RestartCount 3 `
  -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
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
  -Description "Backfill diario de big_trades (aggTrades de Binance) -- alimenta el heatmap/pools del dashboard" | Out-Null

Write-Host "Tarea registrada." -ForegroundColor Green
Write-Host @"

De ahora en mas, big_trades se actualiza solo todos los dias a las $At --
ya no hace falta acordarse de correr ingest.binance_historical +
db.load_historical a mano.

Correrla ahora mismo (no hace falta esperar a $At):

    Start-ScheduledTask -TaskName $TaskName

Ver estado / log / quitar:

    Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
    Get-Content deploy\windows\run-bigtrades-backfill.log -Tail 20
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false
"@
