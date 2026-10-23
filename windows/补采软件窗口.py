"""在 v0.2.4 程序目录运行：还原目标窗口，分别采集 UIA 与 Win32 控件。

不启动负载，不修改配置、网卡或时钟。每个窗口单独采集并设置超时，
缺项和失败也打包；收到文件不代表正式自动化已经验证通过。
"""
import argparse
import ctypes
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parent
PROGRAMS = ('HWiNFO', 'BurnInTest', '播放器')


def save_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8-sig')


def read_config():
    path = ROOT / 'config.json'
    return json.loads(path.read_text('utf-8-sig')) if path.is_file() else {}


def rectangle_values(window):
    rect = window.rectangle()
    return [rect.left, rect.top, rect.right, rect.bottom]


def usable_rectangle(rect):
    # 负坐标可能属于左侧扩展屏；仅排除最小化的特殊坐标和零面积。
    left, top, right, bottom = rect
    return right > left and bottom > top and left > -30000 and top > -30000


def export_controls(handle, backend, destination, expected_pid):
    """只读取该句柄所属的控件树；不依据模糊标题选择其他窗口。"""
    from pywinauto import Desktop
    root = Desktop(backend=backend).window(handle=handle).wrapper_object()
    if root.element_info.process_id != expected_pid:
        raise RuntimeError('窗口所属进程已变化，请重新采集')
    records = []
    queue = [(root, None, 0)]
    errors = []
    truncated = False
    while queue:
        if len(records) >= 2500:
            truncated = True
            break
        control, parent, depth = queue.pop(0)
        index = len(records)
        record = {'index': index, 'parent': parent, 'depth': depth}
        for field, getter in (
            ('text', control.window_text),
            ('class_name', control.class_name),
            ('rectangle', lambda: rectangle_values(control)),
            ('visible', control.is_visible),
            ('enabled', control.is_enabled),
            ('control_type', lambda: control.element_info.control_type),
            ('automation_id', lambda: control.element_info.automation_id),
            ('control_id', control.control_id),
        ):
            try:
                record[field] = getter()
            except Exception as exc:
                record[field + '_error'] = str(exc)
        records.append(record)
        # 设置窗口需要当前值/勾选状态，只有控件标题无法还原真实配置。
        kind = record.get('control_type') or record.get('class_name')
        value_methods = {'CheckBox': 'get_toggle_state', 'RadioButton': 'is_selected',
                         'ComboBox': 'selected_text', 'Edit': 'get_value',
                         'Slider': 'value'}
        method = value_methods.get(kind)
        if backend == 'win32':
            method = {'Button': 'get_check_state', 'ComboBox': 'selected_text',
                      'Edit': 'window_text'}.get(record.get('class_name'))
        if method:
            try:
                record['value'] = getattr(control, method)()
            except Exception as exc:
                record['value_error'] = str(exc)
        try:
            children = control.children()
            if depth >= 12:
                truncated = truncated or bool(children)
            else:
                queue.extend((child, index, depth + 1) for child in children)
        except Exception as exc:
            errors.append({'index': index, 'error': str(exc)})
    result = {'backend': backend, 'handle': handle, 'controls': records,
              'child_count': max(0, len(records) - 1), 'truncated': truncated,
              'errors': errors}
    # 设置页的自绘控件有时只有标题。保留该窗口截图，便于核对值；不截整个桌面。
    if backend == 'win32':
        try:
            if root.is_visible() and usable_rectangle(rectangle_values(root)):
                picture = Path(destination).with_suffix('.png')
                root.capture_as_image().save(picture)
                result['screenshot'] = picture.name
        except Exception as exc:
            result['screenshot_error'] = str(exc)
    save_json(destination, result)
    lines = [f'{backend} 窗口句柄 {handle}；子控件 {result["child_count"]} 个']
    for item in records:
        lines.append('  ' * item['depth'] + json.dumps(item, ensure_ascii=False))
    Path(destination).with_suffix('.txt').write_text('\n'.join(lines), 'utf-8-sig')


