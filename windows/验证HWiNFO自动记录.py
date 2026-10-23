"""实测30/60秒自动日志：设置→回读→保存CSV→3个采样→停止→核对。

不运行BIT，不播放视频，不改变网卡或时钟。数据只写入本次验证目录。
"""
import copy
from datetime import datetime
import os
from pathlib import Path
import sys
import time
import traceback
import uuid

import windows_temperature_test as app
from 补采软件窗口 import make_archive


def observed_hwinfo_configuration(cfg):
    updated = copy.deepcopy(cfg)
    updated['hwinfo']['ui'].update(
        adapter='hwinfo_834', backend='win32',
        window={'title_re': r'HWiNFO.*32 v8\.34-5870 Sensors Status.*', 'class_name': '#32770'})
    updated['hwinfo']['ui'].pop('_logging_session', None)
    # 获得设置页只证明控件已识别，必须等真实CSV验证完成才通过预检。
    updated['hwinfo']['smart_refresh_verified'] = False
    updated['hwinfo'].pop('logging_verification', None)
    return updated


def check_csv_samples(path, settings, interval_s):
    """温度不变也可能是真实平稳；不能拿数值是否变化判断采样是否刷新。"""
    rows, errors = app.read_hwinfo(path, settings)
    if errors:
        raise RuntimeError('CSV存在无法解析的记录：'+str(errors[:3]))
    if len(rows) < 3:
        raise RuntimeError(f'CSV有效记录不足3行：{len(rows)}行')
    required = settings.get('required_metrics', ['Composite', 'CPU', 'NAND'])
    if any(row.get(name) is None for row in rows for name in required):
        raise RuntimeError('CSV中的必需温度存在空值/非法值')
    gaps = [b['epoch']-a['epoch'] for a, b in zip(rows, rows[1:])]
    if any(abs(gap-interval_s) > 2 for gap in gaps):
        raise RuntimeError('CSV实际记录间隔与设置不符，不能通过：'+str(gaps))
    return {'rows': len(rows), 'interval_s': interval_s, 'actual_gaps_s': gaps,
            'first_epoch': rows[0]['epoch'], 'last_epoch': rows[-1]['epoch'],
            'csv_sha256': app.sha256(path), 'sensor_group': settings.get('expected_sensor_group'),
            'passed': True}


