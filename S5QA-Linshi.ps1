<#
Linshi SOCKS5 QA, same Windows->Dutch VPS SSH workflow as successful Watchdog v4.
Only TWO QA files are needed in linshi: this PS1 and s5qa-linshi.sh.
Defaults are intentionally the original, previously tested machine and key.
Runs staged, SHA-pinned source installation only when explicitly -Mode patch.
#>
[CmdletBinding()]
param(
    [ValidateSet('patch','test','check','logs','cleanup')]
    [string]$Mode='patch',
    [string]$ProxyHost='162.211.231.219',
    [int]$SshPort=22,
    [string]$KeyFile=''
)
$ErrorActionPreference='Stop'
if (-not $KeyFile) { $KeyFile=Join-Path $env:USERPROFILE '.ssh\vps_root_ed25519' }
$RemoteUser="root@$ProxyHost"
$RemoteDir='/root/s5qa-linshi'
$RemoteTool="$RemoteDir/s5qa-linshi.sh"
$QaUrl='https://raw.githubusercontent.com/liucong552-art/linshi/refs/heads/main/s5qa-linshi.sh'
# SHA256 of the exact s5qa-linshi.sh shipped with this launcher.
$QaHash='728cbdaa0634057414b67faaad2a7d79fcff1dd699c1fb03fc9c8e5e15111fb5'
$SshArgs=@('-i',$KeyFile,'-p',"$SshPort",'-o','BatchMode=yes','-o','ConnectTimeout=10',
           '-o','ServerAliveInterval=10','-o','ServerAliveCountMax=2','-o','StrictHostKeyChecking=yes',$RemoteUser)
$ScpArgs=@('-i',$KeyFile,'-P',"$SshPort",'-o','BatchMode=yes','-o','ConnectTimeout=10',
           '-o','StrictHostKeyChecking=yes')
