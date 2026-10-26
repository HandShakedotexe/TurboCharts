#Requires -RunAsAdministrator
param([string]$ApiTaskName = "LiqIntelAPI", [string]$WebTaskName = "LiqIntelWeb")
$ErrorActionPreference = "Stop"
foreach ($name in @($ApiTaskName, $WebTaskName)) {
  if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "Tarea '$name' eliminada." -ForegroundColor Green
  } else {
    Write-Host "No existe ninguna tarea llamada '$name'."
  }
}
