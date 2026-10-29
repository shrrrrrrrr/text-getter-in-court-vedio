param([Parameter(Mandatory=$true)][string]$Target,
      [Parameter(Mandatory=$true)][string]$RemoteDirectory)
$ErrorActionPreference='Stop'
if ($Target -notmatch '^([a-z_][a-z0-9_-]{0,31})@[A-Za-z0-9][A-Za-z0-9.-]*$') { throw 'Invalid ordinary SSH target' }
$courtAccount=$Matches[1]
$courtRoot=Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $courtRoot
$courtPython=Join-Path $courtRoot '.venv/Scripts/python.exe'
$courtReceipt=Get-Content -Raw -LiteralPath (Join-Path $courtRoot '.state/deploy/platform-bundle.local.json') | ConvertFrom-Json
if ((Get-FileHash -LiteralPath $courtReceipt.archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $courtReceipt.sha256) { throw 'Local archive checksum mismatch' }
$courtCommands=& $courtPython tools/build_platform_bundle.py --server-directory $RemoteDirectory --sha256 $courtReceipt.sha256 --account $courtAccount
if ($LASTEXITCODE -ne 0) { throw 'Invalid server preparation arguments' }
& $courtPython tools/ssh_bundle_upload.py --target $Target --archive $courtReceipt.archive --sha256 $courtReceipt.sha256 --remote-path ($RemoteDirectory+'/bundle-'+$courtReceipt.sha256+'.tar')
if ($LASTEXITCODE -ne 0) { throw 'Verified upload stopped; keep the cache and resume this same entry' }
$courtText=($courtCommands -join "`n")+"`n"
$courtOutput=Join-Path $courtRoot '.state/deploy/prepare-platform-campus.commands.sh'
[IO.File]::WriteAllText($courtOutput,$courtText,[Text.UTF8Encoding]::new($false))
Write-Host 'COURT_PLATFORM_UPLOAD_VERIFIED'
Write-Host 'Paste the following block into the CAMPUS SERVER Bash terminal, not Alibaba Cloud.'
$courtCommands | ForEach-Object { Write-Host $_ }
try { Set-Clipboard -Value $courtText; Write-Host 'The campus preparation block is also copied to the clipboard.' }
catch { Write-Host ('Copy the saved block: '+$courtOutput) }
Write-Host 'Return the final preparation JSON. It contains a PUBLIC certificate and SSH PUBLIC key; both private keys stay on the campus server.'
