# -*- coding: utf-8 -*-
"""一台 Windows / 一台 JK808 / 两台独立 Linux；原测试与出表沿用单机引擎。

SharedSampler 唯一持有温度仪；LaneControl 对齐各测次；DualController 管理两条流程。
双机配置可编辑，双机工作区中的状态和快照由程序生成，不是第二套手填配置。
"""
import builtins
import copy
import csv
from contextlib import contextmanager, nullcontext
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

import linux_temperature_test as base

ROOT = Path(__file__).resolve().parent
DUAL_FILE = ROOT / '双机配置.json'
OUTPUT_LOCK = threading.RLock()
REPORT_LOCK = threading.RLock()
LANES = ('A', 'B')
DEFAULT_MAPS = {
    'A': {'CPU_Surface': 1, 'NAND_Surface': 2, 'AMB': 3},
    'B': {'CPU_Surface': 4, 'NAND_Surface': 5, 'AMB': 6},
}


def say(label, *values, **kwargs):
    with OUTPUT_LOCK:
        builtins.print('[' + label + ']', *values, **dict(kwargs, flush=True))


def new_dual_configuration(single):
    """旧配置只导入 A；B 的身份和写盘许可清空，防止复制 A 的盘身份。"""
    cfg = {'format_version': 1, 'output_dir': 'deliveries_dual',
           'clock_sync': copy.deepcopy(single.get('clock_sync', {})),
           'thermometer': copy.deepcopy(single['thermometer']), 'stations': {}}
    cfg['clock_sync']['ntp_servers'] = cfg['clock_sync'].get('ntp_servers') or ['ntp.aliyun.com','ntp1.aliyun.com']
    cfg['thermometer']['active_channels'] = [1, 2, 3, 4, 5, 6]
    # 增加的探针必须重新与仪器屏幕核对。
    cfg['thermometer']['protocol_verified'] = False
    for lane in LANES:
        station = copy.deepcopy(single)
        station['thermometer']['channel_map'] = copy.deepcopy(DEFAULT_MAPS[lane])
        station['company_report']['output_dir'] = 'deliveries_dual/主机' + lane
        station['ssh']['password_env'] = 'JK500_DUAL_' + lane + '_PASSWORD'
        station['sample_assignment'] = None
        station['deployment_complete'] = False
        if lane == 'B':
            station['ssh'].update(host='', password='', host_key=None, device='',
                                  expected_pci='', allow_destructive=False,
                                  key_filename='', known_hosts='')
            station['detected_disk'] = {}
            for key in ('capacity', 'expected_serial', 'test_version'):
                station['company_report'][key] = ''
        cfg['stations'][lane] = station
    return cfg


def validate_dual_configuration(cfg):
    if cfg.get('format_version') != 1 or set(cfg.get('stations', {})) != set(LANES):
        raise ValueError('双机配置必须包含 A、B 两台主机，format_version=1')
    if not isinstance(cfg.get('output_dir'), str) or not cfg['output_dir'].strip():
        raise ValueError('output_dir 必须是结果保存目录')
    used = []
    for lane in LANES:
        station = cfg['stations'][lane]
        mapping = station['thermometer']['channel_map']
        if set(mapping) != set(base.METRICS[:3]):
            raise ValueError('主机' + lane + '必须配置主控表面、NAND表面、环境三路')
        channels = list(mapping.values())
        if any(type(n) is not int or not 1 <= n <= 8 for n in channels):
            raise ValueError('探针编号必须是 1～8 的整数')
        used += channels
    if len(set(used)) != 6:
        raise ValueError('两台主机必须使用互不重复的六个探针通道')
    if not set(used).issubset(set(cfg['thermometer']['active_channels'])):
        raise ValueError('六根探针必须全部列入 thermometer.active_channels')
    hosts = [(str(cfg['stations'][lane]['ssh']['host']).strip().lower(),
              cfg['stations'][lane]['ssh']['port']) for lane in LANES]
    if hosts[0][0] and hosts[0] == hosts[1]:
        raise ValueError('A、B 不能填写同一个 SSH 地址和端口')
    base.validate_jk808_settings(cfg['thermometer'], require_path=False)


