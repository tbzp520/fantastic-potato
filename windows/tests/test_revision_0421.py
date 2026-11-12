"""换机平台协议与接收端归属回归；不访问真实CIM、串口、网卡或浏览器。"""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import windows_temperature_test as app
import batch_report
from platform_identity import REPORT_PLATFORM_KEYS, sync_platform_report
from platform_fixtures import attach_platform, platform_raw, platform_snapshot
from test_revision_037 import configuration


DISK=dict(model='Unit Test SSD',serial='UNIT-SSD-001',bytes=1000204886016,drive='C:')


def with_disk(cfg):
    cfg['report'].update(capacity='1T',system_disk_model=DISK['model'],expected_serial=DISK['serial'])
    cfg['detected_disk']=copy.deepcopy(DISK)
    return cfg


def reply(cfg, snapshot=None):
    return dict(problems=[],sample={key:cfg['report'][key] for key in ('capacity','system_disk_model','expected_serial')},
        platform=copy.deepcopy(snapshot or cfg['detected_platform']),detected_disk=copy.deepcopy(DISK),
        host_tools={key:copy.deepcopy(cfg[key]) for key in app.HOST_TOOL_SECTIONS})


class PlatformProtocolTests(unittest.TestCase):
    def test_real_platform_reader_is_called_only_with_the_host_runner(self):
        cfg=with_disk(configuration());raw=platform_raw(cpu=['New Test CPU'],bios='UNIT.002')
        host=app.HostController(cfg)
        with patch.object(app,'powershell',side_effect=[json.dumps(raw),json.dumps(DISK)]) as runner, \
             patch.object(app,'save_setup_config'):
            host.refresh_sample()
        self.assertEqual(runner.call_count,2)
        self.assertIn('Win32_ComputerSystem',runner.call_args_list[0].args[0])
        self.assertIn('Get-Partition',runner.call_args_list[1].args[0])
        self.assertEqual(cfg['report']['cpu'],'New Test CPU')
        self.assertEqual(cfg['report']['bios'],'UNIT.002')
        self.assertEqual(cfg['report']['expected_serial'],DISK['serial'])
        self.assertEqual(cfg['detected_platform']['cpu'],'New Test CPU')

    def test_failed_platform_read_clears_every_old_auto_field(self):
        cfg=configuration();before={key:cfg['report'][key] for key in ('project','batch','test_version')}
        with patch.object(app,'powershell',side_effect=RuntimeError('CIM unavailable')):
            with self.assertRaisesRegex(RuntimeError,'平台读取失败'):
                app.refresh_test_platform(cfg)
        self.assertNotIn('detected_platform',cfg)
        self.assertTrue(all(cfg['report'][key]=='' for key in REPORT_PLATFORM_KEYS))
        self.assertEqual({key:cfg['report'][key] for key in before},before)

    def test_preflight_uses_host_platform_even_when_receiver_contains_another_computer(self):
        host_cfg=with_disk(configuration());incoming=attach_platform(configuration(),cpu=['Receiver Computer CPU'])
        host=app.HostController(host_cfg)
        with patch.object(host,'refresh_sample'),patch.object(app,'preflight_host',return_value=[]), \
             patch.object(app,'workload_window_problems',return_value=[]),patch.object(app,'network_snapshot',return_value=[]), \
             patch.object(app,'powershell',side_effect=AssertionError('preflight reader is already mocked')):
            result=host.handle('preflight',dict(config=incoming,modes=['idle']))
        self.assertEqual(result['platform'],host_cfg['detected_platform'])
        self.assertNotEqual(result['platform']['cpu'],incoming['report']['cpu'])
        self.assertEqual(set(result['sample']),{'capacity','system_disk_model','expected_serial'})

    def test_effective_config_overwrites_host_auto_fields_and_preserves_manual_metadata(self):
        host_cfg=with_disk(configuration());host=app.HostController(host_cfg)
        incoming=attach_platform(configuration(),cpu=['Receiver CPU'],model='RECEIVER-PC')
        incoming['report'].update(project='Manual Project',batch='Manual Batch',test_version='Manual Version')
        result=host.effective_config(incoming)
        for key in REPORT_PLATFORM_KEYS:self.assertEqual(result['report'][key],host_cfg['report'][key])
        self.assertEqual(result['detected_platform'],host_cfg['detected_platform'])
        for key in ('project','batch','test_version'):self.assertEqual(result['report'][key],incoming['report'][key])
        result['detected_platform']['cpu']='edited'
        self.assertNotEqual(host_cfg['detected_platform']['cpu'],'edited')
        with self.assertRaises(ValueError):host.effective_config(incoming,strict_platform=True)

    def test_start_rereads_platform_and_rejects_a_machine_changed_after_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            host_cfg=with_disk(configuration());host_cfg['host_workspace']=directory
            received=copy.deepcopy(host_cfg);host=app.HostController(host_cfg)
            changed=platform_raw(product_uuid='AA36F63B-D643-4FF9-800E-96A216BC2376')
            with patch.object(app,'powershell',return_value=json.dumps(changed)),patch.object(app.threading,'Thread') as thread:
                with self.assertRaisesRegex(ValueError,'原物理电脑'):
                    host.handle('start',dict(job_id='a'*32,config=received,modes=['idle']))
            thread.assert_not_called();self.assertFalse(list(Path(directory).iterdir()))

    def test_start_reply_and_idempotent_retry_keep_the_original_task_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            host_cfg=with_disk(configuration());host_cfg['host_workspace']=directory
            received=copy.deepcopy(host_cfg);host=app.HostController(host_cfg)
            with patch.object(app,'powershell',return_value=json.dumps(platform_raw())) as runner, \
                 patch.object(app,'preflight_host',return_value=[]),patch.object(app,'workload_window_problems',return_value=[]), \
                 patch.object(app,'recover_pending_network'),patch.object(app.threading,'Thread') as thread:
                result=host.handle('start',dict(job_id='a'*32,config=received,modes=['idle']))
                dispatched=thread.call_args.kwargs['args'][0]
                attach_platform(host.cfg,cpu=['A later preflight CPU'])
                retried=host.handle('start',dict(job_id='a'*32))
            self.assertEqual(runner.call_count,1)
            self.assertEqual(result['platform'],received['detected_platform'])
            self.assertEqual(retried['platform'],result['platform'])
            self.assertEqual(dispatched['detected_platform'],result['platform'])
            self.assertEqual(dispatched['report']['cpu'],received['report']['cpu'])

    def test_restart_reopens_the_saved_task_platform_without_starting_any_workload(self):
        with tempfile.TemporaryDirectory() as directory:
            old=with_disk(configuration());current=attach_platform(with_disk(configuration()),cpu=['Current Computer CPU'])
            current['host_workspace']=directory;folder=Path(directory)/('c'*32);folder.mkdir()
            app.save_json(folder/'task.json',dict(job_id='c'*32,station=current['station'],finished=True))
            app.save_json(folder/'config_snapshot.json',old)
            host=app.HostController(current)
            with patch.object(app,'powershell',side_effect=AssertionError('recovery must not query CIM')), \
                 patch.object(app.threading,'Thread') as thread:
                reopened=host.handle('start',dict(job_id='c'*32))
                retried=host.handle('start',dict(job_id='c'*32))
            thread.assert_not_called()
            self.assertEqual(reopened['platform'],old['detected_platform'])
            self.assertEqual(retried['platform'],old['detected_platform'])
            self.assertNotEqual(reopened['platform'],current['detected_platform'])
            self.assertEqual(reopened['sample']['expected_serial'],DISK['serial'])

    def test_corrupt_saved_config_still_allows_raw_recovery_without_borrowing_current_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            current=with_disk(configuration());current['host_workspace']=directory
            folder=Path(directory)/('d'*32);folder.mkdir()
            app.save_json(folder/'task.json',dict(job_id='d'*32,station=current['station'],finished=True))
            (folder/'config_snapshot.json').write_text('{truncated',encoding='utf-8')
            (folder/'hwinfo.csv').write_bytes(b'original raw bytes')
            host=app.HostController(current)
            with patch.object(app,'powershell',side_effect=AssertionError('recovery must not query CIM')), \
                 patch.object(app.threading,'Thread') as thread:
                reopened=host.handle('open_job',dict(job_id='d'*32))
                retried=host.handle('start',dict(job_id='d'*32))
                files=host.handle('files',dict(job_id='d'*32))
            self.addCleanup(host.transfer_cache.cleanup)
            thread.assert_not_called()
            self.assertIsNone(reopened['platform']);self.assertIsNone(retried['platform'])
            self.assertEqual(retried['sample'],dict(capacity='',system_disk_model='',expected_serial=''))
            self.assertIn('hwinfo.csv',{entry['name'] for entry in files})


