"""应用已收到的窗口定位，并自动补采设置页。不会开始 BIT 或温度测试。"""
import copy
import ctypes
from datetime import datetime
import json
from pathlib import Path
import re
import time
import uuid

import windows_temperature_test as app
from 补采软件窗口 import collect_one_backend, make_archive, rectangle_values, usable_rectangle


IGNORED_WINDOW_CLASSES = {'tooltips_class32', '#32768', 'combolbox', 'sysshadow',
                          'ime', 'msctfime ui', 'cicmarshalwndclass'}


def is_visible_content_window(window):
    """提示框、菜单、隐藏窗口和零面积窗口均不能作为设置对话框。"""
    try:
        return (window.is_visible() and not window.is_minimized()
                and window.class_name().lower() not in IGNORED_WINDOW_CLASSES
                and usable_rectangle(rectangle_values(window)))
    except Exception:
        # 刚消失的提示框可能在枚举之后、读取属性之前销毁。
        return False


def confirmed_selectors(cfg):
    """仅填入 2026-10-09 实际采到的定位；保留未核实的动作和现场参数。"""
    updated = copy.deepcopy(cfg)
    hw = updated['hwinfo']['ui']
    hw['backend'] = 'uia'
    hw['window'] = {'title_re': r'HWiNFO.*Sensors Status.*', 'class_name': '#32770'}
    hw['settings_control'] = {'auto_id': '1436', 'control_type': 'Button'}
    hw['logging_control'] = {'auto_id': '1437', 'control_type': 'Button'}
    player = updated['local_video']['ui']
    player['backend'] = 'uia'
    player['window'] = {'title': '媒体播放器', 'class_name': 'ApplicationFrameWindow'}
    player['position_control'] = {'auto_id': 'TimeElapsedElement', 'control_type': 'Text'}
    player['progress_control'] = {'auto_id': 'ProgressSlider', 'control_type': 'Slider'}
    player['play_control'] = {'auto_id': 'MTCPlayPauseButton.Play', 'control_type': 'Button'}
    updated['software_controls_source'] = '窗口补采_20261009_161458_a67724'
    # 不设置 smart_refresh_verified 或 system_disk_file_test_verified，也不猜运行状态。
    return updated


def program_windows(program, cfg):
    from pywinauto import Desktop
    targets = []
    for w in Desktop(backend='win32').windows(visible_only=True):
        try:
            pid = w.element_info.process_id
            name = app.process_executable_name(pid)
            title = w.window_text()
            if app.inspection_software_group(name, title, cfg) != program:
                continue
            if program == 'HWiNFO' and not re.search(r'Sensors|传感器', title, re.I):
                continue
            if program == 'BurnInTest' and w.class_name() != 'bit':
                continue
            targets.append(w)
        except OSError:
            continue
    return targets


def visible_handles(pid):
    from pywinauto import Desktop
    # Desktop.windows() 的默认值是 visible_only=False，必须显式指定。
    return {w.handle for w in Desktop(backend='win32').windows(process=pid, visible_only=True)
            if is_visible_content_window(w)}


def wait_for_new_dialog(pid, before, timeout=10):
    """等待弹窗稳定出现，避免刚出现的短暂窗口抢先结束等待。"""
    deadline = time.monotonic() + timeout
    previous, stable_since = set(), None
    while time.monotonic() < deadline:
        current = visible_handles(pid) - before
        now = time.monotonic()
        if current != previous:
            stable_since = now
        elif current and stable_since is not None and now - stable_since >= .6:
            return current
        previous = current
        time.sleep(.2)
    return previous


def save_window_diagnostics(pid, folder, label):
    """逐个保存现场；某个空窗口不能阻止其他真实窗口的采集。"""
    from pywinauto import Desktop
    folder.mkdir(parents=True, exist_ok=True)
    records = []
    for window in Desktop(backend='win32').windows(process=pid, visible_only=False):
        record = {'handle': window.handle}
        records.append(record)
        try:
            record.update(title=window.window_text(), class_name=window.class_name(),
                          visible=window.is_visible(), rectangle=rectangle_values(window))
            record['content_window'] = is_visible_content_window(window)
            if record['content_window']:
                try:
                    snapshot(window.handle, pid, folder, f'{label}_{window.handle}')
                except Exception as exc:
                    record['capture_error'] = str(exc)
        except Exception as exc:
            record['error'] = str(exc)
    app.save_json(folder/f'{label}_窗口清单.json', records)


