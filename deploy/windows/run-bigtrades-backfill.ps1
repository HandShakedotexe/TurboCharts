<#
  Backfill diario de big_trades (aggTrades de Binance, ver
  ingest/binance_historical.py + db/load_historical.py) -- pensado para que
  lo corra la tarea programada LiqIntelBigTradesBackfill, no a mano.

  Por que hace falta esto (2026-09-17, ver CLAUDE.md): a diferencia del
  grabador (Fase 0), este backfill SIEMPRE fue un paso MANUAL -- "python -m
  ingest.binance_historical ... && python -m db.load_historical ...". Sin
  nadie corriendolo cada pocos dias, `big_trades` se queda atras en
  silencio (nada lo avisa, a diferencia del heartbeat del grabador) y el
  heatmap/pools del dashboard dejan de pintar el tramo reciente -- ya paso
  DOS VECES (2026-09-11 y de nuevo 2026-09-17, ~5 dias de hueco la segunda
  vez). Esta tarea corre TODOS LOS DIAS con una ventana corta (--days,
  parametro de abajo) para que nunca se acumule mas de un dia de atraso.

  Barato de correr seguido: `db/load_historical.py` ahora salta cualquier
  CSV que ya cargo entero sin cambios (ver LOAD_STATE_FILENAME ahi) -- una
  corrida diaria normal solo relee la ventana de superposicion (unos pocos
  dias), no el archivo completo cada vez.

  Sin bucle de reintento propio (a diferencia de run-api.ps1/run-web.ps1):
  esto NO es un servicio de larga duracion, es un trabajo por lotes que
  debe TERMINAR. Si falla, el log lo registra y Task Scheduler ya esta
  configurado (ver install-bigtrades-task.ps1) para reintentar mas tarde
  y para simplemente volver a correr manana -- un dia sin correr no pierde
  datos (a diferencia de un hueco del grabador), solo atrasa el backfill
  un dia mas.
#>
param(
  [string]$Symbol = "BTCUSDT",
  [int]$Days = 3
)
$ErrorActionPreference = "Continue"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$appDir = Join-Path $repo "app"
$envFile = Join-Path $appDir ".env"
$logFile = Join-Path $PSScriptRoot "run-bigtrades-backfill.log"

function Write-Log($msg) {
  $line = "$(Get-Date -Format o) $msg"
  Add-Content -Path $logFile -Value $line
  Write-Host $line
}

if (-not (Test-Path $envFile)) {
  Write-Log "ERROR: no existe $envFile -- abortando"
  exit 1
}
foreach ($line in Get-Content $envFile) {
  if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
    Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]
  }
}

Set-Location $appDir
Write-Log "arrancando backfill de big_trades: symbol=$Symbol days=$Days"

python -m ingest.binance_historical --symbol $Symbol --days $Days --datasets aggTrades 2>&1 |
  ForEach-Object { Write-Log "  [ingest] $_" }
if ($LASTEXITCODE -ne 0) {
  Write-Log "ingest.binance_historical termino con codigo $LASTEXITCODE -- se intenta igual la carga con lo que ya haya en disco"
}

python -m db.load_historical --datasets aggTrades 2>&1 |
  ForEach-Object { Write-Log "  [load] $_" }
$loadExit = $LASTEXITCODE

if ($loadExit -eq 0) {
  Write-Log "backfill terminado sin errores"
} else {
  Write-Log "db.load_historical termino con codigo $loadExit"
}
exit $loadExit
