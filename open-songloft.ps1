$ErrorActionPreference = 'Stop'
$line = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime\songloft-local.env') | Where-Object { $_.StartsWith('SONGLOFT_ADMIN_PASSWORD=') }
Set-Clipboard -Value $line.Substring('SONGLOFT_ADMIN_PASSWORD='.Length)
Start-Process 'http://127.0.0.1:58091' -WindowStyle Hidden
Write-Host 'Username: admin. Password copied; paste into the browser password field (Ctrl+V).'
