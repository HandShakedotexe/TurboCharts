<#
  Lanzador minimo -- esto, no run-web.ps1 directo, es lo que registra la
  tarea programada LiqIntelWeb. Mismo motivo que run-api-launcher.ps1 (ver
  ese archivo y CLAUDE.md, 2026-09-14): desacopla el proceso que Task
  Scheduler vigila directamente (este, desechable, termina en
  milisegundos) del que hace el trabajo real (run-web.ps1, lanzado
  DESACOPLADO y con su propio bucle de reintento).
#>
$ErrorActionPreference = "Continue"
$target = Join-Path $PSScriptRoot "run-web.ps1"
Start-Process -FilePath "powershell.exe" `
  -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", "`"$target`"" `
  -WindowStyle Hidden
