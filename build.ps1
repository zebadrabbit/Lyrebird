# Builds dist\Lyrebird\Lyrebird.exe
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
Exec uv pip install --python .venv -r requirements.txt --index-strategy unsafe-best-match
Exec uv pip install --python .venv --no-deps chatterbox-tts==0.1.7
Exec uv pip install --python .venv pyinstaller
Exec .venv\Scripts\python.exe test_lyrebird.py

Exec .venv\Scripts\pyinstaller.exe --noconfirm --clean --windowed --name Lyrebird `
    --collect-data perth `
    --collect-data chatterbox `
    --collect-data s3tokenizer `
    --collect-data pykakasi `
    --collect-data spacy_pkuseg `
    --copy-metadata requests `
    --copy-metadata pykakasi `
    --collect-submodules srsly `
    lyrebird.py

Write-Host "`nBuilt dist\Lyrebird\Lyrebird.exe"