function Remote-Run {
    param([string]$Command)
    & ssh.exe @SshArgs $Command | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "SSH command failed, code=$LASTEXITCODE" }
}
function Remote-Read {
    param([string]$Command)
    $output=@(& ssh.exe @SshArgs $Command)
    if ($LASTEXITCODE -ne 0) { throw "SSH read failed, code=$LASTEXITCODE" }
    return ($output -join "`n")
}
function Upload-Qa {
    $tmp=Join-Path ([System.IO.Path]::GetTempPath()) ('s5qa-linshi-'+[guid]::NewGuid().ToString('N')+'.sh')
    try {
        & curl.exe -q -fLsS --retry 3 --connect-timeout 10 --max-time 90 -o $tmp $QaUrl
        if ($LASTEXITCODE -ne 0) { throw 'Cannot download s5qa-linshi.sh from linshi' }
        $actual=(Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $QaHash) {throw 'QA runner SHA256 mismatch; refusing upload'}
        Remote-Run "install -d -m 0700 $RemoteDir"
        & scp.exe @ScpArgs $tmp "${RemoteUser}:$RemoteTool" | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'SCP upload failed' }
        Remote-Run "chmod 0700 $RemoteTool; test `$(sha256sum $RemoteTool | cut -d' ' -f1) = $QaHash"
        Write-Host 'PASS: verified linshi QA runner on Dutch VPS' -ForegroundColor Green
    } finally { Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue }
}
function Test-PublicProxy {
    param([string]$AccountId,[int]$Port,[string]$Instance)
    if ($AccountId -notmatch '^qa[0-9]{14}[0-9a-f]{4}$') {throw 'Invalid test account ID'}
    $entry=Remote-Read "socks5 show $AccountId --credentials" | ConvertFrom-Json
    if ($entry.id -ne $AccountId -or [int]$entry.port -ne $Port -or $entry.instance -ne $Instance) {
        throw 'Test account identity changed; refusing proxy test'
    }
    if (-not $entry.username -or -not $entry.password) {throw 'No test account credentials'}
    # Credentials are used only in memory, never printed to logs.
    $auth='{0}:{1}' -f $entry.username,$entry.password
    $uri="socks5h://${ProxyHost}:$Port"
    $result=@(& curl.exe -q -4 -fsS --noproxy localhost --connect-timeout 8 --max-time 20 `
        --proxy $uri --proxy-user $auth 'https://api.ipify.org')
    if ($LASTEXITCODE -ne 0) {throw "Real Windows TCP SOCKS proxy request failed, curl=$LASTEXITCODE"}
    $seen=($result -join '').Trim()
    if ($seen -ne $ProxyHost) {throw "Unexpected SOCKS egress: $seen (expected $ProxyHost)"}
    Write-Host "PASS: Windows external SOCKS5 CONNECT, exit IP=$seen" -ForegroundColor Green
}
if (-not (Test-Path -LiteralPath $KeyFile -PathType Leaf)) {throw "SSH private key not found: $KeyFile"}
foreach($exe in @('ssh.exe','scp.exe','curl.exe')) {
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) {throw "Missing Windows utility: $exe"}
}
Write-Host "`n===== linshi QA | $Mode | Dutch VPS $ProxyHost =====" -ForegroundColor Cyan
Write-Host 'Original SSH key, IP and QA port 41006 already configured; no credentials need input.'
switch($Mode) {
    'check' {
        Upload-Qa
        Remote-Run "bash $RemoteTool check"
        Write-Host 'PASS: exact linshi program SHA256, Python/Bash syntax. No installation performed.' -ForegroundColor Green
    }
    'patch' {
        Upload-Qa
        Remote-Run "bash $RemoteTool patch"
        Write-Host 'PASS: linshi candidate deployed. NEXT: .\S5QA-Linshi.ps1 -Mode test' -ForegroundColor Green
    }
    'test' {
        Upload-Qa
        Remote-Run "bash $RemoteTool inspect"
        $prepared=$false
        $failed=$false
        try {
            Remote-Run "bash $RemoteTool prepare"
            $prepared=$true
            Write-Host 'Intentionally stopping ONLY the isolated qa* account (as in original WD4 test).'
            & ssh.exe @SshArgs "bash $RemoteTool stop" | Out-Host
            if ($LASTEXITCODE -ne 0) {
                Write-Host 'Stop SSH session returned an error; using new SSH connections to probe state.' -ForegroundColor Yellow
            }
            $passed=$false
            $best=$null
            for($i=1;$i -le 18;$i++) {
                Start-Sleep -Seconds 6
                try {
                    $json=Remote-Read "bash $RemoteTool probe"
                    $probe=$json | ConvertFrom-Json
                    $best=$probe
                    Write-Host ("Probe {0}/18: active={1} guard={2} listen={3} recovery={4}s" -f `
                        $i,$probe.active,$probe.guard,$probe.listening,$probe.recovery_seconds)
                    if($probe.passed) { $passed=$true;break }
                    if($null -ne $probe.recovery_seconds -and [double]$probe.recovery_seconds -gt 90) {break}
                } catch {
                    Write-Host "Probe SSH retry: $($_.Exception.Message)" -ForegroundColor Yellow
                }
            }
            Write-Host '===== Watchdog logs ====='
            Remote-Run "bash $RemoteTool logs"
            if (-not $passed) {throw 'Watchdog not proven to recover within original 90-second acceptance threshold'}
            Remote-Run "bash $RemoteTool validate"
            $meta=Remote-Read "cat $RemoteDir/account.json" | ConvertFrom-Json
            Test-PublicProxy -AccountId $meta.id -Port ([int]$meta.port) -Instance $meta.instance
            Write-Host "PASS: WD4-style real QA ($($best.recovery_seconds)s), ACL/auth/quota/traffic and Windows external proxy." -ForegroundColor Green
        } catch {
            $failed=$true
            Write-Host "FAIL: $($_.Exception.Message)" -ForegroundColor Red
        } finally {
            if ($prepared) {
                try {Remote-Run "bash $RemoteTool cleanup"}
                catch { $failed=$true;Write-Host "WARNING: QA cleanup failed; use -Mode cleanup. $($_.Exception.Message)" -ForegroundColor Red }
            }
        }
        if ($failed) {throw 'Not yet accepted: send the complete output to ChatGPT; do not publish to zuizhongheji'}
    }
    'logs' { Remote-Run "bash $RemoteTool logs" }
    'cleanup' { Remote-Run "bash $RemoteTool cleanup" }
}
