$ErrorActionPreference = 'Stop'
$config = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime\music-local.json') -Raw | ConvertFrom-Json
Set-Clipboard -Value $config.token
Start-Process 'http://127.0.0.1:58092' -WindowStyle Hidden
Write-Host 'Music key copied. Paste it into the password field in the browser (Ctrl+V), then click Enter.'
