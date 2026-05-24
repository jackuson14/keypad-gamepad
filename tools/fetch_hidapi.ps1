<#
    fetch_hidapi.ps1 - download the native hidapi.dll the `hid` Python package needs.

    The `hid` PyPI package is a pure-Python ctypes wrapper; on Windows it does NOT
    bundle the native library. This script fetches the official prebuilt DLL from the
    canonical upstream (libusb/hidapi GitHub releases), pinned to a known version, and
    vendors x64/hidapi.dll into the project root next to the Python sources.

    It prints the SHA-256 of both the downloaded archive and the extracted DLL so the
    result can be verified against the published release. Re-running is idempotent.

    Usage:  powershell -ExecutionPolicy Bypass -File tools\fetch_hidapi.ps1
#>
$ErrorActionPreference = "Stop"

# --- Pinned release -------------------------------------------------------
$Version = "0.14.0"
$Url     = "https://github.com/libusb/hidapi/releases/download/hidapi-$Version/hidapi-win.zip"

# Project root = parent of this script's directory
$ProjRoot = Split-Path -Parent $PSScriptRoot
$DestDll  = Join-Path $ProjRoot "hidapi.dll"

$Zip = Join-Path $env:TEMP "hidapi-win-$Version.zip"
$Ext = Join-Path $env:TEMP "hidapi-win-$Version-extracted"

Write-Output "[fetch_hidapi] downloading hidapi $Version"
Write-Output "[fetch_hidapi]   from: $Url"
Invoke-WebRequest -Uri $Url -OutFile $Zip -UseBasicParsing

$zipHash = (Get-FileHash $Zip -Algorithm SHA256).Hash
Write-Output "[fetch_hidapi] archive SHA256: $zipHash"

if (Test-Path $Ext) { Remove-Item $Ext -Recurse -Force }
Expand-Archive -Path $Zip -DestinationPath $Ext -Force

$Src = Join-Path $Ext "x64\hidapi.dll"
if (-not (Test-Path $Src)) {
    throw "x64\hidapi.dll not found inside the archive - layout changed?"
}
Copy-Item $Src -Destination $DestDll -Force

$dllHash = (Get-FileHash $DestDll -Algorithm SHA256).Hash
Write-Output "[fetch_hidapi] vendored -> $DestDll"
Write-Output "[fetch_hidapi] hidapi.dll SHA256: $dllHash"
Write-Output "[fetch_hidapi] done."
