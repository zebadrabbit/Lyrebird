# Builds the release: dist\Lyrebird-win64.zip (a small launcher; PyTorch and the models download on first run).
#   powershell -ExecutionPolicy Bypass -File build.ps1
# Needs uv (https://docs.astral.sh/uv/). uv also downloads a Python 3.12 that includes tkinter,
# which some python.org installs leave out.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Exec($exe) {
    & $exe @args
    if ($LASTEXITCODE) { throw "$exe $args failed with exit code $LASTEXITCODE" }
}

if (-not (Test-Path .venv)) { Exec uv venv --managed-python --python 3.12 .venv }
# The uv package ships uv.exe, which the launcher bundles to install everything on the user's PC.
Exec uv pip install --python .venv pyinstaller uv==0.11.24 "numpy<2"  # numpy: for the tests only
Exec .venv\Scripts\python.exe test_lyrebird.py

# Build under %TEMP%: OneDrive/Dropbox-synced folders lock freshly written files mid-build.
$out = Join-Path $env:TEMP "lyrebird-build"
Exec .venv\Scripts\pyinstaller.exe --noconfirm --clean --windowed --name Lyrebird `
    --distpath "$out\dist" --workpath "$out\build" --specpath $out `
    --add-binary "$PSScriptRoot\.venv\Scripts\uv.exe;." `
    --add-data "$PSScriptRoot\lyrebird.py;." `
    --add-data "$PSScriptRoot\requirements.txt;." `
    --add-data "$PSScriptRoot\brand\lyrebird.ico;." `
    --add-data "$PSScriptRoot\brand\splash.png;." `
    --add-data "$PSScriptRoot\brand\splash@2x.png;." `
    --icon "$PSScriptRoot\brand\lyrebird.ico" `
    launcher.py

New-Item -ItemType Directory -Force dist | Out-Null
Compress-Archive -Path "$out\dist\Lyrebird" -DestinationPath dist\Lyrebird-win64.zip -Force
Write-Host "`nBuilt dist\Lyrebird-win64.zip (unzipped app: $out\dist\Lyrebird\Lyrebird.exe)"
