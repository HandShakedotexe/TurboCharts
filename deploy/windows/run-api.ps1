<#
  Arranca la API (FastAPI/uvicorn) en primer plano -- pensado para que lo
  ejecute la tarea programada LiqIntelAPI, no para correrlo a mano (para
  eso ya esta levantar-todo.bat, que abre una ventana visible con logs).

  Bucle de reintento PROPIO, no solo el de Task Scheduler (2026-09-14,
  ver CLAUDE.md): un apagon real confirmo que el RestartInterval/
  RestartCount de la tarea programada (configurados y visibles en
  Get-ScheduledTask, no es un error de config) simplemente NO se disparo
  tras el primer fallo -- LastRunTime se quedo pegado en el primer
  intento minutos despues, sin ningun reintento, con
  LastTaskResult=3221225786 (STATUS_CONTROL_C_EXIT). La causa exacta no
  se pudo aislar sin forzar otro reinicio real (un script identico
  disparado a mano con Start-ScheduledTask SI funciono, asi que no es la
  configuracion de la tarea en si -- es algo especifico del disparador
  AtLogOn durante un arranque todavia caotico tras un corte de luz, con
  Hyper-V/Credential Guard/el propio Explorer inicializandose al mismo
  tiempo). En vez de seguir apostando a que Task Scheduler reintente,
  este script se reintenta A SI MISMO mientras siga vivo -- cubre el
  caso comun (Postgres/Docker todavia no listos, uvicorn se cae al
  conectar) sin depender de que el sistema operativo vuelva a invocar la
  tarea. El RestartInterval/RestartCount de la tarea se deja como
  respaldo en una segunda capa, no se quita.
#>
$ErrorActionPreference = "Continue"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$appDir = Join-Path $repo "app"
$envFile = Join-Path $appDir ".env"
$logFile = Join-Path $PSScriptRoot "run-api.log"

function Write-Log($msg) {
  $line = "$(Get-Date -Format o) $msg"
  Add-Content -Path $logFile -Value $line
}

# Espera corta antes del primer intento -- da tiempo a que el perfil de
# usuario/PATH/servicios terminen de cargar si el disparador AtLogOn
# arranco este script muy temprano en un arranque todavia caotico (ver
# comentario de arriba). No hace falta en un arranque normal, pero no
# cuesta nada tenerla.
Start-Sleep -Seconds 15

while ($true) {
  try {
    if (-not (Test-Path $envFile)) {
      Write-Log "ERROR: no existe $envFile -- reintentando en 30s"
      Start-Sleep -Seconds 30
      continue
    }
    foreach ($line in Get-Content $envFile) {
      if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]
      }
    }

    # Intento best-effort de arrancar Docker Desktop si no esta corriendo
    # -- igual que levantar-todo.bat. No bloquea ni espera: si Postgres
    # no esta listo todavia, uvicorn se cae al conectar y el bucle de
    # abajo reintenta solo, dandole tiempo a Docker de arrancar.
    $dockerRunning = Get-Process "Docker Desktop" -ErrorAction SilentlyContinue
    if (-not $dockerRunning) {
      try { Start-Process "shell:AppsFolder\Docker.DockerForWindows.Settings" -ErrorAction SilentlyContinue } catch {}
    }

    Set-Location $appDir
    Write-Log "arrancando uvicorn"
    python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
    Write-Log "uvicorn termino (codigo $LASTEXITCODE) -- reintentando en 5s"
  } catch {
    Write-Log "excepcion: $($_.Exception.Message) -- reintentando en 5s"
  }
  Start-Sleep -Seconds 5
}
