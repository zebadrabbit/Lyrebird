# Renders components\Splash.html to splash.png (560x320) and splash@2x.png with headless Edge.
# Re-run after changing the splash design:  powershell -ExecutionPolicy Bypass -File brand\render-splash.ps1
$ErrorActionPreference = "Stop"
$edge = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
$page = "file:///" + ((Join-Path $PSScriptRoot "components\Splash.html") -replace "\\", "/")
foreach ($scale in 1, 2) {
    $out = Join-Path $PSScriptRoot $(if ($scale -eq 1) { "splash.png" } else { "splash@2x.png" })
    & $edge --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=$scale `
        --window-size=560,320 --virtual-time-budget=3000 --screenshot="$out" $page | Out-Null
    Write-Host "wrote $out"
}
