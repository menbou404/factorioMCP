$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$secretFile = Join-Path $projectRoot '.rcon-password'
if (-not (Test-Path -LiteralPath $secretFile)) {
    throw 'Start Factorio with scripts/start-factorio.ps1 first.'
}

$env:FACTORIO_RCON_PASSWORD = [System.IO.File]::ReadAllText($secretFile).Trim()
$env:FACTORIO_RCON_PORT = '27015'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Python environment is missing. Run uv sync in the project folder first.'
}
& $python -m factorio_mcp.server
