"""v2.2：记住密码、两段续测、独立归档。全部使用假设备，不运行真实 SSH/USB。"""
from test_program import program, CONFIG, EPOCH, ROOT, VirtualClock
import test_program as baseline
from pathlib import Path
from contextlib import nullcontext, ExitStack
from unittest.mock import patch, Mock
import copy
import json
import os
import tempfile
import threading
import unittest


class PasswordTests(unittest.TestCase):
    def test_remembered_plaintext_is_used_without_prompt_and_environment_is_restored(self):
        cfg=baseline.ConfigurationTests().configured_receiver()
        cfg['ssh']['password']='test-only-秘密!'
        name=cfg['ssh']['password_env']
        for old in (None,'old-environment-value'):
            with self.subTest(previous=old),patch.dict(os.environ,{},clear=True),patch.object(program.getpass,'getpass',side_effect=AssertionError('不应询问密码')):
                if old is not None:os.environ[name]=old
                with program.supply_linux_password(cfg):
                    self.assertEqual(os.environ[name],cfg['ssh']['password'])
                self.assertEqual(os.environ.get(name),old)

    def test_saved_password_reaches_all_ssh_connections(self):
        import paramiko
        cfg=baseline.ConfigurationTests().configured_receiver();cfg['ssh']['password']='fake-secret'
        with patch.object(paramiko,'SSHClient') as client,patch.object(program.getpass,'getpass',side_effect=AssertionError):
            with program.supply_linux_password(cfg):
                for _ in range(3):
                    connection=program.LinuxConnection(dict(cfg['ssh']));connection.connect();connection.close()
            self.assertEqual(client.return_value.connect.call_count,3)
            self.assertEqual(client.return_value.connect.call_args.kwargs['password'],'fake-secret')

    def test_a_persists_password_only_after_success_and_retains_it_on_next_deployment(self):
        cfg=baseline.ConfigurationTests().configured_receiver();cfg['deployment_complete']=True
        cfg['ssh']['password']='old-test-secret'
        for supplied,expected in [('new-test-secret','new-test-secret'),('','old-test-secret')]:
            with self.subTest(supplied=supplied),tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
                folder=Path(directory);configuration=copy.deepcopy(cfg)
                stack.enter_context(patch.object(program,'PROGRAM_FOLDER',folder))
                stack.enter_context(patch.object(program,'CONFIGURATION_FILE',folder/'config.json'))
                stack.enter_context(patch.object(program,'load_configuration',return_value=configuration))
                stack.enter_context(patch.object(program,'ask_text',side_effect=lambda label,current:current))
                stack.enter_context(patch.object(program,'lock_receiver_during_test',return_value=nullcontext()))
                stack.enter_context(patch.object(program.getpass,'getpass',return_value=supplied))
                connection=stack.enter_context(patch.object(program,'LinuxConnection')).return_value
                connection.execute.return_value=(0,'fio-3.33','')
                stack.enter_context(patch.object(program,'detect_and_fill_nvme_information'))
                stack.enter_context(patch.object(program,'confirm_disk_project_and_batch'))
                stack.enter_context(patch.object(program,'identify_and_verify_jk808'))
                stack.enter_context(patch('builtins.print'))
                program.first_deployment()
                saved=json.loads((folder/'config.json').read_text('utf-8'))
                self.assertEqual(saved['ssh']['password'],expected)
                self.assertEqual(saved['ssh']['host'],cfg['ssh']['host'])

    def test_change_of_host_clears_previous_password_before_prompt(self):
        cfg=baseline.ConfigurationTests().configured_receiver();cfg['deployment_complete']=True
        cfg['ssh']['password']='password-for-old-host'
        def new_prompt(message):
            self.assertEqual(cfg['ssh']['password'],'')
            raise RuntimeError('模拟操作员中止')
        with patch.object(program,'load_configuration',return_value=cfg),patch.object(program,'ask_text',side_effect=['192.0.2.2','root','22']),patch.object(program,'lock_receiver_during_test',return_value=nullcontext()),patch.object(program.getpass,'getpass',side_effect=new_prompt),patch.object(program,'save_configuration') as save:
            with self.assertRaisesRegex(RuntimeError,'模拟'):program.first_deployment()
            save.assert_not_called()

    def test_export_snapshot_has_no_password_but_active_configuration_still_has_it(self):
        cfg=baseline.ConfigurationTests().configured_receiver();cfg['ssh']['password']='not-for-report'
        cfg['_identity']={'temporary':True}
        clean=program.configuration_without_password(cfg)
        self.assertNotIn('not-for-report',json.dumps(clean));self.assertNotIn('_identity',clean)
        self.assertEqual(cfg['ssh']['password'],'not-for-report')


class TwoStageWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name)
        self.cfg=baseline.ConfigurationTests().configured_receiver()
        self.cfg['ssh']['password']='private-test-password'
        self.clock=VirtualClock();self.calls=[]
        self.stack=ExitStack();self.addCleanup(self.temporary.cleanup);self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(program,'PROGRAM_FOLDER',self.root))
        self.stack.enter_context(patch.object(program,'lock_receiver_during_test',side_effect=lambda:nullcontext()))
        self.stack.enter_context(patch.object(program,'validate_test_configuration'))
        self.stack.enter_context(patch.object(program.time,'time',side_effect=self.clock.time))
        self.stack.enter_context(patch('builtins.print'))
        self.execute=self.stack.enter_context(patch.object(program,'execute_selected_test',side_effect=self.fake_test))
        self.stack.enter_context(patch.object(program,'wait_before_starting_test',side_effect=self.fake_wait))
        # 真正的磁盘与关机等 shell 操作都不准在这些流程测试中出现。
        self.stack.enter_context(patch.object(program.subprocess,'run',side_effect=AssertionError('不应执行命令')))
        self.stack.enter_context(patch.object(program,'LinuxConnection',side_effect=AssertionError('不应连接设备')))

    def fake_wait(self,seconds,event):
        self.calls.append(('wait',seconds));self.clock.sleep(seconds)

    def fake_test(self,configuration,mode,receiver_locked=False):
        self.assertTrue(receiver_locked)
        self.calls.append(mode);self.clock.sleep(100)
        folder=self.root/'deliveries'/(mode+'_'+str(len(self.calls)))
        data=folder/'手工复测';data.mkdir(parents=True)
        result={'completed':True,'results_saved':True,'result_folder':str(folder),'errors':[]}
        program.save_json(data/'本次运行记录.json',result)
        return result

    def test_l12_stops_then_second_operation_runs_l0_write_read_with_separate_waits(self):
        program.start_l12_then_pause(self.cfg)
        self.assertEqual(self.calls,['l12_ps4'])
        self.assertEqual(program.load_workflow_progress()['phase'],'waiting_for_manual_bios')
        self.clock.sleep(1000)  # 人工关机、改 BIOS、开机所用时间。
        self.cfg['bios_profile']='anything-manually-entered'
        program.continue_l0_write_read(self.cfg)
        self.assertEqual(self.calls,['l12_ps4','l0_ps4',('wait',300),'write_1m',('wait',300),'read_1m'])
        saved=program.load_workflow_progress()
        self.assertEqual(saved['phase'],'complete')
        self.assertEqual(list(saved['completed']),['l12_ps4','l0_ps4','write_1m','read_1m'])
        self.assertNotIn('private-test-password',json.dumps(saved))
        previous_id=saved['id'];before=len(self.calls);program.continue_l0_write_read(self.cfg)
        self.assertEqual(self.calls[before:],['l0_ps4',('wait',300),'write_1m',('wait',300),'read_1m'])
        self.assertNotEqual(program.load_workflow_progress()['id'],previous_id)

    def test_failed_write_stops_chain_and_resume_skips_completed_l0(self):
        program.start_l12_then_pause(self.cfg)
        def failing(configuration,mode,receiver_locked=False):
            if mode=='write_1m':
                self.calls.append('write-failed');raise OSError('模拟写失败')
            return self.fake_test(configuration,mode,receiver_locked)
        self.execute.side_effect=failing
        with self.assertRaisesRegex(OSError,'模拟'):program.continue_l0_write_read(self.cfg)
        self.assertNotIn('read_1m',self.calls)
        self.assertEqual(program.load_workflow_progress()['phase'],'sequence_paused')
        self.clock.sleep(400);self.execute.side_effect=self.fake_test
        program.continue_l0_write_read(self.cfg)
        self.assertEqual(self.calls,['l12_ps4','l0_ps4',('wait',300),'write-failed','write_1m',('wait',300),'read_1m'])

    def test_incomplete_l12_does_not_block_independent_sequence(self):
        self.execute.return_value=None
        self.execute.side_effect=lambda *a,**kw:{'completed':False,'results_saved':True}
        with self.assertRaisesRegex(RuntimeError,'未完整'):program.start_l12_then_pause(self.cfg)
        self.assertEqual(program.load_workflow_progress()['phase'],'l12_incomplete')
        self.execute.side_effect=self.fake_test
        program.continue_l0_write_read(self.cfg)
        self.assertEqual(self.calls,['l0_ps4',('wait',300),'write_1m',('wait',300),'read_1m'])
        self.assertNotIn('l12_ps4',program.load_workflow_progress()['completed'])

    def test_keyboard_interrupt_between_tests_keeps_last_completed_stage(self):
        program.start_l12_then_pause(self.cfg)
        with patch.object(program,'wait_before_starting_test',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):program.continue_l0_write_read(self.cfg)
        self.assertEqual(list(program.load_workflow_progress()['completed']),['l12_ps4','l0_ps4'])
        self.clock.sleep(1000)
        program.continue_l0_write_read(self.cfg)
        self.assertEqual(self.calls.count('l0_ps4'),1)
        self.assertIn('read_1m',self.calls)

    def test_new_disk_or_batch_starts_separate_progress_but_password_change_resumes(self):
        program.start_l12_then_pause(self.cfg)
        self.cfg['ssh']['password']='changed-login-password'
        self.assertEqual(program.workflow_configuration_key(self.cfg),program.load_workflow_progress()['configuration_key'])
        original_id=program.load_workflow_progress()['id']
        for section,key,value in [('ssh','device','/dev/nvme9n1'),('company_report','batch','new-batch')]:
            altered=copy.deepcopy(self.cfg);altered[section][key]=value
            program.continue_l0_write_read(altered)
            saved=program.load_workflow_progress()
            self.assertNotEqual(saved['id'],original_id)
            self.assertNotIn('l12_ps4',saved['completed'])
            self.assertEqual(saved['configuration_key'],program.workflow_configuration_key(altered))
        self.assertTrue((self.root/'运行记录'/('流程_'+original_id+'.json')).exists())

    def test_missing_l12_result_does_not_block_independent_sequence(self):
        program.start_l12_then_pause(self.cfg)
        saved=program.load_workflow_progress()
        Path(saved['completed']['l12_ps4']['result_folder'],'手工复测','本次运行记录.json').unlink()
        program.continue_l0_write_read(self.cfg)
        self.assertEqual(self.calls,['l12_ps4','l0_ps4',('wait',300),'write_1m',('wait',300),'read_1m'])

    def test_saved_sample_loss_notices_do_not_stop_sequence(self):
        def missing(*args,**kwargs):
            result=self.fake_test(*args,**kwargs);result['errors']=['SMART 第60秒丢失'];return result
        self.execute.side_effect=missing
        program.start_l12_then_pause(self.cfg);program.continue_l0_write_read(self.cfg)
        saved=program.load_workflow_progress()
        self.assertEqual(saved['phase'],'complete')
        self.assertEqual(saved['completed']['read_1m']['sampling_notices'],['SMART 第60秒丢失'])

    def test_wait_counts_already_elapsed_time_and_never_exceeds_five_minutes(self):
        progress={'completed':{'l0_ps4':{'finished_epoch':EPOCH}}}
        self.clock.sleep(42.5)
        program.wait_between_completed_tests(progress,'l0_ps4',threading.Event())
        self.assertEqual(self.calls,[('wait',258)])
        self.clock.sleep(300)
        program.wait_between_completed_tests(progress,'l0_ps4',threading.Event())
        self.assertEqual(len(self.calls),1)