def station_configuration(cfg, lane):
    station = copy.deepcopy(cfg['stations'][lane])
    mapping = station['thermometer']['channel_map']
    station['thermometer'] = dict(copy.deepcopy(cfg['thermometer']), channel_map=mapping)
    station['clock_sync'] = copy.deepcopy(cfg['clock_sync'])
    station['ssh']['password_env'] = 'JK500_DUAL_' + lane + '_PASSWORD'
    station['company_report']['output_dir'] = str(Path(cfg['output_dir']) / ('主机' + lane))
    station['_config_dir'] = str(ROOT)
    return station


def load_dual_configuration():
    if not DUAL_FILE.exists():
        single = base.load_configuration()
        base.save_json(DUAL_FILE, new_dual_configuration(single))
    cfg = json.loads(DUAL_FILE.read_text('utf-8-sig'))
    changed = [lane for lane in LANES if base.migrate_rw_sampling(cfg['stations'][lane])]
    if changed:
        save_dual_configuration(cfg)
        say('接收端','已备份并升级主机'+ '/'.join(changed)+'的读写采样规则；L1.2/L0保留。')
    validate_dual_configuration(cfg)
    return cfg


def save_dual_configuration(cfg):
    validate_dual_configuration(cfg)
    if DUAL_FILE.exists():
        backup = ROOT / '配置备份' / ('双机_' + time.strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:6] + '.json')
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(DUAL_FILE.read_bytes())
    base.save_json(DUAL_FILE, cfg)


class CancellationEvent:
    """本项收尾只设置 local；人工取消 parent 才取消后面的项目。"""
    def __init__(self, parent):
        self.parent, self.local = parent, threading.Event()

    def set(self):
        self.local.set()

    def is_set(self):
        return self.local.is_set() or self.parent.is_set()

    def wait(self, seconds=None):
        deadline = None if seconds is None else time.monotonic() + max(0, seconds)
        while not self.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                return False
            self.local.wait(.1 if deadline is None else min(.1, max(0, deadline-time.monotonic())))
        return True


