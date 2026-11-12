"""双机离线验证。用假HID/SSH，不访问真实设备，不校时、不TRIM或启动fio。"""
from contextlib import ExitStack, nullcontext
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dual_linux as dual
import test_program as baseline


def ready_config():
    single = baseline.ConfigurationTests().configured_receiver()
    cfg = dual.new_dual_configuration(single)
    cfg['thermometer'].update(protocol_verified=True, hid_path_hex='1234')
    for lane in dual.LANES:
        station = cfg['stations'][lane]
        station.update(deployment_complete=True, detected_disk={'firmware': 'DEMO_FW'})
        station['ssh'].update(host='192.0.2.' + ('11' if lane == 'A' else '12'),
                              password='password-' + lane, device='/dev/nvme0n1',
                              expected_pci='0000:01:00.0', allow_destructive=True)
        station['company_report'].update(project='离线测试', batch='模拟批次', capacity='512G',
                                         expected_serial='SN-' + lane, test_version='DEMO_FW')
        materialized = dual.station_configuration(cfg, lane)
        station['sample_assignment'] = dict(dual.base.disk_assignment_fields(materialized), confirmed_beijing='offline')
    return cfg


class DualConfigurationTests(unittest.TestCase):
    def test_second_host_cannot_inherit_disk_password_or_write_permission(self):
        old = baseline.ConfigurationTests().configured_receiver()
        old['ssh'].update(password='must-not-copy', device='/dev/nvme3n1')
        cfg = dual.new_dual_configuration(old)
        b = cfg['stations']['B']
        self.assertEqual(b['ssh']['host'], '')
        self.assertEqual(b['ssh']['password'], '')
        self.assertEqual(b['ssh']['device'], '')
        self.assertFalse(b['ssh']['allow_destructive'])
        self.assertIsNone(b['sample_assignment'])
        self.assertEqual(b['detected_disk'], {})
        self.assertFalse(cfg['thermometer']['protocol_verified'])
        self.assertEqual(old['ssh']['password'], 'must-not-copy')

    def test_disjoint_six_channel_map_and_duplicate_host_check(self):
        cfg = ready_config()
        dual.validate_dual_configuration(cfg)
        cfg['stations']['B']['thermometer']['channel_map']['AMB'] = 3
        with self.assertRaisesRegex(ValueError, '互不重复'):
            dual.validate_dual_configuration(cfg)
        cfg = ready_config()
        cfg['stations']['B']['ssh']['host'] = cfg['stations']['A']['ssh']['host']
        with self.assertRaisesRegex(ValueError, '同一个SSH|同一个 SSH'):
            dual.validate_dual_configuration(cfg)

    def test_same_capacity_disks_have_distinct_output_and_identity(self):
        cfg = ready_config()
        a, b = [dual.station_configuration(cfg, lane) for lane in dual.LANES]
        self.assertNotEqual(dual.base.get_report_batch_folder(a), dual.base.get_report_batch_folder(b))
        self.assertNotEqual(a['ssh']['password_env'], b['ssh']['password_env'])
        self.assertNotEqual(a['company_report']['expected_serial'], b['company_report']['expected_serial'])
        self.assertEqual(b['thermometer']['channel_map'], dual.DEFAULT_MAPS['B'])
        dual.base.validate_test_configuration(a, 'read_1m')
        dual.base.validate_test_configuration(b, 'read_1m')

    def test_worker_passwords_and_snapshots_are_isolated(self):
        cfg = ready_config()
        a, b = [dual.station_configuration(cfg, lane) for lane in dual.LANES]
        barrier, observed = threading.Barrier(2), {}
        def run(lane, station):
            with dual.base.supply_linux_password(station):
                barrier.wait(timeout=3)
                observed[lane] = os.environ[station['ssh']['password_env']]
                barrier.wait(timeout=3)
        threads = [threading.Thread(target=run, args=(lane, station)) for lane, station in [('A', a), ('B', b)]]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        self.assertEqual(observed, {'A': 'password-A', 'B': 'password-B'})
        a['_dual_control'] = threading.Lock()  # 不能被deepcopy的私有运行对象也不进入报告。
        snapshot = dual.base.configuration_without_password(a)
        self.assertNotIn('_dual_control', snapshot)
        self.assertNotIn('password-A', json.dumps(snapshot))


