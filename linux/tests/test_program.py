"""离线测试：不连接真实 Linux/USB，不执行 TRIM，不修改系统时间。"""
from pathlib import Path
import ast
import base64
import copy
import csv
from contextlib import nullcontext
import importlib.util
import json
import math
import os
import tempfile
import unittest
from unittest.mock import patch, Mock
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('temperature_program',ROOT/'linux_temperature_test.py')
program = importlib.util.module_from_spec(spec)
spec.loader.exec_module(program)
CONFIG = json.loads((ROOT/'config.json').read_text('utf-8'))
EPOCH = 1790000000


class VirtualClock:
    def __init__(self): self.second = 0
    def monotonic(self): return self.second
    def time(self): return EPOCH+self.second
    def sleep(self,seconds): self.second += seconds


class VirtualEvent:
    def __init__(self, clock): self.clock,self.stopped = clock,False
    def wait(self,seconds):
        self.clock.second += seconds
        return self.stopped
    def set(self): self.stopped = True
    def is_set(self): return self.stopped


class FlowTests(unittest.TestCase):
    def test_four_sequences_and_no_trim_for_low_power(self):
        cases = [('run_1m_read_test',['trim','prewrite','wait300','collect_read_1m']),
                 ('run_1m_write_test',['trim','wait300','collect_write_1m']),
                 ('run_l0_ps4_test',['wait30','collect_l0_ps4']),
                 ('run_l12_ps4_test',['wait30','collect_l12_ps4'])]
        for name,expected in cases:
            with self.subTest(name=name):
                calls = []
                with patch.object(program,'trim_selected_nvme_disk',side_effect=lambda *a:calls.append('trim')), \
                     patch.object(program,'run_prewrite_one_gib',side_effect=lambda *a:calls.append('prewrite')), \
                     patch.object(program,'wait_before_starting_test',side_effect=lambda n,e:calls.append('wait'+str(n))), \
                     patch.object(program,'collect_during_formal_test',side_effect=lambda c,m,*a:calls.append('collect_'+m)):
                    getattr(program,name)(CONFIG,None,None,None,None)
                self.assertEqual(calls,expected)

    def test_wait_has_no_sampling_or_linux_calls(self):
        clock = VirtualClock()
        with patch.object(program,'time',clock),patch.object(program,'JK808Thermometer',side_effect=AssertionError), \
             patch.object(program,'LinuxConnection',side_effect=AssertionError),patch('builtins.print'):
            program.wait_before_starting_test(300,VirtualEvent(clock))
        self.assertAlmostEqual(clock.second,300)

    def test_cancelled_wait_does_not_start_formal_test(self):
        event = VirtualEvent(VirtualClock());event.set()
        with patch.object(program,'collect_during_formal_test') as collect:
            with self.assertRaises(InterruptedError):
                program.run_l0_ps4_test(CONFIG,None,None,None,event)
            collect.assert_not_called()

    def test_read_command_is_explicit_and_prewrite_is_one_gib(self):
        cfg = copy.deepcopy(CONFIG);cfg['ssh']['device']='/dev/nvme0n1'
        prewrite = program.build_fio_command(cfg,'write','/tmp/run',None)
        self.assertIn('--size=1G',prewrite)
        self.assertNotIn('--time_based',prewrite)
        actual = program.build_fio_command(cfg,'read','/tmp/run',780)
        self.assertIn('--rw=read',actual)
        self.assertIn('--runtime=780',actual)
        self.assertIn('--log_avg_msec=1000',actual)

    def test_trim_success_displays_completion_and_saves_actual_response(self):
        cfg=copy.deepcopy(CONFIG)
        cfg['ssh'].update(device='/dev/nvme0n1',allow_destructive=True)
        cfg['company_report']['expected_serial']='DEMO_SN'
        remote=Mock();remote.execute.side_effect=[(0,'{"nsze":1000}',''),(0,'DSM success','')]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(program,'verify_test_disk_is_unmounted'), \
             patch.object(program,'read_disk_identity',return_value={'serial':'DEMO_SN'}), \
             patch('builtins.print') as display:
            program.trim_selected_nvme_disk(cfg,remote,Path(directory))
            record=json.loads((Path(directory)/'TRIM执行记录.json').read_text('utf-8'))
        self.assertEqual(record['command'],'nvme dsm /dev/nvme0n1 -s 0 -b 1000 -d')
        self.assertEqual(record['exit_code'],0);self.assertEqual(record['stdout'],'DSM success')
        self.assertIn('开始 TRIM',display.call_args_list[0].args[0])
        self.assertIn('TRIM 完成',display.call_args_list[1].args[0])
        self.assertIn('TRIM执行记录.json',display.call_args_list[2].args[0])

    def test_failed_trim_cannot_reach_prewrite_wait_or_formal_sampling(self):
        cfg=copy.deepcopy(CONFIG)
        cfg['ssh'].update(device='/dev/nvme0n1',allow_destructive=True)
        cfg['company_report']['expected_serial']='DEMO_SN'
        for flow in ('run_1m_read_test','run_1m_write_test'):
            with self.subTest(flow=flow):
                remote=Mock();remote.execute.side_effect=[(0,'{"nsze":1000}',''),RuntimeError('DSM failed')]
                with tempfile.TemporaryDirectory() as directory, \
                     patch.object(program,'verify_test_disk_is_unmounted'), \
                     patch.object(program,'read_disk_identity',return_value={'serial':'DEMO_SN'}), \
                     patch.object(program,'run_prewrite_one_gib') as prewrite, \
                     patch.object(program,'wait_before_starting_test') as wait, \
                     patch.object(program,'collect_during_formal_test') as collect,patch('builtins.print') as display:
                    with self.assertRaisesRegex(RuntimeError,'DSM failed'):
                        getattr(program,flow)(cfg,remote,None,Path(directory),None)
                    self.assertFalse((Path(directory)/'TRIM执行记录.json').exists())
                    self.assertFalse(any('TRIM 完成' in c.args[0] for c in display.call_args_list))
                    prewrite.assert_not_called();wait.assert_not_called();collect.assert_not_called()

    def test_flat_program_has_no_import_of_old_internal_framework(self):
        source = (ROOT/'linux_temperature_test.py').read_text('utf-8')
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom):
                self.assertEqual(node.level,0)
                self.assertNotIn('jk500_auto',node.module or '')
        self.assertNotIn('exec(',source)