class SharedSampler:
    """一个 HID 句柄、每秒一个八通道读请求；按统一单调时钟分发，禁止复用旧秒数据。"""
    def __init__(self, settings, folder, factory=None):
        self.settings, self.folder = copy.deepcopy(settings), Path(folder)
        self.factory = factory or base.JK808Thermometer
        self.anchor = time.monotonic()
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.subscriptions, self.records = {}, {}
        self.thread, self.device = None, None
        self.fatal_error, self.current_trace = '', []

    def next_start(self):
        slot = math.ceil(time.monotonic() - self.anchor + .3)
        tick = self.anchor + slot
        return tick, time.time() + tick - time.monotonic()

    def start(self):
        if self.thread is None:
            self.folder.mkdir(parents=True, exist_ok=True)
            self.thread = threading.Thread(target=self._run, name='JK808统一采集', daemon=False)
            self.thread.start()

    @contextmanager
    def session(self, start, duration):
        token = uuid.uuid4().hex
        first = round(start - self.anchor)
        if abs(self.anchor + first - start) > .001:
            raise ValueError('正式起点没有对齐共享采样时钟')
        with self.condition:
            if self.fatal_error or self.stop_event.is_set():
                raise RuntimeError(self.fatal_error or '统一采集已停止')
            self.subscriptions[token] = (first, first + duration)
            self.condition.notify_all()
        try:
            yield first
        finally:
            with self.condition:
                self.subscriptions.pop(token, None)
                self.condition.notify_all()

    def get(self, slot, cancel):
        deadline = self.anchor + slot + 1
        with self.condition:
            while slot not in self.records:
                if cancel.is_set():
                    raise InterruptedError('当前主机采样已取消')
                if self.fatal_error or self.stop_event.is_set():
                    raise RuntimeError(self.fatal_error or '统一采集已停止')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {'slot': slot, 'actual_time': '', 'values': {}, 'statuses': {},
                            'raw': {}, 'trace': [], 'error': '本秒共享采样缺失或响应超时'}
                self.condition.wait(min(.1, remaining))
            return copy.deepcopy(self.records[slot])

    def _trace(self, direction, payload):
        self.current_trace.append({'direction': direction, 'hex': bytes(payload).hex(' '),
                                   'ActualTime': base.utc_timestamp()})

    def capture(self, slot):
        self.current_trace = []
        record = {'slot': slot, 'actual_time': '', 'values': {}, 'statuses': {},
                  'raw': {}, 'trace': [], 'error': ''}
        try:
            if time.monotonic() >= self.anchor + slot + 1:
                raise TimeoutError('本秒未按时发起共享采样')
            if self.device is None:
                self.device = self.factory(self.settings, self._trace)
            values, statuses, raw = self.device.read()
            record['actual_time'] = base.utc_timestamp()
            record['raw'], record['statuses'] = raw, statuses
            if time.monotonic() >= self.anchor + slot + 1:
                raise TimeoutError('共享采样响应晚于本秒末端；保留原报文，不补到下一秒')
            record['values'] = values
        except Exception as exc:
            record['error'] = type(exc).__name__ + ': ' + str(exc)
            if self.device is not None:
                try:
                    self.device.close()
                except Exception:
                    pass
                self.device = None
        record['trace'] = self.current_trace
        return record

    def _run(self):
        try:
            with (self.folder / 'JK808八通道共享原始记录.jsonl').open('w', encoding='utf-8') as stream:
                slot = math.ceil(time.monotonic() - self.anchor)
                while not self.stop_event.is_set():
                    with self.condition:
                        if not self.subscriptions:
                            self.condition.wait(.1)
                            slot = math.ceil(time.monotonic() - self.anchor)
                            continue
                    if self.stop_event.wait(max(0, self.anchor + slot - time.monotonic())):
                        break
                    # 等待下一秒期间可能新加入另一台测次，必须在读仪器之前重新判断。
                    with self.condition:
                        active = any(first <= slot < last for first, last in self.subscriptions.values())
                    if active:
                        record = self.capture(slot)
                        base.append_json_record(stream, record)
                        with self.condition:
                            self.records[slot] = record
                            self.records = {k: v for k, v in self.records.items() if k >= slot - 120}
                            self.condition.notify_all()
                    slot = max(slot + 1, math.ceil(time.monotonic() - self.anchor))
        except BaseException as exc:
            with self.condition:
                self.fatal_error = '共享温度记录无法继续：' + type(exc).__name__ + ': ' + str(exc)
                self.condition.notify_all()
        finally:
            if self.device is not None:
                self.device.close()
                self.device = None

    def close(self):
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError('温度采集尚未退出，请保留窗口等待，不能再打开第二个采集程序')


