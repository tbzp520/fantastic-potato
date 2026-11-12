"""登录和窗口动作的纯桩序列回归；不启动浏览器、网络或实际窗口。"""
from contextlib import contextmanager, ExitStack
import copy
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_edge as native


MISSING = object()


def configuration():
    return {'online_video': {'url': 'https://www.bilibili.com/video/BVoffline/',
                             'page_retry_count': 5}}


class Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1_700_000_000 + self.now

    def advance(self, seconds):
        self.now += seconds
        if self.now > 2000:
            raise AssertionError('虚拟准备流程超过预算，取消没有生效')


class Stop:
    def __init__(self, clock, cancel_at=None):
        self.clock, self.cancel_at = clock, cancel_at

    def is_set(self):
        return self.cancel_at is not None and self.clock.now >= self.cancel_at


def observed(now, *, confirmed=True, login_visible=False, **changes):
    state = dict(has_video=True, ready='complete', login_visible=login_visible)
    if confirmed is not MISSING:
        state['login_confirmed'] = confirmed
    sample = dict(observation_available=True, position=now, paused=False, ended=False,
                  readyState=4, width=1920, height=1080, playback_rate=1,
                  frame_age_s=.1, frame_span_s=2, observed_fps=30,
                  quality=dict(selected='80', label='1080P', choices=[]),
                  fullscreen=dict(active=True, contains_video=True, fills_viewport=True),
                  page_state=state)
    sample.update(changes)
    return sample


