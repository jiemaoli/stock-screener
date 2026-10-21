param(
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $OutputPath) {
    $OutputPath = Join-Path (Split-Path $ProjectRoot -Parent) "stock_screener_deploy.tar.gz"
}
$OutputPath = [System.IO.Path]::GetFullPath($OutputPath)

if (Test-Path -LiteralPath $OutputPath) {
    Remove-Item -LiteralPath $OutputPath -Force
}

Push-Location $ProjectRoot
try {
    tar.exe -czf $OutputPath `
        --exclude=.git `
        --exclude=.github `
        --exclude=.env `
        --exclude=.env.local `
        --exclude=.env.*.local `
        --exclude=deploy/.initial_credentials `
        --exclude='*.htpasswd' `
        --exclude=venv `
        --exclude=.venv `
        --exclude=__pycache__ `
        --exclude='*/__pycache__' `
        --exclude='*.pyc' `
        --exclude=.pytest_cache `
        --exclude=.coverage `
        --exclude=htmlcov `
        --exclude=data/cache `
        --exclude=data/config `
        --exclude=data/logs `
        --exclude=data/daily_scans `
        --exclude=data/fundamentals_cache `
        --exclude=data/position_reports `
        .

    if ($LASTEXITCODE -ne 0) {
        throw "tar.exe failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$Archive = Get-Item -LiteralPath $OutputPath
Write-Host "Created deployment package: $($Archive.FullName)"
Write-Host "Size: $([math]::Round($Archive.Length / 1MB, 2)) MB"
