param([switch]$Gpu)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (!(Test-Path .env)) {
    $bytes = New-Object byte[] 32
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    $key = [Convert]::ToBase64String($bytes)
    Set-Content -Path .env -Value ("VISIONFORGE_API_KEY=" + $key) -Encoding ascii
}
$compose = @('compose', '-f', 'compose.yaml')
if ($Gpu) { $compose += @('-f', 'compose.gpu.yaml') }
& docker @compose up --build -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) { throw 'Model service startup failed.' }
$address = & docker @compose port model 8080
if ($LASTEXITCODE -ne 0) { throw 'Cannot determine model service address.' }
Write-Host "Open http://$($address -replace '0.0.0.0', '127.0.0.1'). API key is in .env."
