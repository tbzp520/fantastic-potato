"""新执行顺序：菜单单项不串位，旧任务续测不重复已完成项。"""
from contextlib import nullcontext
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import windows_temperature_test as app
import 集中联调 as menu
import environment_setup as env
import runtime_check
import resume_flow as flow
from test_revision_0411 import config,fixture_task,LEGACY_ORDER


class OrderTests(unittest.TestCase):
    def test_full_and_single_menu_choices_match_displayed_items(self):
        expected=('online_video','idle','local_video','bit')
        self.assertEqual(app.ORDER,expected)
        for choice,modes in [('1',expected),*[(str(i+2),(m,)) for i,m in enumerate(expected)]]:
            with self.subTest(choice=choice):
                cfg=config();cfg['serial']['receiver_port']='COM8';output=io.StringIO()
                with patch('启动校验.verify_release'),patch.object(sys,'argv',['集中联调.py','2']), \
                     patch.object(env,'restart_as_administrator',return_value=False),patch.object(runtime_check,'require_runtime'), \
                     patch.object(env,'ensure_ftdi_driver',return_value={'ports':['COM8']}), \
                     patch.object(app,'require_administrator'),patch.object(app,'single_instance',return_value=nullcontext()), \
                     patch.object(app,'load_config',return_value=cfg),patch.object(app,'save_setup_config'), \
                     patch.object(menu,'prepare_configuration',return_value=(cfg,[])), \
                     patch('builtins.input',side_effect=['B','','',choice,'0']),patch('sys.stdout',output), \
                     patch.object(app,'run_receiver') as run:
                    menu.main()
                run.assert_called_once_with(cfg,modes)
                self.assertIn('在线视频 → 待机 → 本地视频 → BIT',output.getvalue())
                self.assertIn('2. 在线视频\n3. 待机\n4. 本地视频\n5. BIT',output.getvalue())

    def test_all_failed_legacy_task_resumes_online_first(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);fixture_task(root,LEGACY_ORDER,[])
            original,_,_,pending=flow.plan(root)
            self.assertEqual(tuple(original['modes']),LEGACY_ORDER)
            self.assertEqual(pending,['online_video','idle','local_video','bit'])

    def test_old_completed_idle_is_preserved_and_remaining_modes_reordered(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);fixture_task(root,LEGACY_ORDER,['idle'])
            _,_,sources,pending=flow.plan(root)
            self.assertEqual(pending,['online_video','local_video','bit'])
            self.assertEqual(sources['idle'],root)

    def test_new_task_resumes_idle_after_online_finished(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);fixture_task(root,app.ORDER,['online_video'])
            self.assertEqual(flow.plan(root)[-1],['idle','local_video','bit'])

    def test_single_stage_task_never_expands_to_other_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);fixture_task(root,['local_video'],[])
            self.assertEqual(flow.plan(root)[-1],['local_video'])


if __name__=='__main__':unittest.main()
