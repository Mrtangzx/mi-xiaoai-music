param([switch]$NoOpen)
$ErrorActionPreference = 'Stop'
$base = $PSScriptRoot
$python = if ($env:HOME_MUSIC_PYTHON) { $env:HOME_MUSIC_PYTHON } elseif (Test-Path (Join-Path $base 'runtime\python-path.txt')) { (Get-Content (Join-Path $base 'runtime\python-path.txt') -Raw).Trim() } else { 'python' }
& $python -X utf8 (Join-Path $base 'local_runner.py') init
if ($LASTEXITCODE -ne 0) { throw 'Python initialization failed. Set HOME_MUSIC_PYTHON to Python 3.10+.' }
$listener = Get-NetTCPConnection -State Listen -LocalPort 58092 -ErrorAction SilentlyContinue
if (-not $listener) {
    $p = Start-Process -FilePath $python -ArgumentList @('-X','utf8','local_runner.py','serve') -WorkingDirectory $base -WindowStyle Hidden -RedirectStandardOutput (Join-Path $base 'runtime\music.stdout.log') -RedirectStandardError (Join-Path $base 'runtime\music.stderr.log') -PassThru
    @{id=$p.Id; start=$p.StartTime.ToUniversalTime().ToString('o'); executable=$python} | ConvertTo-Json | Set-Content (Join-Path $base 'runtime\music.process.json') -Encoding utf8
}
Push-Location $base
try {
    docker compose --project-name codex-home-music-local-20261106 --file compose.songloft-local.yaml --env-file runtime/songloft-local.env up --detach songloft
    if ($LASTEXITCODE -ne 0) { throw 'Docker startup failed. Open Docker Desktop and retry.' }
} finally { Pop-Location }
Start-Sleep -Seconds 2
& $python -X utf8 (Join-Path $base 'local_runner.py') verify
if ($LASTEXITCODE -ne 0) { throw 'Local verification failed. Check runtime logs.' }
if (-not $NoOpen) {
    Start-Process 'http://127.0.0.1:58091' -WindowStyle Hidden
    Start-Process 'http://127.0.0.1:58092' -WindowStyle Hidden
}
