$ErrorActionPreference = 'Stop'
$record = Join-Path $PSScriptRoot 'runtime\music.process.json'
if (Test-Path $record) {
    $saved = Get-Content -LiteralPath $record -Raw | ConvertFrom-Json
    $p = Get-Process -Id $saved.id -ErrorAction SilentlyContinue
    if ($p -and $p.Path -eq $saved.executable -and $p.StartTime.ToUniversalTime().ToString('o') -eq $saved.start) { Stop-Process -Id $p.Id }
}
Push-Location $PSScriptRoot
try { docker compose --project-name codex-home-music-local-20261106 --file compose.songloft-local.yaml --env-file runtime/songloft-local.env stop songloft }
finally { Pop-Location }
