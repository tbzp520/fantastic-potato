"""四个独立入口的分派、取消、数据归档及连跑进度隔离；不接真实设备。"""
from contextlib import ExitStack, nullcontext
from pathlib import Path
from unittest.mock import patch
import json
import shutil
import tempfile
import unittest

from test_program import program, ROOT, CONFIG
import test_program as baseline


class SingleMenuTests(unittest.TestCase):
    def test_all_four_menu_choices_run_only_selected_mode(self):
        for choice, mode in [('1','l12_ps4'),('2','l0_ps4'),('3','write_1m'),('4','read_1m')]:
            with self.subTest(mode=mode), ExitStack() as stack:
                answers=['3',choice]+(['Y'] if choice=='1' else [])
                stack.enter_context(patch('builtins.input',side_effect=answers))
                output=stack.enter_context(patch('builtins.print'))
                editor=stack.enter_context(patch.object(program.subprocess,'Popen'))
                stack.enter_context(patch.object(program,'load_configuration',return_value=CONFIG))
                single=stack.enter_context(patch.object(program,'run_single_test'))
                stack.enter_context(patch.object(program,'start_l12_then_pause',side_effect=AssertionError('不能创建连跑流程')))
                stack.enter_context(patch.object(program,'continue_l0_write_read',side_effect=AssertionError('不能连跑')))
                program.choose_and_run_test()
                single.assert_called_once_with(CONFIG,mode)
                self.assertEqual(editor.call_count,1 if choice=='1' and program.os.name=='nt' else 0)
                labels=' '.join(str(c.args) for c in output.call_args_list)
                for label in ('1. L1.2 / PS4','2. L0 / PS4','3. 1M 写测试','4. 1M 读测试'):
                    self.assertIn(label,labels)

    def test_invalid_selection_and_back_never_start_or_load_configuration(self):
        with patch('builtins.input',side_effect=['3','bad','0','0']),patch('builtins.print'), \
             patch.object(program,'load_configuration',side_effect=AssertionError), \
             patch.object(program,'execute_selected_test',side_effect=AssertionError), \
             patch.object(program.subprocess,'Popen',side_effect=AssertionError):
            program.choose_and_run_test()

    def test_l12_declined_confirmation_returns_without_running(self):
        with patch('builtins.input',side_effect=['3','1','N','0']),patch('builtins.print'), \
             patch.object(program.subprocess,'Popen'), \
             patch.object(program,'load_configuration',side_effect=AssertionError), \
             patch.object(program,'execute_selected_test',side_effect=AssertionError):
            program.choose_and_run_test()

    def test_incomplete_or_unsaved_result_is_not_reported_as_finished(self):
        for result in ({'completed':False,'results_saved':True},{'completed':True,'results_saved':False}):
            with self.subTest(result=result),patch.object(program,'execute_selected_test',return_value=result),patch('builtins.print') as output:
                with self.assertRaisesRegex(RuntimeError,'未完整结束或结果未保存'):
                    program.run_single_test(CONFIG,'l0_ps4')
                self.assertNotIn('单项测试已完成',' '.join(str(c.args) for c in output.call_args_list))

    def test_unknown_mode_cannot_reach_executor(self):
        with patch.object(program,'execute_selected_test',side_effect=AssertionError):
            with self.assertRaisesRegex(ValueError,'不支持'):
                program.run_single_test(CONFIG,'all')


class SingleExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.cfg=baseline.ConfigurationTests().configured_receiver()
        self.cfg['company_report']['output_dir']=str(self.root/'deliveries')
        self.cfg['ssh']['password']='test-only-password'
        self.cfg['sample_assignment']=dict(program.disk_assignment_fields(self.cfg),confirmed_beijing='2026-10-23T10:00:00+08:00')
        shutil.copy2(ROOT/'温度模版_原始.xlsx',self.root/'温度模版_原始.xlsx')
        shutil.copytree(ROOT/'人工复算代码',self.root/'人工复算代码')
        progress=self.root/'运行记录';progress.mkdir()
        self.progress=progress/'当前流程.json'
        self.saved_progress=b'{"id":"previous-sequence","phase":"sequence_paused","completed":{"l0_ps4":{}}}'
        self.progress.write_bytes(self.saved_progress)
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(program,'PROGRAM_FOLDER',self.root))
        self.stack.enter_context(patch.object(program,'lock_receiver_during_test',side_effect=lambda:nullcontext()))
        self.connection=self.stack.enter_context(patch.object(program,'LinuxConnection'))
        self.sync=self.stack.enter_context(patch.object(program,'synchronize_receiver_clock',return_value=None))
        self.stack.enter_context(patch.object(program,'check_linux_identity_before_test',return_value={'firmware':'DEMO_FW'}))
        self.stack.enter_context(patch.object(program,'save_workflow_progress',side_effect=AssertionError('不能修改连跑进度')))
        self.stack.enter_context(patch('builtins.print'))
        self.calls=[]
        self.stack.enter_context(patch.object(program,'trim_selected_nvme_disk',side_effect=lambda *a:self.calls.append('trim')))
        self.stack.enter_context(patch.object(program,'run_prewrite_one_gib',side_effect=lambda *a:self.calls.append('prewrite')))
        self.stack.enter_context(patch.object(program,'wait_before_starting_test',side_effect=lambda n,e:self.calls.append(('wait',n))))
        self.collect=self.stack.enter_context(patch.object(program,'collect_during_formal_test',side_effect=self.fake_collect))

    def fake_collect(self,configuration,mode,linux,smart,folder,event):
        self.calls.append(mode)
        (folder/'原始记录.txt').write_text('offline '+mode,'utf-8')
        return {'completed':True,'errors':[],'mode':mode,'configuration':program.configuration_without_password(configuration)}

    def test_single_modes_use_original_preparation_and_save_independent_results(self):
        cases=[('l12_ps4',[('wait',30),'l12_ps4']),('l0_ps4',[('wait',30),'l0_ps4']),
               ('write_1m',['trim',('wait',300),'write_1m']),('read_1m',['trim','prewrite',('wait',300),'read_1m'])]
        paths=[]
        for mode,expected in cases:
            with self.subTest(mode=mode):
                self.calls.clear()
                result=program.run_single_test(self.cfg,mode)
                self.assertEqual(self.calls,expected)
                folder=Path(result['result_folder']);paths.append(folder)
                self.assertIn(program.TEST_NAMES[mode],folder.name)
                saved=json.loads((folder/'手工复测/本次运行记录.json').read_text('utf-8'))
                self.assertEqual(saved['mode'],mode)
                self.assertTrue(saved['completed'] and saved['results_saved'])
                self.assertNotIn('test-only-password',json.dumps(saved))
                self.assertEqual(self.progress.read_bytes(),self.saved_progress)
        self.assertEqual(len(set(paths)),4)
        self.assertEqual(self.sync.call_count,4)

    def test_repeated_single_test_keeps_previous_raw_directory(self):
        first=program.run_single_test(self.cfg,'l0_ps4')
        second=program.run_single_test(self.cfg,'l0_ps4')
        self.assertNotEqual(first['result_folder'],second['result_folder'])
        for result in (first,second):
            self.assertTrue((Path(result['result_folder'])/'手工复测/原始记录.txt').is_file())
        self.assertEqual(self.progress.read_bytes(),self.saved_progress)

    def test_low_power_single_does_not_require_read_write_permission_or_settings(self):
        self.cfg['ssh']['allow_destructive']=False
        self.cfg['tests']['read_1m']['duration_s']=0
        self.cfg['tests']['write_1m']['fio_duration_s']=0
        for mode in ('l0_ps4','l12_ps4'):
            self.assertTrue(program.run_single_test(self.cfg,mode)['completed'])
        self.assertNotIn('trim',self.calls)

    def test_read_and_write_permission_is_checked_before_connecting_or_clock_sync(self):
        self.cfg['ssh']['allow_destructive']=False
        for mode in ('read_1m','write_1m'):
            with self.assertRaisesRegex(ValueError,'TRIM'):
                program.run_single_test(self.cfg,mode)
        self.connection.assert_not_called();self.sync.assert_not_called()
        self.collect.assert_not_called()

    def test_failure_or_cancel_preserves_original_sequence_and_diagnostics(self):
        for failure in (OSError('模拟采样失败'),KeyboardInterrupt()):
            with self.subTest(kind=type(failure).__name__):
                self.collect.side_effect=failure
                with self.assertRaises(type(failure)):
                    program.run_single_test(self.cfg,'l0_ps4')
                self.assertEqual(self.progress.read_bytes(),self.saved_progress)
        errors=list((self.root/'deliveries').rglob('操作未完成.txt'))
        self.assertEqual(len(errors),2)


if __name__=='__main__':
    unittest.main(verbosity=2)
