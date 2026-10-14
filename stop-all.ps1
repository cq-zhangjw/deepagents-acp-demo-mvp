# stop-all.ps1 - Stop DeepAgents ACP (8000) + MooFile knowledge backend (8888)
# Usage: run from the deepagents_acp project root:  .\stop-all.ps1
# Resolves the moofile_base root the same way as start-all.ps1:
#   MOOFILE_BASE_ROOT_DIR from .env -> sibling directory fallback
$ErrorActionPreference = 'SilentlyContinue'
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

# --- 1) stop moofile_base with an exact path match (kills the backend process
#        and the worker subprocesses spawned by app.py) ---
Write-Host "[停止] 停止 MooFile 知识库 (:8888) ..."
$mbTargets = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match [regex]::Escape($MbRoot) -and $_.CommandLine -match 'app\.py'
}
foreach ($t in $mbTargets) {
    Write-Host "[停止] 终止 moofile_base PID $($t.ProcessId)"
    Stop-Process -Id $t.ProcessId -Force -ErrorAction SilentlyContinue
}
$listener = Get-NetTCPConnection -LocalPort 8888 -State Listen -ErrorAction SilentlyContinue
foreach ($l in $listener) {
    Write-Host "[停止] 释放端口 8888 (PID $($l.OwningProcess))"
    Stop-Process -Id $l.OwningProcess -Force -ErrorAction SilentlyContinue
}

# --- 2) stop DeepAgents ACP (its stop.ps1 kills app.py/acp_agent + child tree) ---
if (Test-Path (Join-Path $AcpRoot 'stop.ps1')) {
    Write-Host "[停止] 停止 DeepAgents ACP (:8000) ..."
    & (Join-Path $AcpRoot 'stop.ps1') -NoWait
}

Write-Host "[停止] 全部服务已停止。"