class DualClock:
    """全接收端空闲时校准 Windows；测试期间只校准即将启动的 Linux。"""
    def __init__(self, record_folder):
        self.folder, self.lock = Path(record_folder), threading.Lock()
        self.prepared = False

    def prepare_receiver(self, configuration):
        base.require_windows_clock_permission()
        reference = base.obtain_internet_standard_time(configuration)
        result = subprocess.run(['tzutil.exe', '/s', 'China Standard Time'], capture_output=True, timeout=15)
        if result.returncode:
            raise RuntimeError('接收电脑北京时区设置失败')
        base.set_windows_system_time(base.standard_time_now(reference))
        offset = time.time() - base.standard_time_now(reference)
        if abs(offset) > .5:
            raise RuntimeError('接收电脑校时未通过，未启动两台测试')
        self.prepared = True
        base.save_json(self.folder / '接收电脑校时.json',
                       {'passed': True, 'time': base.beijing_timestamp(), 'offset_s': offset,
                        'ntp_reference': reference, 'scope': '仅在两台均空闲时调整Windows'})

    def synchronize_host(self, engine, cfg, folder):
        target = Path(folder) / '北京时间校准记录.json'
        record = {'outcome': 'running', 'host': cfg['ssh']['host'],
                  'changed_windows': False, 'changed_linux': False,
                  'method': '接收电脑空闲时统一NTP校时；本项只经SSH校准本台Linux'}
        base.save_json(target, record)
        remote = engine.LinuxConnection(cfg['ssh'])
        try:
            with self.lock:
                if not self.prepared:
                    raise RuntimeError('接收电脑尚未统一校时')
                reference = engine.obtain_internet_standard_time(cfg)
                record['ntp_reference'] = reference
                if abs(time.time() - engine.standard_time_now(reference)) > .5:
                    raise RuntimeError('接收电脑与NTP偏差超过0.5秒，请等两台结束后重新开始以校时；本次未改Windows时间')
                remote.connect()
                if remote.execute('id -u')[1].strip() != '0':
                    raise PermissionError('Linux校时需要root权限')
                remote.execute('timedatectl set-timezone Asia/Shanghai')
                if remote.execute('date +%z')[1].strip() != '+0800':
                    raise RuntimeError('Linux北京时区核对失败')
                delays = []
                for _ in range(3):
                    start = time.monotonic()
                    remote.execute('printf ok')
                    delays.append(time.monotonic()-start)
                if min(delays) > .5:
                    raise RuntimeError('SSH校时往返超过0.5秒')
                epoch = engine.standard_time_now(reference) + min(delays)/2
                script = 'import time; time.clock_settime(time.CLOCK_REALTIME, ' + f'{epoch:.6f}' + '); print(time.time())'
                remote.execute('python3 -c ' + engine.quote_shell_argument(script))
                record['changed_linux'] = True
                after = engine.measure_linux_clock_difference(remote)
                record['after'] = after
                tolerance = min(.5, cfg['ssh'].get('clock_tolerance_s', 2))
                if abs(after['offset_s']) + after['rtt_s']/2 > tolerance:
                    raise RuntimeError('本台Linux与接收电脑校时偏差未通过')
                record['outcome'] = 'ok'
                return record
        except BaseException as exc:
            record.update(outcome='failed', error=type(exc).__name__ + ': ' + str(exc))
            raise
        finally:
            remote.close()
            base.save_json(target, record)


class LaneControl:
    def __init__(self, lane, sampler, clock, engine, cancel):
        self.lane, self.sampler, self.clock = lane, sampler, clock
        self.engine, self.cancel_event = engine, cancel
        self.report_lock = REPORT_LOCK

    def make_stop_event(self):
        return CancellationEvent(self.cancel_event)

    def formal_start_point(self):
        return self.sampler.next_start()

    def synchronize_host(self, configuration, folder):
        return self.clock.synchronize_host(self.engine, configuration, folder)

    def collect_surface(self, cfg, epoch, start, duration, cancel, folder, rows, errors):
        folder = Path(folder)
        mapping = cfg['thermometer']['channel_map']
        base.save_json(folder / '共享仪器与通道.json',
                       {'station': self.lane, 'channel_map': mapping,
                        'shared_record_dir': str(self.sampler.folder.resolve()),
                        'start_epoch': epoch, '说明': '同秒一次物理采集；本测次只使用所分配的三根探针'})
        columns = ['Time', 'ActualTime', 'Second', 'Error'] + base.METRICS[:3]
        columns += [f'CH{n:03d}' + suffix for n in range(1, 9) for suffix in ('', '_Raw', '_Error')]
        try:
            with (self.sampler.session(start, duration) as first,
                  (folder / 'JK808逐秒原始数据.csv').open('w', encoding='utf-8-sig', newline='') as csv_stream,
                  (folder / 'JK808原始报文.jsonl').open('w', encoding='utf-8') as raw_stream):
                writer = csv.DictWriter(csv_stream, fieldnames=columns, extrasaction='ignore')
                writer.writeheader()
                for second in range(duration):
                    if cancel.wait(max(0, start + second - time.monotonic())):
                        break
                    record = self.sampler.get(first + second, cancel)
                    base.append_json_record(raw_stream,
                        {'Second': second, 'SharedSlot': first + second, 'ActualTime': record['actual_time'],
                         'error': record['error'], 'messages': record['trace']})
                    row = {'Time': base.utc_timestamp(epoch + second), 'Second': second,
                           'ActualTime': record['actual_time'], 'Error': record['error']}
                    for n in range(1, 9):
                        name = f'CH{n:03d}'
                        row[name] = record['values'].get(n)
                        row[name + '_Raw'] = record['raw'].get(n)
                        status = record['statuses'].get(n, 'no_data')
                        row[name + '_Error'] = record['error'] or ('' if status == 'ok' else status)
                    for metric, channel in mapping.items():
                        row[metric] = row[f'CH{channel:03d}']
                        row[metric + '_Error'] = row[f'CH{channel:03d}_Error']
                    rows.append(row)
                    writer.writerow(row)
                    csv_stream.flush()
        except InterruptedError:
            if not cancel.is_set():
                errors.append('当前主机共享采样意外停止')
                cancel.set()
        except BaseException as exc:
            errors.append('共享采样无法继续：' + str(exc))
            cancel.set()