class ReceiverPlatformTests(unittest.TestCase):
    def test_adoption_uses_only_host_snapshot_and_keeps_receiver_project_and_version(self):
        cfg=attach_platform(with_disk(configuration()),cpu=['Receiver CPU'])
        cfg['report'].update(project='Manual Project',batch='Manual Batch',test_version='Manual Version')
        before=copy.deepcopy(cfg);host=with_disk(configuration())
        with patch.object(app,'powershell',side_effect=AssertionError('receiver must never query its CIM')) as local, \
             patch.object(app,'refresh_test_platform',side_effect=AssertionError('receiver is not the tested computer')), \
             patch.object(app,'save_setup_config'):
            adopted=app.adopt_current_sample(cfg,reply(host))
        local.assert_not_called();self.assertEqual(cfg,before)
        self.assertEqual(adopted['detected_platform'],host['detected_platform'])
        for key in REPORT_PLATFORM_KEYS:self.assertEqual(adopted['report'][key],host['report'][key])
        for key in ('project','batch','test_version'):self.assertEqual(adopted['report'][key],before['report'][key])

    def test_missing_or_invalid_platform_reply_does_not_reuse_the_receivers_old_identity(self):
        cfg=with_disk(configuration());before=copy.deepcopy(cfg)
        for snapshot in (None,{},dict(schema=1,platform='forged')):
            with self.subTest(snapshot=snapshot),patch.object(app,'save_setup_config') as save:
                value=reply(cfg);value['platform']=snapshot
                with self.assertRaises(ValueError):app.adopt_current_sample(cfg,value)
                save.assert_not_called();self.assertEqual(cfg,before)

    def test_resume_checks_original_platform_before_adopting_the_new_host_reply(self):
        with tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
            root=Path(directory);cfg=with_disk(configuration())
            app.save_json(root/'手工复测/本次任务.json',dict(job_id='b'*32,config=cfg,modes=['idle']))
            other=attach_platform(copy.deepcopy(cfg),product_uuid='AA36F63B-D643-4FF9-800E-96A216BC2376')
            link=Mock();link.request.side_effect=[dict(protocol=2,version=app.VERSION,station=cfg['station'],busy=False),reply(other)]
            for name,value in dict(require_administrator=Mock(),SerialConnection=Mock(return_value=link),
                    powershell=Mock(side_effect=AssertionError('receiver local CIM forbidden'))).items():
                stack.enter_context(patch.object(app,name,value))
            adopt=stack.enter_context(patch.object(app,'adopt_current_sample'))
            probe=stack.enter_context(patch.object(app,'JK808Thermometer'))
            stack.enter_context(patch.object(app.ctypes.windll.kernel32,'SetThreadExecutionState',return_value=1))
            before=copy.deepcopy(cfg)
            with self.assertRaisesRegex(ValueError,'原物理电脑'):
                app.run_receiver(cfg,['idle'],resume_folder=root)
            adopt.assert_not_called();probe.assert_not_called();link.close.assert_called_once()
            self.assertEqual(cfg,before)

    def test_receiver_syncs_platform_before_batch_checks_and_task_recording(self):
        with tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
            root=Path(directory);host=with_disk(configuration())
            cfg=attach_platform(with_disk(configuration()),cpu=['Receiver CPU'],model='RECEIVER-PC')
            cfg['report'].update(project='Manual Project',batch='Manual Batch',test_version='Manual Version')
            cfg['thermometer'].update(protocol_verified=True,hid_path_hex='00')
            calls=[]
            class Link:
                job=None
                def request(self,command,data=None,**kwargs):
                    calls.append(command)
                    if command=='hello':return dict(protocol=2,version=app.VERSION,station=cfg['station'],busy=False)
                    if command=='preflight':return reply(host)
                    if command=='start':self.job=data['job_id'];return dict(job_id=self.job,platform=host['detected_platform'])
                    if command=='status':return dict(job_id=self.job,busy=False,status=dict(phase='完成',message='pure fixture completed'))
                    raise AssertionError(command)
                def close(self):pass
            class Thread:
                def __init__(self,target,args=(),**kwargs):self.target=target;self.args=args
                def start(self):self.target(*self.args)
                def join(self,**kwargs):pass
            instrument=Mock();instrument.read.return_value=({1:40,2:38,3:24},None,None)
            replacements=dict(ROOT=root,require_administrator=Mock(),SerialConnection=Mock(return_value=Link()),
                powershell=Mock(side_effect=AssertionError('receiver local CIM forbidden')),
                JK808Thermometer=Mock(return_value=instrument),save_setup_config=Mock(),synchronize_two_clocks=Mock(),
                collect_jk808=Mock(),receive_files=Mock(),finish_reports=Mock(return_value={}))
            for name,value in replacements.items():stack.enter_context(patch.object(app,name,value))
            stack.enter_context(patch.object(app.threading,'Thread',Thread))
            stack.enter_context(patch.object(app.ctypes.windll.kernel32,'SetThreadExecutionState',return_value=1))
            def check_batch(folder,report):
                self.assertEqual(report['platform_id'],host['report']['platform_id'])
                self.assertEqual(report['cpu'],host['report']['cpu'])
                self.assertFalse(list(root.rglob('本次任务.json')))
            stack.enter_context(patch.object(batch_report,'check_batch_target',side_effect=check_batch))
            output=app.run_receiver(cfg,['idle'])
            saved=json.loads((output/'手工复测/本次任务.json').read_text('utf-8'))['config']
            self.assertEqual(saved['detected_platform'],host['detected_platform'])
            for key in REPORT_PLATFORM_KEYS:self.assertEqual(saved['report'][key],host['report'][key])
            for key in ('project','batch','test_version'):self.assertEqual(saved['report'][key],cfg['report'][key])
            replacements['powershell'].assert_not_called()
            self.assertEqual(calls,['hello','preflight','start','status'])


