$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
$taskRoot=$PSScriptRoot
$taskFailures=New-Object 'Collections.Generic.List[string]'
$taskFiles=@()
try {
    if(-not [Environment]::Is64BitOperatingSystem){throw '本程序包需要64位Windows。'}
    $taskExpected=@{}
    foreach($taskLine in (Get-Content -LiteralPath (Join-Path $taskRoot 'FILES.sha256') -Encoding UTF8)){
        if($taskLine -match '^([a-fA-F0-9]{64})[ \t]+\*?(.+)$'){$taskExpected[$Matches[2].Replace('\','/')]=$Matches[1].ToLowerInvariant()}
    }
    foreach($taskName in @('runtime/python.exe','runtime/python312.dll','runtime/python3.dll','runtime/vcruntime140.dll','runtime/vcruntime140_1.dll','runtime/python312._pth')){
        $taskPath=Join-Path $taskRoot $taskName.Replace('/','\')
        $taskActual=''
        if(Test-Path -LiteralPath $taskPath -PathType Leaf){$taskActual=(Get-FileHash -LiteralPath $taskPath -Algorithm SHA256).Hash.ToLowerInvariant()}
        $taskPassed=$taskActual -and $taskExpected[$taskName] -and $taskActual -eq $taskExpected[$taskName]
        if($taskName -eq 'runtime/python.exe'){$taskPassed=$taskPassed -and $taskActual -eq '372c2eae555b344520bf147be0096e009069aeca4e7f78d6aecea6d53158056a'}
        $taskFiles+=[pscustomobject]@{file=$taskName;passed=[bool]$taskPassed;sha256=$taskActual;expected_sha256=$taskExpected[$taskName]}
        if(-not $taskPassed){$taskFailures.Add($taskName)}
    }
} catch {$taskFailures.Add($_.Exception.Message)}
try {
    $taskRecords=Join-Path $taskRoot '部署记录'
    New-Item -ItemType Directory -Path $taskRecords -Force | Out-Null
    [pscustomobject]@{passed=($taskFailures.Count -eq 0);files=$taskFiles;errors=@($taskFailures);time=(Get-Date).ToString('o')} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $taskRecords '启动运行文件校验.json') -Encoding UTF8
} catch {Write-Host ('保存校验记录失败：'+$_.Exception.Message)}
if($taskFailures.Count){
    Write-Host '启动运行文件校验未通过，尚未执行包内Python：'
    foreach($taskFailure in $taskFailures){Write-Host ('  '+$taskFailure)}
    Write-Host '请将完整包重新解压到新目录，再复制原config.json；不要混用其他Python启动文件。'
    exit 2
}
Write-Host '启动运行文件校验通过。'
exit 0
