"""900 秒读写、共同绘图终点、菜单独立选择；仅离线数据和模拟连接。"""
from test_program import program, CONFIG, EPOCH, ROOT, VirtualClock, VirtualEvent
from test_revision_210 import manual
import copy
import csv
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from contextlib import ExitStack


class DurationUpgradeTests(unittest.TestCase):
    def test_defaults_and_fio_command_are_900_seconds(self):
        for mode in ('read_1m','write_1m'):
            self.assertEqual(CONFIG['tests'][mode],dict(duration_s=900,fio_duration_s=900,smart_interval_s=60))
            self.assertIn('--runtime=900',program.build_fio_command(CONFIG,mode.split('_')[0],'/tmp/test',CONFIG['tests'][mode]['fio_duration_s']))
        self.assertEqual(CONFIG['tests']['l0_ps4'],dict(duration_s=1500,smart_interval_s=60))
        self.assertEqual(CONFIG['tests']['l12_ps4'],dict(duration_s=2000,smart_interval_s=1500))

    def test_field_config_upgrade_backs_up_preserves_values_and_is_idempotent(self):
        old=copy.deepcopy(CONFIG)
        old.pop('_填写说明',None)  # 首次部署后的现场配置只保存正式配置项。
        old['ssh'].update(host='192.0.2.1',password='field-secret',host_key={'example':'keep'})
        old['clock_sync']['ntp_servers']=['192.0.2.2']
        old['company_report'].update(project='项目',batch='9.23',expected_serial='SERIAL')
        for mode in ('read_1m','write_1m'):
            old['tests'][mode].update(duration_s=840,fio_duration_s=780)
        with tempfile.TemporaryDirectory() as folder,patch.object(program,'PROGRAM_FOLDER',Path(folder)),patch.object(program,'CONFIGURATION_FILE',Path(folder)/'config.json'),patch('builtins.print'):
            program.save_json(program.CONFIGURATION_FILE,old)
            cfg=program.load_configuration()
            expected=copy.deepcopy(old)
            for mode in ('read_1m','write_1m'):
                expected['tests'][mode].update(duration_s=900,fio_duration_s=900)
            self.assertEqual(json.loads(program.CONFIGURATION_FILE.read_text('utf-8')),expected)
            self.assertEqual(cfg['ssh']['password'],'field-secret')
            backups=list((Path(folder)/'配置备份').glob('*.json'))
            self.assertEqual(len(backups),1)
            self.assertEqual(json.loads(backups[0].read_text('utf-8')),old)
            program.load_configuration()
            self.assertEqual(len(list((Path(folder)/'配置备份').glob('*.json'))),1)

    def test_upgrade_corrects_partly_changed_900_config_but_keeps_custom_duration(self):
        cfg=copy.deepcopy(CONFIG)
        cfg['tests']['read_1m']['fio_duration_s']=780
        cfg['tests']['write_1m'].update(duration_s=1200,fio_duration_s=1200)
        with patch.object(program,'save_configuration'),patch('builtins.print'):
            program.upgrade_read_write_duration(cfg)
        self.assertEqual(cfg['tests']['read_1m']['fio_duration_s'],900)
        self.assertEqual(cfg['tests']['write_1m']['duration_s'],1200)


