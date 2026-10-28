param([int]$Port = 8766, [switch]$Reload)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw '请先运行 python -m venv .venv，然后运行 .\.venv\Scripts\python.exe -m pip install -e ".[local,test]"' }
if (Test-Path -LiteralPath '.env') {
  foreach ($taskLine in Get-Content -LiteralPath '.env') {
    if ($taskLine -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
      [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Trim().Trim('"').Trim("'"), 'Process')
    }
  }
}
$env:PYTHONUTF8 = '1'
$taskArgs = @('-m', 'uvicorn', 'court_transcript.app:app', '--host', '127.0.0.1', '--port', "$Port")
if ($Reload) { $taskArgs += '--reload' }
Write-Host "庭录本地工作台：http://127.0.0.1:$Port"
& $taskPython @taskArgs