def record_three_samples(cfg, folder, interval):
    folder.mkdir(parents=True, exist_ok=False)
    result = {'interval_s': interval, 'passed': False}
    try:
        try:
            path = app.start_hwinfo_logging(cfg, folder, interval)
            partial_settings = copy.deepcopy(cfg['hwinfo'])
            partial_settings.pop('expected_sensor_group', None)  # 停止后才会写入盘分组尾注。
            deadline = time.monotonic()+3*interval+15
            last_count, last_print = -1, 0.0
            while time.monotonic() < deadline:
                try:
                    rows, _ = app.read_hwinfo(path, partial_settings)
                    count = len(rows)
                except (ValueError, StopIteration, OSError):
                    count = 0  # 正在追加的行可能暂时不完整，停止后还会严格复查。
                now = time.monotonic()
                if count != last_count or now-last_print >= 15:
                    print(f'{interval}秒间隔：已记录 {count}/3 行。', flush=True)
                    last_count, last_print = count, now
                if count >= 3:
                    break
                time.sleep(.5)
            else:
                raise RuntimeError(f'{interval}秒日志超时，未获得3条记录')
        finally:
            already_failed = sys.exc_info()[0] is not None
            try:
                app.stop_hwinfo_logging(cfg)
            except Exception as exc:
                result['stop_error'] = str(exc)
                if not already_failed:
                    raise
        # 停止后等待盘分组尾注写完；没有尾注不假定它属于目标SSD。
        deadline = time.monotonic()+10
        while True:
            try:
                result.update(check_csv_samples(path, cfg['hwinfo'], interval))
                break
            except (ValueError, RuntimeError, OSError, StopIteration):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.5)
        return result
    except BaseException as exc:
        result['passed'] = False
        result['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        app.save_json(folder/'记录验证.json', result)


def capture_failure_context(cfg, folder, known_pid=None):
    """验证失败直接附上设置和日志窗口，不要求再单独补采一次。"""
    result = {'known_pid': known_pid, 'processes': [], 'errors': []}
    try:
        # 设置失败时已在取消弹窗前采过现场，不再重复等待或错过原始画面。
        existing = sorted(folder.glob('*/设置失败现场/现场采集结果.json'))
        if existing:
            result['existing_records'] = [str(p.relative_to(folder)) for p in existing]
            return
        pids = {known_pid} if isinstance(known_pid, int) and known_pid > 0 else set()
        if not pids:
            from pywinauto import Desktop
            executable = app.resolve_hwinfo_executable(cfg['hwinfo']).resolve()
            seen = set()
            # Sensors可能被设置页遮挡或关闭；按完整程序路径查同进程所有窗口。
            for window in Desktop(backend='win32').windows(visible_only=False, enabled_only=False):
                pid = window.element_info.process_id
                if pid in seen:
                    continue
                seen.add(pid)
                try:
                    actual = Path(app.process_executable_path(pid)).resolve()
                    if os.path.normcase(str(actual)) == os.path.normcase(str(executable)):
                        pids.add(pid)
                except OSError:
                    continue
        if not pids:
            result['errors'].append('没有找到绑定U盘的HWiNFO窗口，软件可能已退出；未采集其他程序')
        for pid in sorted(pids):
            result['processes'].append(app.save_hwinfo_failure_context(pid, folder/'失败现场'/str(pid)))
    except Exception as exc:
        result['errors'].append(f'{type(exc).__name__}: {exc}')
    finally:
        app.save_json(folder/'失败现场索引.json', result)


def main():
    app.require_administrator()
    print('HWiNFO自动记录验证 v0.2.8（约3～5分钟）')
    print('请打开U盘中的 HWiNFO Sensors，并结束现有日志记录。')
    print('将设置SMART每1个周期刷新，自动验证30秒和60秒记录；不用手工选择保存位置。')
    print('本步不运行BIT，不播放视频，也不切换网卡或校时。')
    input('准备好后按回车：')
    cfg = app.load_config()
    folder = app.ROOT/'联调资料'/('自动记录验证_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6])
    folder.mkdir(parents=True, exist_ok=False)
    report = {'version': app.VERSION, 'formal_test_started': False, 'records': [], 'passed': False}
    known_pid = None
    updated = observed_hwinfo_configuration(cfg)
    # 立即撤销旧的通过标记，失败时不会保留以前不适用的认证。
    app.save_setup_config(updated, '应用HWiNFO实机控件', {
        'source': '设置适配_20261010_091837_d1495c', 'note': '尚未通过自动记录验证'})
    try:
        if not updated['hwinfo'].get('mapping_verified') or not updated['hwinfo'].get('expected_sensor_group'):
            raise RuntimeError('缺少已确认的SMART盘分组与温度列，请先核对目标SSD')
        executable = app.resolve_hwinfo_executable(updated['hwinfo'])
        sensor = app.hwinfo_sensor_window(updated)
        pid = sensor.wrapper_object().element_info.process_id
        if isinstance(pid, int):
            known_pid = pid
            report['hwinfo_pid'] = pid
        app.save_json(folder/'验证结果.json', report)
        for interval in (30, 60):
            print(f'正在验证 {interval} 秒：设置→回读→自动保存CSV→3个采样→停止。', flush=True)
            report['records'].append(record_three_samples(updated, folder/f'{interval}秒', interval))
            app.save_json(folder/'验证结果.json', report)
        report['passed'] = True
        proof = {'verified_intervals_s': [30, 60], 'executable_sha256': app.sha256(executable),
                 'sensor_group': updated['hwinfo']['expected_sensor_group'],
                 'verified_at': datetime.now().isoformat(timespec='seconds'),
                 'evidence_folder': str(folder),
                 'scope': '设置回读与CSV时间间隔；不以温度变化证明刷新，不代表四项负载全部验证'}
        updated['hwinfo']['smart_refresh_verified'] = True
        updated['hwinfo']['logging_verification'] = proof
        app.save_setup_config(updated, 'HWiNFO自动记录验证', proof)
        print('30秒、60秒自动记录均已通过，配置已保存。')
    except (Exception, KeyboardInterrupt) as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        (folder/'未完成原因.txt').write_text(traceback.format_exc(), 'utf-8-sig')
        print('未通过：'+str(exc))
        capture_failure_context(updated, folder, known_pid)
    finally:
        report['record_files'] = [str(p.relative_to(folder)) for p in sorted(folder.glob('*/记录验证.json'))]
        app.save_json(folder/'验证结果.json', report)
        print('请发送：'+str(make_archive(folder)))
        print('BIT/播放器/在线视频仍按各自适配状态检查，本步不把它们标记为通过。')


if __name__ == '__main__':
    main()
