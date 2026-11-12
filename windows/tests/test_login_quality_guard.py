"""清晰度菜单每一步的登录门禁；UIA、遥测、窗口与网络均为测试桩。"""
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


def sample(*, confirmed=True, overlay=False, available=True):
    state = dict(has_video=True, ready='complete', login_visible=overlay)
    if confirmed is not MISSING:
        state['login_confirmed'] = confirmed
    return dict(observation_available=available, page_state=state,
                quality=dict(selected='80', label='1080P',
                             choices=[dict(value='80', label='1080P 高清')]))


class LoginQualityGuardTests(unittest.TestCase):
    @contextmanager
    def fixture(self, observation=None, *, on_menu=None, on_find_option=None):
        cfg = {'online_video': {'url': 'https://www.bilibili.com/video/BVoffline/'}}
        session = native.NativeEdgeSession(cfg)
        events = []
        option = types.SimpleNamespace(element_info=types.SimpleNamespace(
            name='1080P 高清', control_type='ListItem'), is_visible=Mock(return_value=True),
            click_input=Mock(side_effect=lambda: events.append('option')))

        def descendants():
            events.append('find_option')
            if on_find_option:
                on_find_option()
            return [option]

        window = types.SimpleNamespace(descendants=Mock(side_effect=descendants))

        def click(name):
            self.assertEqual(name, '清晰度')
            events.append('menu')
            if on_menu:
                on_menu()

        def observe():
            events.append('sample')
            return copy.deepcopy(observation() if observation else sample())

        session.sample = Mock(side_effect=observe)
        session._click = Mock(side_effect=click)
        session._window = Mock(return_value=window)
        session._restore_for_login = Mock(side_effect=lambda: events.append('restore'))
        forbidden = AssertionError('清晰度门禁测试禁止实际窗口、浏览器或网络')
        with ExitStack() as stack:
            stack.enter_context(patch.object(native, '_read_title', side_effect=forbidden))
            stack.enter_context(patch.object(native, 'inspect_native_edge', side_effect=forbidden))
            stack.enter_context(patch.object(native.subprocess, 'Popen', side_effect=forbidden))
            stack.enter_context(patch('http.server.ThreadingHTTPServer', side_effect=forbidden))
            yield types.SimpleNamespace(session=session, option=option, window=window, events=events)

    def test_unconfirmed_unknown_and_visible_login_never_click_menu_or_option(self):
        cases = [dict(confirmed=False), dict(confirmed=None), dict(confirmed=MISSING),
                 dict(confirmed=True, overlay=True)]
        for case in cases:
            with self.subTest(case=case), self.fixture(lambda: sample(**case)) as f:
                with self.assertRaisesRegex(RuntimeError, '登录'):
                    f.session.select_quality()
                f.session._click.assert_not_called()
                f.option.click_input.assert_not_called()
                f.session._restore_for_login.assert_called_once_with()

    def test_menu_click_that_opens_login_does_not_click_option_and_restores_window(self):
        state = dict(overlay=False)
        with self.fixture(lambda: sample(overlay=state['overlay']),
                          on_menu=lambda: state.update(overlay=True)) as f:
            with self.assertRaisesRegex(RuntimeError, '登录'):
                f.session.select_quality()
            f.session._click.assert_called_once_with('清晰度')
            f.option.click_input.assert_not_called()
            f.session._restore_for_login.assert_called_once_with()
            self.assertLess(f.events.index('menu'), f.events.index('restore'))

    def test_login_lost_after_finding_option_is_rechecked_before_option_click(self):
        state = dict(confirmed=True)
        with self.fixture(lambda: sample(confirmed=state['confirmed']),
                          on_find_option=lambda: state.update(confirmed=False)) as f:
            with self.assertRaisesRegex(RuntimeError, '登录'):
                f.session.select_quality()
            f.session._click.assert_called_once_with('清晰度')
            f.window.descendants.assert_called_once_with()
            f.option.click_input.assert_not_called()
            f.session._restore_for_login.assert_called_once_with()
            found = f.events.index('find_option')
            self.assertIn('sample', f.events[found+1:])

    def test_confirmed_login_and_valid_observation_click_menu_then_option(self):
        with self.fixture() as f:
            f.session.select_quality()
            f.session._click.assert_called_once_with('清晰度')
            f.option.click_input.assert_called_once_with()
            f.session._restore_for_login.assert_not_called()
            self.assertEqual([e for e in f.events if e in ('menu', 'option')], ['menu', 'option'])
            self.assertGreaterEqual(f.session.sample.call_count, 3,
                                    '菜单后及选项定位后都必须重新确认登录')

    def test_unavailable_observation_never_clicks_menu_or_option(self):
        with self.fixture(lambda: sample(available=False)) as f:
            with self.assertRaises(RuntimeError):
                f.session.select_quality()
            f.session._click.assert_not_called()
            f.option.click_input.assert_not_called()


if __name__ == '__main__':
    unittest.main()