class ResultAndMenuTests(unittest.TestCase):
    def test_remote_fio_paths_remain_unique_after_raw_data_is_nested(self):
        cfg=baseline.ConfigurationTests().configured_receiver()
        remote=Mock();remote.execute.return_value=(0,'12345','')
        with tempfile.TemporaryDirectory() as directory:
            folders=[]
            for name,phase in [('20260918_Run_A','test'),('20260918_Run_B','test'),('20260918_Run_B','prewrite')]:
                data=Path(directory)/name/'手工复测';data.mkdir(parents=True,exist_ok=True)
                result=program.start_remote_fio(remote,cfg,data,'write',phase=phase)
                folders.append(result['remote_folder'])
                self.assertIn(name,result['remote_folder'])
                self.assertTrue((data/(phase+'_fio启动记录.json')).is_file())
            self.assertEqual(len(set(folders)),3)

    def test_b_continue_never_opens_json_or_asks_password_itself(self):
        with patch('builtins.input',side_effect=['2']) as answer,patch('builtins.print'),patch.object(program.subprocess,'Popen',side_effect=AssertionError('不能打开 JSON')),patch.object(program,'load_configuration',return_value=CONFIG),patch.object(program,'continue_l0_write_read') as run:
            program.choose_and_run_test()
        self.assertEqual(answer.call_count,1);run.assert_called_once_with(CONFIG)

    def test_run_files_are_inside_manual_folder_and_password_not_in_snapshots(self):
        cfg=baseline.ConfigurationTests().configured_receiver();cfg['ssh']['password']='result-must-not-contain-me'
        with tempfile.TemporaryDirectory() as directory:
            cfg['company_report']['output_dir']=directory
            cfg['sample_assignment']=dict(program.disk_assignment_fields(cfg),confirmed_beijing='2026-09-24T09:00:00+08:00')
            def run(configuration,linux,smart,folder,event):
                self.assertEqual(folder.name,'手工复测')
                (folder/'原始日志.txt').write_text('demo')
                (folder.parent/'测试报告.html').write_text('demo')
                (folder.parent/'图.png').write_bytes(b'demo')
                return {'completed':True,'errors':[],'configuration':program.configuration_without_password(configuration)}
            with patch.object(program,'lock_receiver_during_test',return_value=nullcontext()),patch.object(program,'synchronize_receiver_clock',return_value=None),patch.object(program,'LinuxConnection'),patch.object(program,'check_linux_identity_before_test',return_value={'firmware':'test'}),patch.object(program,'run_l12_ps4_test',side_effect=run),patch.object(program.getpass,'getpass',side_effect=AssertionError('已有密码')),patch('builtins.print'):
                result=program.execute_selected_test(cfg,'l12_ps4')
            folder=Path(result['result_folder'])
            self.assertEqual({p.name for p in folder.iterdir()},{'手工复测','测试报告.html','图.png'})
            for path in folder.rglob('*.json'):
                self.assertNotIn(cfg['ssh']['password'],path.read_text('utf-8'))

    def test_legacy_result_reorganization_preserves_all_data_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'raw.csv').write_text('original')
            (root/'图.png').write_bytes(b'png');(root/'测试报告.html').write_text('html')
            result,data=program.prepare_result_folders(root)
            self.assertEqual(result,root);self.assertEqual((data/'raw.csv').read_text(),'original')
            self.assertEqual({p.name for p in root.iterdir()},{'图.png','测试报告.html','手工复测'})
            program.prepare_result_folders(data)
            (root/'raw.csv').write_text('new')
            with self.assertRaisesRegex(ValueError,'同名'):program.prepare_result_folders(root)
            self.assertEqual((data/'raw.csv').read_text(),'original');self.assertEqual((root/'raw.csv').read_text(),'new')


if __name__=='__main__':unittest.main(verbosity=2)
