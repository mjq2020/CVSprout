param(
    [switch]$Cpu, [switch]$Gpu, [switch]$Check, [switch]$Status,
    [switch]$Stop, [switch]$Build,
    [ValidateRange(0, 65535)][int]$Port, [switch]$StrictPort
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed (exit $LASTEXITCODE). See output above." }
}
function Start-ComposeStack {
    # Windows PowerShell treats native stderr as ErrorRecord objects.
    # Capture the exit code without turning a recoverable bind error into a throw.
    $ErrorActionPreference = 'Continue'
    $lines = @(& docker @compose up -d --no-build --wait --wait-timeout 180 2>&1 | ForEach-Object {
        Write-Host "$_"
        "$_"
    })
    $code = $LASTEXITCODE
    return [pscustomobject]@{ ExitCode = $code; Output = ($lines -join "`n") }
}
function Show-Address {
    $address = Invoke-Docker @compose port web 8000
    Write-Host "CVSprout ($mode): http://$($address -replace '0\.0\.0\.0', '127.0.0.1')"
}
function Save-Port([int]$Value) {
    $path = Join-Path $PSScriptRoot '.env'
    $temporary = Join-Path $PSScriptRoot ('.env.port.' + [guid]::NewGuid().ToString('N'))
    $contents = [System.IO.File]::ReadAllText($path)
    $pattern = '(?m)^[ \t]*(?:export[ \t]+)?MDP_PORT[ \t]*=[^\r\n]*'
    if ([regex]::IsMatch($contents, $pattern)) {
        $contents = [regex]::Replace($contents, $pattern, "MDP_PORT=$Value")
    } else { $contents = $contents.TrimEnd("`r", "`n") + "`nMDP_PORT=$Value`n" }
    try {
        [System.IO.File]::WriteAllText($temporary, $contents, (New-Object System.Text.UTF8Encoding($false)))
        Move-Item -Force -Path $temporary -Destination $path
    } finally {
        if (Test-Path $temporary) { Remove-Item -Force $temporary }
    }
}

$originalPort = $env:MDP_PORT
try {
    if ($PSBoundParameters.ContainsKey('Port')) { $env:MDP_PORT = [string]$Port }
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
        Show-Address
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
    $result = Start-ComposeStack
    if ($result.ExitCode -ne 0) {
        if ($StrictPort -or $result.Output -notmatch 'address already in use|port is already allocated|only one usage of each socket address') {
            throw 'Startup failed. See the Docker error above.'
        }
        Write-Host 'The requested port is occupied. Asking Docker to allocate an available port...'
        $env:MDP_PORT = '0'
        Invoke-Docker @compose up -d --no-build --wait --wait-timeout 180
    }
    Set-Content -Path .visionforge-mode -Value $mode -Encoding ascii
    Invoke-Docker @compose exec -T web python -m app.setup
    $address = Invoke-Docker @compose port web 8000
    Write-Host "CVSprout ($mode): http://$($address -replace '0\.0\.0\.0', '127.0.0.1')"
    $actualPort = [int](($address -split ':')[-1])
    if ($actualPort -lt 1 -or $actualPort -gt 65535) { throw 'Cannot determine the published port. Run docker compose ps.' }
    try {
        Save-Port $actualPort
        Write-Host "Saved MDP_PORT=$actualPort to .env for subsequent starts."
    } catch {
        Write-Warning "The service is running, but the port could not be saved. Next time use -Port $actualPort."
    }
} finally {
    $env:MDP_PORT = $originalPort
}
