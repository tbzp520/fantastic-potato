"""平台批次/物理主机续测边界：只操作临时测次，不启动负载或连接设备。"""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import batch_report as batch
import resume_flow as flow
import windows_temperature_test as app
from test_revision_0411 import config, fixture_task
from test_revision_0415 import add_run, cell


def identity(report, platform='21SJ', machine='machine-A', **raw_changes):
    from platform_identity import build_platform_snapshot
    result = copy.deepcopy(report)
    raw = dict(
        manufacturer='LENOVO', model=platform+'0000CD', family='ThinkBook 14 G7+ IAH',
        product_uuid=('11111111-1111-4111-8111-111111111111' if machine == 'machine-A'
                      else '22222222-2222-4222-8222-222222222222'),
        cpu='Intel '+platform, os_caption='Microsoft Windows 11', os_version='10.0',
        os_build='26200', os_arch='64-bit', bios='BIOS '+platform)
    raw.update(raw_changes)
    snapshot = build_platform_snapshot(raw)
    result.update({key:snapshot[key] for key in batch.PLATFORM_FIELDS})
    return result


class PlatformBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'2260'/'10-21'; self.root.mkdir(parents=True)
        self.template = app.ROOT/'温度模版_原始.xlsx'
        self.enterContext(patch('builtins.print'))

    def run_record(self, name, capacity='512G', token='a', platform='21SJ', machine='machine-A', legacy=False):
        folder = self.root/name
        cfg = add_run(folder, capacity, ['idle'], token=token)
        if legacy:
            cfg['report'].pop('platform_id', None)
            cfg['report'].pop('machine_id', None)
        else:
            cfg['report'] = identity(cfg['report'], platform, machine)
        path = folder/'手工复测/本次任务.json'
        task = batch.read_json(path); task['config'] = cfg; app.save_json(path, task)
        return cfg, folder

    def rebuild(self):
        return batch.rebuild_batch(self.root, '2260', '10-21', self.template)

    def registry(self, report=None):
        data = dict(format_version=1, project='2260', batch='10-21', samples={}, sources={})
        if report is not None:
            data['platform_identity'] = {key:report[key] for key in batch.PLATFORM_FIELDS if key in report}
        app.save_json(self.root/batch.RECORD, data)
        (self.root/batch.BOOK).write_bytes(b'unit: existing blank workbook, do not change')

    def test_same_platform_different_physical_hosts_can_add_other_capacities(self):
        first,_ = self.run_record('first')
        second = identity(first['report'], machine='machine-B')
        second.update(capacity='1T', system_disk_model='XP-1T', expected_serial='SN-1T')
        batch.check_batch_target(self.root, second)

    def test_different_platform_is_blocked_before_creating_a_workbook(self):
        cfg,_ = self.run_record('first')
        changed = identity(cfg['report'], platform='21TK')
        with patch.object(batch, 'create_empty_company_template') as create, self.assertRaisesRegex(ValueError, '新批次'):
            batch.check_batch_target(self.root, changed)
        create.assert_not_called(); self.assertFalse((self.root/batch.BOOK).exists())

    def test_receiver_platform_conflict_stops_before_instrument_clock_or_workload(self):
        cfg,_ = self.run_record('first')
        candidate = copy.deepcopy(cfg); candidate['report'] = identity(cfg['report'], platform='21TK')
        link = Mock(); link.request.side_effect = [
            dict(protocol=2, version=app.VERSION, station=cfg['station'], busy=False),
            dict(sample=candidate['report'], host_tools={}, problems=[])]
        with patch.object(app, 'require_administrator'), \
             patch.object(app, 'SerialConnection', return_value=link), \
             patch.object(app, 'adopt_current_sample', return_value=candidate), \
             patch.object(app, 'report_batch_folder', return_value=self.root), \
             patch.object(app.ctypes.windll.kernel32, 'SetThreadExecutionState', return_value=1), \
             patch.object(app, 'JK808Thermometer') as instrument, \
             patch.object(app, 'synchronize_two_clocks') as clock, self.assertRaisesRegex(ValueError, '新批次'):
            app.run_receiver(cfg, ['idle'])
        self.assertEqual([call.args[0] for call in link.request.call_args_list], ['hello', 'preflight'])
        instrument.assert_not_called(); clock.assert_not_called()
        link.close.assert_called_once_with()

    def test_cpu_os_or_bios_change_is_not_treated_as_the_same_platform(self):
        cfg,_ = self.run_record('first')
        for change in (dict(cpu='Intel Different CPU'), dict(os_build='26300'), dict(bios='BIOS newer')):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, '平台'):
                batch.check_batch_target(self.root, identity(cfg['report'], **change))

    def test_all_saved_tasks_are_checked_even_before_they_have_results(self):
        _,first = self.run_record('first')
        cfg,second = self.run_record('second', capacity='1T', token='b', platform='21TK')
        (first/'手工复测/结果汇总.json').unlink(); (second/'手工复测/结果汇总.json').unlink()
        with self.assertRaisesRegex(ValueError, '平台'):
            batch.check_batch_target(self.root, cfg['report'])

    def test_task_and_host_snapshot_platform_must_agree(self):
        cfg,folder = self.run_record('first')
        snapshot = copy.deepcopy(cfg); snapshot['report'] = identity(cfg['report'], platform='21TK')
        app.save_json(folder/'手工复测/主机数据/config_snapshot.json', snapshot)
        with self.assertRaisesRegex(ValueError, '平台'):
            batch.check_batch_target(self.root, cfg['report'])

    def test_new_and_legacy_identity_cannot_mix_in_either_direction(self):
        for stored_new in (False, True):
            with self.subTest(stored_new=stored_new), tempfile.TemporaryDirectory() as directory:
                old_root = self.root; self.root = Path(directory)
                try:
                    cfg,_ = self.run_record('first', legacy=not stored_new)
                    candidate = copy.deepcopy(cfg['report'])
                    if stored_new:
                        for key in ('platform_id', 'machine_id'): candidate.pop(key)
                    else:
                        candidate = identity(candidate)
                    with self.assertRaisesRegex(ValueError, '离线'):
                        batch.check_batch_target(self.root, candidate)
                finally:
                    self.root = old_root

    def test_empty_registered_workbook_still_has_platform_ownership(self):
        current = identity(config()['report'])
        self.registry(current)
        before = (self.root/batch.BOOK).read_bytes()
        with self.assertRaisesRegex(ValueError, '平台'):
            batch.check_batch_target(self.root, identity(current, platform='21TK'))
        self.assertEqual((self.root/batch.BOOK).read_bytes(), before)

    def test_old_empty_registered_workbook_cannot_be_silently_claimed_by_new_platform(self):
        self.registry()
        with self.assertRaisesRegex(ValueError, '新批次'):
            batch.check_batch_target(self.root, identity(config()['report']))

    def test_same_platform_empty_registry_allows_another_machine(self):
        report = identity(config()['report'])
        self.registry(report)
        batch.check_batch_target(self.root, identity(report, machine='machine-B'))

    def test_rebuild_persists_platform_registration_and_preserves_statistics(self):
        cfg,_ = self.run_record('first')
        result = self.rebuild()
        self.assertTrue(result['passed'])
        saved = batch.read_json(self.root/batch.RECORD)['platform_identity']
        self.assertEqual(saved['platform_id'], cfg['report']['platform_id'])
        self.assertEqual(cell(self.root/batch.BOOK, 1, 'C11'), '45')

    def test_rebuild_rejects_platform_change_without_overwriting_existing_artifacts(self):
        self.run_record('first'); self.rebuild()
        paths = [self.root/name for name in (batch.BOOK, batch.RECORD, batch.PAGE)]
        before = [path.read_bytes() for path in paths]
        self.run_record('second', capacity='1T', token='b', platform='21TK')
        with self.assertRaisesRegex(ValueError, '平台'):
            self.rebuild()
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_registration_still_protects_platform_when_original_task_is_unavailable(self):
        cfg,folder = self.run_record('first'); self.rebuild()
        (folder/'手工复测/本次任务.json').unlink()
        with self.assertRaisesRegex(ValueError, '平台'):
            batch.check_batch_target(self.root, identity(cfg['report'], platform='21TK'))

    def test_legacy_only_records_keep_previous_offline_rules(self):
        cfg,folder = self.run_record('first', legacy=True)
        cfg['report'].update(platform='legacy manual label A', cpu='legacy CPU')
        task = batch.read_json(folder/'手工复测/本次任务.json'); task['config'] = cfg
        app.save_json(folder/'手工复测/本次任务.json', task)
        other = copy.deepcopy(cfg['report']); other['platform'] = 'legacy manual label B'
        batch.check_batch_target(self.root, other)
        self.assertTrue(self.rebuild()['passed'])

    def test_capacity_and_serial_conflict_remains_blocked_on_same_platform(self):
        cfg,_ = self.run_record('first')
        other = identity(cfg['report'], machine='machine-B'); other['expected_serial'] = 'OTHER'
        with self.assertRaisesRegex(ValueError, '原盘数据不会覆盖'):
            batch.check_batch_target(self.root, other)


