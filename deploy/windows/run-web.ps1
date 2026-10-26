<#
  Arranca el frontend (Vite) en primer plano -- pensado para que lo
  ejecute la tarea programada LiqIntelWeb, no para correrlo a mano.

  Bucle de reintento PROPIO, no solo el de Task Scheduler -- mismo motivo
  y misma historia que run-api.ps1 (ver ese archivo y CLAUDE.md,
  2026-09-14): un apagon real confirmo que Task Scheduler no reintenta
  esta tarea sola tras un fallo del disparador AtLogOn, asi que el script
  se reintenta a si mismo mientras siga vivo.
#>
$ErrorActionPreference = "Continue"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$webDir = Join-Path $repo "app\web"
$logFile = Join-Path $PSScriptRoot "run-web.log"

function Write-Log($msg) {
  $line = "$(Get-Date -Format o) $msg"
  Add-Content -Path $logFile -Value $line
}

Start-Sleep -Seconds 15

while ($true) {
  try {
    if (-not (Test-Path $webDir)) {
      Write-Log "ERROR: no existe $webDir -- reintentando en 30s"
      Start-Sleep -Seconds 30
      continue
    }
    Set-Location $webDir
    Write-Log "arrancando npm run dev"
    npm run dev
    Write-Log "npm run dev termino (codigo $LASTEXITCODE) -- reintentando en 5s"
  } catch {
    Write-Log "excepcion: $($_.Exception.Message) -- reintentando en 5s"
  }
  Start-Sleep -Seconds 5
}
