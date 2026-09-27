param([switch]$PrepareOnly)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$factorioRoot = 'E:\factorio-space-forMCP\Factorio_2.0.77'
$factorioExe = Join-Path $factorioRoot 'bin\x64\factorio.exe'
$factorioConfig = Join-Path $factorioRoot 'config\config.ini'
$modDirectory = Join-Path $projectRoot 'mod'
$secretFile = Join-Path $projectRoot '.rcon-password'
$localConfig = Join-Path $projectRoot '.factorio-mcp-config.ini'

if (-not (Test-Path -LiteralPath $factorioExe)) {
    throw "Factorio was not found at $factorioExe"
}

if (-not (Test-Path -LiteralPath $secretFile)) {
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    [System.IO.File]::WriteAllText($secretFile, [Convert]::ToHexString($bytes))
}

$secret = [System.IO.File]::ReadAllText($secretFile).Trim()
if ($secret -notmatch '^[0-9A-Fa-f]{64}$') {
    throw 'The RCON password file must contain a 64-character hexadecimal secret.'
}

# GUI multiplayer hosting reads these settings from config.ini rather than the
# dedicated-server --rcon-bind and --rcon-password command-line options.
$configContent = [System.IO.File]::ReadAllText($factorioConfig)
foreach ($setting in @('local-rcon-socket', 'local-rcon-password')) {
    if ($configContent -notmatch "(?m)^\s*;?\s*$setting\s*=") {
        throw "Factorio config is missing $setting"
    }
}
$configContent = [regex]::Replace(
    $configContent, '(?m)^\s*;?\s*local-rcon-socket\s*=.*$',
    'local-rcon-socket=127.0.0.1:27015')
$configContent = [regex]::Replace(
    $configContent, '(?m)^\s*;?\s*local-rcon-password\s*=.*$',
    "local-rcon-password=$secret")
[System.IO.File]::WriteAllText($localConfig, $configContent)

if ($PrepareOnly) { return }
& $factorioExe --config $localConfig --mod-directory $modDirectory
