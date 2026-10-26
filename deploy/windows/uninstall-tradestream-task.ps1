#Requires -RunAsAdministrator
param([string]$TaskName = "LiqIntelTradeStream")
$ErrorActionPreference = "Stop"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  Write-Host "Tarea '$TaskName' eliminada." -ForegroundColor Green
} else {
  Write-Host "No existe ninguna tarea llamada '$TaskName'."
}
