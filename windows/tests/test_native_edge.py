"""Native Edge 单元回归；不启动浏览器，不访问真实窗口、注册表或硬件。

覆盖约定：
* 启动参数沿用普通 Edge；保留已有 profile-directory，不创建 user-data-dir。
* 标题遥测必须绑定 nonce、页面来源和新鲜时间；无效观测不能伪造帧率。
* inspect 只检查可用环境；close 只作用于已确认属于本轮的窗口。

所有系统依赖均由测试桩替代，临时文件只用于模拟已有的用户配置。
"""
import base64
import copy
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ORIGIN = 'https://www.bilibili.com'
NONCE = 'unit-nonce-7a836d'
NOW = 1_794_360_000.0


def telemetry_sample(**changes):
    sample = dict(position=25.5, paused=False, ended=False, readyState=4,
                  width=1920, height=1080, playback_rate=1,
                  frame_age_s=.1, frame_span_s=2, observed_fps=29.97,
                  quality=dict(selected='80', label='1080P', choices=[]),
                  fullscreen=dict(active=True, contains_video=True, fills_viewport=True))
    sample.update(changes)
    return sample


def telemetry_packet(**changes):
    packet = dict(nonce=NONCE, origin=ORIGIN, epoch=NOW-.25, seq=7,
                  sample=telemetry_sample(), page_state=dict(has_video=True, ready='complete'))
    packet.update(changes)
    return packet


def telemetry_title(packet, nonce=NONCE, suffix=''):
    encoded = base64.b64encode(json.dumps(packet, ensure_ascii=False).encode('utf-8')).decode('ascii')
    return 'JK808_' + nonce + ':' + encoded + suffix


class NativeEdgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.native = importlib.import_module('native_edge')
        cls.read_title_implementation = staticmethod(cls.native._read_title)

    def setUp(self):
        # 任何未明确提供标题的测试都不得意外读取真实Win32窗口。
        guard = patch.object(self.native, '_read_title',
                             side_effect=AssertionError('测试必须模拟_read_title，禁止读取真实窗口'))
        guard.start()
        self.addCleanup(guard.stop)
        socket_guard = patch('http.server.ThreadingHTTPServer',
                             side_effect=AssertionError('测试必须模拟HTTP服务，禁止打开真实socket'))
        socket_guard.start()
        self.addCleanup(socket_guard.stop)


class LaunchArgumentsTests(NativeEdgeTests):
    def test_regular_edge_arguments_preserve_literal_executable_and_url(self):
        executable = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
        url = ORIGIN + '/video/BV1Yoh2zUECH/?p=1&label=%E6%B5%8B%E8%AF%95'
        arguments = self.native.build_launch_args(executable, url)
        self.assertIsInstance(arguments, (list, tuple))
        self.assertEqual(arguments[0], executable)
        self.assertIn('--new-window', arguments)
        self.assertIn(url, arguments)
        self.assertEqual(sum(item == url for item in arguments), 1)
        self.assertTrue(all(isinstance(item, str) for item in arguments))

    def test_launch_does_not_create_an_automation_or_alternate_data_environment(self):
        arguments = self.native.build_launch_args('C:/Edge/msedge.exe', ORIGIN + '/video/example/')
        forbidden = ('--user-data-dir', '--remote-debugging', '--enable-automation',
                     '--disable-blink-features=AutomationControlled', '--inprivate')
        for argument in arguments:
            for prefix in forbidden:
                self.assertFalse(argument.lower().startswith(prefix.lower()), argument)
        self.assertNotIn('browser_profile', '\n'.join(arguments))

    def test_existing_profile_name_is_passed_as_one_literal_argument(self):
        arguments = self.native.build_launch_args('C:/Edge/msedge.exe', ORIGIN, 'Profile 2')
        self.assertIn('--profile-directory=Profile 2', arguments)
        self.assertFalse(any(item.startswith('--user-data-dir') for item in arguments))

    def test_empty_profile_does_not_force_default_or_create_a_profile(self):
        arguments = self.native.build_launch_args('C:/Edge/msedge.exe', ORIGIN, '')
        self.assertFalse(any(item.startswith('--profile-directory') for item in arguments))

    def test_profile_argument_cannot_be_used_to_change_data_root_or_add_switches(self):
        for profile in ('.', '..', '../Other', r'..\Other', r'C:\Other\Default',
                        'Profile 1" --inprivate', '--user-data-dir=C:/Other'):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                self.native.build_launch_args('C:/Edge/msedge.exe', ORIGIN, profile)