def isolated_engine(lane, folder):
    """每台单独的模块状态和流程目录；模板及依赖共用，输出和密码变量不共用。"""
    spec = importlib.util.spec_from_file_location('jk808_linux_station_' + lane, ROOT / 'linux_temperature_test.py')
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    engine.WORKFLOW_FOLDER = Path(folder) / lane / '运行记录'
    engine.lock_receiver_during_test = nullcontext  # 外层主程序持有与旧版相同的全局设备锁。
    engine.print = lambda *args, **kwargs: say('主机' + lane, *args, **kwargs)
    return engine


class DualController:
    def __init__(self, cfg, folder=None):
        validate_dual_configuration(cfg)
        self.cfg = cfg
        self.folder = Path(folder or ROOT / '双机工作区')
        self.lock = threading.RLock()
        self.clock = DualClock(self.folder / '校时')
        self.engines = {lane: isolated_engine(lane, self.folder) for lane in LANES}
        self.jobs, self.hosts, self.sampler = {}, {}, None
        self.closed = False
        for lane, engine in self.engines.items():
            engine.print = lambda *values, _lane=lane, **kwargs: self.on_output(_lane, values, kwargs)

    def on_output(self, lane, values, kwargs):
        text = ' '.join(str(v) for v in values)
        password = self.cfg['stations'][lane]['ssh'].get('password')
        if password:
            text = text.replace(password, '[已隐藏]')
        with self.lock:
            job = self.jobs.get(lane)
            if job and job['running'] and any(word in text for word in ('测试中：', '正式测试', '准备等待', '项目间隔', '开始 Linux_', '最终公司模板')):
                job['phase'] = text
                self.save_status()
        say('主机' + lane, text, **kwargs)

    def running(self):
        with self.lock:
            return [lane for lane, job in self.jobs.items() if job['running']]

    def save_status(self):
        with self.lock:
            status = {lane: {k: v for k, v in job.items() if k not in ('thread', 'cancel')}
                      for lane, job in self.jobs.items()}
            base.save_json(self.folder / '当前状态.json', status)

    def ensure_idle(self):
        if self.running():
            raise RuntimeError('请等两台任务收尾完毕再修改配置或识别仪器')

    def ensure_lane_idle(self, lane):
        if lane in self.running():
            raise RuntimeError('主机' + lane + '正在执行，等本台收尾后才能部署或换盘')

    def pause_sampler(self):
        self.ensure_idle()
        if self.sampler is not None:
            self.sampler.close()
            self.sampler = None

    def start(self, lanes, workflow):
        if workflow not in ('sequence', 'l12_ps4', 'l0_ps4', 'write_1m', 'read_1m'):
            raise ValueError('不支持的流程')
        if not lanes or len(set(lanes)) != len(lanes) or any(lane not in LANES for lane in lanes):
            raise ValueError('请选择A、B或两台')
        with self.lock:
            if self.closed:
                raise RuntimeError('双机控制器已经关闭')
            busy = self.running()
            if any(lane in busy for lane in lanes):
                raise RuntimeError('所选主机正在执行，不能重复启动')
            configs = {lane: station_configuration(self.cfg, lane) for lane in lanes}
            modes = ['l0_ps4', 'write_1m', 'read_1m'] if workflow == 'sequence' else [workflow]
            for lane, cfg in configs.items():
                if not cfg.get('deployment_complete'):
                    raise ValueError('请先为主机' + lane + '完成A部署并确认新增探针')
                if not cfg['ssh'].get('password') and not cfg['ssh'].get('key_filename'):
                    raise ValueError('请先在A保存主机' + lane + '的登录信息')
                for mode in modes:
                    self.engines[lane].validate_test_configuration(cfg, mode)
            if not busy:
                say('接收端', '正在统一校准接收电脑时间……')
                self.clock.prepare_receiver(configs[lanes[0]])
            if self.sampler is None:
                run_id = time.strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:8]
                self.sampler = SharedSampler(self.cfg['thermometer'], self.folder / '共享采集' / run_id)
                self.sampler.start()
            for lane in lanes:
                cancel = threading.Event()
                self.jobs[lane] = {'running': True, 'workflow': workflow, 'phase': '准备中',
                                   'error': '', 'started': base.beijing_timestamp(), 'cancel': cancel}
            self.save_status()
            for lane in lanes:
                thread = threading.Thread(target=self._worker, args=(lane, configs[lane], workflow),
                                          name='Linux主机' + lane, daemon=False)
                self.jobs[lane]['thread'] = thread
                thread.start()

    @contextmanager
    def reserve_host(self, lane, cfg):
        engine = self.engines[lane]
        remote = engine.LinuxConnection(cfg['ssh'])
        try:
            remote.connect()
            boot_id = remote.execute('cat /proc/sys/kernel/random/boot_id')[1].strip().lower()
            if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', boot_id):
                raise RuntimeError('不能核对Linux启动身份，未执行测试')
            # 在校时之前检查，避免同一主机用两个IP误配置后干扰另一条流程。
            code, _, _ = remote.execute('pgrep -x fio', check=False)
            if code != 1:
                raise RuntimeError('Linux已有fio，或无法确认状态，未校时或执行新测试')
            with self.lock:
                owner = self.hosts.get(boot_id)
                if owner is not None and owner != lane:
                    raise RuntimeError('两组SSH地址实际指向同一台Linux，已阻止重复测试')
                self.hosts[boot_id] = lane
            try:
                yield
            finally:
                with self.lock:
                    if self.hosts.get(boot_id) == lane:
                        self.hosts.pop(boot_id)
        finally:
            remote.close()

    def _worker(self, lane, cfg, workflow):
        job, engine = self.jobs[lane], self.engines[lane]
        cfg['_dual_control'] = LaneControl(lane, self.sampler, self.clock, engine, job['cancel'])
        try:
            with engine.supply_linux_password(cfg), self.reserve_host(lane, cfg):
                if job['cancel'].is_set():
                    raise InterruptedError('测试人员取消')
                if workflow == 'sequence':
                    engine.continue_l0_write_read(cfg)
                elif workflow == 'l12_ps4':
                    engine.start_l12_then_pause(cfg)
                else:
                    engine.run_single_test(cfg, workflow)
            job['phase'] = '已完成；L1.2后请人工关机修改BIOS' if workflow == 'l12_ps4' else '已完成'
        except BaseException as exc:
            message = type(exc).__name__ + ': ' + str(exc)
            password = cfg['ssh'].get('password')
            if password:
                message = message.replace(password, '[已隐藏]')
            job['phase'] = '已取消并保留记录' if job['cancel'].is_set() else '未完成，可重新选择本台继续'
            job['error'] = message
            say('主机' + lane, job['phase'] + '：' + message)
        finally:
            with self.lock:
                job.update(running=False, ended=base.beijing_timestamp())
                self.save_status()
            say('主机' + lane, job['phase'] + '。另一台按自身流程继续。')

    def cancel(self, lanes):
        with self.lock:
            for lane in lanes:
                if lane in self.jobs and self.jobs[lane]['running']:
                    self.jobs[lane]['cancel'].set()
                    say('主机' + lane, '已收到取消，正在停止本次fio并保存原始记录，请等待收尾。')

    def show_status(self):
        with self.lock:
            for lane in LANES:
                job = self.jobs.get(lane, {})
                say('主机' + lane, job.get('phase', '空闲'), job.get('workflow', ''), job.get('error', ''))

    def close(self):
        with self.lock:
            self.closed = True
        self.cancel(LANES)
        for lane in LANES:
            thread = self.jobs.get(lane, {}).get('thread')
            while thread is not None and thread.is_alive():
                thread.join(timeout=1)
        if self.sampler is not None:
            self.sampler.close()


