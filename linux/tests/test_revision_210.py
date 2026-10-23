"""v2.1 离线回归：不联网、不修改任何真实机器的时钟、不执行真实设备命令。"""
from test_program import (program, CONFIG, EPOCH, ROOT, VirtualClock, Path, ast, copy,
                          importlib, json, math, tempfile, unittest, patch)
import test_program as previous_tests
import io
import shlex
import struct
import subprocess

manual_spec = importlib.util.spec_from_file_location('manual_recalculation',ROOT/'人工复算代码'/'复算温度.py')
manual = importlib.util.module_from_spec(manual_spec)
manual_spec.loader.exec_module(manual)


class RevisionConfigurationTests(unittest.TestCase):
    def test_bios_value_or_absence_does_not_gate_any_mode(self):
        cfg = previous_tests.ConfigurationTests().configured_receiver()
        for value in ('all_off','l12_on','L1.2',None):
            cfg['bios_profile'] = value
            for mode in CONFIG['tests']:
                program.validate_test_configuration(cfg,mode)
        del cfg['bios_profile']
        for mode in CONFIG['tests']:
            program.validate_test_configuration(cfg,mode)

    def test_corrected_time_renames_folder_and_preserves_preparation_records(self):
        from contextlib import nullcontext
        from datetime import datetime
        cfg=previous_tests.ConfigurationTests().configured_receiver()
        with tempfile.TemporaryDirectory() as directory:
            cfg['company_report']['output_dir']=directory
            cfg['sample_assignment']=dict(program.disk_assignment_fields(cfg),confirmed_beijing='2026-09-24T09:00:00+08:00')
            def sync(configuration,folder):
                (folder/'北京时间校准记录.json').write_text('{"outcome":"ok"}')
                return {'outcome':'ok'}
            def fake_run(configuration,linux,smart,folder,event):
                self.assertTrue(folder.parent.name.startswith('20260915_180000'))
                self.assertTrue((folder/'测试配置快照.json').exists())
                self.assertTrue((folder/'北京时间校准记录.json').exists())
                return {'completed':True,'errors':[]}
            with patch.object(program,'datetime',wraps=datetime) as dates, \
                 patch.object(program,'lock_receiver_during_test',return_value=nullcontext()), \
                 patch.object(program,'supply_linux_password',return_value=nullcontext()), \
                 patch.object(program,'LinuxConnection'),patch.object(program,'synchronize_receiver_clock',side_effect=sync), \
                 patch.object(program,'check_linux_identity_before_test',return_value={'firmware':'demo'}), \
                 patch.object(program,'check_batch_disk_identity'),patch.object(program,'run_1m_read_test',side_effect=fake_run),patch('builtins.print'):
                dates.now.side_effect=[datetime(2020,1,1),datetime(2026,9,15,18)]
                result=program.execute_selected_test(cfg,'read_1m')
                self.assertTrue(result['completed']);self.assertTrue(result['results_saved'])
            self.assertEqual(len(list(Path(directory).rglob('测试配置快照.json'))),1)

    def test_missing_recalculation_code_is_reported_before_destructive_steps(self):
        cfg=previous_tests.ConfigurationTests().configured_receiver()
        with tempfile.TemporaryDirectory() as directory,patch.object(program,'PROGRAM_FOLDER',Path(directory)), \
             patch.object(program,'read_xlsx_parts'),patch.object(program,'validate_company_template'):
            with self.assertRaisesRegex(ValueError,'人工复算代码'):
                program.validate_test_configuration(cfg,'read_1m')


