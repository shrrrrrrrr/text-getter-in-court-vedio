param([Parameter(Mandatory=$true)][string]$Target)
$ErrorActionPreference = 'Stop'
if ($Target -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*$') { throw 'Invalid SSH target' }
$courtRoot = Split-Path -Parent $PSScriptRoot
$courtPython = Join-Path $courtRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $courtPython)) { throw 'Local project Python environment is missing' }
Set-Location -LiteralPath $courtRoot
$courtTargetHash = [Security.Cryptography.SHA256]::Create()
try { $courtUploadId = ([BitConverter]::ToString($courtTargetHash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Target)))).Replace('-','').ToLowerInvariant().Substring(0,12) }
finally { $courtTargetHash.Dispose() }
$courtProbe = @'
import sys,os,platform,importlib.util,ctypes.util,shutil,json,subprocess,csv,stat
from pathlib import Path
version=str(sys.version_info.major)+'.'+str(sys.version_info.minor)
assert sys.platform=='linux' and platform.machine()=='x86_64','Linux x86_64 required'
libc,libc_version=platform.libc_ver()
assert libc=='glibc' and tuple(map(int,libc_version.split('.')[:2]))>=(2,28),'glibc 2.28+ required'
assert version in ['3.10','3.11','3.12','3.13'],'Unsupported server Python version'
assert importlib.util.find_spec('venv'),'Host Python venv module missing'
assert ctypes.util.find_library('gomp'),'Host libgomp.so.1 missing'
assert all(shutil.which(n) for n in ['sudo','systemd-run','systemctl','nvidia-smi']),'Required server command missing'
assert os.getuid()!=0,'Use your ordinary SSH account'
assert shutil.disk_usage(os.path.expanduser('~')).free>=60*1024**3,'Need 60 GiB free before transfer'
memory=dict(line.split(':',1) for line in open('/proc/meminfo') if ':' in line)
assert int(memory['MemAvailable'].split()[0])>=20*1024**2,'Need 20 GiB available RAM'
rows=list(csv.reader(subprocess.check_output(['nvidia-smi','--query-gpu=index,memory.used,memory.free,utilization.gpu','--format=csv,noheader,nounits'],text=True).splitlines()))
gpu=next((r for r in rows if r[0].strip()=='3'),None)
assert gpu and int(gpu[1].strip())<=1024 and int(gpu[2].strip())>=12*1024 and int(gpu[3].strip())<=5,'GPU 3 currently busy; no upload/test started'
upload_id=sys.argv[1]
assert len(upload_id)==12 and all(c in '0123456789abcdef' for c in upload_id),'Invalid upload ID'
directory=Path.home()/('court-asr-upload.'+upload_id)
marker=directory/'upload-owner.json'
identity={'profile':'court-asr-ssh-upload-v1','uid':os.getuid()}
if not directory.exists() and not directory.is_symlink():
    directory.mkdir(mode=0o700)
    with marker.open('x') as f:json.dump(identity,f)
    marker.chmod(0o600)
assert not directory.is_symlink() and directory.is_dir(),'Unsafe upload directory'
info=directory.stat()
assert info.st_uid==os.getuid() and not stat.S_IMODE(info.st_mode)&0o077,'Upload directory must be private'
assert not marker.is_symlink() and json.loads(marker.read_text())==identity,'Upload ownership marker differs'
print('COURT_PROBE='+json.dumps({'python':version,'directory':str(directory)}))
'@
$courtEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($courtProbe))
$courtProbeCommand = "python3 -c 'import base64,sys;exec(base64.b64decode(sys.argv.pop(1)))' $courtEncoded $courtUploadId"
$courtSshOptions = @('-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=15','-o','ServerAliveInterval=30','-o','ServerAliveCountMax=6')
Write-Host 'Checking server prerequisites over SSH; no server HTTPS download is needed.'
$courtLines = @(& ssh.exe @courtSshOptions $Target $courtProbeCommand)
if ($LASTEXITCODE -ne 0) { throw 'SSH prerequisite check failed; no transfer or model execution started' }
$courtProbeLine = @($courtLines | Where-Object { $_ -like 'COURT_PROBE=*' })
if ($courtProbeLine.Count -ne 1) { throw 'Server did not return an unambiguous prerequisite result' }
$courtServer = $courtProbeLine[0].Substring('COURT_PROBE='.Length) | ConvertFrom-Json
if ($courtServer.directory -notmatch '^/[A-Za-z0-9_./-]+$') { throw 'Unsafe upload directory returned' }
$courtReceipt = Join-Path $courtRoot '.state/offline-bundles/current-transfer.json'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $courtReceipt) | Out-Null
Write-Host 'Preparing verified Linux wheels, model and offline installer on this computer.'
& $courtPython tools/build_campus_offline.py --python-version $courtServer.python --skip-download --result $courtReceipt
if ($LASTEXITCODE -ne 0) { throw 'Local offline bundle preparation failed; server remains unchanged' }
$courtBundle = Get-Content -LiteralPath $courtReceipt -Raw | ConvertFrom-Json
if ((Get-FileHash -LiteralPath $courtBundle.archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $courtBundle.sha256) { throw 'Local archive checksum mismatch' }
$courtRemoteArchive = $courtServer.directory + '/bundle-' + $courtBundle.sha256 + '.tar'
Write-Host 'Uploading over ordinary SSH with verified resume. Keep this PowerShell window open.'
& $courtPython tools/ssh_bundle_upload.py --target $Target --archive $courtBundle.archive --sha256 $courtBundle.sha256 --remote-path $courtRemoteArchive
if ($LASTEXITCODE -ne 0) { throw 'SSH upload stopped; partial archive retained. Rerun this entry to resume; installer was not run.' }
$courtRemoteCommand = & $courtPython tools/build_campus_offline.py --receiver-path $courtRemoteArchive --receiver-sha $courtBundle.sha256
if ($LASTEXITCODE -ne 0) { throw 'Cannot construct verified receiver command' }
Write-Host 'Server will verify/extract, install only the dedicated venv, then short-test GPU 3.'
Write-Host 'Enter SSH/sudo passwords only in the normal terminal prompts; do not paste them into chat.'
& ssh.exe @courtSshOptions -tt $Target $courtRemoteCommand
if ($LASTEXITCODE -ne 0) { throw 'Remote preparation stopped. Share the final error lines; retain caches.' }
Write-Host 'OFFLINE_DEPLOY_COMMAND_FINISHED'