class InvalidTelemetryTests(NativeEdgeTests):
    def assert_unavailable(self, title, nonce=NONCE, allowed_origin=ORIGIN, now=NOW):
        sample = self.native.decode_telemetry(title, nonce, allowed_origin, now=now)
        self.assertIsInstance(sample, dict)
        self.assertIs(sample.get('observation_available'), False)
        self.assertIsNone(sample.get('observed_fps'), '不可用观测不能带入未经验证的帧率')
        return sample

    def test_normal_page_title_is_not_playback_telemetry(self):
        for title in ('', '哔哩哔哩 - Microsoft Edge', '1080P 30fps', '{}', 'null'):
            with self.subTest(title=title):
                self.assert_unavailable(title)

    def test_unrelated_json_cannot_be_accepted_as_browser_observation(self):
        title = json.dumps(dict(nonce=NONCE, timestamp=NOW, origin=ORIGIN,
                                observed_fps=30, width=1920, height=1080))
        self.assert_unavailable(title)

    def test_wrong_prefix_and_payload_nonce_are_rejected(self):
        self.assert_unavailable(telemetry_title(telemetry_packet(), nonce='other-session'))
        self.assert_unavailable(telemetry_title(telemetry_packet(nonce='other-session')))

    def test_only_exact_allowed_origin_can_report_a_video(self):
        for origin in ('http://www.bilibili.com', 'https://www.bilibili.com.evil.test',
                       'https://evil.test', 'file://', 'https://www.bilibili.com:8443'):
            with self.subTest(origin=origin):
                self.assert_unavailable(telemetry_title(telemetry_packet(origin=origin)))

    def test_stale_or_far_future_telemetry_is_not_reused(self):
        for epoch in (NOW-60, NOW+60, 0):
            with self.subTest(epoch=epoch):
                self.assert_unavailable(telemetry_title(telemetry_packet(epoch=epoch)))

    def test_timestamp_must_be_a_finite_number_not_boolean_or_text(self):
        for epoch in (True, False, None, '1794360000', float('nan'), float('inf')):
            with self.subTest(epoch=epoch):
                self.assert_unavailable(telemetry_title(telemetry_packet(epoch=epoch)))

    def test_packet_and_sample_must_be_objects_not_arbitrary_json(self):
        for packet in ([], ['sample'], 'text', 1, None, telemetry_packet(sample=[]),
                       telemetry_packet(sample='text'), telemetry_packet(sample=1)):
            with self.subTest(packet=packet):
                self.assert_unavailable(telemetry_title(packet))

    def test_sequence_number_is_a_positive_integer(self):
        for seq in (None, True, False, 0, -1, 1.5, '7'):
            with self.subTest(seq=seq):
                self.assert_unavailable(telemetry_title(telemetry_packet(seq=seq)))

    def test_malformed_and_truncated_titles_are_missing_observations(self):
        for title in ('JK808_' + NONCE + ':not base64!',
                      'JK808_' + NONCE + ':' + base64.b64encode(b'{"nonce":').decode(),
                      'JK808_' + NONCE + ':' + 'A'*16001):
            with self.subTest(title=title[:80]):
                self.assert_unavailable(title)

    def test_bad_numbers_are_rejected_in_each_video_measurement(self):
        fields = ('position', 'readyState', 'width', 'height', 'playback_rate',
                  'frame_age_s', 'frame_span_s', 'observed_fps')
        for field in fields:
            for value in (True, '30', -1, float('nan'), float('inf')):
                with self.subTest(field=field, value=value):
                    self.assert_unavailable(telemetry_title(telemetry_packet(
                        sample=telemetry_sample(**{field:value}))))

    def test_ready_state_is_an_html_media_state(self):
        for value in (5, 99, 1.5):
            with self.subTest(value=value):
                self.assert_unavailable(telemetry_title(telemetry_packet(
                    sample=telemetry_sample(readyState=value))))

    def test_pause_and_ended_states_are_actual_booleans(self):
        for field in ('paused', 'ended'):
            for value in (None, 0, 1, 'false'):
                with self.subTest(field=field, value=value):
                    self.assert_unavailable(telemetry_title(telemetry_packet(
                        sample=telemetry_sample(**{field:value}))))

    def test_fullscreen_evidence_cannot_be_truthy_strings_or_numbers(self):
        for value in ('fullscreen', [], 1,
                      dict(active='true', contains_video=True, fills_viewport=True),
                      dict(active=True, contains_video=1, fills_viewport=True)):
            with self.subTest(fullscreen=value):
                self.assert_unavailable(telemetry_title(telemetry_packet(
                    sample=telemetry_sample(fullscreen=value))))

    def test_missing_fps_cannot_be_replaced_with_configured_30_fps(self):
        sample = telemetry_sample()
        del sample['observed_fps']
        self.assert_unavailable(telemetry_title(telemetry_packet(sample=sample)))


class ValidTelemetryTests(NativeEdgeTests):
    def test_fresh_matching_page_preserves_observed_values(self):
        packet = telemetry_packet()
        decoded = self.native.decode_telemetry(telemetry_title(packet), NONCE, ORIGIN, now=NOW)
        self.assertIs(decoded['observation_available'], True)
        for key,value in packet['sample'].items():
            self.assertEqual(decoded[key], value)
        self.assertEqual(decoded['bridge_seq'], 7)
        self.assertEqual(decoded['bridge_epoch'], packet['epoch'])

    def test_edge_profile_and_window_title_suffix_are_allowed(self):
        title = telemetry_title(telemetry_packet(), suffix=' - 工作配置 - Microsoft Edge')
        decoded = self.native.decode_telemetry(title, NONCE, ORIGIN, now=NOW)
        self.assertIs(decoded['observation_available'], True)
        self.assertEqual(decoded['observed_fps'], 29.97)

    def test_current_time_is_used_when_caller_does_not_supply_now(self):
        with patch.object(self.native.time, 'time', return_value=NOW):
            decoded = self.native.decode_telemetry(telemetry_title(telemetry_packet()), NONCE, ORIGIN)
        self.assertIs(decoded['observation_available'], True)

    def test_no_video_is_explicit_missing_observation_with_page_state(self):
        packet = telemetry_packet(sample=None, page_state=dict(has_video=False, ready='interactive'))
        decoded = self.native.decode_telemetry(telemetry_title(packet), NONCE, ORIGIN, now=NOW)
        self.assertIs(decoded['observation_available'], False)
        self.assertEqual(decoded['page_state'], packet['page_state'])
        self.assertIsNone(decoded.get('observed_fps'))

    def test_not_yet_observed_frames_remain_zero_instead_of_a_successful_rate(self):
        packet = telemetry_packet(sample=telemetry_sample(frame_age_s=None, frame_span_s=0, observed_fps=0))
        decoded = self.native.decode_telemetry(telemetry_title(packet), NONCE, ORIGIN, now=NOW)
        self.assertEqual(decoded['observed_fps'], 0)
        self.assertEqual(decoded['frame_span_s'], 0)
        self.assertIsNone(decoded['frame_age_s'])


