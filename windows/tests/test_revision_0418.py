"""常用Edge接入回归：只用会话测试桩，不启动浏览器/网络/现场硬件。"""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import windows_temperature_test as app
import runtime_check
from test_revision_037 import configuration, Clock


class NativeSession:
    def __init__(self):
        self.reads = self.selections = self.closes = 0
        self.last = dict(position=12, paused=False, ended=False, readyState=4, width=1920,
                         height=1080, frame_span_s=2, observed_fps=30, observation_available=True,
                         fullscreen=dict(active=True, contains_video=True, fills_viewport=True))

    def sample(self):
        self.reads += 1
        return dict(self.last)

    def select_quality(self):
        self.selections += 1

    def close(self):
        self.closes += 1

    def is_open(self):
        return self.closes == 0

    def wait_for_timeout(self, ms):
        raise AssertionError('普通Edge采样不应进入旧DOM补看循环')


class NativeIntegrationTests(unittest.TestCase):
    def test_default_start_calls_only_native_entry_and_preserves_old_profile(self):
        cfg=configuration();cfg['online_video'].update(profile='browser_profile',browser='msedge')
        before=copy.deepcopy(cfg);stop=threading.Event();status={};session=NativeSession()
        workload=dict(adapter='native_edge',session=session,page=session,last=session.last,quality_attempts={})
        native=types.SimpleNamespace(start_online_video=Mock(return_value=workload))
        with patch.dict(sys.modules, {'native_edge':native}), \
             patch.object(app,'_start_legacy_online_video') as legacy, \
             patch('playwright.sync_api.sync_playwright') as playwright, \
             patch('runtime_integrity.require_browser_driver') as driver:
            result=app.start_online_video(cfg,stop,status)
        self.assertIs(result,workload)
        native.start_online_video.assert_called_once_with(cfg,stop=stop,status=status)
        legacy.assert_not_called();playwright.assert_not_called();driver.assert_not_called()
        self.assertEqual(cfg,before)

    def test_native_start_failure_does_not_fall_back_to_independent_session(self):
        native=types.SimpleNamespace(start_online_video=Mock(side_effect=RuntimeError('普通Edge启动失败')))
        with patch.dict(sys.modules, {'native_edge':native}),patch.object(app,'_start_legacy_online_video') as legacy:
            with self.assertRaisesRegex(RuntimeError,'普通Edge启动失败'):app.start_online_video(configuration())
        legacy.assert_not_called()

    def test_both_status_paths_read_native_session_without_playwright(self):
        session=NativeSession()
        with patch('playwright.sync_api.sync_playwright') as playwright,patch.object(app,'bilibili_quality_state') as dom:
            self.assertEqual(app.online_video_status(session),session.last)
            self.assertEqual(app.runtime_online_video_status(session),session.last)
        self.assertEqual(session.reads,2);playwright.assert_not_called();dom.assert_not_called()

    def test_native_cleanup_only_closes_its_own_session(self):
        session=NativeSession();other_browser=Mock();context=Mock();runtime=Mock()
        app.stop_online_video(dict(adapter='native_edge',session=session,page=session,
                                   context=context,runtime=runtime,other_browser=other_browser))
        self.assertEqual(session.closes,1)
        context.close.assert_not_called();runtime.stop.assert_not_called();other_browser.close.assert_not_called()

    def test_native_cleanup_error_is_exposed_and_does_not_close_other_context(self):
        session=NativeSession();session.close=Mock(side_effect=RuntimeError('当前窗口收尾失败'));context=Mock()
        with self.assertRaisesRegex(RuntimeError,'当前窗口收尾失败'):
            app.stop_online_video(dict(adapter='native_edge',session=session,context=context))
        context.close.assert_not_called()

    def test_native_session_interface_also_selects_owned_cleanup(self):
        session=NativeSession();context=Mock()
        app.stop_online_video(dict(page=session,context=context))
        self.assertEqual(session.closes,1);context.close.assert_not_called()

    def test_native_quality_repair_obeys_existing_attempt_and_cooldown_limits(self):
        session=NativeSession();sample=dict(session.last,width=640,height=360);attempts={}
        with patch.object(app,'select_bilibili_1080') as old_selector:
            for now in (0,1,15,30,45):
                with patch.object(app.time,'monotonic',return_value=now):
                    app.restore_bilibili_quality(session,sample,attempts)
        self.assertEqual(session.selections,3);self.assertEqual(attempts['count'],3)
        old_selector.assert_not_called()
        session.last['observed_fps']=60
        self.assertIsNone(app.restore_bilibili_quality(session,session.last,{}))

    def test_native_preflight_checks_edge_without_old_driver(self):
        native=types.SimpleNamespace(inspect_native_edge=Mock(return_value=dict(passed=True,
            executable='C:/Edge/msedge.exe',profile_directory='C:/fixture/Edge/User Data/Default')))
        with patch.dict(sys.modules, {'native_edge':native}), \
             patch.object(app,'resolve_hwinfo_executable',return_value=Path('HWiNFO32.exe')), \
             patch('runtime_integrity.require_browser_driver',side_effect=RuntimeError('driver blocked')) as driver:
            problems=app.preflight_host(configuration(),['online_video'])
        native.inspect_native_edge.assert_called_once_with();driver.assert_not_called()
        self.assertFalse(any('driver blocked' in p or 'Edge环境检查' in p for p in problems))

    def test_offline_preflight_does_not_check_edge(self):
        native=types.SimpleNamespace(inspect_native_edge=Mock(side_effect=AssertionError('不应检查浏览器')))
        with patch.dict(sys.modules, {'native_edge':native}),patch.object(app,'resolve_hwinfo_executable',return_value=Path('hw.exe')):
            app.preflight_host(configuration(),['idle'])
        native.inspect_native_edge.assert_not_called()

    def test_host_runtime_no_longer_requires_playwright(self):
        imports=[]
        def load(name):
            imports.append(name)
            if name.startswith('playwright'):raise ImportError('旧驱动不可用')
            return types.SimpleNamespace(__file__='test stub',Desktop=object())
        with patch.object(runtime_check.importlib,'import_module',side_effect=load), \
             patch.object(runtime_check.ctypes,'WinDLL',side_effect=OSError('test stub')):
            result=runtime_check.inspect_runtime('host')
        self.assertTrue(result['passed']);self.assertNotIn('playwright.sync_api',imports)
        self.assertIn('pywinauto',imports);self.assertIn('win32gui',imports)

    def test_old_config_gets_native_default_without_rewriting_old_profile_data(self):
        cfg=configuration();cfg['online_video'].pop('browser_control',None)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);profile=root/'browser_profile';profile.mkdir()
            cookies=profile/'Cookies';cookies.write_bytes(b'old login database')
            config=root/'config.json';app.save_json(config,cfg)
            before=config.read_bytes()
            with patch.object(app,'ROOT',root),patch.object(app,'CONFIG',config):
                loaded=app.load_config()
            self.assertEqual(loaded['online_video']['browser_control'],'native_edge')
            self.assertEqual(cookies.read_bytes(),b'old login database')
            self.assertEqual(config.read_bytes(),before)

    def test_execute_stage_runs_native_sampling_and_owned_cleanup_through_normal_and_failed_paths(self):
        for fault in ('none','sample','close'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
                clock=Clock();cfg=configuration();cfg['tests']['online_video'].update(duration_s=10,wait_before_s=200)
                session=NativeSession();status={};folder=Path(directory)/'online_video'
                csv_path=Path(directory)/'hw.csv';csv_path.write_text('raw csv','utf-8')
                stop=Mock();stop.is_set.return_value=False
                stop.wait.side_effect=lambda _:clock.advance(5) or False
                if fault=='sample':
                    def failed_sample():
                        session.reads+=1
                        raise RuntimeError('本轮窗口遥测失效')
                    session.sample=failed_sample
                if fault=='close':
                    def failed_close():
                        session.closes+=1
                        raise RuntimeError('本轮窗口关闭失败')
                    session.close=failed_close
                def ready(*args,**kwargs):
                    self.assertNotIn('start_epoch',status)
                    clock.advance(13)
                    return dict(adapter='native_edge',page=session,session=session,last=session.last,quality_attempts={})
                native=types.SimpleNamespace(start_online_video=Mock(side_effect=ready))
                stack.enter_context(patch.dict(sys.modules,{'native_edge':native}))
                replacements={'apply_network_state':Mock(return_value=[]),'wait_seconds':Mock(),
                    'launch_hwinfo':Mock(),'start_hwinfo_logging':Mock(return_value=csv_path),
                    'minimize_owned_hwinfo':Mock(),'stop_hwinfo_logging':Mock()}
                for name,value in replacements.items():stack.enter_context(patch.object(app,name,value))
                stack.enter_context(patch.object(app.time,'time',clock.time))
                stack.enter_context(patch.object(app.time,'monotonic',clock.monotonic))
                if fault=='close':
                    with self.assertRaisesRegex(app.WorkloadCleanupError,'本轮窗口关闭失败'):
                        app.execute_stage(cfg,'online_video',folder,stop,status,[],skip_prepare_wait=True)
                else:
                    app.execute_stage(cfg,'online_video',folder,stop,status,[],skip_prepare_wait=True)
                conditions=json.loads((folder/'conditions.json').read_text('utf-8'))
                self.assertEqual(conditions['start_epoch'],1700000013)
                self.assertEqual(conditions['end_epoch']-conditions['start_epoch'],10)
                self.assertEqual(session.closes,1);self.assertEqual(session.reads,2)
                replacements['wait_seconds'].assert_not_called()
                replacements['stop_hwinfo_logging'].assert_called_once()
                if fault=='sample':
                    self.assertIn('本轮窗口遥测失效',conditions['workload_status'])
                elif fault=='none':
                    self.assertEqual(conditions['workload_status'],'完成计时')


if __name__=='__main__':unittest.main()
