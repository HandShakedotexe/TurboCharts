<#
  Arranque guiado en Windows. Ejecuta los pasos en orden y se para en el
  primero que falle, explicando que paso.

      powershell -ExecutionPolicy Bypass -File deploy\windows\start-here.ps1

  No cambia nada del sistema por su cuenta: instalar la tarea programada te lo
  pregunta antes, y para eso hara falta una consola de administrador.
#>
param([switch]$SkipTests)
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $repo

function Section($n, $t) { Write-Host "`n=== $n. $t ===" -ForegroundColor Cyan }

Write-Host "`nGRABADOR DE LIQUIDACIONES - arranque guiado" -ForegroundColor White
Write-Host "Carpeta: $repo"

# --- 0. Hay uno corriendo ya -------------------------------------------
Section 0 "Instancias en marcha"
$running = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -match 'liqrec' })
if ($running.Count -gt 0) {
  Write-Host "  Ya hay $($running.Count) grabador(es) corriendo:" -ForegroundColor Yellow
  foreach ($r in $running) { Write-Host "    PID $($r.ProcessId)  -  $($r.CommandLine)" }
  Write-Host ""
  Write-Host "  Dos grabadores sobre el mismo directorio corromperian los ficheros," -ForegroundColor Yellow
  Write-Host "  asi que el nuevo se negaria a arrancar. Para el viejo con:"
  foreach ($r in $running) { Write-Host "      Stop-Process -Id $($r.ProcessId)" }
  Write-Host ""
  Write-Host "  Vuelve a lanzar este script cuando lo hayas parado." -ForegroundColor Yellow
  exit 1
}
Write-Host "  Ninguna. Via libre."

# --- 1. Python ------------------------------------------------------------
Section 1 "Python"
$python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $python) {
  Write-Host "No se encontro python.exe en el PATH." -ForegroundColor Red
  Write-Host "Instalalo desde https://www.python.org/downloads/ y MARCA la casilla"
  Write-Host "'Add python.exe to PATH' durante la instalacion. Luego vuelve a abrir"
  Write-Host "esta ventana y ejecuta el script otra vez."
  exit 2
}
$ver = & $python -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
Write-Host "  $python  ($ver)"
if ([version]($ver -replace '^(\d+\.\d+).*','$1') -lt [version]"3.11") {
  Write-Host "Se necesita Python 3.11 o superior." -ForegroundColor Red
  exit 2
}

# --- 2. Tests -------------------------------------------------------------
if (-not $SkipTests) {
  Section 2 "Tests (sin red, ~20 s)"
  & $python -m unittest discover -s tests -t . 2>&1 | Select-Object -Last 4
  if ($LASTEXITCODE -ne 0) {
    Write-Host "Los tests fallaron. Algo se corrompio al descomprimir." -ForegroundColor Red
    exit 2
  }
} else {
  Section 2 "Tests (omitidos)"
}

# --- 3. Preflight ---------------------------------------------------------
Section 3 "Responden los exchanges desde esta conexion?"
& $python -m liqrec.preflight --data-dir "C:/Users/Bass Flavor/Documents/Bitcoin/prod/data" --seconds 15
$pf = $LASTEXITCODE
if ($pf -eq 2) {
  Write-Host "`nNo se puede arrancar todavia: mira los fallos de arriba." -ForegroundColor Red
  exit 2
}

# --- 4. Prueba en primer plano -------------------------------------------
Section 4 "Prueba de 60 segundos"
Write-Host "  Arrancando el grabador en primer plano. Se detiene solo."
$job = Start-Process -FilePath $python `
  -ArgumentList "-m","liqrec","--config","config.toml","--health-port","8787" `
  -WorkingDirectory $repo -PassThru -NoNewWindow
Start-Sleep -Seconds 60
try { $job.CloseMainWindow() | Out-Null } catch {}
Start-Sleep -Seconds 3
if (-not $job.HasExited) { Stop-Process -Id $job.Id -Force }

Write-Host "`n  Que se grabo:"
& $python -m liqrec.doctor --data-dir "C:/Users/Bass Flavor/Documents/Bitcoin/prod/data"

# --- 5. Dejarlo permanente ------------------------------------------------
Section 5 "Dejarlo corriendo siempre"
Write-Host @"
  Si lo de arriba tiene buena pinta, quedan dos pasos, ambos como
  ADMINISTRADOR (clic derecho en PowerShell -> Ejecutar como administrador):

      powershell -ExecutionPolicy Bypass -File deploy\windows\harden.ps1
      powershell -ExecutionPolicy Bypass -File deploy\windows\install-task.ps1 -DataDir "C:\Users\Bass Flavor\Documents\Bitcoin\prod\data"

  Y antes de eso, pega tu URL de healthchecks.io en config.toml
  (heartbeat_url). Sin eso, si el grabador se para no te enteras.
"@