class FioEndTests(unittest.TestCase):
    def run_collector(self,finish_at=901,code=0,cancel_at=None):
        clock=VirtualClock()
        class Event(VirtualEvent):
            def wait(self,seconds):
                super().wait(seconds)
                if cancel_at is not None and clock.second>=cancel_at:self.set()
                return self.stopped
        event=Event(clock);remote=Mock();threads=[]
        class Thread:
            def __init__(self,**kw):self.args=kw['args'];self.ident=None;threads.append(self)
            def start(self):self.ident=1
            def join(self,**kw):pass
            def is_alive(self):return False
        fio={'finished':False,'remote_folder':'/tmp/fake','phase':'test','process_group':123}
        def launch(*args):
            self.assertEqual(args[-1],900)
            clock.sleep(1)
            return fio
        def status(*args):
            if clock.second>=finish_at:
                fio['finished']=True
                return code
            return None
        with tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
            folder=Path(directory)
            (folder/'started_epoch').write_text(str(EPOCH+1))
            (folder/'bandwidth_bw.1.log').write_text('')
            stack.enter_context(patch.object(program,'time',clock))
            stack.enter_context(patch.object(program.threading,'Thread',Thread))
            stack.enter_context(patch.object(program,'start_remote_fio',side_effect=launch))
            stack.enter_context(patch.object(program,'get_fio_exit_code',side_effect=status))
            stop=stack.enter_context(patch.object(program,'stop_this_fio_process'))
            stack.enter_context(patch.object(program,'retrieve_fio_files',return_value=folder))
            stack.enter_context(patch.object(program,'parse_fio_bandwidth_log',return_value=([],[])))
            generate=stack.enter_context(patch.object(program,'generate_all_test_results'))
            stack.enter_context(patch('builtins.print'))
            result=program.collect_during_formal_test(CONFIG,'read_1m',remote,remote,folder,event)
            generate.assert_called_once()
            stop.assert_called_once()
        self.assertTrue(event.is_set())
        self.assertEqual([threads[0].args[3],threads[1].args[4]],[900,900])
        return result,clock

    def test_delayed_fio_normal_exit_after_sampling_not_reported_as_failure(self):
        result,clock=self.run_collector()
        self.assertTrue(result['completed'])
        self.assertEqual(result['fio_exit_code'],0)
        self.assertAlmostEqual(clock.second,901,delta=.01)
        self.assertEqual(result['ended_at'],program.utc_timestamp(EPOCH+900))

    def test_nonzero_exit_marks_run_incomplete(self):
        result,_=self.run_collector(code=1)
        self.assertFalse(result['completed'])
        self.assertEqual(result['fio_exit_code'],1)

    def test_stuck_fio_is_bounded_and_cancel_does_not_wait_for_duration(self):
        result,clock=self.run_collector(finish_at=9999)
        self.assertFalse(result['completed'])
        self.assertLessEqual(clock.second,930.01)
        self.assertIn('超过 30 秒',' '.join(result['errors']))
        result,clock=self.run_collector(finish_at=9999,cancel_at=2)
        self.assertFalse(result['completed'])
        self.assertLess(clock.second,3)