class SamplingTests(unittest.TestCase):
    def test_formal_run_continues_after_one_surface_failure(self):
        cfg=copy.deepcopy(CONFIG);cfg['tests']['l0_ps4']['duration_s']=2
        class Instrument:
            def __init__(self,*args):self.calls=0
            def close(self):pass
            def read(self):
                self.calls+=1
                if self.calls==2:raise TimeoutError('模拟第二秒断线')
                return {1:30.,2:31.,3:25.},{1:'ok',2:'ok',3:'ok'},{1:300,2:310,3:250}
        smart=Mock();smart.execute.return_value=(0,'{"temperature":333,"temperature_sensor_1":335,"temperature_sensor_2":323}','')
        remote=Mock()
        with tempfile.TemporaryDirectory() as directory,patch.object(program,'JK808Thermometer',Instrument),patch.object(program,'generate_all_test_results') as generate,patch('builtins.print'):
            state=program.collect_during_formal_test(cfg,'l0_ps4',remote,smart,Path(directory),program.threading.Event())
        self.assertTrue(state['completed']);self.assertFalse(state['errors'])
        self.assertEqual(len(generate.call_args.args[3]),2)
        self.assertIn('断线',generate.call_args.args[3][1]['Error'])
        remote.execute.assert_not_called()
        self.assertEqual(smart.execute.call_count,1)

    def test_surface_disconnect_and_partial_sensor_resume_next_second(self):
        clock = VirtualClock();event=VirtualEvent(clock)
        rows, errors,called = [],[],[]
        class Instrument:
            def __init__(self,*args): pass
            def close(self): pass
            def read(self):
                n=round(clock.second);called.append(n)
                if n in (0,2,3): raise TimeoutError('模拟断线')
                return {1:30.,2:None if n==4 else 31.,3:25.}, {1:'ok',2:'invalid_code' if n==4 else 'ok',3:'ok'}, {1:300,2:32767 if n==4 else 310,3:250}
        with tempfile.TemporaryDirectory() as directory,patch.object(program,'time',clock),patch.object(program,'JK808Thermometer',Instrument):
            program.collect_jk808_every_second(CONFIG,EPOCH,0,6,event,Path(directory),rows,errors)
            with (Path(directory)/'JK808逐秒原始数据.csv').open(encoding='utf-8-sig') as stream:
                self.assertEqual(len(list(csv.DictReader(stream))),6)
        self.assertEqual(called,list(range(6)))
        self.assertFalse(event.is_set());self.assertFalse(errors)
        missing=program.find_missing_measurements(range(6),rows,program.METRICS[:3],'JK808',EPOCH)
        self.assertEqual(len(missing),10)
        self.assertEqual(rows[5]['CPU_Surface'],30.)
        self.assertEqual(rows[4]['NAND_Surface_Error'],'invalid_code')

    def test_smart_keeps_both_cadences_after_missing_point(self):
        for interval,duration in [(60,181),(1500,3001)]:
            with self.subTest(interval=interval):
                clock=VirtualClock();event=VirtualEvent(clock);rows=[];errors=[];calls=[]
                class Connection:
                    def close(self): pass
                    def execute(self,*args,**kwargs):
                        calls.append(clock.second)
                        if round(clock.second)==interval: raise TimeoutError('模拟丢失')
                        return 0,json.dumps({'temperature':333,'temperature_sensor_1':335,'temperature_sensor_2':323}),''
                with tempfile.TemporaryDirectory() as directory,patch.object(program,'time',clock):
                    program.collect_smart_at_scheduled_times(CONFIG,Connection(),EPOCH,0,duration,interval,event,Path(directory),rows,errors)
                self.assertEqual(calls,list(range(0,duration,interval)))
                self.assertEqual(rows[-1]['Composite'],60)
                self.assertFalse(errors);self.assertFalse(event.is_set())
                missing=program.find_missing_measurements(range(0,duration,interval),rows,program.METRICS[3:6],'SMART',EPOCH)
                self.assertEqual({r['相对测试秒'] for r in missing},{interval})
                self.assertEqual(len(missing),3)

    def test_late_smart_response_is_not_backdated_as_good(self):
        clock=VirtualClock();rows=[];errors=[];event=VirtualEvent(clock)
        class Connection:
            def close(self): pass
            def execute(self,*args,**kwargs):
                clock.second+=1.1
                return 0,'{"temperature":333}', ''
        with tempfile.TemporaryDirectory() as directory,patch.object(program,'time',clock):
            program.collect_smart_at_scheduled_times(CONFIG,Connection(),EPOCH,0,1,60,event,Path(directory),rows,errors)
            text=(Path(directory)/'SMART完整原始响应.jsonl').read_text('utf-8')
        self.assertNotIn('Composite',rows[0])
        self.assertIn('temperature',text)
        self.assertIn('晚于',rows[0]['Error'])

    def test_partial_smart_fields_retain_good_metrics(self):
        values,errors=program.convert_nvme_smart_temperatures({'temperature':333,'temperature_sensor_1':None,'temperature_sensor_2':323},CONFIG['ssh'])
        self.assertEqual(values,{'Composite':60,'CPU':None,'NAND':50})
        self.assertIn('CPU_Error',errors)

    def test_missing_first_last_and_duplicate_are_reported(self):
        rows=[{'Second':1,'Composite':0},{'Second':2,'Composite':30},{'Second':2,'Composite':31}]
        missing=program.find_missing_measurements(range(4),rows,['Composite'],'SMART',EPOCH)
        self.assertEqual([r['相对测试秒'] for r in missing],[0,2,3])
        self.assertIn('多条',missing[1]['原因'])

    def test_fio_epoch_interval_mapping_and_invalid_row(self):
        text=f'{(EPOCH+1)*1000}, 1024, 0\n{(EPOCH+3)*1000}, 0, 0\nnot_a_record\n'
        rows,errors=program.parse_fio_bandwidth_log(text,EPOCH,3,0)
        self.assertEqual([r['Second'] for r in rows],[0,2])
        self.assertEqual([r['BW(MiB/s)'] for r in rows],[1,0])
        self.assertEqual(len(errors),1)
        missing=program.find_missing_measurements(range(3),rows,['BW(MiB/s)'],'带宽',EPOCH)
        self.assertEqual(missing[0]['相对测试秒'],1)