class EnvironmentInspectionTests(NativeEdgeTests):
    def fake_user_environment(self, root):
        return {'LOCALAPPDATA':str(root/'local'), 'APPDATA':str(root/'roaming'),
                'PROGRAMFILES(X86)':str(root/'pf86'), 'PROGRAMFILES':str(root/'pf64')}

    def test_taskbar_edge_and_existing_profile_are_kept_without_starting_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'Edge 安装目录/msedge.exe'
            executable.parent.mkdir(); executable.write_bytes(b'unit fixture, never executed')
            profile = root/'local/Microsoft/Edge/User Data/Profile 2'
            profile.mkdir(parents=True)
            with patch.dict(self.native.os.environ, self.fake_user_environment(root), clear=True), \
                 patch.object(self.native, '_same_desktop_user', return_value=True), \
                 patch.object(self.native, '_pinned_edge', return_value=(str(executable), 'Profile 2')), \
                 patch.object(self.native.subprocess, 'Popen') as launch:
                result = self.native.inspect_native_edge()
            self.assertIs(result['passed'], True)
            self.assertEqual(result['executable'], str(executable))
            self.assertEqual(result['profile_directory'], 'Profile 2')
            self.assertEqual(result['source'], 'taskbar_shortcut')
            launch.assert_not_called()

    def test_other_desktop_user_is_rejected_before_profile_discovery(self):
        with patch.object(self.native, '_same_desktop_user', return_value=False), \
             patch.object(self.native, '_pinned_edge') as pinned, \
             patch.object(self.native.subprocess, 'Popen') as launch:
            result = self.native.inspect_native_edge()
        self.assertIs(result['passed'], False)
        self.assertIn('账号', result['error'])
        pinned.assert_not_called(); launch.assert_not_called()

    def test_unknown_desktop_user_is_explicitly_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'Edge/msedge.exe'
            executable.parent.mkdir(); executable.write_bytes(b'unit fixture, never executed')
            (root/'local/Microsoft/Edge/User Data/Profile 2').mkdir(parents=True)
            with patch.dict(self.native.os.environ, self.fake_user_environment(root), clear=True), \
                 patch.object(self.native, '_same_desktop_user', return_value=None), \
                 patch.object(self.native, '_pinned_edge', return_value=(str(executable), 'Profile 2')) as pinned, \
                 patch.object(self.native.subprocess, 'Popen') as launch:
                result = self.native.inspect_native_edge()
        self.assertIs(result['passed'], False)
        self.assertIn('桌面', result['error'])
        pinned.assert_not_called(); launch.assert_not_called()

    def test_missing_selected_profile_is_not_created_or_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'Edge/msedge.exe'
            executable.parent.mkdir(); executable.write_bytes(b'unit fixture, never executed')
            user_data = root/'local/Microsoft/Edge/User Data'
            user_data.mkdir(parents=True)
            missing_profile = user_data/'Profile 7'
            self.assertFalse(missing_profile.exists())
            with patch.dict(self.native.os.environ, self.fake_user_environment(root), clear=True), \
                 patch.object(self.native, '_same_desktop_user', return_value=True), \
                 patch.object(self.native, '_pinned_edge', return_value=(str(executable), 'Profile 7')), \
                 patch.object(self.native.subprocess, 'Popen') as launch:
                result = self.native.inspect_native_edge()
            self.assertIs(result['passed'], False)
            self.assertIn('用户配置', result['error'])
            self.assertFalse(missing_profile.exists())
            launch.assert_not_called()

    def test_installed_edge_uses_existing_last_used_profile_without_changing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'Edge/msedge.exe'
            executable.parent.mkdir(); executable.write_bytes(b'unit fixture, never executed')
            user_data = root/'local/Microsoft/Edge/User Data'
            profile = user_data/'Profile 3'; profile.mkdir(parents=True)
            preferences = profile/'Preferences'; preferences.write_bytes(b'{"unit":"preserve"}\r\n')
            local_state = user_data/'Local State'
            local_state.write_text(json.dumps(dict(profile=dict(last_used='Profile 3'))), 'utf-8')
            original = {p:p.read_bytes() for p in (preferences, local_state)}
            key = Mock(); key.__enter__ = Mock(return_value=object()); key.__exit__ = Mock(return_value=False)
            registry = types.SimpleNamespace(HKEY_CURRENT_USER=1, HKEY_LOCAL_MACHINE=2,
                OpenKey=Mock(return_value=key), QueryValueEx=Mock(return_value=(str(executable), 1)))
            with patch.dict(self.native.os.environ, self.fake_user_environment(root), clear=True), \
                 patch.dict(sys.modules, {'winreg':registry}), \
                 patch.object(self.native, '_same_desktop_user', return_value=True), \
                 patch.object(self.native, '_pinned_edge', return_value=(None, '')), \
                 patch.object(self.native.subprocess, 'Popen') as launch:
                result = self.native.inspect_native_edge()
            self.assertIs(result['passed'], True)
            self.assertEqual(result['profile_directory'], 'Profile 3')
            self.assertEqual(result['source'], 'installed_edge')
            for path,data in original.items():
                self.assertEqual(path.read_bytes(), data)
            launch.assert_not_called()

    def test_missing_edge_is_reported_without_trying_a_browser_launch(self):
        registry = types.SimpleNamespace(HKEY_CURRENT_USER=1, HKEY_LOCAL_MACHINE=2,
                                         OpenKey=Mock(side_effect=OSError('unit: not installed')))
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(self.native.os.environ, self.fake_user_environment(Path(directory)), clear=True), \
             patch.dict(sys.modules, {'winreg':registry}), \
             patch.object(self.native, '_same_desktop_user', return_value=True), \
             patch.object(self.native, '_pinned_edge', return_value=(None, '')), \
             patch.object(self.native.subprocess, 'Popen') as launch:
            result = self.native.inspect_native_edge()
        self.assertIs(result['passed'], False)
        self.assertIn('Edge', result['error'])
        launch.assert_not_called()


