"""常用Edge准备流程回归：虚拟时间、页面与窗口桩，不启动实际浏览器。"""
from contextlib import contextmanager, ExitStack
import copy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_edge as native
import windows_temperature_test as app
from test_revision_037 import configuration


class Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1700000000 + self.now

    def advance(self, seconds):
        self.now += seconds
        if self.now > 3000:
            raise AssertionError('准备流程超过测试时间预算，可能没有退出')


class Stop:
    def __init__(self, clock, cancel_at=None):
        self.clock, self.cancel_at = clock, cancel_at

    def is_set(self):
        return self.cancel_at is not None and self.clock.now >= self.cancel_at


def playing(now, **changes):
    sample = dict(observation_available=True, position=now, paused=False, ended=False,
                  readyState=4, width=1920, height=1080, playback_rate=1,
                  frame_age_s=.1, frame_span_s=2, observed_fps=30,
                  quality=dict(selected='80', label='1080P', choices=[]),
                  page_state=dict(has_video=True, ready='complete', login_confirmed=True, login_visible=False, error=False),
                  fullscreen=dict(active=True, contains_video=True, fills_viewport=True))
    sample.update(changes)
    return sample


class PrepareTests(unittest.TestCase):
    @contextmanager
    def fixture(self, observations, *, cancel_at=None, page_retry_count=5):
        """保留真实prepare/判断器，仅替换时间及有外部副作用的边界。"""
        clock = Clock(); status = {}; cfg = configuration()
        cfg['online_video']['page_retry_count'] = page_retry_count
        stop = Stop(clock, cancel_at)
        session = native.NativeEdgeSession(cfg, stop=stop, status=status)
        seen, messages, bridges, navigations = [], [], [], []
        original_message = session._message

        def message(text, waiting=False):
            messages.append((clock.now, text, waiting))
            original_message(text, waiting)

        def observe():
            # prepare未通过前，正式起点不能被写入状态。
            self.assertNotIn('start_epoch', status)
            value = copy.deepcopy(observations(clock.now))
            seen.append((clock.now, value))
            return value

        def wait(seconds):
            session._check()
            clock.advance(seconds)
            session._check()

        session.sample = Mock(side_effect=observe)
        session._address = Mock(side_effect=lambda value:navigations.append((clock.now, value)))
        session._install_bridge = Mock(side_effect=lambda:bridges.append(clock.now))
        session._inject = Mock()
        session._click = Mock()
        session._restore_for_login = Mock()
        session._maximize_for_playback = Mock()
        session._wait = Mock(side_effect=wait)
        session._message = Mock(side_effect=message)
        session.open = Mock()
        session.close = Mock()
        session.save_record = Mock()
        with ExitStack() as stack:
            stack.enter_context(patch.object(native.time, 'monotonic', clock.monotonic))
            stack.enter_context(patch.object(native.time, 'time', clock.time))
            stack.enter_context(patch.object(native.time, 'sleep', side_effect=AssertionError('禁止真实等待')))
            stack.enter_context(patch.object(native, '_read_title', side_effect=AssertionError('禁止读取真实窗口')))
            stack.enter_context(patch.object(native, 'inspect_native_edge', side_effect=AssertionError('禁止读取真实用户环境')))
            stack.enter_context(patch.object(native.subprocess, 'Popen', side_effect=AssertionError('禁止启动实际浏览器')))
            yield types.SimpleNamespace(clock=clock, cfg=cfg, stop=stop, status=status, session=session,
                                        seen=seen, messages=messages, bridges=bridges, navigations=navigations)

    def start(self, fixture):
        with patch.object(native, 'NativeEdgeSession', return_value=fixture.session):
            return native.start_online_video(fixture.cfg, stop=fixture.stop, status=fixture.status)

    def test_page_error_or_missing_bridge_uses_at_least_six_attempts_before_failure(self):
        for sample in (dict(observation_available=False, page_state=dict(error=True, has_video=False)),
                       dict(observation_available=False, observation_error='遥测尚未安装')):
            with self.subTest(sample=sample), self.fixture(lambda now:sample, page_retry_count=1) as f:
                with self.assertRaisesRegex(RuntimeError, '至少5次重新加载'):
                    self.start(f)
                self.assertEqual(len(f.navigations), 6)
                self.assertTrue(all(value == f.session.url for _, value in f.navigations))
                self.assertEqual(len(f.session.record['page_attempts']), 6)
                self.assertTrue(all(not attempt['bridge_ready'] for attempt in f.session.record['page_attempts']))
                self.assertGreaterEqual(f.clock.now-100, 120)
                f.session.close.assert_called_once()
                self.assertNotIn('passed', f.session.record)

    def test_late_error_page_consumes_the_same_six_navigation_budget_and_fails_explicitly(self):
        observations = [0]
        def sample(now):
            observations[0] += 1
            # 初次桥握手后才出现错误页，不能把后续错误当作无限扫码等待。
            return dict(observation_available=False,
                page_state=dict(has_video=False, ready='complete', error=observations[0] > 1))
        with self.fixture(sample, cancel_at=400) as f:
            with self.assertRaises(RuntimeError) as caught:
                self.start(f)
            self.assertRegex(str(caught.exception), '页面|重试|重新加载|遥测')
            self.assertEqual(len(f.navigations), 6, '初次导航与晚到错误必须共用总预算')
            attempts = f.session.record['page_attempts']
            self.assertEqual(len(attempts), 6)
            self.assertEqual([attempt['attempt'] for attempt in attempts], list(range(1, 7)))
            self.assertFalse(f.status['waiting_for_user'])
            self.assertNotIn('passed', f.session.record)
            self.assertNotIn('start_epoch', f.status)
            f.session.close.assert_called_once()

    def test_initial_temporary_bridge_install_error_is_retried_until_ready(self):
        state = dict(installs=0, restored=False)
        def sample(now):
            return playing(now) if state['restored'] else dict(observation_available=False, observation_error='bridge missing')
        with self.fixture(sample) as f:
            def install():
                state['installs'] += 1
                if state['installs'] == 1:
                    raise RuntimeError('暂时未找到地址栏焦点')
                state['restored'] = True
            f.session._install_bridge.side_effect = install
            workload = self.start(f)
            self.assertGreaterEqual(state['installs'], 2)
            self.assertTrue(workload['last']['observation_available'])
            self.assertTrue(f.session.record['passed'])
            self.assertLessEqual(len(f.navigations), 6)
            f.session.close.assert_not_called()

    def test_recovery_temporary_bridge_install_error_is_retried_until_ready(self):
        state = dict(reads=0, installs=0, restored=False)
        def sample(now):
            state['reads'] += 1
            if state['reads'] == 1 or state['restored']:
                return playing(now)
            return dict(observation_available=False, observation_error='bridge lost after initial handshake')
        with self.fixture(sample) as f:
            def install():
                state['installs'] += 1
                if state['installs'] == 1:
                    raise RuntimeError('页面切换时地址栏暂不可用')
                state['restored'] = True
            f.session._install_bridge.side_effect = install
            workload = self.start(f)
            self.assertGreaterEqual(state['installs'], 2)
            self.assertTrue(workload['last']['observation_available'])
            self.assertTrue(f.session.record['passed'])
            self.assertLessEqual(len(f.navigations), 6)
            f.session.close.assert_not_called()

    def test_bridge_install_cancellation_propagates_in_initial_and_recovery_paths(self):
        for recovery in (False, True):
            reads = [0]
            def sample(now):
                reads[0] += 1
                if recovery and reads[0] == 1:
                    return playing(now)
                return dict(observation_available=False, observation_error='bridge missing')
            with self.subTest(recovery=recovery), self.fixture(sample) as f:
                f.session._install_bridge.side_effect = InterruptedError('测试人员取消安装遥测')
                with self.assertRaisesRegex(InterruptedError, '取消'):
                    self.start(f)
                self.assertNotIn('passed', f.session.record)
                self.assertFalse(f.status['waiting_for_user'])
                f.session.close.assert_called_once()

    def test_cancel_during_page_bootstrap_exits_and_closes_the_owned_session(self):
        with self.fixture(lambda now:dict(observation_available=False), cancel_at=103) as f:
            with self.assertRaisesRegex(InterruptedError, '取消'):
                self.start(f)
            self.assertLessEqual(f.clock.now, 103.5)
            self.assertEqual(len(f.navigations), 1)
            f.session.close.assert_called_once()
            self.assertFalse(f.status['waiting_for_user'])
            self.assertNotIn('start_epoch', f.status)

    def test_cancel_during_login_wait_does_not_refresh_or_start_the_timer(self):
        login = dict(observation_available=False, page_state=dict(has_video=False, ready='complete', error=False))
        with self.fixture(lambda now:login, cancel_at=120) as f:
            with self.assertRaises(InterruptedError):
                self.start(f)
            self.assertEqual(len(f.navigations), 1)
            f.session._install_bridge.assert_not_called()
            f.session._inject.assert_not_called()
            f.session.close.assert_called_once()
            self.assertTrue(any(waiting for _, _, waiting in f.messages))
            self.assertFalse(f.status['waiting_for_user'])
            self.assertNotIn('start_epoch', f.status)

    def test_login_can_wait_ten_minutes_without_refresh_or_timeout_then_prepare_normally(self):
        def sample(now):
            if now < 700:
                return dict(observation_available=False, page_state=dict(has_video=False, ready='complete', error=False))
            return playing(now)
        with self.fixture(sample) as f:
            workload = self.start(f)
            self.assertGreaterEqual(f.clock.now, 702)
            self.assertLess(f.clock.now, 703)
            self.assertEqual(len(f.navigations), 1)
            f.session._install_bridge.assert_not_called()
            f.session._inject.assert_not_called()
            f.session._click.assert_not_called()
            self.assertIs(workload['page'], f.session)
            self.assertTrue(f.session.record['passed'])
            self.assertFalse(f.status['waiting_for_user'])
            self.assertTrue(any(now >= 699 and waiting for now, _, waiting in f.messages))

    def test_good_video_must_remain_valid_for_two_seconds_before_returning(self):
        with self.fixture(playing) as f:
            workload = self.start(f)
            self.assertGreaterEqual(f.clock.now, 102)
            self.assertLess(f.clock.now, 103)
            self.assertEqual(workload['last']['position'], f.clock.now)
            self.assertTrue(f.session.record['passed'])
            self.assertEqual(len(f.navigations), 1)
            f.session.close.assert_not_called()

    def test_visible_login_overlay_waits_six_hundred_seconds_without_controls_then_continues(self):
        def sample(now):
            if now < 700:
                # 页面虽有正在播放的低清预览，扫码遮罩仍须优先于选档和全屏。
                return playing(now, width=640, height=360, observed_fps=24,
                    fullscreen=dict(active=False, contains_video=False, fills_viewport=False),
                    page_state=dict(has_video=True, ready='complete', login_visible=True, error=False))
            return playing(now, page_state=dict(has_video=True, ready='complete', login_visible=False, login_confirmed=True, error=False))
        with self.fixture(sample) as f:
            workload = self.start(f)
            self.assertGreaterEqual(f.clock.now, 702)
            self.assertLess(f.clock.now, 703)
            self.assertEqual(len(f.navigations), 1)
            f.session._install_bridge.assert_not_called()
            f.session._inject.assert_not_called()
            f.session._click.assert_not_called()
            self.assertTrue(any(now >= 699 and waiting and '扫码' in text for now, text, waiting in f.messages))
            self.assertTrue(all(waiting for now, text, waiting in f.messages if 100 < now < 700))
            self.assertTrue(f.session.record['passed'])
            self.assertIs(workload['page'], f.session)
            self.assertEqual(workload['last']['position'], f.clock.now)
            self.assertFalse(f.status['waiting_for_user'])
            self.assertNotIn('start_epoch', f.status)

    def test_visible_login_takes_priority_over_a_preview_media_error(self):
        def sample(now):
            if now < 700:
                return dict(observation_available=False,
                    page_state=dict(has_video=False, login_visible=True, error=True))
            return playing(now)
        with self.fixture(sample) as f:
            self.start(f)
            self.assertGreaterEqual(f.clock.now, 702)
            self.assertLess(f.clock.now, 703)
            self.assertEqual(len(f.navigations), 1)
            f.session._inject.assert_not_called()
            f.session._click.assert_not_called()
            self.assertTrue(f.session.record['passed'])

    def test_lost_observation_resets_the_two_second_stability_window(self):
        def sample(now):
            if 101 <= now < 102:
                return dict(observation_available=False, page_state=dict(has_video=False, ready='interactive'))
            return playing(now)
        with self.fixture(sample) as f:
            workload = self.start(f)
            self.assertGreaterEqual(f.clock.now, 104)
            self.assertLess(f.clock.now, 105)
            self.assertEqual(workload['last']['position'], f.clock.now)
            self.assertTrue(any(101 <= now < 102 and waiting for now, _, waiting in f.messages))

    def test_paused_or_buffering_video_can_wait_ten_minutes_without_timeout(self):
        for changes in (dict(paused=True), dict(readyState=2)):
            with self.subTest(changes=changes):
                def sample(now):
                    return playing(now, **changes) if now < 700 else playing(now)
                with self.fixture(sample) as f:
                    self.start(f)
                    self.assertGreaterEqual(f.clock.now, 702)
                    self.assertEqual(len(f.navigations), 1)
                    self.assertTrue(f.session.record['passed'])

    def test_running_nonbuffering_video_that_stays_invalid_fails_after_forty_seconds(self):
        for changes in (dict(width=1280, height=720), dict(observed_fps=24),
                        dict(fullscreen=dict(active=False, contains_video=False, fills_viewport=False))):
            with self.subTest(changes=changes), self.fixture(lambda now:playing(now, **changes)) as f:
                with self.assertRaisesRegex(RuntimeError, '未达到测试要求.*尚未开始计时'):
                    self.start(f)
                self.assertGreaterEqual(f.clock.now-100, 40)
                self.assertLess(f.clock.now-100, 43)
                self.assertEqual(len(f.navigations), 1)
                self.assertNotIn('passed', f.session.record)
                f.session.close.assert_called_once()

    def test_a_valid_observation_resets_the_forty_second_invalid_period(self):
        def sample(now):
            # 先低清39秒、合格1秒，再低清；第二段必须重新获得完整40秒。
            return playing(now) if 139 <= now < 140 else playing(now, width=640, height=360)
        with self.fixture(sample) as f:
            with self.assertRaisesRegex(RuntimeError, '未达到测试要求.*尚未开始计时'):
                self.start(f)
            self.assertTrue(any(139 <= now < 140 and value['width'] == 1920 for now, value in f.seen))
            self.assertGreaterEqual(f.clock.now-140, 40)
            self.assertLess(f.clock.now-140, 44)
            self.assertNotIn('passed', f.session.record)
            self.assertNotIn('start_epoch', f.status)
            f.session.close.assert_called_once()

    def test_formal_stage_timer_starts_only_after_bridge_recovery_and_stable_video(self):
        def sample(now):
            return dict(observation_available=False, observation_error='遥测恢复中') if now < 112 else playing(now)
        with self.fixture(sample) as f, tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory); stage = root/'online_video'; csv_path = root/'hw.csv'
            csv_path.write_text('raw fixture', 'utf-8')
            f.cfg['tests']['online_video'].update(duration_s=0, wait_before_s=200)
            replacements = dict(apply_network_state=Mock(return_value=[]), launch_hwinfo=Mock(),
                start_hwinfo_logging=Mock(return_value=csv_path), minimize_owned_hwinfo=Mock(),
                stop_hwinfo_logging=Mock(), wait_seconds=Mock())
            for name, value in replacements.items():
                stack.enter_context(patch.object(app, name, value))
            stack.enter_context(patch.object(native, 'NativeEdgeSession', return_value=f.session))
            app.execute_stage(f.cfg, 'online_video', stage, f.stop, f.status, [], skip_prepare_wait=True)
            conditions = json.loads((stage/'conditions.json').read_text('utf-8'))
            self.assertGreaterEqual(conditions['start_epoch'], 1700000114)
            self.assertLess(conditions['start_epoch'], 1700000115)
            self.assertEqual(conditions['workload_status'], '完成计时')
            self.assertTrue(any(now < 112 for now, value in f.seen if not value['observation_available']))
            self.assertTrue(f.bridges)
            self.assertTrue(f.session.record['passed'])
            self.assertEqual(f.cfg['tests']['online_video']['wait_before_s'], 200)
            replacements['wait_seconds'].assert_not_called()
            replacements['stop_hwinfo_logging'].assert_called_once()
            f.session.close.assert_called_once()

    def test_start_failure_during_open_or_prepare_always_closes_before_reporting_failure(self):
        for failing_method in ('open', 'prepare'):
            with self.subTest(failing_method=failing_method):
                status = dict(waiting_for_user=True)
                session = types.SimpleNamespace(open=Mock(), prepare=Mock(), close=Mock(),
                                                save_record=Mock(), record={})
                getattr(session, failing_method).side_effect = RuntimeError('纯桩准备失败')
                with patch.object(native, 'NativeEdgeSession', return_value=session):
                    with self.assertRaisesRegex(RuntimeError, '纯桩准备失败'):
                        native.start_online_video(configuration(), status=status)
                session.close.assert_called_once()
                session.save_record.assert_called_once()
                self.assertIsInstance(session.save_record.call_args.kwargs['error'], RuntimeError)
                self.assertFalse(status['waiting_for_user'])
                self.assertNotIn('passed', session.record)
                if failing_method == 'open':
                    session.prepare.assert_not_called()

    def test_failed_start_and_failed_close_report_both_errors(self):
        session = types.SimpleNamespace(open=Mock(), prepare=Mock(side_effect=RuntimeError('页面失败')),
            close=Mock(side_effect=RuntimeError('窗口关闭失败')), save_record=Mock(), record={})
        status = dict(waiting_for_user=True)
        with patch.object(native, 'NativeEdgeSession', return_value=session):
            with self.assertRaisesRegex(app.WorkloadCleanupError, '页面失败.*窗口关闭失败'):
                native.start_online_video(configuration(), status=status)
        session.close.assert_called_once()
        self.assertFalse(status['waiting_for_user'])

    def test_running_two_x_video_is_reset_to_one_x_then_fresh_fullscreen_is_restored(self):
        state = dict(rate=2, fullscreen=True)
        actions = []
        def sample(now):
            return playing(now, playback_rate=state['rate'],
                fullscreen=dict(active=state['fullscreen'], contains_video=state['fullscreen'],
                                fills_viewport=state['fullscreen']))
        with self.fixture(sample) as f:
            def inject(code):
                actions.append((f.clock.now, code))
                if code == 'window.__jk808Native.play()':
                    state.update(rate=1, fullscreen=False)  # 改速地址操作后，模拟真实退出全屏。
                elif code != 'window.__jk808Native.controls()':
                    raise AssertionError('正常1080P视频不应额外改画质')
            def click(name):
                self.assertEqual(name, 'JK808视频全屏', '已经播放的视频不应切换播放/暂停')
                actions.append((f.clock.now, name))
                state['fullscreen'] = True
            f.session._inject.side_effect = inject
            f.session._click.side_effect = click
            workload = self.start(f)
            self.assertEqual(actions[0][1], 'window.__jk808Native.play()')
            restored_at = next(now for now, action in actions if action == 'JK808视频全屏')
            self.assertTrue(any(value['playback_rate'] == 1 and not value['fullscreen']['active']
                                and now < restored_at for now, value in f.seen))
            valid = [now for now, value in f.seen if now >= restored_at and value['playback_rate'] == 1
                     and value['fullscreen']['active']]
            self.assertGreaterEqual(valid[-1]-valid[0], 2)
            self.assertEqual(workload['last']['playback_rate'], 1)
            self.assertTrue(workload['last']['fullscreen']['active'])
            self.assertTrue(f.session.record['passed'])
            f.session.close.assert_not_called()

    def test_speed_action_cancellation_propagates_without_following_fullscreen_actions(self):
        for cancellation in ('injection', 'after_action'):
            with self.subTest(cancellation=cancellation), self.fixture(lambda now:playing(now, playback_rate=2)) as f:
                def inject(code):
                    self.assertEqual(code, 'window.__jk808Native.play()')
                    if cancellation == 'injection':
                        raise InterruptedError('测试人员取消改速')
                    f.stop.cancel_at = f.clock.now
                f.session._inject.side_effect = inject
                with self.assertRaisesRegex(InterruptedError, '取消'):
                    self.start(f)
                f.session._click.assert_not_called()
                f.session.close.assert_called_once()
                self.assertNotIn('last_speed_error', f.session.record)
                self.assertNotIn('passed', f.session.record)
                self.assertFalse(f.status['waiting_for_user'])

    def test_visible_login_overlay_prevents_speed_changes_until_login_finishes(self):
        state = dict(rate=2)
        actions = []
        def sample(now):
            return playing(now, playback_rate=state['rate'],
                page_state=dict(has_video=True, ready='complete', error=False, login_visible=now < 700, login_confirmed=now >= 700))
        with self.fixture(sample) as f:
            def inject(code):
                self.assertGreaterEqual(f.clock.now, 700, '扫码遮罩可见时禁止改速或操作播放器')
                self.assertEqual(code, 'window.__jk808Native.play()')
                actions.append(f.clock.now)
                state['rate'] = 1
            f.session._inject.side_effect = inject
            workload = self.start(f)
            self.assertTrue(actions)
            self.assertTrue(any(now >= 699 and waiting for now, _, waiting in f.messages))
            self.assertGreaterEqual(f.clock.now, 702)
            self.assertEqual(workload['last']['playback_rate'], 1)
            self.assertFalse(workload['last']['page_state']['login_visible'])
            f.session._click.assert_not_called()
            self.assertTrue(f.session.record['passed'])
            self.assertFalse(f.status['waiting_for_user'])


if __name__ == '__main__':
    unittest.main()
