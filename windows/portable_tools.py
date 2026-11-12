"""换机时只读定位已有视频和BIT；不下载、安装或猜换测试素材。"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path, PureWindowsPath
import stat
import uuid


KNOWN_FOLDERS = {
    'Desktop': 'B4BFCC3A-DB2C-424C-B029-7FE99A87C641',
    'Videos': '18989B1D-99B5-455B-841C-AB7C74E4DDFC',
    'Documents': 'FDD39AD0-238F-46AF-ADB4-6C85480369C7',
}
BIT_VERSION = [9, 0, 1003]


def known_folder(identifier):
    """使用KnownFolders实际位置，包含OneDrive和管理员配置的目录重定向。"""
    from ctypes import wintypes
    class GUID(ctypes.Structure):
        _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                    ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte*8)]
    guid = GUID.from_buffer_copy(uuid.UUID(identifier).bytes_le)
    api = ctypes.WinDLL('shell32', use_last_error=True)
    query = api.SHGetKnownFolderPath
    query.argtypes = (ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                      ctypes.POINTER(ctypes.c_wchar_p))
    query.restype = ctypes.c_long
    result = ctypes.c_wchar_p()
    status = query(ctypes.byref(guid), 0, None, ctypes.byref(result))
    if status != 0:
        raise OSError(f'KnownFolder查询失败（0x{status & 0xffffffff:08X}）')
    try:
        return Path(result.value)
    finally:
        free = ctypes.WinDLL('ole32').CoTaskMemFree
        free.argtypes = (ctypes.c_void_p,)
        free.restype = None
        free(ctypes.cast(result, ctypes.c_void_p))


def system_drive():
    return os.environ.get('SystemDrive', 'C:').rstrip('/\\').upper()


def on_system_drive(path, drive=None):
    try:
        resolved = Path(path).resolve()
        return resolved.drive.upper() == (drive or system_drive()).rstrip('/\\').upper()
    except (OSError, RuntimeError):
        return False


def video_search_roots():
    """仅当前用户三类已知目录及固定媒体目录，失败也保留明确诊断。"""
    roots, errors = [], []
    for label, identifier in KNOWN_FOLDERS.items():
        try:
            roots.append(known_folder(identifier))
        except OSError as exc:
            errors.append(label+'：'+str(exc))
    roots.append(Path(system_drive()+'/JK808_Windows/media'))
    return roots, errors


def _unique_paths(paths):
    result, seen = [], set()
    for item in paths:
        resolved = Path(item).resolve()
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            result.append(resolved)
    return result


def find_named_video(filename, roots, *, drive=None, max_depth=2, max_entries=5000):
    """限定深度和条目数；不跟随子目录链接，不把未完整扫描当作唯一结果。"""
    found, searched, excluded, errors = [], [], [], []
    for root in _unique_paths(roots):
        if not on_system_drive(root, drive):
            excluded.append(str(root))
            continue
        if not root.is_dir():
            continue
        searched.append(str(root))
        count = 0
        pending = [(root, 0)]
        while pending:
            directory, depth = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        count += 1
                        if count > max_entries:
                            raise OSError('扫描条目达到限制，未继续深入，请明确填写视频完整路径')
                        metadata = entry.stat(follow_symlinks=False)
                        if entry.is_symlink() or getattr(metadata, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if depth < max_depth:
                                pending.append((Path(entry.path), depth+1))
                        elif entry.is_file(follow_symlinks=False) and entry.name.casefold() == filename.casefold():
                            candidate = Path(entry.path).resolve(strict=True)
                            if on_system_drive(candidate, drive):
                                found.append(candidate)
            except OSError as exc:
                errors.append(str(directory)+'：'+str(exc))
                pending.clear()
    return {'candidates': [str(p) for p in _unique_paths(found)], 'searched_roots': searched,
            'excluded_non_system_roots': excluded, 'scan_errors': errors,
            'max_depth': max_depth, 'filename': filename}


def rebind_local_video(local, *, roots=None, drive=None):
    old = str(local.get('file') or '').strip().strip('"')
    if old and Path(old).is_file() and on_system_drive(old, drive):
        return {'passed': True, 'rebound': False, 'file': old}
    filename = PureWindowsPath(old).name
    if not filename or filename in ('.', '..') or not PureWindowsPath(filename).suffix:
        raise RuntimeError('原本地视频文件名未填写，不能猜选其他视频；请填写系统盘视频完整路径')
    root_errors = []
    if roots is None:
        roots, root_errors = video_search_roots()
    report = find_named_video(filename, roots, drive=drive)
    report['scan_errors'].extend(root_errors)
    detail = ('原路径：'+old+'；仅查找同名文件 '+filename+'；已查位置：'+
              ('、'.join(report['searched_roots']) or '无可用系统盘目录'))
    candidates = report['candidates']
    if report['scan_errors']:
        raise RuntimeError(detail+'；未完整检查：'+'；'.join(report['scan_errors'])+
                           '；当前候选：'+('、'.join(candidates) or '无')+'。请填写准确路径，未自动替换')
    if len(candidates) != 1:
        raise RuntimeError(detail+'；'+('找到多个候选：'+'、'.join(candidates) if candidates else '没有找到同名文件')+
                           '。请在config.json的local_video.file填写系统盘上的准确路径，未改用其他视频')
    local['file'] = candidates[0]
    return dict(report, passed=True, rebound=True, old_file=old, file=candidates[0])


def registered_bit_candidates():
    """只读App Paths和BurnInTest卸载登记，不运行注册表内的命令。"""
    import winreg
    result = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            access = winreg.KEY_READ | view
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\bit.exe', 0, access) as key:
                    result.append(Path(os.path.expandvars(str(winreg.QueryValueEx(key, '')[0])).strip('"')))
            except OSError:
                pass
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', 0, access) as parent:
                    for index in range(1024):
                        try:
                            name = winreg.EnumKey(parent, index)
                        except OSError:
                            break
                        try:
                            with winreg.OpenKey(parent, name) as child:
                                label = str(winreg.QueryValueEx(child, 'DisplayName')[0])
                                if 'burnintest' not in label.casefold().replace(' ', ''):
                                    continue
                                location = str(winreg.QueryValueEx(child, 'InstallLocation')[0])
                                if location.strip().strip('"'):
                                    result.append(Path(os.path.expandvars(location).strip('"'))/'bit.exe')
                        except OSError:
                            continue
            except OSError:
                pass
    return result


def bit_installation_candidates():
    roots = [Path(os.environ.get(key, fallback)) for key, fallback in
             (('ProgramFiles', 'C:/Program Files'), ('ProgramFiles(x86)', 'C:/Program Files (x86)'))]
    result = [root/sub/'bit.exe' for root in roots for sub in
              ('BurnInTest', 'BurnInTest V9', 'PassMark/BurnInTest')]
    result.extend(registered_bit_candidates())
    return _unique_paths(result)


def rebind_bit_executable(bit, version_reader):
    """只选择真实的已适配版本；多个合格候选不能猜，也不自动安装/升级。"""
    old = str(bit.get('executable') or '').strip().strip('"')
    current = Path(old) if old else None
    if current is not None and current.is_file():
        version = version_reader(current)
        if list(version[:3]) != BIT_VERSION:
            raise RuntimeError('当前BIT不是已适配的9.0 build1003：'+old+'；实读版本 '+str(version)+'。保留原路径，未安装或升级')
        return {'passed': True, 'rebound': False, 'executable': old, 'version': list(version)}
    existing = [p for p in bit_installation_candidates() if p.is_file()]
    supported, checked = [], []
    for candidate in existing:
        item = {'executable': str(candidate)}
        try:
            version = list(version_reader(candidate))
            item['version'] = version
            if version[:3] == BIT_VERSION:
                supported.append((candidate, version))
        except Exception as exc:
            item['error'] = str(exc)
        checked.append(item)
    detail = '；'.join(item['executable']+'（'+str(item.get('version', item.get('error')))+'）' for item in checked)
    if len(supported) != 1:
        raise RuntimeError('原BIT路径缺失：'+old+'；'+('合格候选不唯一' if supported else '未找到已安装的9.0 build1003')+
                           '；已检查：'+(detail or '标准安装位置与注册表均未找到真实bit.exe')+'。未自动安装或升级')
    candidate, version = supported[0]
    bit['executable'] = str(candidate)
    return {'passed': True, 'rebound': True, 'old_executable': old, 'executable': str(candidate),
            'version': version, 'checked': checked}