def deploy_station(controller, lane):
    controller.ensure_lane_idle(lane)
    cfg = station_configuration(controller.cfg, lane)
    engine = controller.engines[lane]
    old_sample, old_login = dict(cfg['company_report']), tuple(cfg['ssh'][key] for key in ('host', 'port', 'username'))
    cfg['ssh']['host'] = base.ask_text('主机' + lane + ' Linux IP', cfg['ssh']['host'])
    cfg['ssh']['username'] = base.ask_text('Linux账号', cfg['ssh']['username'])
    cfg['ssh']['port'] = int(base.ask_text('SSH端口', str(cfg['ssh']['port'])))
    if tuple(cfg['ssh'][key] for key in ('host', 'port', 'username')) != old_login:
        cfg['ssh'].update(password='', host_key=None, allow_destructive=False)
    other = controller.cfg['stations']['B' if lane == 'A' else 'A']['ssh']
    if other['host'] and (cfg['ssh']['host'].strip().lower(), cfg['ssh']['port']) == (other['host'].strip().lower(), other['port']):
        raise ValueError('与另一台配置的SSH地址重复，尚未连接')
    remote = engine.LinuxConnection(cfg['ssh'])
    with engine.supply_linux_password(cfg, remember=True):
        try:
            remote.connect(allow_new_host_key=True)
            engine.detect_and_fill_nvme_information(cfg, remote)
            engine.confirm_disk_project_and_batch(cfg, old_sample)
            remote.execute('command -v nvme')
            if not re.match(r'fio-\d+\.', remote.execute('fio --version')[1].strip()):
                raise ValueError('Linux上的fio不是Flexible I/O Tester')
            remote.execute('command -v setsid')
        finally:
            remote.close()
    cfg['deployment_complete'] = True
    controller.cfg['stations'][lane] = {k: v for k, v in cfg.items() if not k.startswith('_')}
    save_dual_configuration(controller.cfg)
    say('主机' + lane, '部署完成，登录与盘信息已保存。读写TRIM许可沿用原配置，请核对专用测试盘。')