class StatisticsTests(unittest.TestCase):
    def smart_rows(self):
        return [{'Second':n*60,'Time':program.utc_timestamp(EPOCH+n*60),'Composite':v,'CPU':44,'NAND':38}
                for n,v in enumerate([48,32,40,40,40])]

    def test_threshold_inclusive_composite_both_directions_and_no_mutation(self):
        rows=self.smart_rows();old=copy.deepcopy(rows)
        for mode in ['read_1m','write_1m']:
            result,notes=program.exclude_initial_smart_outliers(mode,rows,60)
            self.assertEqual([r['相对测试秒'] for r in result],[0,60])
            self.assertEqual(result[0]['后续Composite均值'],40)
        self.assertEqual(rows,old)

    def test_low_power_keeps_all_valid_points(self):
        for mode in ['l0_ps4','l12_ps4']:
            self.assertEqual(program.exclude_initial_smart_outliers(mode,self.smart_rows(),60),([],[]))

    def test_missing_first_two_not_replaced_by_later_points(self):
        rows=self.smart_rows()[2:];rows[0]['Composite']=100
        rejected,_=program.exclude_initial_smart_outliers('read_1m',rows,60)
        self.assertEqual(rejected,[])

    def test_no_reference_means_no_exclusion(self):
        rejected,notes=program.exclude_initial_smart_outliers('write_1m',self.smart_rows()[:2],60)
        self.assertEqual(rejected,[]);self.assertTrue(notes)

    def test_cpu_alone_does_not_trigger_composite_exclusion(self):
        rows=self.smart_rows()
        for row in rows:row['Composite']=40
        rows[0]['CPU']=99
        self.assertEqual(program.exclude_initial_smart_outliers('read_1m',rows,60)[0],[])

    def test_last_600_seconds_and_zero_are_counted(self):
        rows=[{'Second':n,'Composite':100 if n<240 else 0 if n==240 else 50} for n in range(840)]
        rows[241]['Composite']=None
        statistics,selected=program.calculate_test_statistics(rows,240,840)
        self.assertEqual(len(selected),600)
        self.assertEqual(statistics['Composite']['count'],599)
        self.assertEqual(statistics['Composite']['min'],0)
        self.assertAlmostEqual(statistics['Composite']['mean'],598*50/599)


