$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$factorioRoot = 'E:\factorio-space-forMCP\Factorio_2.0.77'
$factorioExe = Join-Path $factorioRoot 'bin\x64\factorio.exe'
$factorioConfig = Join-Path $factorioRoot 'config\config.ini'
$modDirectory = Join-Path $projectRoot 'mod'
$secretFile = Join-Path $projectRoot '.rcon-password'

if (-not (Test-Path -LiteralPath $factorioExe)) {
    throw "Factorio was not found at $factorioExe"
}

if (-not (Test-Path -LiteralPath $secretFile)) {
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    [System.IO.File]::WriteAllText($secretFile, [Convert]::ToHexString($bytes))
}

$secret = [System.IO.File]::ReadAllText($secretFile).Trim()
& $factorioExe --config $factorioConfig --mod-directory $modDirectory `
    --rcon-bind '127.0.0.1:27015' --rcon-password $secret