class ModeAwarePreflightTests(unittest.TestCase):
    def usable(self, root):
        cfg=with_disk(configuration())
        for name in ('hw.exe','bit.exe','video.mp4'):(root/name).write_bytes(b'fixture only, never executed')
        cfg['hwinfo']['executable']=str(root/'hw.exe')
        cfg['local_video']['file']=str(root/'video.mp4')
        cfg['burnintest'].update(executable=str(root/'bit.exe'),adapter='bit9_script',
            configuration=str(app.ROOT/'工具配置/已确认_C盘.bitcfg'),
            configuration_sha256=app.CONFIRMED_BIT_CONFIG_SHA256,system_disk_file_test_verified=True)
        return cfg

    def test_selected_bit_version_is_checked_before_any_workload_and_idle_is_unaffected(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg=self.usable(Path(directory))
            with patch.object(app,'resolve_hwinfo_executable',return_value=Path(cfg['hwinfo']['executable'])), \
                 patch.object(app,'bit_program_version',return_value=[10,0,1000,0]) as version:
                problems=app.preflight_host(cfg,['bit'])
                self.assertTrue(any('9.0 build1003' in item for item in problems))
                version.assert_called_once()
                version.reset_mock()
                self.assertFalse(any('BIT版本' in item for item in app.preflight_host(cfg,['idle'])))
                version.assert_not_called()

    def test_local_video_system_disk_rule_is_only_checked_for_selected_local_video(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg=self.usable(Path(directory))
            with patch.object(app,'resolve_hwinfo_executable',return_value=Path(cfg['hwinfo']['executable'])), \
                 patch('portable_tools.on_system_drive',return_value=False) as location:
                problems=app.preflight_host(cfg,['local_video'])
                self.assertTrue(any('本地视频必须在被测系统盘' in item for item in problems))
                location.assert_called_once_with(cfg['local_video']['file'],'C:')
                location.reset_mock();app.preflight_host(cfg,['idle']);location.assert_not_called()

    def test_full_suite_reports_later_bit_and_local_video_problems_before_start(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg=self.usable(Path(directory))
            with patch.object(app,'resolve_hwinfo_executable',return_value=Path(cfg['hwinfo']['executable'])), \
                 patch('portable_tools.on_system_drive',return_value=False), \
                 patch.object(app,'bit_program_version',return_value=[11,0,0,0]), \
                 patch('native_edge.inspect_native_edge',return_value={'passed':True}):
                problems=app.preflight_host(cfg,app.ORDER)
            self.assertTrue(any('本地视频必须在被测系统盘' in item for item in problems))
            self.assertTrue(any('BIT版本不是已适配' in item for item in problems))


if __name__=='__main__':unittest.main()
