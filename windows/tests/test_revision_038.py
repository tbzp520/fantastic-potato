"""接收端不得依赖测试主机窗口库；缺少必要依赖仍应在操作设备前报错。"""
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import nullcontext

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import runtime_check
import environment_setup
import windows_temperature_test as app
import 集中联调 as commissioning


class RoleRuntimeTests(unittest.TestCase):
    def setUp(self):
        idle = patch.object(environment_setup, 'require_installers_idle')
        idle.start()
        self.addCleanup(idle.stop)

    def probe(self, mode):
        result = subprocess.run([sys.executable, '-B', str(ROOT/'tests/receiver_without_ui_probe.py'), mode],
            cwd=ROOT, capture_output=True, encoding='utf-8', errors='replace', timeout=90,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        self.assertEqual(result.returncode, 0, result.stdout+'\n'+result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_receiver_entry_runs_when_window_libraries_are_blocked(self):
        self.assertEqual(self.probe('receiver')['window_import_attempts'], [])

    def test_unified_entry_selects_receiver_before_checking_dependencies(self):
        self.assertEqual(self.probe('unified')['window_import_attempts'], [])

    def test_exit_before_role_never_imports_window_libraries(self):
        self.assertEqual(self.probe('exit')['window_import_attempts'], [])

    def test_receiver_generates_four_charts_html_and_excel_with_window_imports_blocked(self):
        self.assertEqual(self.probe('reports')['window_import_attempts'], [])

    def test_host_still_stops_for_missing_required_window_library(self):
        self.assertIn('pythoncom', self.probe('host')['window_import_attempts'])

    def test_receiver_missing_hid_fails_and_keeps_diagnostic(self):
        actual = importlib.import_module
        def missing_hid(name):
            if name == 'hid': raise ImportError('simulated missing hid.dll')
            return actual(name)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(runtime_check.importlib, 'import_module', side_effect=missing_hid):
            with self.assertRaisesRegex(RuntimeError, '接收电脑.*检查未通过'):
                runtime_check.require_runtime('receiver', directory)
            report = json.loads((Path(directory)/'部署记录/最近运行库检查_receiver.json').read_text('utf-8'))
            self.assertFalse(report['modules']['hid']['ok'])
            self.assertEqual(report['dlls'], {})

    def test_failed_receiver_check_happens_before_config_or_hardware_operations(self):
        with patch('启动校验.verify_release'), \
             patch.object(environment_setup, 'restart_as_administrator', return_value=False), \
             patch.object(app, 'single_instance', return_value=nullcontext()), \
             patch.object(sys, 'argv', ['集中联调.py', '2']), \
             patch.object(runtime_check, 'require_runtime', side_effect=RuntimeError('missing hid')) as check, \
             patch.object(app, 'load_config') as config, patch.object(app, 'require_administrator') as admin, \
             patch.object(commissioning, 'prepare_configuration') as hardware, patch('sys.stdout', io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'missing hid'): commissioning.main()
            check.assert_called_once_with('receiver', app.ROOT)
            config.assert_not_called(); admin.assert_not_called(); hardware.assert_not_called()

    def test_cli_receiver_uses_receiver_dependency_check(self):
        with patch.object(sys, 'argv', ['windows_temperature_test.py', 'receiver']), \
             patch.object(runtime_check, 'require_runtime', side_effect=RuntimeError('stop before devices')) as check, \
             patch.object(app, 'load_config') as config:
            with self.assertRaisesRegex(RuntimeError, 'stop before devices'): app.main()
            check.assert_called_once_with('receiver', app.ROOT)
            config.assert_not_called()

    def test_runtime_cli_accepts_receiver_role(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'probe.json'
            expected = {'passed': True, 'modules': {}}
            with patch.object(sys, 'argv', ['runtime_check.py', '--role', 'receiver', '--output', str(output)]), \
                 patch.object(runtime_check, 'inspect_runtime', return_value=expected) as check, \
                 patch('sys.stdout', io.StringIO()):
                self.assertEqual(runtime_check.main(), 0)
                check.assert_called_once_with('receiver')
            self.assertEqual(json.loads(output.read_text('utf-8')), expected)

    def test_invalid_role_cannot_silently_skip_checks(self):
        with self.assertRaises(ValueError): runtime_check.inspect_runtime('unknown')


if __name__ == '__main__':
    unittest.main()