class RebasedChartTests(unittest.TestCase):
    def test_start_is_first_valid_after_last_exclusion_not_next_missing_slot(self):
        rows = [{'Second':0,'Composite':70},{'Second':60,'Composite':None},{'Second':120,'Composite':45}]
        self.assertEqual(program.find_chart_start_second(rows,[{'相对测试秒':0}]),120)
        self.assertEqual(program.find_chart_start_second(rows,[]),0)

    def test_common_zero_preserves_relative_alignment_and_no_missing_tail(self):
        import matplotlib.pyplot as plt
        cfg = copy.deepcopy(CONFIG)
        cfg['company_report'].update(capacity='512G')
        surface = [{'Second':s,'CPU_Surface':40+s/100,'NAND_Surface':35,'AMB':25} for s in range(840) if s<780 and s!=301]
        smart = [{'Time':program.utc_timestamp(EPOCH+s),'Second':s,'Composite':70 if s==0 else 45,'CPU':50,'NAND':41}
                 for s in range(0,780,60) if s!=300]
        bandwidth = [{'Second':s+.12,'BW(MiB/s)':7000} for s in range(700) if s!=350]
        state = {'mode':'read_1m','start_epoch':EPOCH,'duration_s':840,'smart_interval_s':60,
                 'errors':[],'completed':True,'simulated':True,'fio_offset_s':.12}
        captured = []
        real = program.create_company_temperature_figure
        def capture(*args,**kwargs):
            figure = real(*args,**kwargs);captured.append(figure);return figure
        with tempfile.TemporaryDirectory() as directory,patch.object(program,'create_company_temperature_figure',side_effect=capture),patch('builtins.print'):
            program.generate_all_test_results(cfg,state,Path(directory),surface,smart,bandwidth)
            figure = captured[0]
            lines = {line.get_label():line for axis in figure.axes for line in axis.lines}
            self.assertEqual(lines['复合温度'].get_xdata()[0],0)
            self.assertEqual(lines['CPU表面'].get_xdata()[0],0)
            self.assertEqual(lines['复合温度'].get_xdata()[-1],660)  # 原720秒
            self.assertEqual(lines['CPU表面'].get_xdata()[-1],660)  # 所有曲线统一不超过原720秒 SMART 点
            bw = lines['带宽 BW(MiB/s)']
            self.assertAlmostEqual(bw.get_xdata()[0],.12)
            self.assertAlmostEqual(bw.get_xdata()[-1],639.12)
            self.assertTrue(math.isnan(lines['复合温度'].get_ydata()[4]))  # 原300秒
            self.assertTrue(math.isnan(lines['CPU表面'].get_ydata()[241]))  # 原301秒
            saved = json.loads((Path(directory)/'手工复测'/'统计结果.json').read_text('utf-8'))
            self.assertEqual(saved['left_second'],240)
            self.assertEqual(saved['chart_start_original_second'],60)
            self.assertIn('+08:00',saved['chart_start_beijing'])
            loss = (Path(directory)/'手工复测'/'缺失数据时间点.csv').read_text('utf-8-sig')
            self.assertIn(',839,',loss)  # 裁掉的末尾仍在缺失清单
            with patch('builtins.print'):
                independent = manual.recalculate(Path(directory)/'手工复测')
            self.assertEqual(saved['statistics'],independent)
            plt.close(figure)

    def test_no_tail_for_each_metric_keeps_interior_nan(self):
        import pandas as pd
        import matplotlib.pyplot as plt
        frame = pd.DataFrame({'Time':[program.utc_timestamp(EPOCH+s) for s in range(6)],
                              'Composite':[None,40,None,42,None,None],
                              'CPU':[None,50,None,None,None,None]})
        figure = program.create_company_temperature_figure([(frame,['Composite','CPU'],True)],'test',start=program.utc_timestamp(EPOCH))
        first,second = figure.axes[0].lines
        self.assertEqual(list(first.get_xdata()),[1,2,3])
        self.assertTrue(math.isnan(first.get_ydata()[1]))
        self.assertEqual(list(second.get_xdata()),[1])  # 单个点不向后拉线
        plt.close(figure)


