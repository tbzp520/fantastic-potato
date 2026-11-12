"""回归：驱动被拦截时保留真正原因，未执行项不得伪造统计或掩盖数据丢失。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime_integrity as runtime
import temperature_report as report
import windows_temperature_test as app
from test_revision_0411 import config


class DriverChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.driver = self.root/'runtime/driver/node.exe'
        self.driver.parent.mkdir(parents=True)
        self.driver.write_bytes(b'unit fixture; never executed')
        (self.root/'FILES.sha256').write_text(runtime.BROWSER_DRIVER_SHA256+'  runtime/driver/node.exe\n', 'utf-8')
        self.path_patch = patch.object(runtime, 'browser_driver_path', return_value=self.driver)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def test_modified_driver_is_not_executed(self):
        with patch.object(runtime.subprocess, 'run') as run:
            result = runtime.inspect_browser_driver(self.root)
        self.assertFalse(result['passed'])
        self.assertEqual(result['reason'], 'checksum_mismatch')
        run.assert_not_called()

    def test_policy_error_records_exact_executable_and_hash(self):
        error = OSError('application control policy blocked this file')
        error.winerror = 4556
        record = self.root/'checks/browser.json'
        with patch.object(runtime, 'file_sha256', return_value=runtime.BROWSER_DRIVER_SHA256), \
             patch.object(runtime.subprocess, 'run', side_effect=error):
            with self.assertRaises(runtime.BrowserDriverError):
                runtime.require_browser_driver(self.root, record)
        saved = json.loads(record.read_text('utf-8'))
        self.assertEqual(saved['winerror'], 4556)
        self.assertEqual(saved['file'], str(self.driver))
        self.assertEqual(saved['sha256'], runtime.BROWSER_DRIVER_SHA256)
        self.assertIn('应用控制', saved['error'])

    def test_success_is_only_after_zero_exit(self):
        with patch.object(runtime, 'file_sha256', return_value=runtime.BROWSER_DRIVER_SHA256), \
             patch.object(runtime.subprocess, 'run', return_value=subprocess.CompletedProcess([], 3, '', 'cannot start')):
            self.assertFalse(runtime.inspect_browser_driver(self.root)['passed'])
        with patch.object(runtime, 'file_sha256', return_value=runtime.BROWSER_DRIVER_SHA256), \
             patch.object(runtime.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'v22', '')):
            self.assertTrue(runtime.inspect_browser_driver(self.root)['passed'])


class UnstartedReports(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.host = self.root/'host'
        self.host.mkdir()
        self.output = self.root/'output'
        self.modes = ['online_video', 'idle', 'local_video', 'bit']
        (self.host/'task.json').write_text(json.dumps({'job_id':'example','finished':True,'modes':self.modes}), 'utf-8')
        self.status = {'job_id':'example','phase':'未完成','mode':'online_video','message':'OSError: [WinError 4556] application control blocked this file'}
        self.save_status()

    def save_status(self):
        (self.host/'status.json').write_text(json.dumps(self.status), 'utf-8')

    def test_active_startup_failure_and_three_unexecuted_modes_have_no_temperatures(self):
        for mode in self.modes:
            result, image = report.export_stage(self.host/mode, self.output, mode=mode, capacity='512G')
            self.assertEqual(result['status'], '启动失败' if mode=='online_video' else '未执行')
            self.assertEqual(result['statistics'], {})
            self.assertEqual(result['selected_count'], 0)
            self.assertFalse(result['workload_ok'])
            self.assertIsNone(image)
            if mode=='online_video':self.assertIn('4556', result['error'])
        self.assertFalse(list(self.output.glob('*.png')))

    def test_prior_formal_start_with_missing_conditions_is_still_a_data_error(self):
        self.status['start_epoch'] = 123
        self.save_status()
        with self.assertRaises(FileNotFoundError):
            report.export_stage(self.host/'online_video', self.output, mode='online_video')

    def test_unknown_or_completed_job_does_not_become_unexecuted(self):
        self.status['phase'] = '完成'
        self.save_status()
        with self.assertRaises(FileNotFoundError):
            report.export_stage(self.host/'idle', self.output, mode='idle')

    def test_new_failure_record_is_used_without_inventing_conditions(self):
        stage = self.host/'bit'
        stage.mkdir()
        (stage/'stage_failure.json').write_text(json.dumps({'mode':'bit','formal_started':False,'error':'BIT launch was blocked'}), 'utf-8')
        result, _ = report.export_stage(stage, self.output, mode='bit')
        self.assertEqual(result['status'], '启动失败')
        self.assertIn('BIT launch', result['error'])
        self.assertFalse((stage/'conditions.json').exists())


class PreflightChecks(unittest.TestCase):
    def test_offline_single_modes_are_not_blocked_by_browser_driver(self):
        with patch.object(app, 'resolve_hwinfo_executable', return_value=Path('hwinfo.exe')), \
             patch.object(runtime, 'require_browser_driver') as check:
            app.preflight_host(config(), ['bit'])
        check.assert_not_called()

    def test_online_mode_includes_native_edge_diagnostic_without_driver(self):
        with patch.object(app, 'resolve_hwinfo_executable', return_value=Path('hwinfo.exe')), \
             patch('native_edge.inspect_native_edge', return_value={'passed':False,'error':'本机Edge缺失'}), \
             patch.object(runtime, 'require_browser_driver') as driver:
            problems=app.preflight_host(config(), ['online_video'])
        self.assertTrue(any('本机Edge缺失' in p for p in problems))
        driver.assert_not_called()

    def test_cleanup_without_started_runtime_does_not_mask_startup_error(self):
        app.stop_online_video({'runtime':None, 'context':None})


if __name__ == '__main__':unittest.main()