class IntegrityTests(unittest.TestCase):
    def test_trim_does_not_run_without_explicit_permission(self):
        remote=Mock()
        with self.assertRaises(ValueError):program.trim_selected_nvme_disk(CONFIG,remote,Path('.'))
        remote.execute.assert_not_called()

    def test_mounted_partition_blocks_disk(self):
        remote=Mock()
        remote.execute.side_effect=[(0,json.dumps({'blockdevices':[{'name':'/dev/nvme0n1','type':'disk','mountpoint':None,'children':[{'name':'/dev/nvme0n1p1','type':'part','mountpoint':'/'}]}]}),''),(0,'','')]
        with self.assertRaises(ValueError):program.verify_test_disk_is_unmounted(remote,'/dev/nvme0n1')

    def test_same_capacity_other_serial_cannot_mix_batch(self):
        cfg=copy.deepcopy(CONFIG);cfg['company_report'].update(capacity='512G',expected_serial='DEMO1',test_version='F1');cfg['_identity']={'firmware':'F1'}
        with tempfile.TemporaryDirectory() as directory:
            program.check_batch_disk_identity(cfg,Path(directory))
            cfg['company_report']['expected_serial']='DEMO2'
            with self.assertRaises(ValueError):program.check_batch_disk_identity(cfg,Path(directory))

    def test_existing_unregistered_workbook_is_not_overwritten(self):
        cfg=copy.deepcopy(CONFIG);cfg['_identity']={'firmware':'F1'}
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'温度模版.xlsx').write_bytes(b'existing workbook')
            with self.assertRaises(ValueError):program.check_batch_disk_identity(cfg,Path(directory))
            self.assertFalse((Path(directory)/'汇总对象记录.json').exists())

    def test_rebuild_only_reads_existing_measurements(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            state={'configuration':CONFIG,'mode':'l0_ps4','duration_s':1500,'start_epoch':EPOCH,'smart_interval_s':60,'completed':True,'errors':[]}
            program.save_json(folder/'本次运行记录.json',state)
            program.save_csv_table(folder/'JK808逐秒原始数据.csv',['Second','Time','CPU_Surface'],[{'Second':0,'Time':program.utc_timestamp(EPOCH),'CPU_Surface':31.7}])
            program.save_csv_table(folder/'SMART计划采样数据.csv',['Second','Time','Composite'],[{'Second':0,'Time':program.utc_timestamp(EPOCH),'Composite':60}])
            with patch.object(program,'LinuxConnection',side_effect=AssertionError),patch.object(program,'JK808Thermometer',side_effect=AssertionError),patch.object(program,'generate_all_test_results') as generate:
                program.rebuild_saved_test_results(folder)
            self.assertEqual(generate.call_args.args[3][0]['CPU_Surface'],31.7)
            self.assertEqual(generate.call_args.args[4][0]['Composite'],60)
            self.assertEqual(generate.call_args.args[5],[])

    def test_b_l12_opens_configuration_then_waits_for_confirmation(self):
        events=[]
        answers=iter(['1','Y'])
        def answer(prompt):
            events.append(prompt)
            return next(answers)
        with patch('builtins.input',side_effect=answer),patch.object(program.subprocess,'Popen') as editor,patch.object(program,'load_configuration',return_value=CONFIG),patch.object(program,'start_l12_then_pause') as run,patch('builtins.print'):
            program.choose_and_run_test()
        self.assertIn('本次操作',events[0]);self.assertIn('保存 JSON',events[1])
        run.assert_called_once_with(CONFIG)
        if program.os.name=='nt':editor.assert_called_once()

    def test_b_without_confirmation_does_not_start_test(self):
        with patch('builtins.input',side_effect=['1','N']),patch.object(program.subprocess,'Popen'),patch.object(program,'execute_selected_test') as run,patch('builtins.print'):
            program.choose_and_run_test()
        run.assert_not_called()

    def test_menu_runs_test_in_current_venv_for_ctrl_c_cleanup(self):
        with patch('builtins.input',side_effect=['B','','0']),patch('builtins.print'), \
             patch.object(program.sys,'argv',['linux_temperature_test.py']), \
             patch.object(program.sys,'executable',str(ROOT/'.venv/Scripts/python.exe')), \
             patch.object(Path,'exists',return_value=True),patch.object(program,'choose_and_run_test') as choose, \
             patch.object(program.subprocess,'run',side_effect=AssertionError('B 不应有会截断 Ctrl+C 收尾的父进程')):
            program.main()
        choose.assert_called_once_with()

    def test_pinned_key_api_available(self):
        import paramiko
        key=paramiko.RSAKey.generate(1024)
        restored=paramiko.PKey.from_type_string(key.get_name(),base64.b64decode(key.get_base64()))
        self.assertEqual(restored.get_base64(),key.get_base64())

    def test_v13_crc_and_invalid_probe(self):
        words=[317,313,249,32767,32767,32767,32767,32767]
        body=bytes.fromhex('01 03 00 00 00 08')+b''.join(value.to_bytes(2,'big',signed=True) for value in words)
        frame=body+program.calculate_crc16(body).to_bytes(2,'big')
        decoder=program.JK808ResponseDecoder()
        for offset in range(0,len(frame),3):decoder.feed(frame[offset:offset+3])
        values,statuses=program.convert_jk808_temperature_words(decoder.words,CONFIG['thermometer'])
        self.assertEqual([values[n] for n in [1,2,3]],[31.7,31.3,24.9])
        self.assertIsNone(values[4]);self.assertEqual(statuses[4],'disabled')
        with self.assertRaises(program.ProtocolError):program.JK808ResponseDecoder().feed(frame[:-1]+bytes([frame[-1]^1]))


class ConfigurationTests(unittest.TestCase):
    def configured_receiver(self):
        cfg=copy.deepcopy(CONFIG)
        cfg['company_report'].update(project='2260-PCBA-512G',batch='9.15',capacity='512G',
                                     expected_serial='DEMO_SN',test_version='DEMO_FW')
        cfg['ssh'].update(host='192.0.2.1',allow_destructive=True)
        cfg['thermometer'].update(protocol_verified=True,hid_path_hex='1234')
        cfg['detected_disk'] = {'firmware':'DEMO_FW'}
        cfg['sample_assignment'] = dict(program.disk_assignment_fields(cfg),confirmed_beijing='2026-09-24T09:00:00+08:00')
        return cfg

    def test_filled_dot_batch_passes_actual_test_validation(self):
        cfg=self.configured_receiver()
        before=copy.deepcopy(cfg)
        program.validate_test_configuration(cfg,'read_1m')
        self.assertEqual(cfg,before)

    def test_common_folder_names_keep_operator_text(self):
        for name in ('9.15','2260-PCBA-512G','样品 A (复测)','2026.09.15_01','批次（甲）'):
            with self.subTest(name=name):
                program.validate_report_folder_name(name,'batch')

    def test_invalid_folder_components_report_exact_field(self):
        for name in ('','.','..','../escape','a/b','a\\b','D:/data','a:stream','a*','a?','a|',
                     'a<','a>','a"','a\x00','a\n','a.','a ','CON','con.txt','LPT1','COM¹',
                     'NUL .txt','CONIN$','x'*61):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError,r'company_report\.batch'):
                    program.validate_report_folder_name(name,'batch')

    def test_blank_password_login_files_prompt_without_changing_host_key(self):
        cfg=self.configured_receiver()
        cfg['ssh']['host_key']={'host':'192.0.2.1','type':'demo','base64':'demo'}
        before=copy.deepcopy(cfg)
        with patch.dict(os.environ,{},clear=True),patch.object(program.getpass,'getpass',return_value='mock-secret') as ask:
            with program.supply_linux_password(cfg):
                self.assertEqual(os.environ['JK500_SSH_PASSWORD'],'mock-secret')
            self.assertNotIn('JK500_SSH_PASSWORD',os.environ)
        ask.assert_called_once()
        self.assertEqual(cfg,before)

    def test_bad_ssh_paths_explain_both_fields_without_echoing_values(self):
        settings=dict(CONFIG['ssh'],key_filename='mistaken-secret-value',known_hosts='192.0.2.1')
        with tempfile.TemporaryDirectory() as directory,patch.object(program,'PROGRAM_FOLDER',Path(directory)):
            with self.assertRaises(ValueError) as error:
                program.validate_ssh_file_paths(settings)
        message=str(error.exception)
        self.assertIn('ssh.key_filename',message);self.assertIn('ssh.known_hosts',message)
        self.assertIn('""',message);self.assertNotIn('mistaken-secret-value',message)
        self.assertEqual(settings['key_filename'],'mistaken-secret-value')

    def test_existing_optional_files_resolve_from_program_folder(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(program,'PROGRAM_FOLDER',Path(directory)):
            for name in ('private_key','known_hosts'):(Path(directory)/name).write_text('test fixture')
            settings=dict(CONFIG['ssh'],key_filename='private_key',known_hosts='known_hosts')
            paths=program.validate_ssh_file_paths(settings)
            self.assertEqual(paths['key_filename'],str(Path(directory)/'private_key'))
            self.assertEqual(paths['known_hosts'],str(Path(directory)/'known_hosts'))
            self.assertEqual(settings['key_filename'],'private_key')

    def test_invalid_file_is_rejected_before_password_network_or_test(self):
        cfg=self.configured_receiver();cfg['ssh']['key_filename']='missing-key-file'
        with tempfile.TemporaryDirectory() as directory,patch.object(program,'PROGRAM_FOLDER',Path(directory)), \
             patch.object(program.getpass,'getpass') as ask,patch.object(program,'LinuxConnection') as connection:
            with self.assertRaisesRegex(ValueError,'ssh.key_filename'):
                with program.supply_linux_password(cfg):self.fail('invalid configuration entered context')
            with self.assertRaisesRegex(ValueError,'ssh.key_filename'):
                program.execute_selected_test(cfg,'read_1m')
            ask.assert_not_called();connection.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_ssh_connect_checks_paths_before_creating_client(self):
        import paramiko
        settings=dict(CONFIG['ssh'],known_hosts='192.0.2.1')
        with tempfile.TemporaryDirectory() as directory,patch.object(program,'PROGRAM_FOLDER',Path(directory)), \
             patch.object(paramiko,'SSHClient') as client:
            with self.assertRaisesRegex(ValueError,'ssh.known_hosts'):
                program.LinuxConnection(settings).connect()
            client.assert_not_called()

    def test_blank_output_uses_default_and_each_task_gets_unique_folder(self):
        cfg=self.configured_receiver();cfg['company_report']['output_dir']=''
        program.validate_test_configuration(cfg,'read_1m')
        with tempfile.TemporaryDirectory() as directory,patch.object(program,'PROGRAM_FOLDER',Path(directory)),patch('builtins.print'):
            first=program.create_test_result_folder(cfg,'read_1m')
            (first/'original.txt').write_text('keep')
            second=program.create_test_result_folder(cfg,'read_1m')
            third=program.create_test_result_folder(cfg,'write_1m')
            self.assertEqual(first.parent,Path(directory)/'deliveries'/'2260-PCBA-512G'/'9.15'/'512G')
            self.assertEqual(len({first,second,third}),3)
            self.assertTrue(all(folder.is_dir() for folder in (first,second,third)))
            self.assertIn('Linux_1M_Read',first.name);self.assertIn('Linux_1M_Write',third.name)
            self.assertEqual((first/'original.txt').read_text(),'keep')

    def test_custom_output_root_is_created_without_manual_directories(self):
        cfg=self.configured_receiver()
        with tempfile.TemporaryDirectory() as directory,patch('builtins.print'):
            root=Path(directory)/'尚未创建的结果目录'
            cfg['company_report']['output_dir']=str(root)
            folder=program.create_test_result_folder(cfg,'l12_ps4')
            self.assertTrue(folder.is_dir())
            self.assertEqual(folder.parent.parent.parent.parent,root)
            self.assertIn('Linux_L1_2_PS4',folder.name)

    def test_connection_stage_failure_has_own_folder_and_configuration_record(self):
        cfg=self.configured_receiver()
        with tempfile.TemporaryDirectory() as directory,patch.object(program,'PROGRAM_FOLDER',Path(directory)), \
             patch.object(program,'validate_test_configuration'), \
             patch.object(program,'lock_receiver_during_test',return_value=nullcontext()), \
             patch.object(program,'supply_linux_password',return_value=nullcontext()), \
             patch.object(program,'LinuxConnection') as connection,patch('builtins.print'), \
             patch.object(program,'synchronize_receiver_clock',side_effect=TimeoutError('模拟校时连接失败')), \
             patch.object(program,'run_1m_read_test') as run:
            with self.assertRaisesRegex(TimeoutError,'模拟校时连接失败'):
                program.execute_selected_test(cfg,'read_1m')
            records=list(Path(directory).rglob('测试配置快照.json'))
            self.assertEqual(len(records),1)
            self.assertEqual(json.loads(records[0].read_text('utf-8'))['company_report']['batch'],'9.15')
            self.assertIn('模拟校时连接失败',(records[0].parent/'操作未完成.txt').read_text('utf-8-sig'))
            connection.return_value.connect.assert_not_called();run.assert_not_called()


class WorkbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 可指定目录保留离线验证结果，未指定则使用临时目录。
        cls.temporary=tempfile.TemporaryDirectory()
        cls.directory=Path(os.environ.get('JK808_VERIFY_OUTPUT',cls.temporary.name))
        cls.directory.mkdir(parents=True,exist_ok=True)
        cfg=copy.deepcopy(CONFIG)
        cfg['company_report'].update(capacity='512G',expected_serial='SIMULATED',test_version='模拟固件')
        cls.configuration=cfg
        cls.folders={}
        for mode in CONFIG['tests']:
            folder=cls.directory/'模拟批次'/'512G'/mode;folder.mkdir(parents=True,exist_ok=True)
            duration=cfg['tests'][mode]['duration_s'];interval=cfg['tests'][mode]['smart_interval_s']
            surface=[{'Second':second,'Time':program.utc_timestamp(EPOCH+second),'CPU_Surface':35+10*(1-math.exp(-second/80)),
                      'NAND_Surface':34+7*(1-math.exp(-second/80)),'AMB':25.0} for second in range(duration) if second not in (12,13,duration-1)]
            smart=[{'Second':second,'Time':program.utc_timestamp(EPOCH+second),'Composite':70 if second<120 else 45,'CPU':50,'NAND':41}
                    for second in range(0,duration,interval) if second!=interval]
            bandwidth=[{'Second':second+.12,'Time':program.utc_timestamp(EPOCH+second+.12),'BW(MiB/s)':7000}
                       for second in range(cfg['tests'][mode].get('fio_duration_s',0)) if second!=400]
            state={'mode':mode,'start_epoch':EPOCH,'duration_s':duration,'smart_interval_s':interval,'completed':True,
                   'errors':['离线自动验证模拟数据，不可上交。'],'identity':{'platform':'模拟平台','cpu':'模拟CPU','os':'模拟Linux'},'fio_offset_s':.12}
            with patch.dict(program.TEST_NAMES,{mode:'模拟验证_'+program.TEST_NAMES[mode]}),patch('builtins.print'):
                program.generate_all_test_results(cfg,state,folder,surface,smart,bandwidth)
            cls.folders[mode]=folder

    @classmethod
    def tearDownClass(cls):cls.temporary.cleanup()

    def test_original_three_tabs_and_names_preserved(self):
        parts=program.read_xlsx_parts(self.directory/'模拟批次'/'温度模版.xlsx')
        program.validate_company_template(parts)
        text=parts['xl/workbook.xml'].decode()
        self.assertIn('xmlns:x15=',text);self.assertIn('mc:Ignorable="x15"',text)
        self.assertIn('512G_读_基础数据',text)
        original=program.read_xlsx_parts(ROOT/'温度模版_原始.xlsx')
        for number in (1,2,3):
            a=ET.fromstring(original[f'xl/worksheets/sheet{number}.xml'])
            b=ET.fromstring(parts[f'xl/worksheets/sheet{number}.xml'])
            self.assertEqual(ET.tostring(a.find(f'{{{program.S}}}mergeCells')),ET.tostring(b.find(f'{{{program.S}}}mergeCells')))
            self.assertEqual(ET.tostring(a.find(f'{{{program.S}}}cols')),ET.tostring(b.find(f'{{{program.S}}}cols')))

    def test_content_types_uses_excel_compatible_default_namespace(self):
        for path in [self.folders['read_1m']/'手工复测'/'基础数据.xlsx',self.directory/'模拟批次'/'温度模版.xlsx']:
            text=program.read_xlsx_parts(path)['[Content_Types].xml'].decode()
            self.assertIn('<Types xmlns=',text)
            self.assertNotIn(':Types',text)

    def test_last_ten_minutes_and_expected_missing_times_in_each_report(self):
        for mode,folder in self.folders.items():
            data=json.loads((folder/'手工复测'/'统计结果.json').read_text('utf-8'))
            self.assertEqual(data['left_second'],CONFIG['tests'][mode]['duration_s']-600)
            with (folder/'手工复测'/'缺失数据时间点.csv').open(encoding='utf-8-sig') as stream:missing=list(csv.DictReader(stream))
            smart=[r for r in missing if r['来源']=='SMART']
            self.assertEqual({float(r['相对测试秒']) for r in smart},{CONFIG['tests'][mode]['smart_interval_s']})
            if mode=='l12_ps4':self.assertEqual(data['statistics']['Composite']['count'],0)

    def test_excel_dates_numeric_formulas_and_caches(self):
        import openpyxl  # 检查生成结果，不用它重写公司模板。
        folder=self.folders['read_1m']
        workbook=openpyxl.load_workbook(folder/'手工复测'/'基础数据.xlsx',data_only=True,read_only=True)
        self.assertEqual(workbook['完整基础数据']['B2'].value,35)
        self.assertEqual(workbook['完整基础数据']['A2'].value.year,2026)
        stats=json.loads((folder/'手工复测'/'统计结果.json').read_text('utf-8'))['statistics']
        self.assertEqual(workbook['人工复算']['B5'].value,stats['Composite']['count'])
        self.assertEqual(workbook['人工复算']['D5'].value,stats['Composite']['mean'])
        workbook.close()
        workbook=openpyxl.load_workbook(folder/'手工复测'/'基础数据.xlsx',data_only=False,read_only=True)
        self.assertIn("AVERAGE('参与统计的数据'!E2:E",workbook['人工复算']['D5'].value)
        workbook.close()

    def test_chart_and_html_and_company_workbook_use_same_png(self):
        parts=program.read_xlsx_parts(self.directory/'模拟批次'/'温度模版.xlsx')
        for mode,folder in self.folders.items():
            png=(folder/'【最终结果+双轴图表】_双Y轴图表.png').read_bytes()
            self.assertEqual(parts[f'xl/media/auto_linux_1_{mode}.png'],png)
            self.assertIn(base64.b64encode(png).decode(),(folder/'测试报告.html').read_text('utf-8'))

    def test_each_real_report_has_only_png_html_and_manual_folder_outside(self):
        for mode,folder in self.folders.items():
            with self.subTest(mode=mode):
                self.assertEqual({p.name for p in folder.iterdir()},
                                 {'【最终结果+双轴图表】_双Y轴图表.png','测试报告.html','手工复测'})
                self.assertIn('手工复测/基础数据.xlsx',(folder/'测试报告.html').read_text('utf-8'))
                self.assertTrue((folder/'手工复测'/'人工复算代码'/'复算温度.py').is_file())

    def test_manual_folders_can_be_copied_alone_and_recalculate_all_four_modes(self):
        import subprocess
        import sys
        import shutil
        import hashlib
        for mode,folder in self.folders.items():
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                target=Path(directory)/'手工复测'
                shutil.copytree(folder/'手工复测',target)
                raw=target/'完整基础数据.csv'
                digest=hashlib.sha256(raw.read_bytes()).hexdigest()
                # 删除原统计对照值后仍能独立复算，且原始文件不变。
                (target/'程序统计结果_仅供对照.json').unlink()
                result=subprocess.run([sys.executable,str(target/'人工复算代码'/'复算温度.py')],
                                      capture_output=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stdout.decode('utf-8',errors='replace'))
                recomputed=json.loads((target/'人工复算结果'/'复算统计.json').read_text('utf-8'))
                expected=json.loads((folder/'手工复测'/'统计结果.json').read_text('utf-8'))['statistics']
                self.assertEqual(recomputed,expected)
                self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(),digest)
                self.assertTrue((target/'本次出表时的温度模版.xlsx').is_file())

    def test_graph_retains_nan_gap_and_unrejected_values(self):
        import pandas as pd
        frame=pd.DataFrame({'Time':[program.utc_timestamp(EPOCH+n) for n in range(3)],'Composite':[70,None,40]})
        figure=program.create_company_temperature_figure([(frame,['Composite'],True)],'test',start=program.utc_timestamp(EPOCH))
        values=figure.axes[0].lines[0].get_ydata()
        self.assertEqual(len(values),3);self.assertTrue(math.isnan(values[1]));self.assertEqual(values[0],70)
        import matplotlib.pyplot as plt
        plt.close(figure)

    def test_actual_report_plot_omits_rejected_points_labels_and_keeps_missing_gaps(self):
        import pandas as pd
        import matplotlib.pyplot as plt
        cfg=copy.deepcopy(self.configuration)
        duration=cfg['tests']['read_1m']['duration_s']
        smart=[{'Second':s,'Time':program.utc_timestamp(EPOCH+s),'Composite':70 if s<120 else 45,
                'CPU':50,'NAND':41} for s in range(0,duration,60) if s!=180]
        original=copy.deepcopy(smart)
        captured=[]
        real_figure=program.create_company_temperature_figure
        def capture(frames,*args,**kwargs):
            figure=real_figure(frames,*args,**kwargs)
            captured.append(figure)
            return figure
        # round/composite 范围、低功耗不剔除，在真实报表入口与实际图对象上检查。
        for mode,scope in [('read_1m','round'),('write_1m','round'),('read_1m','composite'),('l0_ps4','round'),('l12_ps4','round')]:
            with self.subTest(mode=mode,scope=scope),tempfile.TemporaryDirectory() as directory:
                cfg['report']['initial_smart_scope']=scope
                interval=1500 if mode=='l12_ps4' else 60
                run_duration=2000 if mode=='l12_ps4' else duration
                rows=smart if mode!='l12_ps4' else [dict(smart[0]),dict(smart[2],Second=1500,Time=program.utc_timestamp(EPOCH+1500))]
                state={'mode':mode,'start_epoch':EPOCH,'duration_s':run_duration,'smart_interval_s':interval,
                       'completed':True,'errors':[],'simulated':True}
                with patch.object(program,'create_company_temperature_figure',side_effect=capture), \
                     patch('builtins.print'):
                    program.generate_all_test_results(cfg,state,Path(directory),[],rows,[])
                axis=captured[-1].axes[0]
                lines={line.get_label():line for line in axis.lines}
                composite=lines['复合温度']
                self.assertEqual(list(composite.get_xdata()[:3]),[0,1500] if mode=='l12_ps4' else [0,60,120])
                if mode in ('read_1m','write_1m'):
                    self.assertEqual(composite.get_ydata()[0],45)  # 原第120秒成为新第0秒
                    self.assertTrue(pd.isna(composite.get_ydata()[1]))  # 原180秒缺失 = 新60秒
                    self.assertEqual(composite.get_ydata()[2],45)
                    self.assertNotIn('70',[text.get_text() for text in axis.texts])
                    self.assertNotIn('不计统计',[text.get_text() for text in axis.texts])
                    for label in ('CPU','NAND'):
                        first=lines[label].get_ydata()[0]
                        self.assertFalse(pd.isna(first))
                    # scatter 的 masked 点不可见；不允许只删曲线、还留下散点。
                    offsets=axis.collections[0].get_offsets()
                    self.assertFalse(offsets.mask[0].any());self.assertTrue(offsets.mask[1].all())
                    self.assertFalse(offsets.mask[2].any())
                else:
                    self.assertEqual(composite.get_ydata()[0],70)
                self.assertEqual(smart,original)
                with (Path(directory)/'手工复测'/'完整基础数据.csv').open(encoding='utf-8-sig') as stream:
                    self.assertEqual(float(next(csv.DictReader(stream))['Composite']),70)
                with (Path(directory)/'手工复测'/'缺失数据时间点.csv').open(encoding='utf-8-sig') as stream:
                    self.assertFalse(any(r['来源']=='SMART' and float(r['相对测试秒'])==0 for r in csv.DictReader(stream)))
                metadata=json.loads((Path(directory)/'手工复测'/'统计结果.json').read_text('utf-8'))
                self.assertEqual(metadata['left_second'],run_duration-600)
                if mode in ('read_1m','write_1m'):
                    self.assertEqual(len(metadata['rejected_smart_points']),2)
                    self.assertEqual(metadata['statistics']['Composite']['mean'],45)
                else:
                    self.assertEqual(metadata['rejected_smart_points'],[])
                plt.close(captured[-1])

    def test_failed_detail_write_leaves_previous_company_workbook_intact(self):
        target=self.directory/'模拟批次'/'温度模版.xlsx'
        previous=target.read_bytes()
        folder=self.directory/'模拟批次'/'512G'/'failed_export';folder.mkdir(exist_ok=True)
        state={'mode':'read_1m','start_epoch':EPOCH,'duration_s':840,'smart_interval_s':60,'completed':False,'errors':['模拟写表失败'],'identity':{}}
        real_add=program.add_or_replace_data_sheet
        def fail_at_company_details(parts,name,*args,**kwargs):
            if name.startswith('512G_'):raise OSError('模拟：写入明细失败')
            return real_add(parts,name,*args,**kwargs)
        with patch.object(program,'add_or_replace_data_sheet',side_effect=fail_at_company_details),patch('builtins.print'):
            with self.assertRaises(OSError):program.generate_all_test_results(self.configuration,state,folder,[],[],[])
        self.assertEqual(target.read_bytes(),previous)


if __name__=='__main__':unittest.main(verbosity=2)
