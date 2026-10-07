# stop.ps1 — 停止 DeepAgents ACP 网关（app.py / acp_agent 及相关子进程，释放端口 8000）
# 用法：在项目根目录执行  .\stop.ps1
$ErrorActionPreference = 'SilentlyContinue'
Set-Location $PSScriptRoot

# 递归终止指定进程的所有子进程（深度优先）
function Stop-ChildTree([int]$ParentId) {
    $children = Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $ParentId }
    foreach ($c in $children) {
        Stop-ChildTree $c.ProcessId
        Write-Host "[停止] 终止子进程 PID $($c.ProcessId)"
        Stop-Process -Id $c.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

$stopped = $false

# 1) 按命令行匹配网关相关进程（app.py / acp_agent），先杀其子进程树再杀本体
$targets = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match 'app\.py' -or $_.CommandLine -match 'acp_agent'
}
foreach ($t in $targets) {
    Stop-ChildTree $t.ProcessId
    $cmd = $t.CommandLine
    if ($cmd.Length -gt 80) { $cmd = $cmd.Substring(0, 80) + '...' }
    Write-Host "[停止] 终止网关进程 PID $($t.ProcessId): $cmd"
    Stop-Process -Id $t.ProcessId -Force -ErrorAction SilentlyContinue
    $stopped = $true
}

# 2) 兜底：终止仍占用 8000 端口的监听进程
$listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
foreach ($l in $listener) {
    Write-Host "[停止] 终止 8000 端口监听进程 PID $($l.OwningProcess)"
    Stop-Process -Id $l.OwningProcess -Force -ErrorAction SilentlyContinue
    $stopped = $true
}

Start-Sleep -Seconds 2
if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "[停止] 警告: 端口 8000 仍被占用，请手动检查残留进程。"
    exit 1
} elseif ($stopped) {
    Write-Host "[停止] 网关已停止，端口 8000 已释放。"
} else {
    Write-Host "[停止] 未发现运行中的网关进程。"
}
exit 0