def setup(controller):
    choice = input('配置哪台：1=A，2=B，3=两台，0=返回：').strip()
    lanes = {'1': ['A'], '2': ['B'], '3': ['A', 'B']}.get(choice)
    if lanes is None:
        return
    for lane in lanes:
        controller.ensure_lane_idle(lane)
    candidate = load_dual_configuration()
    busy = controller.running()
    if busy:
        # 另一台在采样时不能抢占HID、切换通道设置或改变它已确认的身份。
        if candidate['thermometer'] != controller.cfg['thermometer']:
            raise ValueError('另一台运行期间不能修改共享仪器；请等两台空闲后调整')
        for lane in LANES:
            if candidate['stations'][lane]['thermometer']['channel_map'] != controller.cfg['stations'][lane]['thermometer']['channel_map']:
                raise ValueError('另一台运行期间不能重新分配探针通道')
        for lane in busy:
            if candidate['stations'][lane] != controller.cfg['stations'][lane]:
                raise ValueError('配置文件修改了正在运行的主机' + lane + '，请先恢复该部分')
        controller.cfg = candidate
        say('接收端', '共享仪器继续由在测任务采集；本次只更新空闲主机，不重新打开USB或校时。')
    else:
        controller.pause_sampler()
        controller.cfg = candidate
        physical = copy.deepcopy(controller.cfg['thermometer'])
        physical['channel_map'] = {lane + '_' + metric: channel for lane in LANES
            for metric, channel in controller.cfg['stations'][lane]['thermometer']['channel_map'].items()}
        base.identify_and_verify_jk808({'thermometer': physical})
        physical.pop('channel_map', None)
        controller.cfg['thermometer'] = physical
        save_dual_configuration(controller.cfg)
    for lane in lanes:
        deploy_station(controller, lane)


