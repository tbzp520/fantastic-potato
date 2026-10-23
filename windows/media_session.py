"""只读Windows媒体会话。无需安装软件，不依赖播放器进度条是否显示。"""
import base64
import ctypes
from ctypes import wintypes
import json
import os
import subprocess


def process_app_id(pid):
    """读取进程的真实AppUserModelId；普通窗口宿主没有应用身份时返回空。"""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetApplicationUserModelId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
    kernel.GetApplicationUserModelId.restype = wintypes.LONG
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        length = wintypes.UINT(0)
        code = kernel.GetApplicationUserModelId(handle, ctypes.byref(length), None)
        if code == 15703: return ''  # APPMODEL_ERROR_NO_APPLICATION，例如ApplicationFrameHost。
        if code != 122 or not 0 < length.value <= 4096:
            raise OSError(code, '不能读取播放器进程应用身份')
        value = ctypes.create_unicode_buffer(length.value)
        code = kernel.GetApplicationUserModelId(handle, ctypes.byref(length), value)
        if code: raise OSError(code, '读取播放器应用身份失败')
        return value.value
    finally:
        kernel.CloseHandle(handle)


def window_app_ids(handle, expected_pid):
    """UWP播放器外框和内容可能分属不同进程；只检查本轮窗口及其子窗口。"""
    import win32gui
    import win32process
    if win32process.GetWindowThreadProcessId(handle)[1] != expected_pid:
        raise RuntimeError('本轮播放器窗口已改变，不能借用其他窗口的媒体会话')
    pids = {expected_pid}
    win32gui.EnumChildWindows(handle, lambda child, _: pids.add(win32process.GetWindowThreadProcessId(child)[1]), None)
    ids, errors = set(), []
    for pid in sorted(pids):
        try:
            app_id = process_app_id(pid)
            if app_id: ids.add(app_id)
        except OSError as exc:
            errors.append(str(exc))
    if win32process.GetWindowThreadProcessId(handle)[1] != expected_pid:
        raise RuntimeError('读取期间播放器窗口已改变')
    if not ids:
        raise RuntimeError('本轮播放器尚未提供媒体应用身份：'+'；'.join(errors))
    return sorted(ids)


MEDIA_SNAPSHOT_SCRIPT = r'''
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
$wanted = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__APP_IDS__')) | ConvertFrom-Json
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$managerType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
$propertiesType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetGenericArguments().Count -eq 1 -and
    $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Await-WinRT($operation, [Type]$resultType) {
    $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
    if (-not $task.Wait(2500)) { throw 'Windows media API timed out' }
    return $task.GetAwaiter().GetResult()
}
$manager = Await-WinRT ($managerType::RequestAsync()) $managerType
$items = @()
foreach ($session in $manager.GetSessions()) {
    $appId = [string]$session.SourceAppUserModelId
    if ($appId -notin $wanted) { continue }
    try {
        $media = Await-WinRT ($session.TryGetMediaPropertiesAsync()) $propertiesType
        $playback = $session.GetPlaybackInfo()
        $entry = @{
            app_id=$appId; title=[string]$media.Title; subtitle=[string]$media.Subtitle
            status=[string]$playback.PlaybackStatus
        }
        if (__INCLUDE_TIMELINE__) {
            $timeline = $session.GetTimelineProperties()
            $entry.position_s=$timeline.Position.TotalSeconds
            $entry.start_s=$timeline.StartTime.TotalSeconds
            $entry.end_s=$timeline.EndTime.TotalSeconds
            $entry.updated_epoch=([DateTimeOffset]$timeline.LastUpdatedTime).ToUnixTimeMilliseconds()/1000.0
        }
        $items += $entry
    } catch { $items += @{app_id=$appId; error=$_.Exception.Message} }
}
@{sessions=@($items)} | ConvertTo-Json -Depth 5 -Compress
'''


def read_media_sessions(app_ids, include_timeline=True):
    """必须明确给出本轮窗口的应用身份；不使用全局当前媒体会话。"""
    if not app_ids or any(not isinstance(value, str) or not value for value in app_ids):
        raise ValueError('必须提供本轮播放器应用身份')
    token = base64.b64encode(json.dumps(app_ids).encode('utf-8')).decode('ascii')
    script = MEDIA_SNAPSHOT_SCRIPT.replace('__APP_IDS__', token).replace(
        '__INCLUDE_TIMELINE__', '$true' if include_timeline else '$false')
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    # PowerShell 7不支持这种WinRT投影，固定使用系统Windows PowerShell 5.1。
    executable = os.path.join(os.environ.get('SystemRoot', 'C:/Windows'), 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    process = subprocess.run([executable, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
        capture_output=True, timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if process.returncode:
        raise RuntimeError('Windows媒体会话读取失败：'+process.stderr.decode('utf-8-sig', errors='replace')[:1200])
    result = json.loads(process.stdout.decode('utf-8-sig'))
    if not isinstance(result.get('sessions'), list):
        raise RuntimeError('Windows媒体会话返回格式错误')
    return result['sessions']
