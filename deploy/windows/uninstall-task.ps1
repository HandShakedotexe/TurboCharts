#Requires -RunAsAdministrator
param([string]$TaskName = "LiqIntelRecorder")
$ErrorActionPreference = "Stop"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  Write-Host "Tarea '$TaskName' eliminada. Los datos en disco NO se han tocado." -ForegroundColor Green
} else {
  Write-Host "No existe ninguna tarea llamada '$TaskName'."
}