class PlatformResumeTests(unittest.TestCase):
    def test_resume_rejects_another_physical_machine_on_same_platform(self):
        original = identity(config()['report'])
        with self.assertRaisesRegex(ValueError, 'C补取'):
            flow.check_identity(original, identity(original, machine='machine-B'))

    def test_disk_only_preflight_still_requires_the_same_physical_machine(self):
        original = identity(config()['report'])
        with self.assertRaisesRegex(ValueError, '未启动负载'):
            flow.check_identity(original, identity(original, machine='machine-B'), disk_only=True)

    def test_identity_comparison_for_platform_only_does_not_require_machine_uuid(self):
        original = identity(config()['report'])
        flow.check_identity(original, identity(original, machine='machine-B'), physical=False)

    def test_one_sided_new_identity_blocks_resume_with_recovery_guidance(self):
        legacy = config()['report']
        legacy.pop('platform_id', None); legacy.pop('machine_id', None)
        current = identity(legacy)
        for before,after in ((legacy,current), (current,legacy)):
            with self.subTest(before=before.get('platform_id')), self.assertRaisesRegex(ValueError, '无需重跑'):
                flow.check_identity(before, after)

    def test_two_legacy_reports_keep_original_resume_rules(self):
        first = config()['report']
        first.pop('platform_id', None); first.pop('machine_id', None)
        second = copy.deepcopy(first)
        first['platform'] = 'old manual A'; second['platform'] = 'old manual B'
        flow.check_identity(first, second)

    def test_saved_attempt_cannot_register_a_different_machine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'original'
            cfg = config(); cfg['report'] = identity(cfg['report'])
            fixture_task(root, ['idle'], ['idle'], cfg=cfg)
            retry = root/'手工复测/续测记录/retry'
            other = copy.deepcopy(cfg); other['report'] = identity(cfg['report'], machine='machine-B')
            fixture_task(retry, ['idle'], ['idle'], cfg=other, token='b')
            with self.assertRaisesRegex(ValueError, 'C补取'):
                flow.add_attempt(root, retry, app.save_json)
            self.assertFalse((root/'手工复测/续测记录.json').exists())

    def test_saved_host_snapshot_is_checked_before_recalculating_any_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'original'
            cfg = config(); cfg['report'] = identity(cfg['report'])
            fixture_task(root, ['idle'], ['idle'], cfg=cfg)
            other = copy.deepcopy(cfg); other['report'] = identity(cfg['report'], machine='machine-B')
            app.save_json(root/'手工复测/主机数据/config_snapshot.json', other)
            with patch('temperature_report.build_stage_report') as build, self.assertRaisesRegex(ValueError, '平台'):
                flow.plan(root)
            build.assert_not_called()


if __name__ == '__main__':
    unittest.main()
