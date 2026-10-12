# start-all.ps1 - Start MooFile knowledge backend (8888) + DeepAgents ACP (8000)
# Usage: run from the deepagents_acp project root:  .\start-all.ps1
# The knowledge backend root is resolved as:
#   MOOFILE_BASE_ROOT_DIR from .env (absolute, or relative to this project)
#   -> otherwise the sibling directory <parent>/moofile_base
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$AcpRoot = $PSScriptRoot

function Get-MbRoot {
    param([string]$Root)
    $envFile = Join-Path $Root '.env'
    if (Test-Path $envFile) {
        $line = Get-Content $envFile -Encoding utf8 | Where-Object { $_ -match '^\s*MOOFILE_BASE_ROOT_DIR\s*=' } | Select-Object -First 1
        if ($line) {
            $val = ($line -split '=', 2)[1].Trim().Trim('"').Trim("'")
            if ($val) { return (Join-Path $Root $val) }
        }
    }
    return (Join-Path (Split-Path $Root -Parent) 'moofile_base')
}

$MbRoot = Get-MbRoot $AcpRoot
Write-Host "[启动] moofile_base 根目录: $MbRoot"

if (-not (Test-Path (Join-Path $MbRoot 'app.py'))) {
    Write-Host "[启动] 错误: 未找到 moofile_base (期望位于 $MbRoot)"
    exit 1
}
if (-not (Test-Path (Join-Path $MbRoot '.venv\Scripts\python.exe'))) {
    Write-Host "[启动] 错误: 未找到 moofile_base 虚拟环境解释器"
    exit 1
}

# --- 1) stop everything (moofile_base first with an exact path match, so the
#        ACP stop.ps1, which matches any 'app.py', cannot kill a stray copy) ---
Write-Host "[启动] 检查并停止旧实例 ..."
$mbTargets = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match [regex]::Escape($MbRoot) -and $_.CommandLine -match 'app\.py'
}
foreach ($t in $mbTargets) {
    Write-Host "[启动] 停止旧 moofile_base PID $($t.ProcessId)"
    Stop-Process -Id $t.ProcessId -Force -ErrorAction SilentlyContinue
}
if (Test-Path (Join-Path $AcpRoot 'stop.ps1')) {
    & (Join-Path $AcpRoot 'stop.ps1')
}
Start-Sleep -Seconds 2

# --- 2) start moofile_base (knowledge backend) ---
Write-Host "[启动] 正在启动 MooFile 知识库 (app.py, :8888) ..."
$env:MOOFILE_DB_DIR = Join-Path $AcpRoot 'db'
$env:MOOFILE_WITH_MCP = 'false'
New-Item -ItemType Directory -Path (Join-Path $MbRoot 'logs') -Force | Out-Null
$mbProc = Start-Process -FilePath (Join-Path $MbRoot '.venv\Scripts\python.exe') `
    -ArgumentList 'app.py' `
    -WorkingDirectory $MbRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $MbRoot 'logs\app.out.log') `
    -RedirectStandardError (Join-Path $MbRoot 'logs\app.err.log') `
    -PassThru

# wait for :8888 health
$ready = $false
for ($i = 1; $i -le 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        $resp = Invoke-WebRequest -Uri 'http://127.0.0.1:8888/api/system/health' -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
}
if (-not $ready) {
    Write-Host "[启动] 错误: moofile_base 未就绪 (:8888)，请查看 logs\app.err.log"
    exit 1
}
Write-Host "[启动] MooFile 知识库已启动: http://127.0.0.1:8888 (PID $($mbProc.Id))"

# --- 3) start DeepAgents ACP ---
if (Test-Path (Join-Path $AcpRoot 'start.ps1')) {
    & (Join-Path $AcpRoot 'start.ps1')
} else {
    Write-Host "[启动] 警告: 未找到 start.ps1，请手动启动 DeepAgents ACP"
}

Write-Host "[启动] 全部完成:"
Write-Host "    DeepAgents ACP : http://127.0.0.1:8000"
Write-Host "    MooFile 知识库  : http://127.0.0.1:8888  (管理页面 /)"
Write-Host "    停止请执行     : .\stop-all.ps1"
