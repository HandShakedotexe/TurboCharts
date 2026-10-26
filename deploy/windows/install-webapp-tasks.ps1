<#
  Registra la API y el frontend como tareas programadas de Windows, igual
  que install-task.ps1 hace para el grabador -- para que sobrevivan un
  corte de luz/reinicio sin depender de que alguien corra
  levantar-todo.bat a mano (motivo real: tres cortes de luz seguidos
  dejaron la API y el frontend caidos porque las ventanas cmd que las
  hospedaban no sobreviven un reinicio, ver CLAUDE.md/docs/HANDOFF.md).

      powershell -ExecutionPolicy Bypass -File deploy\windows\install-webapp-tasks.ps1

  Diferencia real con LiqIntelRecorder: el grabador no depende de nada
  mas y corre como SYSTEM sin sesion iniciada. La API depende de
  Postgres, que corre en Docker Desktop, que necesita una sesion de
  usuario real para su GUI/integracion WSL2 -- por eso estas dos tareas
  usan un disparador "al iniciar sesion" del usuario actual, no "al
  arrancar el sistema" como SYSTEM.

  Como cubre la espera a que Docker/Postgres esten listos: NO hay logica
  de espera propia en run-api.ps1 -- si uvicorn no puede conectar todavia
  se cae, y el reintento de la propia tarea (cada 1 minuto, hasta 999
  veces) le da tiempo a Docker Desktop de terminar de arrancar. Mismo
  patron que ya usa install-task.ps1 para el grabador.
#>
#Requires -RunAsAdministrator
param(
  [string]$ApiTaskName = "LiqIntelAPI",
  [string]$WebTaskName = "LiqIntelWeb"
)
$ErrorActionPreference = "Stop"

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$powershell = (Get-Command powershell.exe).Source
$user = "$env:USERDOMAIN\$env:USERNAME"

Write-Host "`nRepo   : $repo"
Write-Host "Usuario: $user`n"

# LogonType Interactive SIN credenciales explicitas: la tarea corre en la
# MISMA sesion de escritorio que ya abrio el usuario al iniciar sesion,
# sin pedir ni guardar contrasena -- Windows lo permite para tareas "solo
# si el usuario tiene sesion iniciada" (a diferencia de SYSTEM, que
# corre sin sesion pero tampoco puede lanzar la GUI de Docker Desktop).
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user

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

function Install-WebAppTask($name, $script, $description) {
  $action = New-ScheduledTaskAction -Execute $powershell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory $repo
  if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Write-Host "Ya existia una tarea '$name': se reemplaza." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
  }
  Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description $description | Out-Null
  Start-ScheduledTask -TaskName $name
}

Install-WebAppTask $ApiTaskName (Join-Path $PSScriptRoot "run-api.ps1") `
  "API (FastAPI) de Liquidation Intelligence -- puerto 8000"
Install-WebAppTask $WebTaskName (Join-Path $PSScriptRoot "run-web.ps1") `
  "Frontend (Vite) de Liquidation Intelligence -- puerto 5173"

Write-Host "`nTareas registradas y arrancadas." -ForegroundColor Green
Write-Host "  Esperando a que respondan (hasta 1 minuto -- Docker/Postgres pueden tardar)..."

$apiReady = $false
$webReady = $false
for ($i = 0; $i -lt 20; $i++) {
  if (-not $apiReady) {
    try {
      $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/symbols" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
      if ($r.StatusCode -eq 200) { $apiReady = $true }
    } catch {}
  }
  if (-not $webReady) {
    try {
      $r = Invoke-WebRequest -Uri "http://localhost:5173" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
      if ($r.StatusCode -eq 200) { $webReady = $true }
    } catch {}
  }
  if ($apiReady -and $webReady) { break }
  Start-Sleep -Seconds 3
}

Write-Host "  API  (8000): $(if ($apiReady) {'lista'} else {'todavia no responde -- Task Scheduler la sigue reintentando'})" -ForegroundColor $(if ($apiReady) {'Green'} else {'Yellow'})
Write-Host "  Web  (5173): $(if ($webReady) {'lista'} else {'todavia no responde -- Task Scheduler la sigue reintentando'})" -ForegroundColor $(if ($webReady) {'Green'} else {'Yellow'})

Write-Host @"

De ahora en mas, la API y el frontend arrancan solos cada vez que
$user inicia sesion en Windows -- ya no hace falta correr
levantar-todo.bat a mano (Docker Desktop si necesita que su propia
opcion "Start Docker Desktop when you log in" este activada, ver
Docker Desktop -> Settings -> General).

Ver estado / parar / arrancar / quitar:

    Get-ScheduledTask -TaskName $ApiTaskName,$WebTaskName | Select-Object TaskName,State
    Stop-ScheduledTask  -TaskName $ApiTaskName
    Start-ScheduledTask -TaskName $ApiTaskName
    powershell -File deploy\windows\uninstall-webapp-tasks.ps1
"@
