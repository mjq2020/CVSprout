param(
    [switch]$Cpu, [switch]$Gpu, [switch]$Check, [switch]$Status,
    [switch]$Stop, [switch]$Build
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed (exit $LASTEXITCODE). See output above." }
}
if ($Cpu -and $Gpu) { throw 'Choose only -Cpu or -Gpu.' }
Invoke-Docker info --format '{{.ServerVersion}}'
Invoke-Docker compose version
if (!(Test-Path .env)) { Copy-Item .env.example .env }
$mode = 'cpu'
if (Test-Path .visionforge-mode) { $mode = (Get-Content .visionforge-mode -Raw).Trim() }
elseif (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    & nvidia-smi -L *> $null
    if ($LASTEXITCODE -eq 0) { $mode = 'gpu' }
}
if ($Cpu) { $mode = 'cpu' }
if ($Gpu) { $mode = 'gpu' }
if ($mode -notin @('cpu', 'gpu')) { throw 'Invalid .visionforge-mode.' }
$compose = @('compose', '-f', 'compose.yaml')
if ($mode -eq 'gpu') { $compose += @('-f', 'compose.gpu.yaml') }
Invoke-Docker @compose config --quiet
if ($Stop) { Invoke-Docker @compose stop; exit }
if ($Status) {
    Invoke-Docker @compose ps
    Invoke-Docker @compose exec -T web python -m app.setup
    exit
}
$images = Invoke-Docker @compose config --images
if ($Build -or ($images -match ':local$')) {
    Write-Host 'Source checkout: building images. Release packages pull prebuilt images.'
    Invoke-Docker @compose build
} else { Invoke-Docker @compose pull }
$diagnostic = @('run', '--rm', '--no-deps', 'worker', 'python', '-m', 'app.diagnostics')
if ($mode -eq 'gpu') { $diagnostic += '--gpu' }
Invoke-Docker @compose @diagnostic
if ($Check) { exit }
Invoke-Docker @compose up -d --no-build --wait --wait-timeout 180
Set-Content -Path .visionforge-mode -Value $mode -Encoding ascii
Invoke-Docker @compose exec -T web python -m app.setup
$address = Invoke-Docker @compose port web 8000
Write-Host "CVSprout ($mode): http://$($address -replace '0.0.0.0', '127.0.0.1')"
