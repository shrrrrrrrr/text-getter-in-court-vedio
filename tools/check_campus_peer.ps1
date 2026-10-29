param([Parameter(Mandatory=$true)][string]$Url,
      [int]$TimeoutSeconds=600,
      [string]$ReportPath)
$ErrorActionPreference='Stop'
$courtUri=[Uri]$Url
$courtAddress=$null
if ($courtUri.Scheme -ne 'http' -or $courtUri.UserInfo -or $courtUri.Query -or $courtUri.Fragment -or $courtUri.AbsolutePath -ne '/' -or -not [Net.IPAddress]::TryParse($courtUri.Host,[ref]$courtAddress)) { throw 'Use an explicit campus HTTP IP and port without credentials or query parameters' }
if ($TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 1800) { throw 'Invalid wait duration' }
if (-not $ReportPath) { $ReportPath=Join-Path (Split-Path -Parent $PSScriptRoot) '.state/deploy/campus-peer-check.local.json' }
Add-Type -AssemblyName System.Net.Http
$courtHandler=[Net.Http.HttpClientHandler]::new()
$courtHandler.UseProxy=$false
$courtHandler.UseCookies=$false
$courtHandler.AllowAutoRedirect=$false
$courtClient=[Net.Http.HttpClient]::new($courtHandler)
$courtClient.Timeout=[TimeSpan]::FromSeconds(3)
$courtDeadline=[DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
$courtEndpoint=$courtUri.AbsoluteUri.TrimEnd('/')+'/api/auth/session'
$courtLastError='Service has not started'
$courtNextNotice=[DateTime]::UtcNow
try {
    while ([DateTime]::UtcNow -lt $courtDeadline) {
        try {
            $courtPayload=$courtClient.GetStringAsync($courtEndpoint).GetAwaiter().GetResult() | ConvertFrom-Json
            if ($courtPayload.required -eq $true -and $courtPayload.authenticated -eq $false -and $courtPayload.campus_http -eq $true) {
                $courtRecord=@{status='passed'; checked_at=[DateTime]::UtcNow.ToString('o'); url=$Url; access_required=$true; credentials_sent=$false}
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ReportPath) | Out-Null
                [IO.File]::WriteAllText($ReportPath,($courtRecord | ConvertTo-Json),[Text.UTF8Encoding]::new($false))
                Write-Host 'CAMPUS_PEER_HTTP_OK'
                Write-Host ('Open in your browser: '+$Url)
                exit 0
            }
            $courtLastError='The endpoint did not confirm protected campus mode'
        } catch { $courtLastError=$_.Exception.GetBaseException().GetType().Name }
        if ([DateTime]::UtcNow -ge $courtNextNotice) {
            Write-Host 'Waiting for the server installation and protected campus listener. Keep this window open.'
            $courtNextNotice=[DateTime]::UtcNow.AddSeconds(30)
        }
        Start-Sleep -Seconds 2
    }
    $courtRecord=@{status='not_verified'; checked_at=[DateTime]::UtcNow.ToString('o'); url=$Url; error=$courtLastError; credentials_sent=$false}
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ReportPath) | Out-Null
    [IO.File]::WriteAllText($ReportPath,($courtRecord | ConvertTo-Json),[Text.UTF8Encoding]::new($false))
    Write-Host 'CAMPUS_PEER_ACCESS_NOT_VERIFIED. The uploaded package is retained; do not upload it again.'
    exit 1
} finally { $courtClient.Dispose(); $courtHandler.Dispose() }
