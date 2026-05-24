<#
    build_exe.ps1 - package the analog GUI into a single windowed .exe.

    Produces dist\keypad-gamepad-analog.exe. Requires the build deps:
        py -m pip install -r requirements-dev.txt

    The runtime app still needs the ViGEmBus driver installed on the target
    machine (the .exe bundles the vgamepad client + hidapi.dll, but ViGEmBus is a
    kernel driver and must be installed separately).

    Usage:  powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path (Join-Path $root "hidapi.dll"))) {
    throw "hidapi.dll missing - run tools\fetch_hidapi.ps1 first."
}

Write-Output "[build] cleaning previous build..."
foreach ($d in @("build", "dist")) {
    if (Test-Path $d) { Remove-Item $d -Recurse -Force }
}

Write-Output "[build] running PyInstaller..."
py -m PyInstaller --noconfirm "analog_gui.spec"

$exe = Join-Path $root "dist\keypad-gamepad-analog.exe"
if (Test-Path $exe) {
    $sz = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Output "[build] OK -> $exe  ($sz MB)"
} else {
    throw "[build] FAILED - exe not produced."
}