class ManualRecalculationTests(unittest.TestCase):
    def test_manual_rule_and_last_600_boundary_independent_of_chart_origin(self):
        conditions = {'mode':'read_1m','duration_s':660,'statistics_window_s':600,
                      'smart_interval_s':60,'outlier_threshold_c':8,'initial_smart_scope':'round',
                      'chart_start_original_second':120}
        smart = [{'Second':0,'Composite':70},{'Second':60,'Composite':53},
                 {'Second':120,'Composite':45},{'Second':180,'Composite':45}]
        rejected,mean = manual.find_excluded_smart_seconds(smart,conditions)
        self.assertEqual(mean,45)
        self.assertEqual([r['原始相对秒'] for r in rejected],[0,60])  # 8℃包含边界
        rows=[]
        for second,value in [(59,99),(60,0),(120,20),(659,40),(660,999)]:
            rows.append({'Second':second,**{key:value for key in manual.METRICS}})
        stats,selected = manual.calculate_last_ten_minutes(rows,rejected,conditions)
        self.assertEqual(stats['CPU_Surface'],{'count':3,'max':40,'mean':20,'min':0})
        self.assertEqual(stats['Composite'],{'count':2,'max':40,'mean':30,'min':20})
        self.assertEqual([r['Second'] for r in selected],[60,120,659])
        self.assertEqual(rows[1]['Composite'],0)

    def test_manual_low_power_no_outlier_and_missing_last_window_remains_blank(self):
        conditions = {'mode':'l12_ps4','duration_s':2000,'statistics_window_s':600,
                      'smart_interval_s':1500,'outlier_threshold_c':8,'initial_smart_scope':'round'}
        rows = [{'Second':0,**{key:70 for key in manual.METRICS}}]
        excluded,_ = manual.find_excluded_smart_seconds(rows,conditions)
        self.assertEqual(excluded,[])
        stats,_ = manual.calculate_last_ten_minutes(rows,excluded,conditions)
        self.assertEqual(stats['Composite'],{'count':0,'max':None,'mean':None,'min':None})

    def test_manual_code_is_standalone_standard_library_only(self):
        tree = ast.parse((ROOT/'人工复算代码'/'复算温度.py').read_text('utf-8'))
        imports = {node.names[0].name for node in ast.walk(tree) if isinstance(node,ast.Import)}
        self.assertEqual(imports,{'csv','hashlib','json','math','sys'})
        self.assertFalse(any(isinstance(node,ast.ClassDef) for node in ast.walk(tree)))


