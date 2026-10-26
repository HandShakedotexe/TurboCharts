#Requires -RunAsAdministrator
<#
  Actualiza LiqIntelAPI/LiqIntelWeb (ya instaladas con
  install-webapp-tasks.ps1) para que apunten a los lanzadores
  desacoplados (run-api-launcher.ps1/run-web-launcher.ps1) en vez de a
  run-api.ps1/run-web.ps1 directo -- ver el comentario de cabecera de
  run-api-launcher.ps1 y CLAUDE.md (2026-09-14) para el porque.

  Solo cambia la Action de cada tarea; no toca Trigger/Principal/Settings
  (ya estan bien, confirmado con una tarea de diagnostico identica).
#>
$ErrorActionPreference = "Stop"
$scriptDir = $PSScriptRoot

$apiAction = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptDir\run-api-launcher.ps1`""
Set-ScheduledTask -TaskName "LiqIntelAPI" -Action $apiAction
Write-Host "LiqIntelAPI actualizada -> run-api-launcher.ps1" -ForegroundColor Green

$webAction = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptDir\run-web-launcher.ps1`""
Set-ScheduledTask -TaskName "LiqIntelWeb" -Action $webAction
Write-Host "LiqIntelWeb actualizada -> run-web-launcher.ps1" -ForegroundColor Green

Write-Host ""
Write-Host "Verificacion:" -ForegroundColor Cyan
Get-ScheduledTask -TaskName "LiqIntelAPI", "LiqIntelWeb" | ForEach-Object {
  $a = $_.Actions[0]
  Write-Host "  $($_.TaskName): $($a.Execute) $($a.Arguments)"
}