class OwnedWindowTests(NativeEdgeTests):
    def setUp(self):
        super().setUp()
        stop = patch.object(self.native.NativeEdgeSession, '_stop_marker')
        self.stop_marker = stop.start()
        self.addCleanup(stop.stop)

    def session(self):
        return self.native.NativeEdgeSession(dict(online_video=dict(url=ORIGIN + '/video/example/')))

    def start_marker(self, session):
        session.marker_url = 'http://127.0.0.1:54321/jk808/' + session.nonce

    def test_constructing_session_does_not_open_a_browser(self):
        with patch.object(self.native.subprocess, 'Popen') as launch:
            session = self.session()
        self.assertIsNone(session.hwnd)
        launch.assert_not_called()

    def test_close_without_an_assigned_window_does_nothing(self):
        windows = types.SimpleNamespace(IsWindow=Mock(), GetProp=Mock(), PostMessage=Mock())
        with patch.dict(sys.modules, {'win32gui':windows}):
            self.session().close()
        windows.IsWindow.assert_not_called(); windows.PostMessage.assert_not_called()

    def test_close_does_not_touch_a_window_that_already_disappeared(self):
        session = self.session(); session.hwnd = 123
        windows = types.SimpleNamespace(IsWindow=Mock(return_value=False), GetProp=Mock(), PostMessage=Mock())
        with patch.dict(sys.modules, {'win32gui':windows}):
            session.close()
        windows.GetProp.assert_not_called(); windows.PostMessage.assert_not_called()

    def test_close_refuses_missing_or_changed_ownership(self):
        session = self.session(); session.hwnd = 123
        windows = types.SimpleNamespace(IsWindow=Mock(return_value=True), GetProp=Mock(return_value=0), PostMessage=Mock())
        with patch.dict(sys.modules, {'win32gui':windows}), \
             patch.object(self.native, '_window_property', return_value=0) as ownership, \
             self.assertRaisesRegex(RuntimeError, '所有权'):
            session.close()
        ownership.assert_called_with(123, session.property)
        windows.PostMessage.assert_not_called()

    def test_close_sends_only_wm_close_to_its_owned_handle(self):
        session = self.session(); session.hwnd = 123
        windows = types.SimpleNamespace(IsWindow=Mock(side_effect=[True, True, False]),
                                         GetProp=Mock(return_value=1), PostMessage=Mock())
        with patch.dict(sys.modules, {'win32gui':windows}), \
             patch.object(self.native, '_window_property', return_value=1), \
             patch.object(self.native.subprocess, 'Popen') as launch, \
             patch.object(self.native.os, 'kill') as kill:
            session.close()
        windows.PostMessage.assert_called_once_with(123, 0x0010, 0, 0)
        launch.assert_not_called(); kill.assert_not_called()

    def test_refused_window_close_is_reported_instead_of_silently_claiming_cleanup(self):
        session = self.session(); session.hwnd = 123
        windows = types.SimpleNamespace(IsWindow=Mock(return_value=True), GetProp=Mock(return_value=1), PostMessage=Mock())
        with patch.dict(sys.modules, {'win32gui':windows}), \
             patch.object(self.native, '_window_property', return_value=1), \
             patch.object(self.native.time, 'monotonic', side_effect=[0, 9]), \
             patch.object(self.native.time, 'sleep'), self.assertRaisesRegex(RuntimeError, '未能关闭'):
            session.close()
        windows.PostMessage.assert_called_once_with(123, 0x0010, 0, 0)

    def test_marker_stop_runtime_error_still_closes_owned_window_and_reports_service_failure(self):
        session = self.session(); session.hwnd = 123
        alive = {'value':True}
        windows = types.SimpleNamespace(
            IsWindow=Mock(side_effect=lambda hwnd:alive['value']),
            PostMessage=Mock(side_effect=lambda *args:alive.update(value=False)))
        with patch.dict(sys.modules, {'win32gui':windows}), \
             patch.object(self.native, '_window_property', return_value=1), \
             patch.object(session, '_stop_marker', side_effect=RuntimeError('unit marker service failure')), \
             self.assertRaisesRegex(RuntimeError, 'marker service failure'):
            session.close()
        windows.PostMessage.assert_called_once_with(123, 0x0010, 0, 0)
        self.assertIs(alive['value'], False)

    def test_sample_never_reads_an_unowned_window_title(self):
        session = self.session(); session.hwnd = 123
        windows = types.SimpleNamespace(IsWindow=Mock(return_value=True), GetProp=Mock(return_value=0),
                                         GetWindowText=Mock())
        with patch.dict(sys.modules, {'win32gui':windows}), \
             patch.object(self.native, '_window_property', return_value=0), \
             patch.object(self.native, '_read_title') as read_title, self.assertRaises(RuntimeError):
            session.sample()
        read_title.assert_not_called(); windows.GetWindowText.assert_not_called()

    def test_repeated_sequence_cannot_keep_old_fps_alive_indefinitely(self):
        session = self.session(); session.hwnd = 123
        windows = types.SimpleNamespace(IsWindow=Mock(return_value=True), GetProp=Mock(return_value=1),
                                         GetWindowText=Mock(return_value='unit telemetry title'))
        observation = dict(telemetry_sample(), observation_available=True, bridge_seq=7, bridge_epoch=NOW)
        with patch.dict(sys.modules, {'win32gui':windows}), \
             patch.object(self.native, '_window_property', return_value=1), \
             patch.object(self.native, '_read_title', return_value='unit telemetry title') as read_title, \
             patch.object(self.native, 'decode_telemetry', side_effect=lambda *a,**k:copy.deepcopy(observation)), \
             patch.object(self.native.time, 'monotonic', side_effect=[10, 16]):
            first = session.sample(); stale = session.sample()
        self.assertIs(first['observation_available'], True)
        self.assertIs(stale['observation_available'], False)
        self.assertIsNone(stale.get('observed_fps'))
        self.assertEqual(read_title.call_args_list, [unittest.mock.call(123), unittest.mock.call(123)])

    def test_new_sequence_restores_only_the_new_observed_fps(self):
        session = self.session(); session.hwnd = 123
        windows = types.SimpleNamespace(IsWindow=Mock(return_value=True),
                                         GetWindowText=Mock(return_value='unit telemetry title'))
        observations = [dict(telemetry_sample(observed_fps=29.97), observation_available=True, bridge_seq=7),
                        dict(telemetry_sample(observed_fps=59.94), observation_available=True, bridge_seq=8)]
        with patch.dict(sys.modules, {'win32gui':windows}), \
             patch.object(self.native, '_window_property', return_value=1), \
             patch.object(self.native, '_read_title', return_value='unit telemetry title'), \
             patch.object(self.native, 'decode_telemetry', side_effect=observations), \
             patch.object(self.native.time, 'monotonic', side_effect=[10, 16]):
            first = session.sample(); newer = session.sample()
        self.assertEqual(first['observed_fps'], 29.97)
        self.assertIs(newer['observation_available'], True)
        self.assertEqual(newer['observed_fps'], 59.94)

    def test_sample_passes_the_complete_long_title_to_telemetry_decoder(self):
        session = self.session(); session.hwnd = 123
        quality = dict(selected='80', label='中文1080P画质信息'*80,
                       choices=[dict(value='80', label='1080P')])
        packet = telemetry_packet(nonce=session.nonce, sample=telemetry_sample(quality=quality))
        title = telemetry_title(packet, nonce=session.nonce, suffix=' - 常用用户配置 - Microsoft Edge')
        self.assertGreater(len(title), 511)
        windows = types.SimpleNamespace(IsWindow=Mock(return_value=True))
        with patch.dict(sys.modules, {'win32gui':windows}), \
             patch.object(self.native, '_window_property', return_value=1), \
             patch.object(self.native, '_read_title', return_value=title) as read_title, \
             patch.object(self.native, 'decode_telemetry', wraps=self.native.decode_telemetry) as decode, \
             patch.object(self.native.time, 'time', return_value=NOW), \
             patch.object(self.native.time, 'monotonic', return_value=10):
            sample = session.sample()
        read_title.assert_called_once_with(123)
        decode.assert_called_once_with(title, session.nonce, session.allowed_origin)
        self.assertIs(sample['observation_available'], True)
        self.assertEqual(sample['quality'], quality)
        self.assertEqual(sample['observed_fps'], 29.97)

    def test_unicode_title_reader_allocates_for_the_whole_title_without_real_win32_calls(self):
        title = 'JK808_完整标题:' + '完整遥测内容'*180 + ' - 常用配置 - Microsoft Edge'
        self.assertGreater(len(title), 511)
        length = Mock(return_value=len(title))
        def copy_title(hwnd, buffer, capacity):
            self.assertEqual(hwnd, 123)
            self.assertGreater(capacity, len(title))
            buffer.value = title
            return len(title)
        read = Mock(side_effect=copy_title)
        windows = types.SimpleNamespace(user32=types.SimpleNamespace(
            GetWindowTextLengthW=length, GetWindowTextW=read))
        with patch.object(self.native.ctypes, 'windll', windows):
            result = self.read_title_implementation(123)
        self.assertEqual(result, title)
        length.assert_called_once_with(123)
        read.assert_called_once()

    def test_open_claims_only_a_new_window_with_this_runs_marker(self):
        session = self.session()
        windows = types.SimpleNamespace(ShowWindow=Mock(), PostMessage=Mock())
        checked = dict(passed=True, executable='C:/Edge/msedge.exe', profile_directory='Profile 2')
        process = types.SimpleNamespace(pid=4321, poll=Mock(return_value=0))
        probes = {20:dict(hwnd=20, marker_matches=False),
                  30:dict(hwnd=30, marker_matches=True, evidence='exact_address_value')}
        with patch.dict(sys.modules, {'win32gui':windows}), \
             patch.object(self.native, 'inspect_native_edge', return_value=checked), \
             patch.object(session, '_start_marker', side_effect=lambda:self.start_marker(session)), \
             patch.object(session, '_windows', side_effect=[{10}, {10,20,30}]), \
             patch.object(session, '_probe_marker', side_effect=lambda hwnd:probes[hwnd]) as probe, \
             patch.object(session, '_address') as address, \
             patch.object(self.native.subprocess, 'Popen', return_value=process) as launch, \
             patch.object(self.native, '_mark_window') as mark, \
             patch.object(self.native.Path, 'write_text', side_effect=AssertionError('启动不应写marker文件')), \
             patch.object(tempfile, 'TemporaryDirectory', side_effect=AssertionError('启动不应创建临时文件夹')), \
             patch.object(self.native.time, 'monotonic', side_effect=[0, 1]):
            session.open()
        self.assertEqual(session.hwnd, 30)
        self.assertEqual(probe.call_args_list, [unittest.mock.call(20), unittest.mock.call(30)])
        mark.assert_called_once_with(30, session.property)
        windows.ShowWindow.assert_called_once_with(30, 1)
        windows.PostMessage.assert_not_called(); address.assert_not_called()
        arguments = launch.call_args.args[0]
        self.assertIn('--profile-directory=Profile 2', arguments)
        self.assertEqual(session.marker_url, 'http://127.0.0.1:54321/jk808/' + session.nonce)
        self.assertEqual(arguments[-1], session.marker_url)
        self.assertFalse(any('file:' in item.lower() or '.html' in item.lower() for item in arguments))
        self.assertEqual(session.record['initial_window_handles'], [10])
        self.assertEqual(session.record['launch']['pid'], 4321)
        self.assertEqual(session.record['launch']['exit_code'], 0)
        self.assertFalse(hasattr(session, '_marker_dir'))
        self.stop_marker.assert_called_once_with()

    def test_unrelated_new_edge_window_is_never_claimed_navigated_or_closed(self):
        session = self.session()
        windows = types.SimpleNamespace(ShowWindow=Mock(), PostMessage=Mock())
        checked = dict(passed=True, executable='C:/Edge/msedge.exe', profile_directory='')
        process = types.SimpleNamespace(pid=4321, poll=Mock(return_value=17))
        probe_result = dict(hwnd=20, marker_matches=False, address_read_error='unit: UIA unavailable')
        with patch.dict(sys.modules, {'win32gui':windows}), \
             patch.object(self.native, 'inspect_native_edge', return_value=checked), \
             patch.object(session, '_start_marker', side_effect=lambda:self.start_marker(session)), \
             patch.object(session, '_windows', side_effect=[{10}, {10,20}]), \
             patch.object(session, '_probe_marker', return_value=probe_result), \
             patch.object(session, '_wait'), patch.object(session, '_address') as address, \
             patch.object(self.native.subprocess, 'Popen', return_value=process), \
             patch.object(self.native, '_mark_window') as mark, \
             patch.object(self.native.time, 'monotonic', side_effect=[0, 1, 31]), \
             self.assertRaisesRegex(RuntimeError, '未能核对本轮地址'):
            session.open()
        self.assertIsNone(session.hwnd)
        mark.assert_not_called(); address.assert_not_called()
        windows.ShowWindow.assert_not_called(); windows.PostMessage.assert_not_called()
        self.assertEqual(session.record['launch']['exit_code'], 17)
        self.assertEqual(session.record['window_scans'][0]['candidates'], [probe_result])

    def test_old_windows_are_never_probed_even_if_their_url_could_match(self):
        session = self.session()
        windows = types.SimpleNamespace(ShowWindow=Mock(), PostMessage=Mock())
        checked = dict(passed=True, executable='C:/Edge/msedge.exe', profile_directory='')
        process = types.SimpleNamespace(pid=4321, poll=Mock(return_value=0))
        with patch.dict(sys.modules, {'win32gui':windows}), \
             patch.object(self.native, 'inspect_native_edge', return_value=checked), \
             patch.object(session, '_start_marker', side_effect=lambda:self.start_marker(session)), \
             patch.object(session, '_windows', side_effect=[{10}, {10}]), \
             patch.object(session, '_probe_marker', return_value=dict(hwnd=10, marker_matches=True)) as probe, \
             patch.object(session, '_wait'), patch.object(session, '_address') as address, \
             patch.object(self.native.subprocess, 'Popen', return_value=process), \
             patch.object(self.native, '_mark_window') as mark, \
             patch.object(self.native.time, 'monotonic', side_effect=[0, 1, 31]), \
             self.assertRaisesRegex(RuntimeError, '未发现本轮Edge新窗口'):
            session.open()
        probe.assert_not_called(); mark.assert_not_called(); address.assert_not_called()
        windows.ShowWindow.assert_not_called(); windows.PostMessage.assert_not_called()

    def test_multiple_matching_new_windows_are_not_arbitrarily_claimed(self):
        session = self.session()
        windows = types.SimpleNamespace(ShowWindow=Mock(), PostMessage=Mock())
        checked = dict(passed=True, executable='C:/Edge/msedge.exe', profile_directory='')
        process = types.SimpleNamespace(pid=4321, poll=Mock(return_value=0))
        with patch.dict(sys.modules, {'win32gui':windows}), \
             patch.object(self.native, 'inspect_native_edge', return_value=checked), \
             patch.object(session, '_start_marker', side_effect=lambda:self.start_marker(session)), \
             patch.object(session, '_windows', side_effect=[{10}, {10,20,30}]), \
             patch.object(session, '_probe_marker', side_effect=lambda hwnd:dict(hwnd=hwnd, marker_matches=True)), \
             patch.object(session, '_address') as address, \
             patch.object(self.native.subprocess, 'Popen', return_value=process), \
             patch.object(self.native, '_mark_window') as mark, \
             patch.object(self.native.time, 'monotonic', side_effect=[0, 1]), \
             self.assertRaisesRegex(RuntimeError, '多个'):
            session.open()
        self.assertIsNone(session.hwnd)
        mark.assert_not_called(); address.assert_not_called()
        windows.ShowWindow.assert_not_called(); windows.PostMessage.assert_not_called()


