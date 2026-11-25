# Rebuild the distributable GUI exe after code changes.
# Usage:  .\build-exe.ps1
# Output: dist\akin-video-finder-gui.exe
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $repo
try {
    & "$repo\.venv\Scripts\python.exe" -m PyInstaller `
        "$repo\packaging\akin-video-finder-gui.spec" --clean --noconfirm
    Write-Host "`nBuilt: $repo\dist\akin-video-finder-gui.exe"
} finally {
    Pop-Location
}