def manually_select_dialog(pid, main_handle, kind):
    """自动定位失败时在同一次运行中补采，不要求重装或重录 CSV。"""
    from pywinauto import Desktop
    guidance = {
        'HWiNFO设置': '在 HWiNFO Sensors 窗口点击齿轮 Config，停留在设置页。',
        'HWiNFO日志保存窗口': '在 HWiNFO Sensors 点击 Log Start，停留在保存对话框，不要保存文件。',
        'BIT测试项目': '将 BIT 主窗口置于前台，按 F3 打开测试项目配置。',
        'BIT磁盘与日志设置': '将 BIT 主窗口置于前台，按 F2 打开 Preferences。',
    }
    print('未能自动定位：'+kind+'。如果窗口已经打开，请保持打开；否则：'+guidance[kind])
    answer = input('窗口准备好后回车继续采集；输入 S 跳过这一项：').strip().lower()
    if answer == 's':
        return None
    candidates = sorted(visible_handles(pid) - {main_handle})
    # 明确让测试人员选择；不把同进程的另一个主窗口误认为设置页并关闭。
    if not candidates:
        return None
    for index, handle in enumerate(candidates, 1):
        window = Desktop(backend='win32').window(handle=handle, process=pid)
        print(f'{index}. {window.window_text()} [{window.class_name()}]')
    choice = input('请输入上面设置/保存对话框的编号；输入 S 跳过：').strip()
    if choice.isdigit() and 1 <= int(choice) <= len(candidates):
        return candidates[int(choice)-1]
    return None


class DialogLeftOpen(RuntimeError):
    """还有弹窗未关闭时，跳过本软件后续自动操作，仍采集另一软件。"""


def snapshot(handle, pid, folder, label):
    folder.mkdir(parents=True, exist_ok=True)
    results = [collect_one_backend(handle, backend, folder/f'{label}_{backend}.json', pid)
               for backend in ('uia', 'win32')]
    app.save_json(folder/f'{label}_采集结果.json', results)
    if not any(item.get('ok') and item.get('child_count', 0) > 0 for item in results):
        raise RuntimeError(label+' 未读取到子控件，错误记录已经保存')
    return results


def capture_setting_tabs(handle, pid, folder):
    """读取当前页以及实际存在的常规、磁盘、日志页；不修改选项值。"""
    from pywinauto import Desktop
    snapshot(handle, pid, folder, '初始页')
    dialog = Desktop(backend='uia').window(handle=handle, process=pid)
    tabs = dialog.descendants(control_type='TabItem')
    failed_tabs = []
    for index, tab in enumerate(tabs):
        title = tab.window_text()
        if re.search(r'Disk|磁盘|Log|日志|General|常规|S\.?M\.?A\.?R\.?T\.?|Polling|轮询', title, re.I):
            try:
                tab.select()
                time.sleep(.3)
                snapshot(handle, pid, folder, f'页{index}_{app.safe_name(title)}')
            except Exception as exc:
                app.save_json(folder/f'页{index}_读取失败.json', {'title': title, 'error': str(exc)})
                failed_tabs.append(title)
    if failed_tabs:
        raise RuntimeError('部分设置页未读完整，已保留其他页：'+', '.join(failed_tabs))


def cancel_dialog(handle, pid):
    """只取消本次打开的同进程对话框，不点击确定/应用/保存。"""
    from pywinauto import Desktop
    dialog = Desktop(backend='win32').window(handle=handle, process=pid)
    if not dialog.exists() or not dialog.is_visible():
        return
    cancel = dialog.child_window(control_id=2, class_name='Button')
    if cancel.exists() and cancel.is_visible() and re.search(r'Cancel|取消', cancel.window_text(), re.I):
        cancel.click()
    else:
        dialog.set_focus()
        dialog.type_keys('{ESC}')
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        if not dialog.exists() or not dialog.is_visible():
            return
        time.sleep(.2)
    raise DialogLeftOpen('对话框未自动取消，请手动点取消；本软件后续自动操作已跳过')


def open_dialog_and_capture(main, kind, folder):
    from pywinauto import Desktop
    pid = main.element_info.process_id
    handle = main.handle
    window = Desktop(backend='win32').window(handle=handle, process=pid)
    if window.is_minimized():
        window.restore()
    window.set_focus()
    before = visible_handles(pid)
    folder.mkdir(parents=True, exist_ok=True)
    app.save_json(folder/'窗口定位.json', {'pid': pid, 'main_handle': handle, 'before': sorted(before)})
    if kind == 'HWiNFO设置':
        window.child_window(control_id=1436, class_name='Button').click()
    elif kind == 'HWiNFO日志保存窗口':
        button = window.child_window(control_id=1437, class_name='Button')
        if button.window_text().replace('&', '').strip() != 'Log Start':
            raise RuntimeError('HWiNFO 可能正在记录日志，未操作日志按钮；请结束当前记录后再补采')
        button.click()
    elif kind == 'BIT测试项目':
        window.type_keys('{F3}')  # 随 9.0 安装包手册定义：Test configuration and duty cycles。
    elif kind == 'BIT磁盘与日志设置':
        window.type_keys('{F2}')  # 同一手册定义：Preferences。
    else:
        raise ValueError('未知设置页')
    new = wait_for_new_dialog(pid, before)
    app.save_json(folder/'窗口定位.json', {'pid': pid, 'main_handle': handle,
        'before': sorted(before), 'new_content_windows': sorted(new)})
    if len(new) != 1:
        save_window_diagnostics(pid, folder, '未唯一定位')
        dialog_handle = manually_select_dialog(pid, handle, kind)
        if dialog_handle is None:
            raise DialogLeftOpen(f'{kind} 弹窗未唯一识别，现场已保存；请手动取消该设置页')
    else:
        dialog_handle = next(iter(new))
    try:
        if kind == 'HWiNFO日志保存窗口':
            snapshot(dialog_handle, pid, folder, '保存对话框')
        else:
            capture_setting_tabs(dialog_handle, pid, folder)
    finally:
        cancel_dialog(dialog_handle, pid)


