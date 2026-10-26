# Arranque en Windows para probar antes de mover a un VPS.
#   powershell -ExecutionPolicy Bypass -File deploy\run.ps1
#
# Para dejarlo corriendo de verdad hace falta que la maquina este siempre
# encendida y con red. Si el portatil se suspende, se pierden esas horas y no
# se recuperan: por eso la recomendacion es un VPS.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not (Test-Path "config.toml")) { Copy-Item "config.example.toml" "config.toml" }
python -m liqrec --config config.toml --data-dir .\data --health-port 8787