def collect_one_backend(handle, backend, destination, expected_pid):
    """单窗口卡住时最多等待 45 秒，继续采集其他窗口。"""
    command = [sys.executable, '-B', '-u', str(Path(__file__).resolve()),
               '--capture', str(handle), '--backend', backend,
               '--expected-pid', str(expected_pid),
               '--output', str(destination)]
    try:
        process = subprocess.run(command, capture_output=True, timeout=45,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if process.returncode:
            raise RuntimeError(process.stderr.decode('utf-8', errors='replace')[-4000:])
        data = json.loads(destination.read_text('utf-8-sig'))
        return {'backend': backend, 'ok': True, 'file': destination.name,
                'child_count': data['child_count'], 'truncated': data['truncated'],
                'errors': data['errors']}
    except Exception as exc:
        message = f'{type(exc).__name__}: {exc}'
        destination.with_suffix('.失败.txt').write_text(message, 'utf-8-sig')
        return {'backend': backend, 'ok': False, 'error': message}


def collect_windows(folder, config):
    from pywinauto import Desktop
    from windows_temperature_test import process_executable_name, inspection_software_group
    # 使用原生句柄枚举，避免同名窗口被 UIA 匹配到另一个窗口。
    desktop = Desktop(backend='win32')
    records, skipped, unreadable = [], [], []
    process_names = {}
    seen = set()
    for window in desktop.windows(visible_only=False):
        try:
            handle = window.handle
            if handle in seen:
                continue
            seen.add(handle)
            pid = window.element_info.process_id
            title = window.window_text()
            if pid not in process_names:
                try:
                    process_names[pid] = process_executable_name(pid)
                except OSError as exc:
                    process_names[pid] = ''
                    unreadable.append({'pid': pid, 'error': str(exc)})
            group = inspection_software_group(process_names[pid], title, config)
            if group is None:
                continue
            if not window.is_visible():
                skipped.append({'program': group, 'handle': handle, 'title': title,
                                'reason': '隐藏窗口；请从任务栏或托盘打开实际软件界面'})
                continue
            # 隐藏的内部窗口不强行显示，只还原已经打开但被最小化的窗口。
            record = {'program': group, 'title': title, 'handle': handle, 'pid': pid,
                      'executable': process_names[pid], 'before': rectangle_values(window),
                      'was_minimized': window.is_minimized(), 'backends': []}
            records.append(record)
            if record['was_minimized']:
                window.restore()
                time.sleep(1)
            try:
                window.set_focus()
                time.sleep(0.5)
            except Exception as exc:
                record['focus_error'] = str(exc)
            record['after'] = rectangle_values(window)
            if not usable_rectangle(record['after']):
                record['error'] = '窗口仍无有效显示区域，请手动还原后补采'
                continue
            print(f'正在采集 {group}：{title}', flush=True)
            for backend in ('uia', 'win32'):
                destination = folder / f'{group}_{handle}_{backend}.json'
                record['backends'].append(collect_one_backend(handle, backend, destination, pid))
        except Exception as exc:
            skipped.append({'reason': f'窗口已关闭或读取失败：{type(exc).__name__}: {exc}'})
    return {'windows': records, 'skipped_windows': skipped, 'unreadable_processes': unreadable}


def summarize(result):
    """区分找到标题、读到子控件、读到 Sensors，避免把空树当作资料齐全。"""
    missing = []
    for group in PROGRAMS:
        windows = [w for w in result['windows'] if w['program'] == group]
        if not windows:
            missing.append(f'{group}：未采集到可见窗口')
        elif not any(b.get('ok') and b.get('child_count', 0) > 0
                     for w in windows for b in w.get('backends', [])):
            missing.append(f'{group}：只有标题或采集失败，未读取到子控件')
    sensors = [w for w in result['windows'] if w['program'] == 'HWiNFO'
               and re.search(r'Sensors|传感器', w['title'], re.I)]
    if not any(b.get('ok') and b.get('child_count', 0) > 0
               for w in sensors for b in w.get('backends', [])):
        missing.append('HWiNFO：缺少 Sensors 传感器窗口的控件')
    result['pending'] = missing
    result['note'] = '仅检查资料可读性；不表示日志启停、刷新周期或负载运行已验证通过。'
    return result


def make_archive(folder):
    target = folder.with_suffix('.zip')
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in folder.rglob('*'):
            if path.is_file():
                archive.write(path, path.relative_to(folder))
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture', type=int)
    parser.add_argument('--backend', choices=('uia', 'win32'))
    parser.add_argument('--expected-pid', type=int)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.capture is not None:
        export_controls(args.capture, args.backend, args.output, args.expected_pid)
        return 0
    if os.name != 'nt':
        raise RuntimeError('请在 Windows 测试主机运行')
    print('软件窗口补采 v1 — 请在测试主机运行')
    print('请打开：HWiNFO Sensors、系统播放器（打开本地视频并暂停）、BIT 主窗口。')
    print('工具会还原和切换这些窗口，但不会点击按钮、启停测试或修改配置。')
    if not ctypes.windll.shell32.IsUserAnAdmin():
        print('为读取管理员身份启动的软件，请关闭本窗口，右键 CMD，选择“以管理员身份运行”。')
        return 1
    input('准备好后按回车开始；采集时请暂时不要操作其他窗口：')
    folder = ROOT / '联调资料' / ('窗口补采_' + datetime.now().strftime('%Y%m%d_%H%M%S')
                                 + '_' + uuid.uuid4().hex[:6])
    folder.mkdir(parents=True)
    try:
        result = summarize(collect_windows(folder, read_config()))
    except Exception as exc:
        import traceback
        (folder / '采集错误.txt').write_text(traceback.format_exc(), 'utf-8-sig')
        result = {'pending': [f'采集未完成：{type(exc).__name__}: {exc}']}
    result['administrator'] = True
    result['tool_version'] = '1.0'
    save_json(folder / '采集状态.json', result)
    for message in result['pending']:
        print('待补：' + message)
    if not result['pending']:
        print('三个软件均采集到子控件，资料还需用于后续适配验证。')
    archive = make_archive(folder)
    print('请发送：' + str(archive))
    print('即使出现“待补”，也请发送这个包，其中保存了排查记录。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
