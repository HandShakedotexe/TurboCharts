<#
  Lanzador minimo -- esto, no run-api.ps1 directo, es lo que registra la
  tarea programada LiqIntelAPI (2026-09-14, ver CLAUDE.md).

  Confirmado dos veces (un apagon real y un reinicio NORMAL, sin apagon de
  por medio) que el proceso de PowerShell que Task Scheduler lanza para un
  disparador AtLogOn con LogonType Interactive puede morir de golpe
  (LastTaskResult=3221225786, STATUS_CONTROL_C_EXIT) segundos despues de
  arrancar, ANTES de que el bucle de reintento propio de run-api.ps1
  llegue siquiera a registrar un solo fallo -- el log mostraba
  "arrancando uvicorn" y nada mas, ni una linea de "termino, reintentando".
  Eso descarta que sea el proceso hijo (uvicorn) el que revienta: es el
  PADRE (el powershell.exe que Task Scheduler vigila directamente) el que
  se mata solo, con todo lo que cuelga de el.

  La correccion real: que Task Scheduler vigile un proceso DESECHABLE que
  termina en milisegundos, no el que hace el trabajo. Este script solo
  lanza run-api.ps1 como un proceso DESACOPLADO (Start-Process, sin
  -Wait) y termina de inmediato -- para cuando cualquier senal de sesion
  le llegue al proceso que Task Scheduler sigue vigilando, ya no hay nada
  corriendo ahi que perder. El proceso real (run-api.ps1, con su propio
  bucle de reintento) queda huerfano de Task Scheduler pero vivo, sin
  depender de que su padre original sobreviva.
#>
$ErrorActionPreference = "Continue"
$target = Join-Path $PSScriptRoot "run-api.ps1"
Start-Process -FilePath "powershell.exe" `
  -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", "`"$target`"" `
  -WindowStyle Hidden