class LoginWindowFlowTests(unittest.TestCase):
    @contextmanager
    def fixture(self, observations, *, cancel_at=None, on_action=None):
        """真实prepare加动作账本；所有系统边界均禁止落到真实环境。"""
        clock = Clock()
        status = {}
        session = native.NativeEdgeSession(configuration(), stop=Stop(clock, cancel_at), status=status)
        actions, samples, messages = [], [], []

        def action(name, detail=None):
            actions.append((clock.now, name, detail))
            if on_action:
                on_action(name, detail)

        def sample():
            self.assertNotIn('start_epoch', status)
            value = copy.deepcopy(observations(clock.now))
            samples.append((clock.now, value))
            return value

        def wait(seconds):
            session._check()
            action('wait', seconds)
            clock.advance(seconds)
            session._check()

        def inject(script):
            # 一段脚本可包含多个动作；保留顺序，让迟到扫码场景可追踪。
            kinds = [(script.find(token), name) for token, name in (
                ('.quality(', 'quality'), ('.play(', 'play'), ('.controls(', 'controls'))
                if token in script]
            if not kinds:
                action('inject', script)
            for _, name in sorted(kinds):
                action(name, script)

        def click(name):
            action({'JK808播放': 'play', 'JK808视频全屏': 'fullscreen'}.get(name, 'click'), name)

        def message(text, waiting=False):
            messages.append((clock.now, text, waiting))
            status.update(message=text, waiting_for_user=waiting)

        session.sample = Mock(side_effect=sample)
        session._address = Mock(side_effect=lambda url: action('navigate', url))
        session._install_bridge = Mock(side_effect=lambda: action('bridge'))
        session._inject = Mock(side_effect=inject)
        session._click = Mock(side_effect=click)
        session._restore_for_login = Mock(side_effect=lambda: action('restore'))
        session._maximize_for_playback = Mock(side_effect=lambda: action('maximize'))
        session._wait = Mock(side_effect=wait)
        session._message = Mock(side_effect=message)
        session.open = Mock()
        session.close = Mock()
        session.save_record = Mock()
        forbidden = AssertionError('纯桩测试禁止访问实际浏览器、窗口或网络')
        with ExitStack() as stack:
            stack.enter_context(patch.object(native.time, 'monotonic', clock.monotonic))
            stack.enter_context(patch.object(native.time, 'time', clock.time))
            stack.enter_context(patch.object(native.time, 'sleep', side_effect=forbidden))
            stack.enter_context(patch.object(native, '_read_title', side_effect=forbidden))
            stack.enter_context(patch.object(native, 'inspect_native_edge', side_effect=forbidden))
            stack.enter_context(patch.object(native.subprocess, 'Popen', side_effect=forbidden))
            stack.enter_context(patch('http.server.ThreadingHTTPServer', side_effect=forbidden))
            yield types.SimpleNamespace(clock=clock, status=status, session=session,
                                        actions=actions, samples=samples, messages=messages)

    def assert_waited_without_player_actions(self, fixture, *, since=100):
        forbidden = {'maximize', 'fullscreen', 'quality', 'play', 'controls'}
        self.assertFalse([a for a in fixture.actions if a[0] >= since and a[1] in forbidden],
                         fixture.actions)
        self.assertGreaterEqual(fixture.clock.now-since, 600)
        self.assertTrue(any(a[1] == 'restore' for a in fixture.actions))
        self.assertTrue(any(waiting for _, _, waiting in fixture.messages))
        self.assertNotIn('passed', fixture.session.record)
        self.assertNotIn('start_epoch', fixture.status)
        self.assertEqual(fixture.session._address.call_count, 1, '扫码等待不得反复刷新页面')

    def test_unconfirmed_login_cannot_pass_on_a_valid_1080_preview_and_waits_until_cancelled(self):
        for confirmed in (MISSING, None, False, 1, 'true'):
            with self.subTest(confirmed=confirmed), self.fixture(
                    lambda now: observed(now, confirmed=confirmed), cancel_at=710) as f:
                with self.assertRaisesRegex(InterruptedError, '取消'):
                    f.session.prepare()
                self.assert_waited_without_player_actions(f)

    def test_visible_login_overlay_overrides_stale_confirmed_flag(self):
        with self.fixture(lambda now: observed(now, confirmed=True, login_visible=True),
                          cancel_at=710) as f:
            with self.assertRaises(InterruptedError):
                f.session.prepare()
            self.assert_waited_without_player_actions(f)

    def test_no_video_login_page_also_waits_without_controls_or_refresh(self):
        def sample(now):
            return dict(observation_available=False, page_state=dict(has_video=False,
                        ready='complete', login_confirmed=False, login_visible=True))
        with self.fixture(sample, cancel_at=710) as f:
            with self.assertRaises(InterruptedError):
                f.session.prepare()
            self.assert_waited_without_player_actions(f)

    def test_missing_page_state_is_unknown_login_even_with_valid_preview(self):
        def sample(now):
            value = observed(now)
            value.pop('page_state')
            return value
        with self.fixture(sample, cancel_at=710) as f:
            with self.assertRaises(InterruptedError):
                f.session.prepare()
            self.assert_waited_without_player_actions(f)

    def test_login_confirmation_allows_maximize_then_fullscreen_and_stability(self):
        state = dict(fullscreen=False)

        def sample(now):
            return observed(now, confirmed=now >= 710,
                            fullscreen=dict(active=state['fullscreen'], contains_video=state['fullscreen'],
                                            fills_viewport=state['fullscreen']))

        def on_action(name, detail):
            if name == 'fullscreen':
                state['fullscreen'] = True

        with self.fixture(sample, cancel_at=760, on_action=on_action) as f:
            result = f.session.prepare()
            operations = [(t, name) for t, name, _ in f.actions if name in ('maximize', 'fullscreen')]
            self.assertTrue(operations, f.actions)
            self.assertTrue(all(t >= 710 for t, _ in operations), operations)
            names = [name for _, name in operations]
            self.assertEqual(names[0], 'maximize')
            self.assertIn('fullscreen', names)
            self.assertLess(names.index('maximize'), names.index('fullscreen'))
            self.assertTrue(result['fullscreen']['active'])
            self.assertGreaterEqual(f.clock.now, 712, '全屏之后仍需稳定观察2秒')
            self.assertEqual(f.session._address.call_count, 1)

    def late_login_case(self, trigger):
        state = dict(login=False, full=False, paused=trigger == 'play',
                     width=1280 if trigger == 'quality' else 1920,
                     rate=2 if trigger == 'speed' else 1)
        triggered_at = []

        def sample(now):
            return observed(now, confirmed=not state['login'], login_visible=state['login'],
                            width=state['width'], height=720 if state['width'] < 1920 else 1080,
                            playback_rate=state['rate'], paused=state['paused'],
                            fullscreen=dict(active=state['full'], contains_video=state['full'],
                                            fills_viewport=state['full']))

        def on_action(name, detail):
            expected = 'play' if trigger in ('play', 'speed') else trigger
            if name == expected and not state['login']:
                state['login'] = True
                triggered_at.append(f.clock.now)

        with self.fixture(sample, cancel_at=760, on_action=on_action) as f:
            with self.assertRaises(InterruptedError):
                f.session.prepare()
            self.assertEqual(len(triggered_at), 1, f.actions)
            self.assertFalse([a for a in f.actions if a[1] in {'maximize', 'fullscreen'}],
                             '必须先完成并重查画质/速度/播放，才能最大化或全屏：'+repr(f.actions))
            index = next(i for i, (_, name, _) in enumerate(f.actions)
                         if name == ('play' if trigger in ('play', 'speed') else trigger))
            later = f.actions[index+1:]
            self.assertFalse([a for a in later if a[1] in {'maximize', 'fullscreen', 'quality', 'play'}], later)
            restore = next((i for i, a in enumerate(later) if a[1] == 'restore'), None)
            self.assertIsNotNone(restore, '迟到扫码必须恢复普通窗口')
            self.assertTrue(any(a[1] == 'wait' for a in later[restore+1:]))
            self.assertGreaterEqual(f.clock.now-triggered_at[0], 600)
            self.assertNotIn('passed', f.session.record)

    def test_quality_action_that_opens_login_restores_normal_and_stops_followup_actions(self):
        self.late_login_case('quality')

    def test_play_click_that_opens_login_restores_normal_and_never_maximizes_or_fullscreens(self):
        self.late_login_case('play')

    def test_speed_repair_that_opens_login_does_not_touch_quality_or_window_afterward(self):
        self.late_login_case('speed')

    def test_late_login_can_clear_and_resume_quality_then_maximize_and_fullscreen(self):
        state = dict(login=False, width=1280, full=False, prompted=False)

        def sample(now):
            if state['login'] and now >= 110:
                state['login'] = False
            return observed(now, confirmed=not state['login'], login_visible=state['login'],
                            width=state['width'], height=1080 if state['width'] == 1920 else 720,
                            fullscreen=dict(active=state['full'], contains_video=state['full'],
                                            fills_viewport=state['full']))

        def on_action(name, detail):
            if name == 'quality':
                if not state['prompted']:
                    state['prompted'] = state['login'] = True
                else:
                    state['width'] = 1920
            elif name == 'fullscreen':
                state['full'] = True

        with self.fixture(sample, cancel_at=180, on_action=on_action) as f:
            result = f.session.prepare()
            self.assertGreaterEqual(sum(a[1] == 'quality' for a in f.actions), 2)
            self.assertTrue(any(a[1] == 'restore' for a in f.actions))
            self.assertFalse([a for a in f.actions if 100 < a[0] < 110 and
                              a[1] in {'quality', 'play', 'maximize', 'fullscreen'}], f.actions)
            operations = [a for a in f.actions if a[1] in {'maximize', 'fullscreen'}]
            self.assertTrue(operations)
            self.assertTrue(all(a[0] >= 110 for a in operations), operations)
            self.assertEqual(operations[0][1], 'maximize')
            self.assertIn('fullscreen', [a[1] for a in operations])
            self.assertTrue(result['fullscreen']['active'])

    def test_cancel_during_login_wait_propagates_and_start_wrapper_closes_only_this_session(self):
        with self.fixture(lambda now: observed(now, confirmed=False), cancel_at=101) as f:
            with patch.object(native, 'NativeEdgeSession', return_value=f.session):
                with self.assertRaisesRegex(InterruptedError, '取消'):
                    native.start_online_video(f.session.cfg, stop=f.session.stop, status=f.status)
            f.session.close.assert_called_once_with()
            self.assertFalse(f.status['waiting_for_user'])
            self.assertNotIn('passed', f.session.record)
            self.assertFalse([a for a in f.actions if a[1] in {'maximize', 'fullscreen', 'quality', 'play'}])


