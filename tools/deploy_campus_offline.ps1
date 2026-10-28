param([Parameter(Mandatory=$true)][string]$Target)
$ErrorActionPreference = 'Stop'
if ($Target -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*$') { throw 'Invalid SSH target' }
$courtRoot = Split-Path -Parent $PSScriptRoot
$courtPython = Join-Path $courtRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $courtPython)) { throw 'Local project Python environment is missing' }
Set-Location -LiteralPath $courtRoot
$courtProbe = @'
import sys,os,platform,importlib.util,ctypes.util,shutil,tempfile,json,subprocess,csv
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
directory=tempfile.mkdtemp(prefix='court-asr-upload.',dir=os.path.expanduser('~'))
print('COURT_PROBE='+json.dumps({'python':version,'directory':directory}))
'@
$courtEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($courtProbe))
$courtProbeCommand = "python3 -c 'import base64,sys;exec(base64.b64decode(sys.argv.pop(1)))' $courtEncoded"
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
& $courtPython tools/build_campus_offline.py --python-version $courtServer.python --result $courtReceipt
if ($LASTEXITCODE -ne 0) { throw 'Local offline bundle preparation failed; server remains unchanged' }
$courtBundle = Get-Content -LiteralPath $courtReceipt -Raw | ConvertFrom-Json
if ((Get-FileHash -LiteralPath $courtBundle.archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $courtBundle.sha256) { throw 'Local archive checksum mismatch' }
$courtRemoteArchive = $courtServer.directory + '/bundle.tar'
Write-Host 'Uploading the verified offline bundle. Keep this PowerShell window open.'
& scp.exe @courtSshOptions $courtBundle.archive ($Target + ':' + $courtRemoteArchive)
if ($LASTEXITCODE -ne 0) { throw 'SCP upload failed; remote installer was not run' }
$courtRemoteCommand = & $courtPython tools/build_campus_offline.py --receiver-path $courtRemoteArchive --receiver-sha $courtBundle.sha256
if ($LASTEXITCODE -ne 0) { throw 'Cannot construct verified receiver command' }
Write-Host 'Server will verify/extract, install only the dedicated venv, then short-test GPU 3.'
Write-Host 'Enter SSH/sudo passwords only in the normal terminal prompts; do not paste them into chat.'
& ssh.exe @courtSshOptions -tt $Target $courtRemoteCommand
if ($LASTEXITCODE -ne 0) { throw 'Remote preparation stopped. Share the final error lines; retain caches.' }
Write-Host 'OFFLINE_DEPLOY_COMMAND_FINISHED'
