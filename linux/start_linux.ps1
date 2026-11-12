param([switch]$CheckOnly)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
$taskRoot=[IO.Path]::GetFullPath($PSScriptRoot)
$taskReport=[ordered]@{version='2.3.4';root=$taskRoot;administrator=$false;candidates=@();selected=$null;venv_backup=$null;exit_code=$null}
$taskLogDir=Join-Path $taskRoot '启动记录'
try {New-Item -ItemType Directory -Path $taskLogDir -Force | Out-Null} catch {
    $taskLogDir=Join-Path ([IO.Path]::GetTempPath()) 'JK808_Linux_Startup'
    New-Item -ItemType Directory -Path $taskLogDir -Force | Out-Null
}
$taskLog=Join-Path $taskLogDir '最近启动检查.log'
Set-Content -LiteralPath $taskLog -Value ('JK808 Linux v2.3.4 '+(Get-Date).ToString('o')) -Encoding UTF8
function Say([string]$Message) {
    Write-Host $Message
    Add-Content -LiteralPath $taskLog -Value $Message -Encoding UTF8
}
function Save-Report {
    $taskReport | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath (Join-Path $taskLogDir '最近启动检查.json') -Encoding UTF8
}
function Probe-Process([string]$Executable,[string]$Arguments) {
    $info=New-Object Diagnostics.ProcessStartInfo
    $info.FileName=$Executable;$info.Arguments=$Arguments;$info.UseShellExecute=$false
    $info.CreateNoWindow=$true;$info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
    $process=New-Object Diagnostics.Process;$process.StartInfo=$info
    try {
        [void]$process.Start()
        $stdout=$process.StandardOutput.ReadToEndAsync();$stderr=$process.StandardError.ReadToEndAsync()
        if(-not $process.WaitForExit(12000)) {
            $process.Kill();$process.WaitForExit()
            return [pscustomobject]@{code=-1;stdout='';stderr='探测超过12秒，已停止本次探测。'}
        }
        return [pscustomobject]@{code=$process.ExitCode;stdout=$stdout.Result;stderr=$stderr.Result}
    } finally {$process.Dispose()}
}
function Probe-Python([string]$Path,[string]$Source) {
    $result=[ordered]@{path=$Path;source=$Source;passed=$false;code=$null;details=$null;error=''}
    try {
        if($Path -match '\\WindowsApps\\') {throw '跳过Windows商店别名，不把别名当作已安装的Python。'}
        $code='import sys,struct,json,importlib.util;print(json.dumps(dict(version=list(sys.version_info[:3]),bits=struct.calcsize(''P'')*8,executable=sys.executable,venv=bool(importlib.util.find_spec(''venv'')),ensurepip=bool(importlib.util.find_spec(''ensurepip'')))))'
        $probe=Probe-Process $Path ('-I -B -c "'+$code+'"')
        $result.code=$probe.code
        if($probe.code -ne 0) {throw ('Python探测失败：'+$probe.stderr.Trim())}
        $details=$probe.stdout.Trim() | ConvertFrom-Json
        $result.details=$details
        if($details.version.Count -lt 2 -or $details.version[0] -ne 3 -or $details.version[1] -ne 12 -or $details.bits -ne 64) {throw '需要Python 3.12 x64，已跳过其他版本/位数。'}
        $result.passed=$true
    } catch {$result.error=$_.Exception.Message}
    $taskReport.candidates+=,[pscustomobject]$result
    if($result.passed) {Say ('Python检查通过：'+$Path)} else {Say ('跳过：'+$Path+'；'+$result.error)}
    return [pscustomobject]$result
}
function Backup-InvalidVenv {
    $source=[IO.Path]::GetFullPath((Join-Path $taskRoot '.venv'))
    if(-not (Test-Path -LiteralPath $source)) {return}
    $target=[IO.Path]::GetFullPath((Join-Path $taskRoot ('.venv_失效备份_'+(Get-Date).ToString('yyyyMMdd_HHmmss_fff'))))
    $prefix=$taskRoot.TrimEnd('\')+'\'
    if(-not $source.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase) -or -not $target.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) {throw '失效环境备份路径不在本程序目录，未移动任何环境。'}
    $item=Get-Item -LiteralPath $source -Force
    if($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {throw '.venv是目录链接，请先核对链接环境；本程序不会移动包外环境。'}
    Move-Item -LiteralPath $source -Destination $target
    $taskReport.venv_backup=$target
    Say ('旧.venv已失效，保留备份：'+$target+'；首次部署将自动重建本目录环境。')
}
try {
    Set-Location -LiteralPath $taskRoot
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    $principal=New-Object Security.Principal.WindowsPrincipal($identity)
    $taskReport.administrator=$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    Say ('程序目录：'+$taskRoot)
    Say ('管理员权限：'+$taskReport.administrator)
    if(-not (Test-Path -LiteralPath (Join-Path $taskRoot 'dual_linux.py') -PathType Leaf)) {throw '缺少dual_linux.py，请完整解压程序包后再启动。'}
    $local=Join-Path $taskRoot '.venv\Scripts\python.exe'
    $localInvalid=Test-Path -LiteralPath (Join-Path $taskRoot '.venv')
    if(Test-Path -LiteralPath $local -PathType Leaf) {
        $probe=Probe-Python $local '本目录环境'
        if($probe.passed) {$taskReport.selected=$probe;$localInvalid=$false}
    }
    if(-not $taskReport.selected) {
        $candidates=New-Object 'Collections.Generic.List[string]'
        foreach($key in @('Registry::HKEY_CURRENT_USER\Software\Python\PythonCore\3.12\InstallPath','Registry::HKEY_LOCAL_MACHINE\Software\Python\PythonCore\3.12\InstallPath')) {
            if(Test-Path -LiteralPath $key) {
                $value=(Get-Item -LiteralPath $key).GetValue('')
                if($value) {$candidates.Add((Join-Path $value 'python.exe'))}
            }
        }
        foreach($path in @((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),(Join-Path $env:ProgramFiles 'Python312\python.exe'),(Join-Path $env:ProgramFiles 'Python3.12\python.exe'),(Join-Path $env:SystemDrive 'Python312\python.exe'))) {$candidates.Add($path)}
        foreach($command in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) {$candidates.Add($command.Source)}
        $launcher=Get-Command py.exe -ErrorAction SilentlyContinue
        if($launcher) {
            $listing=Probe-Process $launcher.Source '-0p'
            foreach($line in ($listing.stdout -split '\r?\n')) {
                if($line -match '([A-Za-z]:\\.+?python(?:3(?:\.12)?)?\.exe)\s*$') {$candidates.Add($Matches[1])}
            }
        }
        # 使用其他管理员凭据时，仍可定位原登录账号安装的3.12；只查这一固定软件路径。
        $profiles=Join-Path $env:SystemDrive 'Users'
        foreach($profile in @(Get-ChildItem -LiteralPath $profiles -Directory -ErrorAction SilentlyContinue)) {
            $candidates.Add((Join-Path $profile.FullName 'AppData\Local\Programs\Python\Python312\python.exe'))
        }
        $seen=@{}
        foreach($path in $candidates) {
            if(-not $path -or $seen.ContainsKey($path.ToLowerInvariant()) -or -not (Test-Path -LiteralPath $path -PathType Leaf)) {continue}
            $seen[$path.ToLowerInvariant()]=$true
            $probe=Probe-Python $path '系统已安装Python'
            if($probe.passed -and $probe.details.venv -and $probe.details.ensurepip) {$taskReport.selected=$probe;break}
        }
    }
    if(-not $taskReport.selected) {throw '没有找到可运行的Python 3.12 x64。py存在不代表3.12已安装；请安装3.12 x64，或恢复本目录的有效.venv。详细探测结果已保存。'}
    if($localInvalid -and -not $CheckOnly) {Backup-InvalidVenv}
    $taskReport.requires_venv_repair=[bool]$localInvalid
    Save-Report
    if($CheckOnly) {Say '启动检查通过；未进入测试、未修改环境或配置。';exit 0}
    Say ('启动解释器：'+$taskReport.selected.path)
    Say '下面进入单机/双机菜单。若需校时，请保持管理员运行。'
    # 必须继承真实控制台输入，不能使用Tee、capture_output或重定向stdin。
    & $taskReport.selected.path -B -u (Join-Path $taskRoot 'dual_linux.py')
    $taskCode=$LASTEXITCODE
    if($null -eq $taskCode) {$taskCode=0}
    $taskReport.exit_code=$taskCode
    Save-Report
    if($taskCode -ne 0) {
        $hex=[BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$taskCode),0).ToString('X8')
        Say ('程序未正常结束，退出码：'+$taskCode+'（0x'+$hex+'）。请保留本窗口报错及启动记录。')
    } else {Say '程序已退出。'}
    Say ('启动检查日志：'+$taskLog)
    exit $taskCode
} catch {
    $taskReport.error=$_.Exception.Message;$taskReport.exit_code=2
    Say ('启动未完成：'+$_.Exception.Message)
    Say ('启动检查日志：'+$taskLog)
    Save-Report
    exit 2
}