class MarkerProbeTests(NativeEdgeTests):
    def setUp(self):
        super().setUp()
        title = patch.object(self.native, '_read_title', return_value='普通Edge页面 - Microsoft Edge')
        title.start()
        self.addCleanup(title.stop)

    def session(self):
        session = self.native.NativeEdgeSession(dict(online_video=dict(url=ORIGIN + '/video/example/')))
        session.marker_url = 'http://127.0.0.1:54321/jk808/' + session.nonce
        return session

    def fake_uia(self, value):
        edit = Mock()
        edit.get_value.return_value = value
        window = Mock()
        window.descendants.return_value = [edit]
        desktop = Mock()
        desktop.return_value.window.return_value = window
        return desktop, window, edit

    def test_exact_nonce_address_probe_only_reads_uia_and_never_focuses_or_types(self):
        session = self.session()
        desktop, window, edit = self.fake_uia(session.marker_url)
        keyboard = types.SimpleNamespace(send_keys=Mock(side_effect=AssertionError('地址探测不可发键')))
        with patch.dict(sys.modules, {'pywinauto':types.SimpleNamespace(Desktop=desktop),
                                     'pywinauto.keyboard':keyboard}), \
             patch.object(self.native, '_process_path', return_value='C:/Edge/msedge.exe'), \
             patch.object(session, '_address') as address, \
             patch.object(self.native, '_mark_window') as mark:
            result = session._probe_marker(42)
        self.assertIs(result['marker_matches'], True)
        self.assertEqual(result['evidence'], 'exact_address_value')
        desktop.assert_called_once_with(backend='uia')
        desktop.return_value.window.assert_called_once_with(handle=42)
        window.descendants.assert_called_once_with(control_type='Edit')
        edit.get_value.assert_called_once_with()
        window.set_focus.assert_not_called(); edit.set_edit_text.assert_not_called()
        edit.type_keys.assert_not_called(); edit.click_input.assert_not_called()
        keyboard.send_keys.assert_not_called(); address.assert_not_called(); mark.assert_not_called()
        self.assertIsNone(session.hwnd)

    def test_wrong_or_similar_urls_are_not_ownership_evidence(self):
        session = self.session()
        variants = ('about:blank', 'http://127.0.0.1:54321/jk808/another-run',
                    session.marker_url+'-extra', session.marker_url+'?extra=1',
                    'https://evil.test/?next='+session.marker_url, '', None)
        for value in variants:
            with self.subTest(value=value):
                desktop, window, edit = self.fake_uia(value)
                with patch.dict(sys.modules, {'pywinauto':types.SimpleNamespace(Desktop=desktop)}), \
                     patch.object(self.native, '_process_path', return_value='C:/Edge/msedge.exe'), \
                     patch.object(self.native, '_read_title', return_value='普通Edge页面'):
                    result = session._probe_marker(42)
                self.assertIs(result['marker_matches'], False)
                window.set_focus.assert_not_called(); edit.set_edit_text.assert_not_called()

    def test_empty_marker_url_never_matches_a_blank_address_bar(self):
        session = self.session(); session.marker_url = ''
        desktop, window, edit = self.fake_uia('')
        with patch.dict(sys.modules, {'pywinauto':types.SimpleNamespace(Desktop=desktop)}), \
             patch.object(self.native, '_process_path', return_value='C:/Edge/msedge.exe'):
            result = session._probe_marker(42)
        self.assertIs(result['marker_matches'], False)
        self.assertIsNone(session.hwnd)
        window.set_focus.assert_not_called(); edit.set_edit_text.assert_not_called()

    def test_unrelated_chrome_process_is_rejected_before_reading_its_page(self):
        session = self.session()
        desktop, window, edit = self.fake_uia(session.marker_url)
        with patch.dict(sys.modules, {'pywinauto':types.SimpleNamespace(Desktop=desktop)}), \
             patch.object(self.native, '_process_path', return_value='C:/Chrome/chrome.exe'):
            result = session._probe_marker(42)
        self.assertIs(result['marker_matches'], False)
        self.assertEqual(result['process_name'], 'chrome.exe')
        desktop.assert_not_called(); edit.get_value.assert_not_called()

    def test_process_path_error_is_recorded_but_does_not_discard_exact_address_match(self):
        session = self.session()
        desktop, window, edit = self.fake_uia(session.marker_url)
        with patch.dict(sys.modules, {'pywinauto':types.SimpleNamespace(Desktop=desktop)}), \
             patch.object(self.native, '_process_path', side_effect=OSError('unit: access denied')):
            result = session._probe_marker(42)
        self.assertIs(result['marker_matches'], True)
        self.assertIn('access denied', result['process_query_error'])
        self.assertIn('OSError', result['process_query_error'])

    def test_uia_address_read_error_is_preserved_without_claiming_the_window(self):
        session = self.session()
        desktop, window, edit = self.fake_uia(session.marker_url)
        window.descendants.side_effect = RuntimeError('unit: UIA tree unavailable')
        with patch.dict(sys.modules, {'pywinauto':types.SimpleNamespace(Desktop=desktop)}), \
             patch.object(self.native, '_process_path', return_value='C:/Edge/msedge.exe'):
            result = session._probe_marker(42)
        self.assertIs(result['marker_matches'], False)
        self.assertIn('UIA tree unavailable', result['address_read_error'])
        self.assertIsNone(session.hwnd)
        window.set_focus.assert_not_called(); edit.set_edit_text.assert_not_called()

    def test_value_pattern_fallback_is_read_only(self):
        session = self.session()
        desktop, window, edit = self.fake_uia('')
        edit.get_value.side_effect = RuntimeError('get_value unavailable')
        edit.iface_value = types.SimpleNamespace(CurrentValue=session.marker_url)
        with patch.dict(sys.modules, {'pywinauto':types.SimpleNamespace(Desktop=desktop)}), \
             patch.object(self.native, '_process_path', return_value='C:/Edge/msedge.exe'):
            result = session._probe_marker(42)
        self.assertIs(result['marker_matches'], True)
        window.set_focus.assert_not_called(); edit.set_edit_text.assert_not_called()

    def test_nonce_title_can_confirm_window_without_touching_uia(self):
        session = self.session()
        desktop, window, edit = self.fake_uia('')
        with patch.dict(sys.modules, {'pywinauto':types.SimpleNamespace(Desktop=desktop)}), \
             patch.object(self.native, '_process_path', return_value='C:/Edge/msedge.exe'), \
             patch.object(self.native, '_read_title', return_value='JK808-OWN-'+session.nonce+' - 工作配置 - Microsoft Edge'):
            result = session._probe_marker(42)
        self.assertIs(result['marker_matches'], True)
        self.assertEqual(result['evidence'], 'exact_nonce_title')
        desktop.assert_not_called(); edit.get_value.assert_not_called()

    def test_edge_address_bar_may_hide_only_the_http_scheme(self):
        session = self.session()
        desktop, window, edit = self.fake_uia(session.marker_url.removeprefix('http://'))
        with patch.dict(sys.modules, {'pywinauto':types.SimpleNamespace(Desktop=desktop)}), \
             patch.object(self.native, '_process_path', return_value='C:/Edge/msedge.exe'):
            result = session._probe_marker(42)
        self.assertIs(result['marker_matches'], True)
        self.assertEqual(result['evidence'], 'exact_address_value')
        window.set_focus.assert_not_called(); edit.set_edit_text.assert_not_called()

    def test_cancellation_inside_address_probe_is_not_swallowed_as_uia_error(self):
        session = self.session()
        desktop, window, edit = self.fake_uia(session.marker_url)
        session.stop = types.SimpleNamespace(is_set=Mock(side_effect=[False, False, False, True]))
        with patch.dict(sys.modules, {'pywinauto':types.SimpleNamespace(Desktop=desktop)}), \
             patch.object(self.native, '_process_path', return_value='C:/Edge/msedge.exe'), \
             self.assertRaises(InterruptedError):
            session._probe_marker(42)
        edit.get_value.assert_not_called()

    def test_candidate_enumeration_does_not_drop_windows_for_process_path_query_errors(self):
        attributes = {10:(True, 'Chrome_WidgetWin_1'), 11:(True, 'Chrome_WidgetWin_0'),
                      12:(False, 'Chrome_WidgetWin_1'), 13:(True, 'OtherApplication')}
        windows = types.SimpleNamespace(
            IsWindowVisible=Mock(side_effect=lambda hwnd:attributes[hwnd][0]),
            GetClassName=Mock(side_effect=lambda hwnd:attributes[hwnd][1]),
            EnumWindows=Mock(side_effect=lambda callback,param:[callback(hwnd,param) for hwnd in attributes]))
        with patch.dict(sys.modules, {'win32gui':windows}), \
             patch.object(self.native, '_process_path', side_effect=AssertionError('候选枚举不可先按进程查询丢弃')) as path:
            result = self.native.NativeEdgeSession._windows()
        self.assertEqual(result, {10,11})
        path.assert_not_called()