class CancellationTests(unittest.TestCase):
    def test_successful_item_does_not_cancel_sequence_or_other_host(self):
        a, b = threading.Event(), threading.Event()
        item = dual.CancellationEvent(a)
        item.set()
        self.assertTrue(item.is_set())
        self.assertFalse(a.is_set())
        self.assertFalse(dual.CancellationEvent(a).is_set())
        a.set()
        self.assertTrue(dual.CancellationEvent(a).wait(.1))
        self.assertFalse(dual.CancellationEvent(b).is_set())


class SamplerTests(unittest.TestCase):
    def test_actual_two_consumers_share_each_physical_read_and_common_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            reads, instances, collected = [], [], {}
            class FakeThermometer:
                def __init__(self, settings, trace):
                    instances.append(self)
                    self.trace = trace
                def read(self):
                    reads.append(time.monotonic())
                    self.trace('rx_hid', b'offline')
                    values = {n: 20.0+n for n in range(1, 9)}
                    return values, {n: 'ok' for n in values}, {n: int(v*10) for n, v in values.items()}
                def close(self): pass
            cfg = ready_config()
            sampler = dual.SharedSampler(cfg['thermometer'], Path(directory)/'shared', FakeThermometer)
            sampler.start()
            start, epoch = sampler.next_start()
            def collect(lane):
                output = Path(directory)/lane
                output.mkdir()
                rows, errors = [], []
                control = dual.LaneControl(lane, sampler, None, None, threading.Event())
                control.collect_surface(dual.station_configuration(cfg, lane), epoch, start, 2,
                                         control.make_stop_event(), output, rows, errors)
                collected[lane] = (rows, errors)
            threads = [threading.Thread(target=collect, args=(lane,)) for lane in dual.LANES]
            try:
                for thread in threads: thread.start()
                for thread in threads: thread.join(timeout=6)
                self.assertFalse(any(thread.is_alive() for thread in threads))
            finally:
                sampler.close()
            self.assertEqual(len(instances), 1)
            self.assertEqual(len(reads), 2)
            a, b = collected['A'][0], collected['B'][0]
            self.assertEqual(len(a), 2)
            self.assertEqual(len(b), 2)
            self.assertEqual(a[0]['ActualTime'], b[0]['ActualTime'])
            self.assertEqual(a[0]['CPU_Surface'], 21)
            self.assertEqual(a[0]['AMB'], 23)
            self.assertEqual(b[0]['CPU_Surface'], 24)
            self.assertEqual(b[0]['AMB'], 26)
            self.assertEqual(collected['A'][1] + collected['B'][1], [])
            records = [json.loads(line) for line in (sampler.folder/'JK808八通道共享原始记录.jsonl').read_text('utf-8').splitlines()]
            self.assertEqual(len(records), 2)

    def test_late_measurement_never_becomes_next_seconds_value(self):
        cfg = ready_config()
        sampler = dual.SharedSampler(cfg['thermometer'], '.')
        sampler.anchor = time.monotonic()-10
        sampler.records[1] = {'slot': 1, 'values': {1: 99}}
        record = sampler.get(2, threading.Event())
        self.assertEqual(record['values'], {})
        self.assertIn('缺失', record['error'])

    def test_probe_failure_is_local_to_its_host(self):
        cfg = ready_config()
        values = {n: float(n) for n in range(1, 9)}
        values[4] = None
        statuses = {n: 'ok' for n in range(1, 9)}
        statuses[4] = 'invalid_code'
        record = {'actual_time': 'offline', 'error': '', 'values': values, 'statuses': statuses,
                  'raw': {n: n*10 for n in range(1, 9)}, 'trace': []}
        class FakeSource:
            folder = Path('.')
            def session(self, start, duration): return nullcontext(10)
            def get(self, slot, cancel): return record
        with tempfile.TemporaryDirectory() as directory:
            for lane in dual.LANES:
                rows, errors = [], []
                folder = Path(directory)/lane
                folder.mkdir()
                control = dual.LaneControl(lane, FakeSource(), None, None, threading.Event())
                control.collect_surface(dual.station_configuration(cfg, lane), time.time(), time.monotonic(),
                                        1, control.make_stop_event(), folder, rows, errors)
                self.assertEqual(errors, [])
                self.assertEqual(rows[0]['CPU_Surface_Error'], '' if lane == 'A' else 'invalid_code')
                if lane == 'A': self.assertEqual(rows[0]['CPU_Surface'], 1)
                else: self.assertIsNone(rows[0]['CPU_Surface'])