class NetworkClockTests(unittest.TestCase):
    @staticmethod
    def packet(epoch=EPOCH,token=b'12345678',header=0x24,stratum=2):
        data = bytearray(48);data[0]=header;data[1]=stratum;data[24:32]=token
        def stamp(value):
            ntp = value+2208988800
            return struct.pack('!II',int(ntp)%(2**32),int((ntp-int(ntp))*(2**32)))
        data[32:40]=stamp(epoch);data[40:48]=stamp(epoch+.01)
        return bytes(data)

    def test_ntp_absolute_epoch_ignores_wrong_local_wall_clock(self):
        with patch.object(program.time,'time',return_value=1):
            sample = program.decode_ntp_reply(self.packet(),b'12345678',.11,10)
        self.assertAlmostEqual(sample['epoch_s'],EPOCH+.06,places=5)
        self.assertAlmostEqual(sample['uncertainty_s'],.05,places=5)

    def test_ntp_era_rollover_after_2036(self):
        sample = program.decode_ntp_reply(self.packet(epoch=2400000000),b'12345678',.11,10)
        self.assertAlmostEqual(sample['epoch_s'],2400000000.06,places=5)

    def test_ntp_rejects_short_unsynced_wrong_origin_and_large_uncertainty(self):
        invalid = [b'bad',self.packet(header=0xe4),self.packet(header=0x23),
                   self.packet(stratum=0),self.packet(token=b'badtoken')]
        excessive = bytearray(self.packet());excessive[8:12]=struct.pack('!I',65536)
        invalid.append(bytes(excessive))
        for data in invalid:
            with self.assertRaises(ValueError):program.decode_ntp_reply(data,b'12345678',.11,10)
        with self.assertRaises(ValueError):program.decode_ntp_reply(self.packet(),b'12345678',2,10)

    def test_ntp_all_blocked_has_actionable_error_and_does_not_change_time(self):
        clock = VirtualClock()
        with patch.object(program,'time',clock),patch.object(program,'request_ntp_time',side_effect=TimeoutError('timeout')), \
             patch.object(program,'set_windows_system_time') as setter:
            with self.assertRaisesRegex(RuntimeError,'UDP 123'):
                program.obtain_internet_standard_time({})
            setter.assert_not_called()

    def test_ntp_inconsistent_samples_not_accepted(self):
        clock = VirtualClock()
        def sample(server):
            return {'epoch_s':EPOCH+clock.second*2,'received_monotonic_s':clock.second,'uncertainty_s':.01}
        with patch.object(program,'time',clock),patch.object(program,'request_ntp_time',side_effect=sample):
            with self.assertRaisesRegex(RuntimeError,'0.25'):
                program.obtain_internet_standard_time({'clock_sync':{'ntp_servers':['example.test']}})

    def test_successful_clock_flow_orders_linux_then_windows_and_audits(self):
        class Clock(VirtualClock):
            wall_offset = -3600
            def time(self):return EPOCH+self.second+self.wall_offset
        clock = Clock();events=[];linux_offset=-86400
        class Remote:
            def connect(self):events.append('connect')
            def close(self):events.append('close')
            def execute(self,command):
                nonlocal linux_offset
                begin=clock.second;clock.second+=.02
                if command=='id -u':return 0,'0',''
                if command=='date +%z':return 0,'+0800',''
                if command.startswith('python3 -c '):
                    script=shlex.split(command)[2]
                    argument=ast.parse(script).body[1].value.args[1].value
                    linux_offset=argument-(EPOCH+begin+.01);events.append('linux-set')
                if command=='date +%s.%N':return 0,str(EPOCH+begin+.01+linux_offset),''
                return 0,'ok',''
        def windows_set(epoch):
            clock.wall_offset=epoch-(EPOCH+clock.second);events.append('windows-set')
        reference={'epoch_s':EPOCH,'received_monotonic_s':0,'uncertainty_s':.01,'server':'mock.test'}
        with tempfile.TemporaryDirectory() as directory,patch.object(program,'time',clock), \
             patch.object(program,'require_windows_clock_permission'),patch.object(program,'obtain_internet_standard_time',return_value=reference), \
             patch.object(program,'LinuxConnection',return_value=Remote()),patch.object(program,'set_beijing_timezones'), \
             patch.object(program,'set_windows_system_time',side_effect=windows_set),patch('builtins.print'):
            record=program.synchronize_receiver_clock(CONFIG,Path(directory))
            self.assertEqual(record['outcome'],'ok')
            self.assertLess(events.index('linux-set'),events.index('windows-set'))
            self.assertEqual(events.count('windows-set'),2)
            self.assertLess(abs(record['after']['offset_s']),.01)
            self.assertLess(abs(record['receiver_vs_standard_s']),.01)
            saved=json.loads((Path(directory)/'北京时间校准记录.json').read_text('utf-8'))
            self.assertTrue(saved['changed_linux']);self.assertTrue(saved['changed_windows'])
            self.assertIn('+08:00',saved['standard_beijing'])

    def test_no_permission_or_no_ntp_cannot_reach_linux_or_test(self):
        for failing_step in ('require_windows_clock_permission','obtain_internet_standard_time'):
            with tempfile.TemporaryDirectory() as directory,patch.object(program,'LinuxConnection') as connection, \
                 patch.object(program,'require_windows_clock_permission'),patch.object(program,failing_step,side_effect=RuntimeError('模拟失败')), \
                 patch.object(program,'set_windows_system_time') as setter,patch('builtins.print'):
                with self.assertRaisesRegex(RuntimeError,'模拟失败'):
                    program.synchronize_receiver_clock(CONFIG,Path(directory))
                setter.assert_not_called();connection.return_value.connect.assert_not_called()
                record=json.loads((Path(directory)/'北京时间校准记录.json').read_text('utf-8'))
                self.assertEqual(record['outcome'],'failed')


if __name__=='__main__':
    unittest.main()
