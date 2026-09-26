$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$secretFile = Join-Path $projectRoot '.rcon-password'
if (-not (Test-Path -LiteralPath $secretFile)) {
    throw 'Start Factorio with scripts/start-factorio.ps1 first.'
}

$env:FACTORIO_RCON_PASSWORD = [System.IO.File]::ReadAllText($secretFile).Trim()
$env:FACTORIO_RCON_PORT = '27015'
& node (Join-Path $projectRoot 'src\index.js')
