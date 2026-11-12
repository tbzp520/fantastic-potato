"""换盘直接正式测试：真实配置/USB处理/文件流程，设备、窗口和负载用桩。"""
import copy
from contextlib import ExitStack, nullcontext
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import windows_temperature_test as app
import 集中联调 as combined
import environment_setup as env
import runtime_check
from platform_fixtures import attach_platform, platform_snapshot, fixture_platform_refresh


def config():
    cfg=json.loads((ROOT/'config.example.json').read_text('utf-8-sig'))
    cfg['report'].update(project='2260',batch='10.20',test_version='人工版本',capacity='1T',system_disk_model='UNIT-TEST-SSD',expected_serial='UNIT-SSD-001')
    return attach_platform(cfg)


def disk(serial='NEW-SERIAL', size=1000204886016):
    return {'model':'XP2260','serial':serial,'bytes':size,'drive':'C:'}


class DirectFormalTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(app,'bit_program_version',return_value=[9,0,1003,0]))
        quiet=patch('sys.stdout',io.StringIO()); quiet.start(); self.addCleanup(quiet.stop)

    def test_each_capacity_and_same_capacity_replacement_refreshes_identity(self):
        cfg=config(); cfg['hwinfo']['logging_verification']={'sensor_group':'old disk'}
        for index,(size,capacity) in enumerate(((512110190592,'512G'),(1000204886016,'1T'),
                                                (2000398934016,'2T'),(2000398934016,'2T'))):
            with self.subTest(capacity=capacity,index=index), patch.object(app,'powershell',return_value=json.dumps(disk('NEW-'+str(index),size))):
                app.refresh_system_disk(cfg)
                self.assertEqual(cfg['report']['capacity'],capacity)
                self.assertEqual(cfg['report']['expected_serial'],'NEW-'+str(index))
                self.assertEqual(cfg['report']['project'],'2260'); self.assertEqual(cfg['report']['batch'],'10.20')
                self.assertEqual(cfg['report']['test_version'],'人工版本')
                self.assertNotIn('logging_verification',cfg['hwinfo'])
                self.assertFalse(cfg['hwinfo']['smart_refresh_verified'])
                self.assertIn('NEW-'+str(index),cfg['hwinfo']['expected_sensor_group'])

    def usable(self, directory):
        cfg=config(); root=Path(directory)
        hw=root/'HWiNFO32.exe'; hw.write_bytes(b'fake ui executable')
        bit=root/'bit.exe'; bit.write_bytes(b'fake bit executable')
        video=root/'video.mp4'; video.write_bytes(b'fake local video')
        cfg['hwinfo'].update(executable=str(hw),smart_refresh_verified=False,ui={**cfg['hwinfo']['ui'],'adapter':'hwinfo_834'})
        cfg['hwinfo'].pop('logging_verification',None)
        cfg['local_video']['file']=str(video)
        cfg['online_video']['adapter']='bilibili_1080'
        cfg['burnintest'].update(executable=str(bit),configuration=str(ROOT/'工具配置/已确认_C盘.bitcfg'),
            configuration_sha256=app.CONFIRMED_BIT_CONFIG_SHA256,system_disk_file_test_verified=True,adapter='bit9_script')
        cfg['burnintest'].pop('script_verification',None)
        return cfg

    def test_known_adapters_without_any_short_test_proof_pass_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg=self.usable(directory)
            with patch.object(app,'resolve_hwinfo_executable',return_value=Path(cfg['hwinfo']['executable'])):
                self.assertEqual(app.preflight_host(cfg,app.ORDER),[])
                cfg['hwinfo']['logging_verification']={'sensor_group':'old disk','executable_sha256':'old'}
                cfg['burnintest']['script_verification']={'passed':False,'executable_sha256':'old'}
                self.assertEqual(app.preflight_host(cfg,app.ORDER),[])

    def test_unknown_mapping_or_changed_bit_configuration_still_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg=self.usable(directory)
            changed=Path(directory)/'unapproved.bitcfg'; changed.write_bytes(b'changed')
            cfg['burnintest']['configuration']=str(changed)
            cfg['hwinfo']['mapping_verified']=False
            with patch.object(app,'resolve_hwinfo_executable',return_value=Path(cfg['hwinfo']['executable'])):
                problems=app.preflight_host(cfg,['bit'])
            self.assertTrue(any('列映射' in text for text in problems))
            self.assertTrue(any('SHA256' in text for text in problems))
            self.assertFalse(any('集中联调' in text for text in problems))

    def test_failed_scan_clears_old_disk_and_does_not_return_receiver_identity(self):
        cfg=config(); cfg['detected_disk']=disk('OLD'); cfg['hwinfo']['system_disk_binding']={'serial':'OLD'}
        controller=app.HostController(cfg)
        with patch.object(app,'powershell',side_effect=RuntimeError('disk query failed')), patch.object(app,'save_setup_config') as save:
            self.enterContext(patch.object(app,'refresh_test_platform',side_effect=fixture_platform_refresh))
            with self.assertRaisesRegex(RuntimeError,'disk query failed'):
                controller.handle('preflight',{'config':config(),'modes':['idle']})
        self.assertNotIn('detected_disk',controller.cfg)
        self.assertNotIn('system_disk_binding',controller.cfg['hwinfo'])
        self.assertEqual(controller.effective_config(config())['report']['expected_serial'],'')
        self.assertEqual(controller.effective_config(config())['report']['capacity'],'')
        save.assert_called_once()

    def test_busy_host_never_rescans_disk(self):
        controller=app.HostController(config()); controller.worker=Mock(is_alive=Mock(return_value=True))
        with patch.object(controller,'refresh_sample') as scan:
            with self.assertRaisesRegex(RuntimeError,'仍在执行任务'): controller.handle('preflight',{})
        scan.assert_not_called()

    def test_host_restart_with_another_disk_cannot_mix_receiver_sample_into_results(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg=config();cfg['host_workspace']=directory;received=copy.deepcopy(cfg)
            cfg['report']['expected_serial']='REPLACED-AFTER-PREFLIGHT'
            controller=app.HostController(cfg)
            with patch.object(app.threading,'Thread') as worker:
                with self.assertRaisesRegex(RuntimeError,'重新选择B'):
                    controller.handle('start',{'job_id':'a'*32,'config':received,'modes':['idle']})
                worker.assert_not_called()
            self.assertFalse(list(Path(directory).iterdir()))

    def test_non_c_system_disk_cannot_use_confirmed_c_drive_bit_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg=self.usable(directory);cfg['detected_disk']={**disk(),'drive':'D:'}
            with patch.object(app,'resolve_hwinfo_executable',return_value=Path(cfg['hwinfo']['executable'])):
                errors=app.preflight_host(cfg,['bit'])
            self.assertTrue(any('当前系统盘不是C盘' in text for text in errors))

    def test_eui_resolves_smart_serial_during_formal_preparation_without_short_test(self):
        eui='0000_0000_0000_0000_1111_2222_3333_4444.'
        cfg=config(); controller=app.HostController(cfg); hardware=disk(eui)
        group='S.M.A.R.T.: XP2260 (TRUE-SERIAL) [C:]'
        desktop=Mock(); desktop.window.return_value.child_window.return_value.descendants.return_value=[Mock(window_text=Mock(return_value=group))]
        with tempfile.TemporaryDirectory() as directory, patch.object(app,'ROOT',Path(directory)), \
             patch.object(app,'powershell',return_value=json.dumps(hardware)), patch.object(app,'launch_hwinfo') as launch, \
             patch.object(app,'refresh_test_platform',side_effect=fixture_platform_refresh), \
             patch.object(app,'hwinfo_sensor_window',return_value=Mock()), patch('pywinauto.Desktop',return_value=desktop), \
             patch.object(app,'save_setup_config'), patch.object(combined,'run_host_checks') as short:
            controller.refresh_sample()
        self.assertEqual(controller.cfg['report']['expected_serial'],'TRUE-SERIAL')
        self.assertEqual(controller.cfg['hwinfo']['expected_sensor_group'],group)
        self.assertEqual(controller.cfg['detected_disk']['serial'],eui)
        launch.assert_called_once(); short.assert_not_called()

    def test_receiver_refuses_incomplete_disk_reply_instead_of_using_previous_disk(self):
        for sample in ({},{'capacity':'1T','system_disk_model':'new','expected_serial':''}):
            with self.subTest(sample=sample), patch.object(app,'save_setup_config') as save:
                with self.assertRaisesRegex(RuntimeError,'未返回完整'):
                    app.adopt_current_sample(config(),{'sample':sample,'host_tools':{}})
                save.assert_not_called()

    def test_receiver_saves_new_disk_without_overwriting_manual_project_or_version(self):
        cfg=config(); cfg['detected_disk']=disk('OLD')
        sample={'capacity':'512G','system_disk_model':'NEW-MODEL','expected_serial':'NEW-SERIAL',
                'project':'wrong remote project','batch':'wrong','test_version':'wrong'}
        with patch.object(app,'save_setup_config') as save:
            updated=app.adopt_current_sample(cfg,{'sample':sample,'host_tools':{},'detected_disk':disk(),
                'platform':platform_snapshot()})
        self.assertEqual(updated['report']['capacity'],'512G')
        self.assertEqual(updated['report']['expected_serial'],'NEW-SERIAL')
        for name in ('project','batch','test_version'): self.assertEqual(updated['report'][name],cfg['report'][name])
        self.assertNotEqual(cfg['report']['expected_serial'],'NEW-SERIAL')
        self.assertEqual(save.call_args.args[0],updated)

    def test_names_prompt_only_project_batch_with_existing_values_retained(self):
        cfg=config()
        with patch('builtins.input',side_effect=['','新批次']) as question, patch.object(app,'save_setup_config'):
            updated=combined.enter_project_batch(cfg)
        self.assertEqual(updated['report']['project'],'2260'); self.assertEqual(updated['report']['batch'],'新批次')
        self.assertEqual(question.call_count,2)
        self.assertEqual(updated['report']['test_version'],'人工版本')

    def test_formal_menu_does_not_open_config_or_run_commissioning(self):
        cfg=config(); cfg['serial']['receiver_port']='COM8'
        with patch('启动校验.verify_release'), patch.object(sys,'argv',['集中联调.py','2']), \
             patch.object(env,'restart_as_administrator',return_value=False), patch.object(runtime_check,'require_runtime'), \
             patch.object(env,'ensure_ftdi_driver',return_value={'ports':['COM8']}), \
             patch.object(app,'require_administrator'), patch.object(app,'single_instance',return_value=nullcontext()), \
             patch.object(app,'load_config',return_value=cfg), patch.object(app,'save_setup_config'), \
             patch.object(combined,'prepare_configuration',side_effect=lambda cfg,role:(copy.deepcopy(cfg),[])), \
             patch('builtins.input',side_effect=['B','新项目','新批次','1','0']), \
             patch.object(combined.os,'startfile') as editor, patch.object(combined,'run_receiver_checks') as short, \
             patch.object(app,'run_receiver') as formal:
            combined.main()
        editor.assert_not_called();short.assert_not_called();formal.assert_called_once()
        self.assertEqual(formal.call_args.args[0]['report']['project'],'新项目')
        self.assertEqual(formal.call_args.args[0]['report']['batch'],'新批次')
        self.assertEqual(formal.call_args.args[1],app.ORDER)

    def test_real_formal_handshake_uses_new_capacity_without_commissioning_or_delay(self):
        for size,capacity in ((512110190592,'512G'),(1000204886016,'1T'),(2000398934016,'2T')):
            with self.subTest(capacity=capacity), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root=Path(directory);cfg=self.usable(directory)
                # 临时ROOT不包含浏览器；驱动完整性由独立用例验证。
                stack.enter_context(patch('runtime_integrity.require_browser_driver',return_value={'passed':True}))
                cfg['host_workspace']=str(root/'host_jobs')
                cfg['thermometer'].update(protocol_verified=True,hid_path_hex='00')
                expected=copy.deepcopy(cfg['tests']);controller=app.HostController(copy.deepcopy(cfg))
                calls=[];workers=[]
                class Thread:
                    def __init__(self,target,args=(),**kwargs):self.target=target;self.args=args
                    def start(self):
                        if self.target==controller.run_job:
                            workers.append(self.args)
                            controller.status={'phase':'完成','message':'simulated formal completion'}
                    def is_alive(self):return False
                    def join(self,*args,**kwargs):pass
                class Link:
                    def request(self,command,data=None,**kwargs):
                        calls.append(command);return controller.handle(command,data or {})
                    def close(self):pass
                instrument=Mock();instrument.read.return_value=({1:40,2:38,3:24},None,None)
                replacements={'ROOT':root,'CONFIG':root/'config.json','require_administrator':Mock(),
                    'SerialConnection':Mock(return_value=Link()),'JK808Thermometer':Mock(return_value=instrument),
                    'synchronize_two_clocks':Mock(),'receive_files':Mock(),'finish_reports':Mock(return_value={}),
                    'powershell':Mock(return_value=json.dumps(disk('CURRENT-'+capacity,size))),
                    'refresh_test_platform':Mock(side_effect=fixture_platform_refresh),
                    'resolve_hwinfo_executable':Mock(return_value=Path(cfg['hwinfo']['executable'])),
                    'workload_window_problems':Mock(return_value=[]),'network_snapshot':Mock(return_value=[]),
                    'recover_pending_network':Mock()}
                for name,value in replacements.items():stack.enter_context(patch.object(app,name,value))
                stack.enter_context(patch.object(app.threading,'Thread',Thread))
                stack.enter_context(patch.object(app.ctypes.windll.kernel32,'SetThreadExecutionState',return_value=1))
                short=stack.enter_context(patch.object(combined,'run_host_checks'))
                output=app.run_receiver(cfg,app.ORDER)
                self.assertTrue(output.name.startswith(capacity+'_'))
                saved=json.loads((root/'config.json').read_text('utf-8'))
                task=json.loads((output/'手工复测/本次任务.json').read_text('utf-8'))
                self.assertEqual(saved['report']['capacity'],capacity)
                self.assertEqual(task['config']['report']['expected_serial'],'CURRENT-'+capacity)
                self.assertEqual(task['config']['report']['project'],'2260')
                self.assertEqual(workers[0][0]['tests'],expected)
                self.assertFalse(workers[0][0].get('_commissioning'))
                self.assertEqual(workers[0][0]['report']['capacity'],capacity)
                self.assertNotIn('commissioning-info',calls);short.assert_not_called()
                self.assertLess(calls.index('preflight'),calls.index('start'))


if __name__=='__main__':unittest.main()