class ClockTests(unittest.TestCase):
    def test_host_sync_can_never_set_windows_clock(self):
        cfg = dual.station_configuration(ready_config(), 'B')
        remote = Mock()
        def execute(command, **kwargs):
            if command == 'id -u': return (0, '0', '')
            if command == 'date +%z': return (0, '+0800', '')
            return (0, 'ok', '')
        remote.execute.side_effect = execute
        engine = Mock()
        engine.LinuxConnection.return_value = remote
        engine.obtain_internet_standard_time.return_value = {'source': 'offline'}
        engine.standard_time_now.side_effect = lambda ref: time.time()
        engine.measure_linux_clock_difference.return_value = {'offset_s': .001, 'rtt_s': .002}
        engine.quote_shell_argument = dual.base.quote_shell_argument
        with tempfile.TemporaryDirectory() as directory, patch.object(dual.base, 'set_windows_system_time', side_effect=AssertionError('不得改接收电脑时间')):
            clock = dual.DualClock(directory)
            clock.prepared = True
            result = clock.synchronize_host(engine, cfg, directory)
            self.assertEqual(result['outcome'], 'ok')
            self.assertFalse(result['changed_windows'])
            engine.set_windows_system_time.assert_not_called()
            record = json.loads((Path(directory)/'北京时间校准记录.json').read_text('utf-8'))
            self.assertFalse(record['changed_windows'])

    def test_receiver_clock_drift_blocks_new_host_without_modifying_any_clock(self):
        cfg = dual.station_configuration(ready_config(), 'A')
        engine = Mock()
        engine.obtain_internet_standard_time.return_value = {'source': 'offline'}
        engine.standard_time_now.return_value = time.time()-60
        with tempfile.TemporaryDirectory() as directory:
            clock = dual.DualClock(directory)
            clock.prepared = True
            with self.assertRaisesRegex(RuntimeError, '未改Windows'):
                clock.synchronize_host(engine, cfg, directory)
            engine.LinuxConnection.return_value.connect.assert_not_called()


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = ready_config()
        self.controller = dual.DualController(self.cfg, self.tmp.name)
        self.addCleanup(self.controller.close)

    def test_flow_state_isolated_between_stations(self):
        a, b = [self.controller.engines[lane] for lane in dual.LANES]
        a.save_workflow_progress({'id': 'a'*32, 'format_version': 1, 'phase': 'sequence_paused'})
        b.save_workflow_progress({'id': 'b'*32, 'format_version': 1, 'phase': 'waiting_for_manual_bios'})
        self.assertEqual(a.load_workflow_progress()['id'], 'a'*32)
        self.assertEqual(b.load_workflow_progress()['id'], 'b'*32)

    def test_aliased_ip_addresses_to_same_linux_are_rejected_before_clocks(self):
        boot = '12345678-1234-1234-1234-123456789012'
        for engine in self.controller.engines.values():
            remote = Mock()
            remote.execute.side_effect = [(0, boot, ''), (1, '', '')]
            engine.LinuxConnection = Mock(return_value=remote)
        with self.controller.reserve_host('A', dual.station_configuration(self.cfg, 'A')):
            with self.assertRaisesRegex(RuntimeError, '同一台Linux'):
                with self.controller.reserve_host('B', dual.station_configuration(self.cfg, 'B')):
                    self.fail('不得启动第二台')
        self.assertEqual(self.controller.hosts, {})

    def test_cancel_one_does_not_cancel_other_and_config_edit_requires_idle(self):
        a, b = threading.Event(), threading.Event()
        self.controller.jobs = {'A': {'running': True, 'cancel': a}, 'B': {'running': True, 'cancel': b}}
        with patch.object(dual, 'say'):
            self.controller.cancel(['A'])
        self.assertTrue(a.is_set())
        self.assertFalse(b.is_set())
        with self.assertRaisesRegex(RuntimeError, '两台任务'):
            self.controller.ensure_idle()
        for job in self.controller.jobs.values(): job['running'] = False

    def test_idle_host_can_deploy_while_other_host_runs_without_reopening_usb(self):
        self.controller.jobs['A'] = {'running': True, 'cancel': threading.Event()}
        candidate = copy.deepcopy(self.controller.cfg)
        candidate['stations']['B']['ssh']['host'] = '192.0.2.99'
        with patch('builtins.input', return_value='2'), patch.object(dual, 'load_dual_configuration', return_value=candidate), \
             patch.object(dual, 'deploy_station') as deploy, patch.object(dual.base, 'identify_and_verify_jk808', side_effect=AssertionError('另一台在采样')), \
             patch.object(dual, 'say'):
            dual.setup(self.controller)
        deploy.assert_called_once_with(self.controller, 'B')
        self.assertEqual(self.controller.cfg['stations']['B']['ssh']['host'], '192.0.2.99')
        self.assertTrue(self.controller.jobs['A']['running'])
        self.controller.jobs['A']['running'] = False

    def test_running_host_cannot_have_its_configuration_or_channel_changed(self):
        self.controller.jobs['A'] = {'running': True, 'cancel': threading.Event()}
        candidate = copy.deepcopy(self.controller.cfg)
        candidate['thermometer']['timeout_s'] = .5
        with patch('builtins.input', return_value='2'), patch.object(dual, 'load_dual_configuration', return_value=candidate), \
             patch.object(dual, 'deploy_station', side_effect=AssertionError):
            with self.assertRaisesRegex(ValueError, '共享仪器'):
                dual.setup(self.controller)
        self.controller.jobs['A']['running'] = False

    def test_core_executor_uses_local_completion_event_not_job_cancellation(self):
        lane, engine = 'A', self.controller.engines['A']
        cfg = dual.station_configuration(self.cfg, lane)
        cancel = threading.Event()
        cfg['_dual_control'] = dual.LaneControl(lane, None, None, engine, cancel)
        output = Path(self.tmp.name)/'result'
        output.mkdir()
        events = []
        def complete(configuration, remote, smart, folder, stop):
            events.append(stop)
            stop.set()
            return {'mode': 'l0_ps4', 'completed': True, 'errors': []}
        with patch.object(engine, 'create_test_result_folder', return_value=output), \
             patch.object(engine, 'LinuxConnection'), patch.object(engine, 'synchronize_receiver_clock', return_value=None), \
             patch.object(engine, 'check_linux_identity_before_test', return_value={}), \
             patch.object(engine, 'check_batch_disk_identity'), patch.object(engine, 'run_l0_ps4_test', side_effect=complete), \
             patch.object(engine, 'print'):
            result = engine.execute_selected_test(cfg, 'l0_ps4', receiver_locked=True)
        self.assertTrue(result['results_saved'])
        self.assertTrue(events[0].is_set())
        self.assertFalse(cancel.is_set())
        self.assertFalse(cfg['_dual_control'].make_stop_event().is_set())

    def test_two_actual_workers_overlap_and_one_failure_keeps_other_running(self):
        barriers = threading.Barrier(2)
        b_running, finish_b = threading.Event(), threading.Event()
        a_engine, b_engine = [self.controller.engines[lane] for lane in dual.LANES]
        def a_run(cfg, mode):
            barriers.wait(timeout=3)
            raise RuntimeError('offline A failure')
        def b_run(cfg, mode):
            barriers.wait(timeout=3)
            b_running.set()
            if not finish_b.wait(4): raise RuntimeError('test timeout')
        a_engine.run_single_test, b_engine.run_single_test = a_run, b_run
        sampler = Mock()
        sampler.folder = Path(self.tmp.name)/'shared'
        self.controller.sampler = sampler
        with patch.object(self.controller.clock, 'prepare_receiver') as prepare, \
             patch.object(self.controller, 'reserve_host', side_effect=lambda *args: nullcontext()), patch.object(dual, 'say'):
            self.controller.start(['A', 'B'], 'l0_ps4')
            try:
                self.assertTrue(b_running.wait(3))
                self.controller.jobs['A']['thread'].join(timeout=3)
                self.assertFalse(self.controller.jobs['A']['running'])
                self.assertTrue(self.controller.jobs['B']['running'])
                self.assertFalse(self.controller.jobs['B']['cancel'].is_set())
                prepare.assert_called_once()
                with self.assertRaisesRegex(RuntimeError, '重复启动'):
                    self.controller.start(['B'], 'l0_ps4')
                failed_status = json.loads((Path(self.tmp.name)/'当前状态.json').read_text('utf-8'))
                self.assertIn('offline A failure', failed_status['A']['error'])
                # A可在B仍运行时重试；不得再次修改接收电脑系统时间。
                a_engine.run_single_test = Mock()
                self.controller.start(['A'], 'l0_ps4')
                self.controller.jobs['A']['thread'].join(timeout=3)
                prepare.assert_called_once()
                self.assertTrue(self.controller.jobs['B']['running'])
            finally:
                finish_b.set()
                self.controller.jobs['B']['thread'].join(timeout=4)
        self.assertEqual(self.controller.jobs['B']['phase'], '已完成')
        status = json.loads((Path(self.tmp.name)/'当前状态.json').read_text('utf-8'))
        self.assertEqual(status['A']['phase'], '已完成')
        self.assertEqual(status['B']['phase'], '已完成')
        self.assertNotIn('password-', json.dumps(status))


if __name__ == '__main__':
    unittest.main()
