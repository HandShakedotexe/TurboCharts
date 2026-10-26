<#
  Prepara un PC con Windows para hacer de grabador 24/7.

  Ejecutar UNA vez, como administrador:
      powershell -ExecutionPolicy Bypass -File deploy\windows\harden.ps1

  Lo que arregla, en orden de cuanto dano hace cada cosa:

  1. Windows Update reiniciando solo. Es la causa numero uno de huecos en un
     PC domestico, y la peor, porque ocurre de madrugada y no te enteras.
  2. Suspension e hibernacion. "No lo apago nunca" no basta: Windows suspende
     igual por inactividad si nadie se lo prohibe.
  3. Ahorro de energia de la tarjeta de red. Windows apaga el adaptador para
     ahorrar bateria aunque sea un equipo de escritorio enchufado.
  4. Inicio rapido. Deja el sistema en un estado hibrido que complica los
     arranques automaticos tras un corte.
#>
#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

Write-Host "`n== Grabador 24/7: preparando este equipo ==`n" -ForegroundColor Cyan

# --- 1. Suspension, hibernacion e inicio rapido ---------------------------
Write-Host "[1/5] Desactivando suspension, hibernacion e inicio rapido..."
powercfg /change standby-timeout-ac 0      # nunca suspender (con corriente)
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 15     # la pantalla si puede apagarse
powercfg /change disk-timeout-ac 0         # el disco NO: estamos escribiendo
powercfg /hibernate off
Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power" `
  -Name HiberbootEnabled -Value 0 -Type DWord

# --- 2. Ahorro de energia del adaptador de red ----------------------------
Write-Host "[2/5] Impidiendo que Windows apague la tarjeta de red..."
foreach ($nic in (Get-NetAdapter -Physical | Where-Object Status -eq "Up")) {
  try {
    Disable-NetAdapterPowerManagement -Name $nic.Name -ErrorAction Stop
    Write-Host "      $($nic.Name): ahorro de energia desactivado"
  } catch {
    Write-Host "      $($nic.Name): no se pudo cambiar ($($_.Exception.Message))" -ForegroundColor Yellow
  }
}

# --- 3. USB selective suspend --------------------------------------------
Write-Host "[3/5] Desactivando la suspension selectiva de USB..."
$scheme = (powercfg /getactivescheme) -replace '.*GUID de.*?:\s*([0-9a-f-]+).*', '$1'
if (-not $scheme -or $scheme -match '\s') {
  $scheme = (powercfg /getactivescheme) -replace '.*:\s*([0-9a-f-]{36}).*', '$1'
}
powercfg /setacvalueindex $scheme 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
powercfg /setactive $scheme

# --- 4. Windows Update: horas activas y aviso antes de reiniciar ----------
Write-Host "[4/5] Ajustando Windows Update para que no reinicie a traicion..."
$au = "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
New-Item -Path $au -Force | Out-Null
# Horas activas: 24 h no se puede, el maximo son 18. Se elige la franja en la
# que menos duele reiniciar y aun asi habra que reiniciar a mano de vez en
# cuando. Un PC domestico NUNCA va a tener 100% de cobertura, y por eso el
# segundo grabador no es opcional.
Set-ItemProperty -Path $au -Name "ActiveHoursStart" -Value 6  -Type DWord
Set-ItemProperty -Path $au -Name "ActiveHoursEnd"   -Value 23 -Type DWord
Set-ItemProperty -Path $au -Name "IsActiveHoursEnabled" -Value 1 -Type DWord

$pol = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
New-Item -Path $pol -Force | Out-Null
Set-ItemProperty -Path $pol -Name "NoAutoRebootWithLoggedOnUsers" -Value 1 -Type DWord

# --- 5. Reinicio automatico tras un corte de luz --------------------------
Write-Host "[5/5] Comprobando el arranque automatico tras corte de corriente..."
Write-Host "      Esto NO se puede configurar desde Windows: esta en la BIOS/UEFI." -ForegroundColor Yellow
Write-Host "      Busca 'Restore on AC Power Loss' o 'After Power Failure' y ponlo en 'Power On'."
Write-Host "      Sin eso, un apagon de dos minutos te deja sin grabar hasta que vuelvas a casa."

Write-Host "`n== Listo ==" -ForegroundColor Green
Write-Host @"

Lo que este script NO puede arreglar:

  - Un apagon. Un no-break (UPS) de 600 VA cuesta poco y da margen para que
    la maquina sobreviva a los microcortes, que son los mas frecuentes.
  - Tu proveedor de internet reiniciando el modem de madrugada.
  - Que un dia necesites reiniciar para instalar algo.

Ninguna de las tres se arregla con configuracion: se arreglan teniendo un
segundo grabador en otro sitio. Los huecos de uno los tapa el otro.

Siguiente paso:  deploy\windows\install-task.ps1
"@