class CommonEndTests(unittest.TestCase):
    def test_end_only_uses_valid_planned_composite_within_run(self):
        rows=[{'Second':s,'Composite':v} for s,v in [(0,30),(60,40),(120,float('nan')),(180,None),(240,False),(250,99),(300,99)]]
        self.assertEqual(program.find_chart_end_second(rows,300,60,0),60)
        self.assertIsNone(program.find_chart_end_second(rows,300,60,120))

    def test_real_cooling_tail_removed_but_raw_statistics_and_manual_data_preserved(self):
        import matplotlib.pyplot as plt
        cfg=copy.deepcopy(CONFIG)
        # 末点840秒丢失，截止必须回退到780；起始0秒剔除后共同归零在60秒。
        smart=[{'Time':program.utc_timestamp(EPOCH+s),'Second':s,'Composite':80 if s==0 else 50,'CPU':52,'NAND':45} for s in range(0,840,60) if s!=300]
        surface=[{'Second':s,'CPU_Surface':70 if s<=780 else 35,'NAND_Surface':60 if s<=780 else 30,'AMB':25} for s in range(900) if s!=301]
        bandwidth=[{'Second':s+.1,'BW(MiB/s)':7000} for s in range(900)]
        state=dict(mode='read_1m',start_epoch=EPOCH,duration_s=900,smart_interval_s=60,errors=[],completed=True,simulated=True,fio_offset_s=.1)
        capture=[];real=program.create_company_temperature_figure
        def figure(*args,**kw):
            result=real(*args,**kw);capture.append(result);return result
        with tempfile.TemporaryDirectory() as folder,patch.object(program,'create_company_temperature_figure',side_effect=figure),patch('builtins.print'):
            program.generate_all_test_results(cfg,state,Path(folder),surface,smart,bandwidth)
            lines={line.get_label():line for axis in capture[0].axes for line in axis.lines}
            for label,line in lines.items():
                self.assertLessEqual(max(line.get_xdata()),720,label)
            self.assertEqual(lines['CPU表面'].get_xdata()[-1],720)
            self.assertEqual(lines['复合温度'].get_xdata()[-1],720)
            self.assertAlmostEqual(lines['带宽 BW(MiB/s)'].get_xdata()[-1],719.1)
            self.assertTrue(math.isnan(lines['CPU表面'].get_ydata()[241]))
            data=Path(folder)/'手工复测'
            saved=json.loads((data/'统计结果.json').read_text('utf-8'))
            self.assertEqual(saved['left_second'],300)
            self.assertEqual(saved['right_second'],900)
            self.assertEqual(saved['chart_end_original_second'],780)
            self.assertEqual(saved['statistics']['CPU_Surface']['min'],35)
            self.assertEqual(saved['statistics'],manual.recalculate(data))
            with (data/'完整基础数据.csv').open(encoding='utf-8-sig',newline='') as file:
                rows=list(csv.DictReader(file))
            self.assertTrue(any(float(row['Second'])==899 and row['CPU_Surface']=='35' for row in rows))
            self.assertIn(',840,',(data/'缺失数据时间点.csv').read_text('utf-8-sig'))
            plt.close(capture[0])

    def test_no_smart_data_does_not_draw_unbounded_surface_tail(self):
        cfg=copy.deepcopy(CONFIG)
        state=dict(mode='l0_ps4',start_epoch=EPOCH,duration_s=1500,smart_interval_s=60,errors=[],completed=False,simulated=True)
        with tempfile.TemporaryDirectory() as folder,patch.object(program,'create_company_temperature_figure',wraps=program.create_company_temperature_figure) as chart,patch('builtins.print'):
            program.generate_all_test_results(cfg,state,Path(folder),[{'Second':0,'CPU_Surface':42}],[],[])
            for frame,_,_ in chart.call_args.args[0]:self.assertTrue(frame.empty)
            saved=json.loads((Path(folder)/'手工复测'/'统计结果.json').read_text('utf-8'))
            self.assertIsNone(saved['chart_end_original_second'])
            self.assertIn('没有有效 SMART',' '.join(saved['notes']))


class IndependentMenuTests(unittest.TestCase):
    def test_first_choice_only_validates_l12_not_write_permissions(self):
        with patch.object(program,'validate_test_configuration') as validate,patch.object(program,'lock_receiver_during_test',side_effect=RuntimeError('end-before-device')):
            with self.assertRaisesRegex(RuntimeError,'end-before-device'):program.start_l12_then_pause(CONFIG)
        validate.assert_called_once_with(CONFIG,'l12_ps4')

    def test_second_choice_starts_without_any_l12_progress(self):
        from contextlib import nullcontext
        with tempfile.TemporaryDirectory() as folder,patch.object(program,'PROGRAM_FOLDER',Path(folder)),patch.object(program,'validate_test_configuration'),patch.object(program,'lock_receiver_during_test',return_value=nullcontext()),patch.object(program,'wait_before_starting_test'),patch('builtins.print'):
            self.assertIsNone(program.load_workflow_progress())
            modes=[]
            def run(cfg,mode,**kwargs):
                modes.append(mode)
                data=Path(folder)/mode/'手工复测';data.mkdir(parents=True)
                program.save_json(data/'本次运行记录.json',{})
                return dict(completed=True,results_saved=True,result_folder=str(data.parent),errors=[])
            with patch.object(program,'execute_selected_test',side_effect=run):program.continue_l0_write_read(CONFIG)
            self.assertEqual(modes,['l0_ps4','write_1m','read_1m'])
            self.assertNotIn('l12_ps4',program.load_workflow_progress()['completed'])


if __name__=='__main__':unittest.main()