class LoopbackMarkerTests(NativeEdgeTests):
    def session(self):
        return self.native.NativeEdgeSession(dict(online_video=dict(url=ORIGIN + '/video/example/')))

    def server(self):
        return types.SimpleNamespace(server_port=54321, serve_forever=Mock(), shutdown=Mock(), server_close=Mock())

    def test_marker_binds_only_ipv4_loopback_and_requests_a_random_port_without_files(self):
        session = self.session()
        self.assertEqual(session.marker_url, '')
        server = self.server()
        thread = Mock()
        with patch('http.server.ThreadingHTTPServer', return_value=server) as listen, \
             patch.object(self.native.threading, 'Thread', return_value=thread) as spawn, \
             patch.object(self.native.Path, 'write_text', side_effect=AssertionError('确认页不能写文件')), \
             patch.object(tempfile, 'TemporaryDirectory', side_effect=AssertionError('确认页不能依赖临时目录')):
            session._start_marker()
        self.assertEqual(listen.call_args.args[0], ('127.0.0.1', 0))
        self.assertEqual(session.marker_url, 'http://127.0.0.1:54321/jk808/'+session.nonce)
        self.assertIs(session._marker_server, server)
        self.assertIs(server.daemon_threads, True)
        self.assertIs(spawn.call_args.kwargs['target'], server.serve_forever)
        self.assertIs(spawn.call_args.kwargs['daemon'], True)
        thread.start.assert_called_once_with()
        server.serve_forever.assert_not_called()

    def test_marker_response_is_only_available_at_this_runs_exact_nonce_route(self):
        session = self.session(); server = self.server()
        with patch('http.server.ThreadingHTTPServer', return_value=server) as listen, \
             patch.object(self.native.threading, 'Thread', return_value=Mock()):
            session._start_marker()
        handler_class = listen.call_args.args[1]
        route = '/jk808/'+session.nonce
        for path in (route, '/', '/favicon.ico', '/jk808/another-run', route+'?extra=1', route+'/'):
            with self.subTest(path=path):
                handler = types.SimpleNamespace(path=path, send_error=Mock(), send_response=Mock(),
                    send_header=Mock(), end_headers=Mock(), wfile=io.BytesIO())
                handler_class.do_GET(handler)
                if path == route:
                    handler.send_response.assert_called_once_with(200)
                    handler.send_error.assert_not_called()
                    self.assertIn(('<title>JK808-OWN-'+session.nonce+'</title>').encode(), handler.wfile.getvalue())
                    self.assertIn(unittest.mock.call('Cache-Control', 'no-store'), handler.send_header.call_args_list)
                else:
                    handler.send_error.assert_called_once_with(404)
                    handler.send_response.assert_not_called()
                    self.assertEqual(handler.wfile.getvalue(), b'')

    def test_stopping_marker_is_idempotent_and_clears_server_references(self):
        session = self.session(); server = self.server()
        session._marker_server = server
        session._marker_thread = types.SimpleNamespace(is_alive=Mock(return_value=True))
        session._stop_marker(); session._stop_marker()
        server.shutdown.assert_called_once_with(); server.server_close.assert_called_once_with()
        self.assertIsNone(session._marker_server); self.assertIsNone(session._marker_thread)

    def test_failed_marker_thread_start_closes_server_without_leaving_live_state(self):
        session = self.session(); server = self.server()
        thread = Mock(); thread.start.side_effect = RuntimeError('unit: thread start failed')
        with patch('http.server.ThreadingHTTPServer', return_value=server), \
             patch.object(self.native.threading, 'Thread', return_value=thread), \
             self.assertRaisesRegex(RuntimeError, 'thread start failed'):
            session._start_marker()
        server.server_close.assert_called_once_with()
        self.assertIsNone(session._marker_server); self.assertIsNone(session._marker_thread)

    def test_failed_thread_construction_closes_server_and_clears_references(self):
        session = self.session(); server = self.server()
        with patch('http.server.ThreadingHTTPServer', return_value=server), \
             patch.object(self.native.threading, 'Thread', side_effect=RuntimeError('unit thread constructor failure')), \
             self.assertRaisesRegex(RuntimeError, 'thread constructor failure'):
            session._start_marker()
        server.server_close.assert_called_once_with()
        server.shutdown.assert_not_called()  # serve_forever未运行，shutdown会等待无从置位的事件。
        self.assertIsNone(session._marker_server); self.assertIsNone(session._marker_thread)

    def test_socket_close_failure_still_clears_marker_references(self):
        session = self.session(); server = self.server()
        session._marker_server = server
        session._marker_thread = types.SimpleNamespace(is_alive=Mock(return_value=True))
        server.server_close.side_effect = OSError('unit socket close failure')
        with self.assertRaisesRegex(OSError, 'socket close failure'):
            session._stop_marker()
        server.shutdown.assert_called_once_with(); server.server_close.assert_called_once_with()
        self.assertIsNone(session._marker_server); self.assertIsNone(session._marker_thread)

    def test_failed_or_cancelled_start_stops_marker_without_closing_other_windows(self):
        for error in (RuntimeError('unit: marker not found'), InterruptedError('unit: cancelled')):
            with self.subTest(error=type(error).__name__):
                session = self.session(); server = self.server()
                session._marker_server = server
                session._marker_thread = types.SimpleNamespace(is_alive=Mock(return_value=True))
                windows = types.SimpleNamespace(PostMessage=Mock())
                with patch.dict(sys.modules, {'win32gui':windows}), \
                     patch.object(self.native, 'NativeEdgeSession', return_value=session), \
                     patch.object(session, 'open', side_effect=error), \
                     patch.object(session, 'save_record'), \
                     patch.object(self.native.subprocess, 'Popen') as launch, \
                     self.assertRaises(type(error)):
                    self.native.start_online_video(dict(online_video=dict(url=ORIGIN)))
                server.shutdown.assert_called_once_with(); server.server_close.assert_called_once_with()
                windows.PostMessage.assert_not_called(); launch.assert_not_called()
                self.assertIsNone(session._marker_server)

    def test_close_stops_marker_even_when_window_ownership_has_changed(self):
        session = self.session(); session.hwnd = 123
        server = self.server(); session._marker_server = server
        session._marker_thread = types.SimpleNamespace(is_alive=Mock(return_value=True))
        windows = types.SimpleNamespace(IsWindow=Mock(return_value=True), PostMessage=Mock())
        with patch.dict(sys.modules, {'win32gui':windows}), \
             patch.object(self.native, '_window_property', return_value=0), self.assertRaises(RuntimeError):
            session.close()
        server.shutdown.assert_called_once_with(); server.server_close.assert_called_once_with()
        windows.PostMessage.assert_not_called()

    def test_unstarted_or_exited_marker_thread_skips_shutdown_but_closes_server(self):
        for state in ('not_constructed', 'not_started', 'already_exited'):
            with self.subTest(state=state):
                session = self.session(); server = self.server()
                thread = None if state == 'not_constructed' else types.SimpleNamespace(is_alive=Mock(return_value=False))
                session._marker_server = server; session._marker_thread = thread
                session._stop_marker()
                server.shutdown.assert_not_called()
                server.server_close.assert_called_once_with()
                if thread is not None:
                    thread.is_alive.assert_called_once_with()
                self.assertIsNone(session._marker_server)
                self.assertIsNone(session._marker_thread)


if __name__ == '__main__':
    unittest.main()