class LoginWindowOpenTests(unittest.TestCase):
    def test_login_restore_only_changes_owned_maximized_or_minimized_window_without_focus(self):
        for show in (1, 3, 2, 6, 7):
            with self.subTest(show=show):
                session = native.NativeEdgeSession(configuration())
                session.hwnd = 30
                session._playback_maximized = True
                placement = (0, show, (0, 0), (0, 0), (0, 0, 1920, 1080))
                windows = types.SimpleNamespace(GetWindowPlacement=Mock(return_value=placement), ShowWindow=Mock(),
                                                SetForegroundWindow=Mock())
                with patch.dict(sys.modules, {'win32gui': windows}), \
                     patch.object(session, '_owns_window', return_value=True), \
                     patch.object(session, '_window', side_effect=AssertionError('登录等待不得聚焦窗口')):
                    session._restore_for_login()
                windows.GetWindowPlacement.assert_called_once_with(30)
                if show in (2, 3, 6, 7):
                    windows.ShowWindow.assert_called_once_with(30, 1)
                else:
                    windows.ShowWindow.assert_not_called()
                windows.SetForegroundWindow.assert_not_called()
                self.assertFalse(session._playback_maximized)

    def test_login_restore_rejects_changed_ownership_before_reading_or_changing_window(self):
        session = native.NativeEdgeSession(configuration())
        session.hwnd = 30
        windows = types.SimpleNamespace(GetWindowPlacement=Mock(), ShowWindow=Mock())
        with patch.dict(sys.modules, {'win32gui': windows}), \
             patch.object(session, '_owns_window', return_value=False):
            with self.assertRaisesRegex(RuntimeError, '所有权'):
                session._restore_for_login()
        windows.GetWindowPlacement.assert_not_called()
        windows.ShowWindow.assert_not_called()

    def test_playback_maximize_checks_ownership_and_only_changes_this_window(self):
        session = native.NativeEdgeSession(configuration())
        session.hwnd = 30
        windows = types.SimpleNamespace(ShowWindow=Mock())
        with patch.dict(sys.modules, {'win32gui': windows}), \
             patch.object(session, '_owns_window', return_value=True) as owns, \
             patch.object(session, '_window', side_effect=AssertionError('最大化不得另找其他窗口')):
            session._maximize_for_playback()
        owns.assert_called_once_with()
        windows.ShowWindow.assert_called_once_with(30, 3)
        self.assertTrue(session._playback_maximized)

    def test_playback_maximize_rejects_changed_ownership(self):
        session = native.NativeEdgeSession(configuration())
        session.hwnd = 30
        windows = types.SimpleNamespace(ShowWindow=Mock())
        with patch.dict(sys.modules, {'win32gui': windows}), \
             patch.object(session, '_owns_window', return_value=False):
            with self.assertRaisesRegex(RuntimeError, '所有权'):
                session._maximize_for_playback()
        windows.ShowWindow.assert_not_called()

    def test_playback_maximize_cancelled_before_ownership_or_window_action(self):
        stop = types.SimpleNamespace(is_set=lambda: True)
        session = native.NativeEdgeSession(configuration(), stop=stop)
        session.hwnd = 30
        windows = types.SimpleNamespace(ShowWindow=Mock())
        with patch.dict(sys.modules, {'win32gui': windows}), \
             patch.object(session, '_owns_window') as owns:
            with self.assertRaises(InterruptedError):
                session._maximize_for_playback()
        owns.assert_not_called()
        windows.ShowWindow.assert_not_called()

    def test_open_restores_inherited_maximized_new_window_without_maximizing_or_touching_old_window(self):
        session = native.NativeEdgeSession(configuration())
        windows = types.SimpleNamespace(ShowWindow=Mock(), PostMessage=Mock(),
                                        GetWindowPlacement=Mock(return_value=(0, 3, (0, 0), (0, 0), (0, 0, 1920, 1080))))
        process = types.SimpleNamespace(pid=1234, poll=Mock(return_value=0))
        checked = dict(passed=True, executable='C:/Edge/msedge.exe', profile_directory='Default')

        def start_marker():
            session.marker_url = 'http://127.0.0.1:54321/jk808/' + session.nonce

        with patch.dict(sys.modules, {'win32gui': windows}), \
             patch.object(native, 'inspect_native_edge', return_value=checked), \
             patch.object(session, '_start_marker', side_effect=start_marker), \
             patch.object(session, '_stop_marker') as stop_marker, \
             patch.object(session, '_windows', side_effect=[{10}, {10, 30}]), \
             patch.object(session, '_probe_marker', return_value=dict(hwnd=30, marker_matches=True)), \
             patch.object(native.subprocess, 'Popen', return_value=process), \
             patch.object(native, '_mark_window') as mark, \
             patch.object(native.time, 'monotonic', side_effect=[0, 1]), \
             patch.object(native, '_read_title', side_effect=AssertionError('禁止读取真实窗口')), \
             patch('http.server.ThreadingHTTPServer', side_effect=AssertionError('禁止真实socket')):
            session.open()
        self.assertEqual(session.hwnd, 30)
        mark.assert_called_once_with(30, session.property)
        windows.ShowWindow.assert_called_once_with(30, 1)
        self.assertFalse(any(c.args[1] == 3 for c in windows.ShowWindow.call_args_list))
        windows.PostMessage.assert_not_called()
        stop_marker.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
