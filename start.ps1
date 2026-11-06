$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python -X utf8 -m qmt daily
$taskExit = $LASTEXITCODE
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'data/dashboard.html')) {
    Invoke-Item -LiteralPath (Join-Path $PSScriptRoot 'data/dashboard.html')
}
exit $taskExit
