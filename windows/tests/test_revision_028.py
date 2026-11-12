"""合成控件复现TabControl异常，并验证故障记录；不含实机采集文件。"""
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import windows_temperature_test as app

spec = importlib.util.spec_from_file_location('logging_check_028', app.ROOT/'验证HWiNFO自动记录.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class CapturedSettings:
    """合成已适配的控件ID和初始状态；模拟弹窗打开、关闭和保存。"""
    def __init__(self, general_visible=True):
        # Synthetic UI state, using only IDs already defined by the public adapter.
        self.controls = {}
        for control_id, text in ((2158,'2000'), (2162,'100'), (2506,'0')):
            self.controls[control_id] = dict(control_id=control_id,class_name='Edit',text=text,value=text)
        for control_id, text in ((2159,'Poll'), (2181,'Direct to disk'), (1172,'Enabled'), (1,'OK'), (2,'Cancel')):
            self.controls[control_id] = dict(control_id=control_id,class_name='Button',text=text,value=0)
        self.general_visible = general_visible
        self.visible = False
        self.pid, self.handle = 5332, 264446
        self.open_count = 0
        self.uia_select_count = 0
        self.native_tab_reads = 0
        self.sensor = Mock()
        self.sensor.wrapper_object.return_value = SimpleNamespace(
            handle=460744, element_info=SimpleNamespace(process_id=self.pid))
        config, logging = Mock(), Mock()
        config.click.side_effect = self.open
        logging.window_text.return_value = '&Log Start'
        self.sensor.child_window.side_effect = lambda **kw: {1436: config, 1437: logging}[kw['control_id']]

    def open(self):
        self.visible = True
        self.open_count += 1

    def desktop(self, backend):
        desktop = Mock()
        desktop.window.side_effect = lambda **kw: self.window(backend, kw)
        return desktop

    def window(self, backend, selector):
        assert selector['process'] == self.pid
        if 'handle' in selector:
            assert selector['handle'] == self.handle
        window = Mock()
        window.wrapper_object.return_value = SimpleNamespace(
            handle=self.handle, element_info=SimpleNamespace(process_id=self.pid))
        window.exists.side_effect = lambda **kw: self.visible
        window.is_visible.side_effect = lambda: self.visible
        window.wait.side_effect = lambda *args, **kw: self.require(self.visible)
        window.wait_not.side_effect = lambda *args, **kw: self.require(not self.visible)
        if backend == 'win32':
            window.child_window.side_effect = self.native_control
        else:
            window.child_window.side_effect = self.general_tab
        return window

    @staticmethod
    def require(value):
        if not value:
            raise RuntimeError('simulated control not ready')

    def general_tab(self, **selector):
        assert selector == {'title': 'General', 'control_type': 'TabItem'}
        tab = Mock()
        def select():
            self.general_visible = True
            self.uia_select_count += 1
        tab.select.side_effect = select
        return tab

    def native_control(self, control_id, class_name):
        if control_id == 12320:
            self.native_tab_reads += 1
            raise IndexError('Only 0 tabs available you asked for tab 1 (zero based)')
        state = self.controls[control_id]
        assert state['class_name'] == class_name
        control = Mock()
        control.exists.return_value = True
        control.is_visible.side_effect = lambda: self.visible and self.general_visible
        control.is_enabled.return_value = True
        control.wait.side_effect = lambda *args, **kw: self.require(self.visible and self.general_visible)
        control.window_text.side_effect = lambda: state['text']
        control.get_check_state.side_effect = lambda: state['value']
        control.set_edit_text.side_effect = lambda value: state.update(text=value, value=value)
        control.check_by_click.side_effect = lambda: state.update(value=1)
        if control_id in (1, 2):
            control.click.side_effect = lambda: setattr(self, 'visible', False)
        return control


class Revision028Tests(unittest.TestCase):
    def test_synthetic_controls_support_both_intervals_despite_broken_native_tab(self):
        for interval in (30, 60):
            with self.subTest(interval=interval), tempfile.TemporaryDirectory() as directory:
                settings = CapturedSettings()
                with patch('pywinauto.Desktop', side_effect=settings.desktop):
                    result = app.configure_hwinfo_sampling(settings.sensor, interval, Path(directory)/'settings.json')
                self.assertEqual(settings.native_tab_reads, 0)
                self.assertEqual(settings.uia_select_count, 0)
                self.assertEqual(settings.open_count, 2)
                self.assertFalse(settings.visible)
                self.assertTrue(result['passed'])
                self.assertEqual(result['before']['smart_cycles'], 100)
                self.assertEqual(result['readback'], {
                    'global_ms': interval*1000, 'smart_cycles': 1, 'direct_to_disk': True})

    def test_other_page_selects_exact_uia_general_then_reads_captured_fields(self):
        settings = CapturedSettings(general_visible=False)
        with tempfile.TemporaryDirectory() as directory, \
             patch('pywinauto.Desktop', side_effect=settings.desktop):
            result = app.configure_hwinfo_sampling(settings.sensor, 60, Path(directory)/'settings.json')
        self.assertTrue(result['passed'])
        self.assertEqual(settings.native_tab_reads, 0)
        self.assertEqual(settings.uia_select_count, 1)

    def test_preexisting_settings_dialog_is_not_changed_or_canceled(self):
        settings = CapturedSettings()
        settings.visible = True
        with tempfile.TemporaryDirectory() as directory, \
             patch('pywinauto.Desktop', side_effect=settings.desktop), \
             patch.object(app, 'save_hwinfo_failure_context', return_value={'completed': True}):
            path = Path(directory)/'settings.json'
            with self.assertRaisesRegex(RuntimeError, '已有设置窗口'):
                app.configure_hwinfo_sampling(settings.sensor, 30, path)
            result = json.loads(path.read_text('utf-8'))
        self.assertEqual(settings.open_count, 0)
        self.assertTrue(settings.visible)
        self.assertFalse(result['passed'])

    def test_page_failure_is_captured_before_cancel_and_never_sets_values(self):
        settings = CapturedSettings(general_visible=False)
        def no_tab(**kw):
            tab = Mock()
            tab.wait.side_effect = RuntimeError('General inaccessible')
            return tab
        settings.general_tab = no_tab
        captured_visibility = []
        def capture(pid, folder):
            captured_visibility.append(settings.visible)
            self.assertEqual(pid, settings.pid)
            return {'completed': True}
        with tempfile.TemporaryDirectory() as directory, \
             patch('pywinauto.Desktop', side_effect=settings.desktop), \
             patch.object(app, 'save_hwinfo_failure_context', side_effect=capture):
            path = Path(directory)/'settings.json'
            with self.assertRaisesRegex(RuntimeError, 'General inaccessible'):
                app.configure_hwinfo_sampling(settings.sensor, 60, path)
            result = json.loads(path.read_text('utf-8'))
        self.assertEqual(captured_visibility, [True])
        self.assertFalse(settings.visible)
        self.assertEqual(settings.controls[2162]['value'], '100')
        self.assertEqual(result['step'], '定位General采样控件')
        self.assertFalse(result['passed'])

    def test_open_failure_also_saves_requested_interval_and_reason(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app, 'hwinfo_log_button_state', return_value='stopped'), \
             patch.object(app, 'open_hwinfo_settings', side_effect=RuntimeError('open failed')), \
             patch.object(app, 'save_hwinfo_failure_context', return_value={'error': 'process exited'}):
            path = Path(directory)/'settings.json'
            with self.assertRaisesRegex(RuntimeError, 'open failed'):
                app.configure_hwinfo_sampling(Mock(), 30, path)
            result = json.loads(path.read_text('utf-8'))
        self.assertEqual(result['requested_interval_s'], 30)
        self.assertEqual(result['step'], '打开设置')
        self.assertFalse(result['passed'])
        self.assertIn('open failed', result['error'])

    def test_cleanup_error_keeps_original_failure(self):
        dialog = Mock()
        dialog.child_window.return_value.click.side_effect = RuntimeError('cancel failed')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app, 'hwinfo_log_button_state', return_value='stopped'), \
             patch.object(app, 'open_hwinfo_settings', return_value=dialog), \
             patch.object(app, 'select_hwinfo_general_page', side_effect=RuntimeError('page failed')), \
             patch.object(app, 'save_hwinfo_failure_context', return_value={}):
            path = Path(directory)/'settings.json'
            with self.assertRaisesRegex(RuntimeError, 'page failed'):
                app.configure_hwinfo_sampling(Mock(), 30, path)
            result = json.loads(path.read_text('utf-8'))
        self.assertIn('page failed', result['error'])
        self.assertIn('cancel failed', result['cleanup_error'])

    def test_record_failure_is_saved_even_when_start_and_stop_both_fail(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app, 'start_hwinfo_logging', side_effect=RuntimeError('start failed')), \
             patch.object(app, 'stop_hwinfo_logging', side_effect=RuntimeError('stop failed')):
            folder = Path(directory)/'30秒'
            with self.assertRaisesRegex(RuntimeError, 'start failed'):
                check.record_three_samples(app.load_config(), folder, 30)
            result = json.loads((folder/'记录验证.json').read_text('utf-8'))
        self.assertFalse(result['passed'])
        self.assertIn('start failed', result['error'])
        self.assertEqual(result['stop_error'], 'stop failed')

    def test_failure_capture_uses_known_pid_even_without_sensor_window(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app, 'save_hwinfo_failure_context', return_value={'pid': 5332}) as capture, \
             patch('pywinauto.Desktop') as desktop:
            folder = Path(directory)
            check.capture_failure_context(app.load_config(), folder, 5332)
            result = json.loads((folder/'失败现场索引.json').read_text('utf-8'))
        desktop.assert_not_called()
        self.assertEqual(capture.call_args.args[0], 5332)
        self.assertEqual(result['processes'], [{'pid': 5332}])

    def test_discovery_can_capture_settings_without_visible_sensors(self):
        window = Mock()
        window.element_info.process_id = 5332
        desktop = Mock()
        desktop.windows.return_value = [window]
        with tempfile.TemporaryDirectory() as directory, \
             patch('pywinauto.Desktop', return_value=desktop), \
             patch.object(app, 'resolve_hwinfo_executable', return_value=Path('E:/HWiNFO32/HWiNFO32.exe')), \
             patch.object(app, 'process_executable_path', return_value='E:/HWiNFO32/HWiNFO32.exe'), \
             patch.object(app, 'save_hwinfo_failure_context', return_value={}) as capture:
            check.capture_failure_context(app.load_config(), Path(directory))
        self.assertEqual(capture.call_args.args[0], 5332)
        desktop.windows.assert_called_once_with(visible_only=False, enabled_only=False)

    def test_zero_windows_does_not_silently_produce_an_empty_archive(self):
        desktop = Mock()
        desktop.windows.return_value = []
        with tempfile.TemporaryDirectory() as directory, \
             patch('pywinauto.Desktop', return_value=desktop), \
             patch.object(app, 'resolve_hwinfo_executable', return_value=Path('E:/HWiNFO32/HWiNFO32.exe')):
            folder = Path(directory)
            check.capture_failure_context(app.load_config(), folder)
            result = json.loads((folder/'失败现场索引.json').read_text('utf-8'))
        self.assertTrue(result['errors'])

    def test_existing_failure_snapshot_is_indexed_without_repeating_capture(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app, 'save_hwinfo_failure_context') as capture:
            folder = Path(directory)
            existing = folder/'30秒/设置失败现场/现场采集结果.json'
            app.save_json(existing, {'completed': True})
            check.capture_failure_context(app.load_config(), folder, 5332)
            result = json.loads((folder/'失败现场索引.json').read_text('utf-8'))
        capture.assert_not_called()
        self.assertEqual(len(result['existing_records']), 1)

    def test_process_exit_still_leaves_diagnostic_manifest(self):
        def save_empty(pid, folder, label):
            app.save_json(folder/f'{label}_窗口清单.json', [])
        with tempfile.TemporaryDirectory() as directory, \
             patch('继续软件适配.save_window_diagnostics', side_effect=save_empty):
            result = app.save_hwinfo_failure_context(5332, Path(directory))
            on_disk = json.loads((Path(directory)/'现场采集结果.json').read_text('utf-8'))
        self.assertFalse(result['completed'])
        self.assertIn('已无窗口', result['error'])
        self.assertEqual(on_disk, result)


if __name__ == '__main__':
    unittest.main()
