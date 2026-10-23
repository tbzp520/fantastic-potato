param([switch]$CheckOnly, [ValidateSet('host','receiver')][string]$Role = 'host')
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONIOENCODING = 'utf-8'
$taskRoot = $PSScriptRoot
$taskPython = Join-Path $taskRoot 'runtime\python.exe'
$taskProbe = Join-Path $taskRoot 'runtime_check.py'
$taskProgram = Join-Path $taskRoot 'windows_temperature_test.py'
if (!(Test-Path -LiteralPath $taskPython) -or !(Test-Path -LiteralPath $taskProgram)) {
    Write-Host '位置不对：请把修复包内容放到 Windows 测试程序目录，与 runtime、config.json、统一启动.cmd 放在同一层。'
    exit 2
}
$taskStamp = (Get-Date -Format 'yyyyMMdd_HHmmss') + '_' + ([guid]::NewGuid().ToString('N').Substring(0,6))
$taskReport = Join-Path $taskRoot ('运行库检查\' + $taskStamp)
New-Item -ItemType Directory -Path $taskReport -Force | Out-Null
$taskSteps = [System.Collections.Generic.List[string]]::new()
$taskPassed = $false
$taskRestartRequired = $false
try {
    Write-Host ('正在按角色检查依赖：' + $Role)
    & $taskPython -B $taskProbe --role $Role --output (Join-Path $taskReport '修复前.json')
    $taskProbeExit = $LASTEXITCODE
    if ($taskProbeExit -eq 0) {
        $taskPassed = $true
        $taskSteps.Add('依赖可加载，无需安装运行库。')
    } elseif ($CheckOnly) {
        $taskSteps.Add('只读检查发现依赖不能加载；本次未启动安装程序。')
    } elseif ($Role -eq 'receiver') {
        $taskSteps.Add('接收端依赖未通过；本工具不对接收电脑安装测试主机窗口运行库。请按检查记录核对接收端依赖。')
    } elseif ((Get-Content -LiteralPath (Join-Path $taskReport '修复前.json') -Raw -Encoding UTF8) -match '应用程序控制策略|应用程序控制政策|application control policy|blocked by.*policy') {
        $taskSteps.Add('系统应用控制策略阻止文件加载，重装 VC 运行库不能解决；未启动安装程序，未修改安全设置。')
    } else {
        $taskInstaller = Join-Path $taskRoot 'prerequisites\VC_redist.x64.exe'
        if (!(Test-Path -LiteralPath $taskInstaller)) { throw '缺少微软运行库离线安装包，请完整解压修复包。' }
        $taskExpectedHash = '843068991DAAA1F73AD9F6239BCE4D0F6A07A51F18C37EA2A867E9BECA71295C'
        if ((Get-FileHash -LiteralPath $taskInstaller -Algorithm SHA256).Hash -ne $taskExpectedHash) {
            throw '离线安装包校验不符，请重新解压；未执行安装。'
        }
        Write-Host '将安装/更新微软 Visual C++ x64 运行库；若出现管理员授权，请允许。不会自动重启。'
        $taskInstallerLog = Join-Path $taskReport '微软运行库安装.log'
        $taskInstallerArguments = '/install /passive /norestart /log "' + $taskInstallerLog + '"'
        $taskProcess = Start-Process -FilePath $taskInstaller -ArgumentList $taskInstallerArguments -Verb RunAs -WindowStyle Hidden -Wait -PassThru
        $taskSteps.Add('微软安装程序退出码：' + $taskProcess.ExitCode)
        if ($taskProcess.ExitCode -eq 3010) {
            $taskRestartRequired = $true
            $taskSteps.Add('微软安装程序提示需重启，请保存工作并手动重启后再次检查。')
        } elseif ($taskProcess.ExitCode -notin @(0, 1638)) {
            $taskSteps.Add('安装程序未返回常见成功状态，以后续加载检查和安装日志为准。')
        }
        Write-Host '正在重新检查 win32ui 和窗口自动化依赖……'
        & $taskPython -B $taskProbe --role $Role --output (Join-Path $taskReport '修复后.json')
        $taskPassed = ($LASTEXITCODE -eq 0 -and !$taskRestartRequired -and $taskProcess.ExitCode -in @(0, 1638))
    }
} catch {
    $taskSteps.Add('未完成：' + $_.Exception.Message)
    Write-Host ('未完成：' + $_.Exception.Message)
} finally {
    if ($taskRestartRequired) {
        $taskSteps.Add('最终结果：安装要求重启，本次不进入测试；请手动重启后从U盘再次启动。')
    } elseif ($taskPassed) {
        $taskSteps.Add('最终结果：本机角色所需依赖可加载。可以运行统一启动.cmd。')
    } else {
        $taskSteps.Add('最终结果：依赖尚未通过。请发送本次运行库检查 ZIP，不要反复重装其他软件。')
    }
    $taskSteps.Add('本程序未更改 config.json、COM口、网卡、时钟、样品或测试结果。未运行视频或BIT。')
    $taskSteps | Set-Content -LiteralPath (Join-Path $taskReport '结果.txt') -Encoding UTF8
    $taskZip = Join-Path (Split-Path -Parent $taskReport) ('请发送_运行库检查_' + $taskStamp + '.zip')
    Compress-Archive -LiteralPath $taskReport -DestinationPath $taskZip
    foreach ($taskLine in $taskSteps) { Write-Host $taskLine }
    Write-Host ('检查记录：' + $taskZip)
}
if ($taskRestartRequired) { exit 3010 } elseif ($taskPassed) { exit 0 } else { exit 1 }
