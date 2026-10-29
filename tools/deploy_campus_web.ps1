param([Parameter(Mandatory=$true)][string]$Target,
      [Parameter(Mandatory=$true)][string]$Listen,
      [int]$Port=8766)
$ErrorActionPreference='Stop'
if ($Target -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*$') { throw 'Invalid SSH target' }
$courtRoot=Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $courtRoot
$courtPython=Join-Path $courtRoot '.venv/Scripts/python.exe'
$courtReceipt=Get-Content -LiteralPath (Join-Path $courtRoot '.state/web-bundles/current-web-transfer.json') -Raw | ConvertFrom-Json
if ((Get-FileHash -LiteralPath $courtReceipt.archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $courtReceipt.sha256) { throw 'Web bundle checksum mismatch' }
$courtHasher=[Security.Cryptography.SHA256]::Create()
try { $courtUploadId=([BitConverter]::ToString($courtHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($Target)))).Replace('-','').ToLowerInvariant().Substring(0,12) }
finally { $courtHasher.Dispose() }
$courtCommands=& $courtPython tools/build_campus_web.py --upload-id $courtUploadId --sha256 $courtReceipt.sha256 --listen $Listen --port $Port
if ($LASTEXITCODE -ne 0) { throw 'Invalid campus listener configuration' }
# The previous ASR upload already created and verified this account-owned directory.
$courtProbe=@'
import os,sys,json
from pathlib import Path
assert sys.platform=='linux' and sys.version_info[:2]==(3,10),'Need verified server Python 3.10'
assert os.getuid()!=0,'Use your normal SSH account'
p=Path.home()/('court-asr-upload.'+sys.argv[1])
assert p.is_dir() and not p.is_symlink() and p.stat().st_uid==os.getuid(),'Verified ASR upload directory missing'
marker=p/'upload-owner.json'
assert not marker.is_symlink() and json.loads(marker.read_text())=={'profile':'court-asr-ssh-upload-v1','uid':os.getuid()},'Upload owner mismatch'
status=json.loads((Path.home()/'court-transcript-runtime-v1/status.json').read_text())
assert status.get('status')=='passed' and status.get('mode')=='offline-venv','ASR preparation is not verified'
print('COURT_WEB_UPLOAD_DIR='+str(p))
'@
$courtEncoded=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($courtProbe))
$courtProbeCommand="python3 -c 'import base64,sys;exec(base64.b64decode(sys.argv.pop(1)))' $courtEncoded $courtUploadId"
$courtOptions=@('-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=15','-o','ServerAliveInterval=30','-o','ServerAliveCountMax=6')
Write-Host 'Checking the already prepared ASR environment; no model transfer is needed.'
$courtLines=@(& ssh.exe @courtOptions -T $Target $courtProbeCommand)
if ($LASTEXITCODE -ne 0) { throw 'Server preparation check failed; no application installed' }
$courtMatches=@($courtLines | Where-Object { $_ -like 'COURT_WEB_UPLOAD_DIR=*' })
if ($courtMatches.Count -ne 1) { throw 'Ambiguous upload directory' }
$courtDirectory=$courtMatches[0].Substring('COURT_WEB_UPLOAD_DIR='.Length)
if ($courtDirectory -notmatch '^/[A-Za-z0-9_./-]+$') { throw 'Unsafe upload directory' }
Write-Host 'Uploading the smaller web delta over ordinary SSH with verified resume.'
& $courtPython tools/ssh_bundle_upload.py --target $Target --archive $courtReceipt.archive --sha256 $courtReceipt.sha256 --remote-path ($courtDirectory+'/bundle-'+$courtReceipt.sha256+'.tar')
if ($LASTEXITCODE -ne 0) { throw 'Upload stopped; rerun this entry to resume' }
$courtOutput=Join-Path $courtRoot '.state/deploy/install-campus-web.commands.sh'
[IO.File]::WriteAllText($courtOutput,($courtCommands -join "`n")+"`n",[Text.UTF8Encoding]::new($false))
Write-Host 'WEB_DELTA_UPLOAD_VERIFIED'
Write-Host 'Paste the following block into your existing SERVER BASH terminal. It starts the requested campus HTTP test service only after its loopback GPU workflow passes.'
$courtCommands | ForEach-Object { Write-Host $_ }
Write-Host ('Saved: '+$courtOutput)
Write-Host 'Personal credentials are in .state/deploy/campus-access.local.json on this computer. Do not paste them into chat.'
Write-Host 'This window will now check campus reachability while you execute the server block in your existing server terminal.'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'check_campus_peer.ps1') -Url ('http://'+$Listen+':'+$Port)
if ($LASTEXITCODE -ne 0) { throw 'Peer check did not pass. Keep server output and the private peer report; the package is already uploaded.' }