def collect_settings_steps(hw, bit, folder, report):
    """一项失败也保留其他资料；未关闭的弹窗只影响所属软件。"""
    blocked = set()
    for kind, window in [('HWiNFO设置', hw), ('HWiNFO日志保存窗口', hw),
                         ('BIT测试项目', bit), ('BIT磁盘与日志设置', bit)]:
        pid = window.element_info.process_id
        step = {'step': kind}
        report['steps'].append(step)
        if pid in blocked:
            step['status'] = '已跳过：本软件前一弹窗未关闭'
            print(step['status']+' — '+kind, flush=True)
            continue
        print('正在读取：'+kind, flush=True)
        try:
            open_dialog_and_capture(window, kind, folder/kind)
            step['status'] = '已读取并取消弹窗'
        except Exception as exc:
            import traceback
            destination = folder/kind
            destination.mkdir(parents=True, exist_ok=True)
            (destination/'未完成原因.txt').write_text(traceback.format_exc(), 'utf-8-sig')
            step.update(status='未完成', error=f'{type(exc).__name__}: {exc}')
            print(kind+' 未完成：'+str(exc), flush=True)
            if isinstance(exc, DialogLeftOpen):
                blocked.add(pid)
        app.save_json(folder/'适配状态.json', report)
    report['complete'] = all(step['status'] == '已读取并取消弹窗' for step in report['steps'])


def main():
    print('Windows v0.2.6 软件适配 — 本步读取设置，不启动正式测试')
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError('请右键“继续软件适配.cmd”，选择以管理员身份运行')
    print('请打开 HWiNFO Sensors，暂停现有 HWiNFO 日志；BIT 只保留一个主窗口且未在测试。')
    input('准备好后回车，工具将打开设置页、读取后取消：')
    cfg = app.load_config()
    folder = app.ROOT/'联调资料'/('设置适配_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6])
    folder.mkdir(parents=True)
    report = {'version': app.VERSION, 'steps': [], 'formal_test_started': False}
    try:
        hw = program_windows('HWiNFO', cfg)
        bit = program_windows('BurnInTest', cfg)
        if len(hw) != 1:
            raise RuntimeError(f'找到 {len(hw)} 个 HWiNFO Sensors 窗口，请只保留一个')
        if len(bit) != 1:
            raise RuntimeError(f'找到 {len(bit)} 个 BIT 主窗口，请先关闭多余窗口，只保留一个后重试')
        # 使用 WindowSpecification 读取状态，拒绝在正在运行的 BIT 上打开设置。
        from pywinauto import Desktop
        status = Desktop(backend='win32').window(handle=bit[0].handle).child_window(
            control_id=2, class_name='msctls_statusbar32').window_text()
        if status.strip() != 'Ready':
            raise RuntimeError('BIT 尚未处于已采到的 Ready 状态，未打开配置：'+status)
        updated = confirmed_selectors(cfg)
        app.save_setup_config(updated, '软件控件定位', {'source': updated['software_controls_source'],
            'note': '仅应用已观察定位；未将日志刷新和BIT负载标记为已验证'})
        collect_settings_steps(hw[0], bit[0], folder, report)
    except Exception as exc:
        import traceback
        report['error'] = f'{type(exc).__name__}: {exc}'
        report['complete'] = False
        (folder/'未完成原因.txt').write_text(traceback.format_exc(), 'utf-8-sig')
        print('未全部完成：'+str(exc))
    app.save_json(folder/'适配状态.json', report)
    completed = sum(step['status'] == '已读取并取消弹窗' for step in report['steps'])
    print(f'设置资料采集：{completed}/4 项完成。'+('' if report.get('complete') else '未完成原因也已放入压缩包。'))
    print('请发送：'+str(make_archive(folder)))
    print('这一步不会开始温度测试；取得设置资料后再验证自动日志和负载操作。')


if __name__ == '__main__':
    main()
