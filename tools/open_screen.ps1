# Opens the projector page (/screen) full-screen with video sound allowed from the start (no click needed).
# Uses its own browser profile, because the autoplay flag is ignored if Edge/Chrome is already running on the normal one.
#   powershell -ExecutionPolicy Bypass -File tools\open_screen.ps1 [-Port 8000]
param(
    [int]$Port = 8000
)
$ErrorActionPreference = 'Stop'
$browser = @(
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $browser) { throw "Microsoft Edge or Google Chrome not found" }
$profileDir = Join-Path $env:LOCALAPPDATA "QuizScreenProfile"
Start-Process $browser -ArgumentList @(
    "--user-data-dir=`"$profileDir`"",
    "--autoplay-policy=no-user-gesture-required",
    "--no-first-run",
    "--start-fullscreen",
    "--app=http://localhost:$Port/screen"
)
Write-Host "Projector opened in $([IO.Path]::GetFileNameWithoutExtension($browser)). F11 leaves full screen; Win+Shift+Arrow moves it to the projector."