def choose_lanes(prompt='选择主机：1=A，2=B，3=两台，0=返回：'):
    return {'1': ['A'], '2': ['B'], '3': ['A', 'B']}.get(input(prompt).strip(), [])


def start_selected(controller):
    lanes = choose_lanes()
    if not lanes:
        return
    print('1. L1.2（完成后本台人工关机改BIOS）\n2. L0→写→读（相同配置可续测）')
    print('3. 单项L0\n4. 单项写\n5. 单项读\n0. 返回')
    workflow = {'1': 'l12_ps4', '2': 'sequence', '3': 'l0_ps4', '4': 'write_1m', '5': 'read_1m'}.get(input('选择流程：').strip())
    if workflow is None:
        return
    for lane in lanes:
        cfg = station_configuration(controller.cfg, lane)
        say('主机' + lane, cfg['ssh']['host'], cfg['company_report']['project'], cfg['company_report']['batch'],
            cfg['company_report']['capacity'], 'SN=' + cfg['company_report']['expected_serial'])
    if input('确认以上盘片、探针和所选BIOS条件正确，输入Y开始：').strip().upper() == 'Y':
        controller.start(lanes, workflow)


def run_dual():
    cfg = load_dual_configuration()
    # 此锁与单机旧包相同，防止第二个窗口同时读取仪器或修改接收电脑时间。
    with base.lock_receiver_during_test():
        controller = DualController(cfg)
        try:
            while True:
                print('\nJK808 Linux v2.3.4 双机：一台接收电脑 / 一台JK808 / 两台Linux')
                print('A. 部署或换盘（所选主机空闲时）\nB. 启动/继续所选主机\nC. 查看两台状态\nD. 取消所选主机\n0. 保存收尾后退出')
                choice = input('选择：').strip().upper()
                try:
                    if choice == '0':
                        break
                    if choice == 'A':
                        setup(controller)
                    elif choice == 'B':
                        if not controller.running():
                            controller.cfg = load_dual_configuration()
                        start_selected(controller)
                    elif choice == 'C':
                        controller.show_status()
                    elif choice == 'D':
                        controller.cancel(choose_lanes())
                except Exception as exc:
                    say('接收端', '操作未完成：' + str(exc))
        finally:
            controller.close()


def main():
    if '--dual' not in sys.argv:
        print('JK808 Linux v2.3.4\n1. 原单机测试\n2. 一对二双机测试\n0. 退出')
        choice = input('选择：').strip()
        if choice == '1':
            return base.main()
        if choice != '2':
            return
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError('沿用原包环境要求：Python 3.12 x64')
    local_python = ROOT / '.venv/Scripts/python.exe'
    if Path(sys.executable).resolve() != local_python.resolve():
        with base.lock_receiver_during_test():
            local_python = base.install_local_python_dependencies()
        os.execv(str(local_python), [str(local_python), '-B', '-u', str(Path(__file__).resolve()), '--dual'])
    run_dual()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        say('接收端', '已取消并完成本次任务收尾。')
    except Exception as error:
        say('接收端', '未完成：' + str(error))
        sys.exit(1)
