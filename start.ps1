$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python -X utf8 -m qmt daily
$taskExit = $LASTEXITCODE
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'data/capital-100k/dashboard.html')) {
    Invoke-Item -LiteralPath (Join-Path $PSScriptRoot 'data/capital-100k/dashboard.html')
}
exit $taskExit
