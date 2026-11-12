"""新读写采集/选点/出表/复算和双机迁移；无真实硬件操作。"""
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from test_program import program, CONFIG, EPOCH, VirtualClock, VirtualEvent
from test_revision_210 import manual
from test_revision_230 import dual, ready_config
from report_sampling import selected_report_rows, migrate_rw_sampling, PROFILE, METRICS


def inputs():
    surface=[dict(Second=s,CPU_Surface=48 if s%30==0 else 80,NAND_Surface=43,AMB=24) for s in range(900)]
    smart=[dict(Time=program.utc_timestamp(EPOCH+s),Second=s,Composite=45 if s%30==0 else 90,
                CPU=47,NAND=42) for s in range(0,900,5)]
    bandwidth=[dict(Second=s+.12,**{'BW(MiB/s)':1000+s}) for s in range(900)]
    return surface,smart,bandwidth


class SelectionTests(unittest.TestCase):
    def test_actual_scheduler_records_180_five_second_slots_in_900_seconds(self):
        clock=VirtualClock();event=VirtualEvent(clock);rows=[];errors=[];remote=Mock()
        remote.execute.return_value=(0,json.dumps({'temperature':318,'temperature_sensor_1':320,'temperature_sensor_2':315}),'')
        with tempfile.TemporaryDirectory() as directory,patch.object(program,'time',clock):
            program.collect_smart_at_scheduled_times(CONFIG,remote,EPOCH,0,900,5,event,Path(directory),rows,errors)
            self.assertEqual(len((Path(directory)/'SMART完整原始响应.jsonl').read_text('utf-8').splitlines()),180)
        self.assertEqual([r['Second'] for r in rows],list(range(0,900,5)))
        self.assertEqual(remote.execute.call_count,180);self.assertFalse(errors)
        self.assertTrue(all(not row['Error'] for row in rows))

    def test_last_ten_minutes_twenty_real_rows_and_nearest_true_bandwidth(self):
        surface,smart,bw=inputs();raw=program.build_full_temperature_data(surface,smart,bw,900,EPOCH)
        before=copy.deepcopy(raw);selected,errors=selected_report_rows(raw,900)
        self.assertEqual([r['Second'] for r in selected],list(range(300,900,30)))
        self.assertFalse(errors);self.assertEqual(raw,before)
        self.assertEqual([r['带宽原始相对秒'] for r in selected],[s+.12 for s in range(300,900,30)])
        stats,_=program.calculate_test_statistics(selected,300,900)
        self.assertEqual(stats['CPU_Surface'],dict(count=20,min=48,max=48,mean=48))
        self.assertEqual(stats['Composite'],dict(count=20,min=45,max=45,mean=45))
        self.assertTrue(all(v['count']==20 for v in stats.values()))

    def test_missing_temperature_is_not_replaced_by_next_five_second_point(self):
        surface,smart,bw=inputs();smart=[r for r in smart if r['Second']!=300]
        raw=program.build_full_temperature_data(surface,smart,bw,900,EPOCH)
        selected,errors=selected_report_rows(raw,900)
        self.assertEqual(len(selected),20);self.assertIsNone(selected[0]['Composite'])
        self.assertEqual(selected[1]['Second'],330);self.assertTrue(errors)
        stats,_=program.calculate_test_statistics(selected,300,900)
        self.assertEqual(stats['Composite']['count'],19)

    def test_duplicate_selected_row_and_ambiguous_bandwidth_are_missing(self):
        surface,smart,bw=inputs();surface.append(dict(surface[300]))
        raw=program.build_full_temperature_data(surface,smart,bw,900,EPOCH)
        raw.append({'Second':299.88,'BW(MiB/s)':0})
        selected,errors=selected_report_rows(raw,900)
        self.assertIsNone(selected[0]['Composite']);self.assertIsNone(selected[0]['BW(MiB/s)'])
        self.assertTrue(errors)

    def test_invalid_geometry_rejected(self):
        for duration,interval,count in [(599,30,20),(900,30,21),(900,5,120)]:
            with self.assertRaises(ValueError):selected_report_rows([],duration,interval,count)


class MigrationTests(unittest.TestCase):
    def legacy(self):
        cfg=copy.deepcopy(CONFIG);cfg.get('config_updates',{}).pop('smart5_plot30_20261108',None)
        cfg.pop('_填写说明',None)
        for mode in ('read_1m','write_1m'):
            cfg['tests'][mode]=dict(duration_s=900,fio_duration_s=900,smart_interval_s=60)
        return cfg

    def test_single_file_upgrade_preserves_login_disk_and_l0_l12(self):
        cfg=self.legacy();cfg['ssh'].update(password='offline-secret',host='192.0.2.10')
        before=copy.deepcopy(cfg)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'config.json';program.save_json(path,cfg)
            with patch.object(program,'PROGRAM_FOLDER',root),patch.object(program,'CONFIGURATION_FILE',path),patch('builtins.print'):
                after=program.load_configuration();self.assertEqual(after,program.load_configuration())
            backups=list((root/'配置备份').glob('*.json'));self.assertEqual(len(backups),1)
            self.assertEqual(json.loads(backups[0].read_text('utf-8')),before)
        for key in ('ssh','thermometer','company_report','clock_sync'):self.assertEqual(after[key],before[key])
        for mode in ('l0_ps4','l12_ps4'):self.assertEqual(after['tests'][mode],before['tests'][mode])
        self.assertEqual(after['tests']['read_1m']['smart_interval_s'],5)

    def test_dual_upgrade_both_lanes_without_changing_probe_or_password(self):
        cfg=ready_config()
        for lane in dual.LANES:
            old=self.legacy();cfg['stations'][lane]['tests']=old['tests']
            cfg['stations'][lane].pop('config_updates',None)
        before=copy.deepcopy(cfg)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch.object(dual,'ROOT',root),patch.object(dual,'DUAL_FILE',root/'双机配置.json'),patch('builtins.print'):
                dual.base.save_json(dual.DUAL_FILE,cfg)
                result=dual.load_dual_configuration();self.assertEqual(result,dual.load_dual_configuration())
            self.assertEqual(len(list((root/'配置备份').glob('*.json'))),1)
        for lane in dual.LANES:
            for key in ('ssh','thermometer','company_report','sample_assignment'):
                self.assertEqual(result['stations'][lane][key],before['stations'][lane][key])
            self.assertEqual(result['stations'][lane]['tests']['write_1m']['report_points'],20)


class ActualReportTests(unittest.TestCase):
    def test_new_report_raw_preserved_plot_and_statistics_twenty_and_manual_agrees(self):
        surface,smart,bw=inputs();cfg=copy.deepcopy(CONFIG)
        cfg['company_report'].update(capacity='512G',expected_serial='SYNTHETIC-NOT-HARDWARE',test_version='offline')
        state=dict(mode='read_1m',start_epoch=EPOCH,duration_s=900,smart_interval_s=5,
                   errors=[],completed=True,simulated=False,fio_offset_s=.12,
                   sampling_profile=PROFILE,report_interval_s=30,report_points=20)
        captured=[];original=program.create_company_temperature_figure
        def capture(*a,**kw):
            fig=original(*a,**kw);captured.append(fig);return fig
        with tempfile.TemporaryDirectory() as directory,patch('builtins.print'),patch.object(program,'create_company_temperature_figure',side_effect=capture):
            root=Path(directory)/'512G'/'run';root.mkdir(parents=True)
            program.generate_all_test_results(cfg,state,root,surface,smart,bw)
            data=root/'手工复测';saved=json.loads((data/'统计结果.json').read_text('utf-8'))
            self.assertTrue(all(v['count']==20 for v in saved['statistics'].values()))
            self.assertEqual(saved['statistics']['Composite']['max'],45)
            self.assertEqual(saved['chart_start_original_second'],300)
            self.assertEqual(saved['chart_end_original_second'],870)
            for axis in captured[0].axes:
                for line in axis.lines:
                    self.assertEqual(len(line.get_xdata()),20,line.get_label())
                    self.assertEqual(line.get_xdata()[-1],570)
            with (data/'参与统计的数据.csv').open(encoding='utf-8-sig') as stream:
                self.assertEqual(len(list(csv.DictReader(stream))),20)
            with (data/'完整基础数据.csv').open(encoding='utf-8-sig') as stream:
                self.assertEqual(len(list(csv.DictReader(stream))),1800)
            with (data/'SMART原始计划点.csv').open(encoding='utf-8-sig') as stream:
                self.assertEqual(len(list(csv.DictReader(stream))),180)
            self.assertEqual(manual.recalculate(data),saved['statistics'])
            self.assertTrue((data/'人工复算代码/report_sampling.py').is_file())
            self.assertEqual(json.loads((data/'本次复算条件.json').read_text('utf-8'))['report_points'],20)
            import xml.etree.ElementTree as ET
            parts=program.read_xlsx_parts(Path(directory)/'温度模版.xlsx')
            sheet=ET.fromstring(parts['xl/worksheets/sheet1.xml']);ns={'s':program.S}
            self.assertEqual(sheet.find('.//s:c[@r="C80"]/s:v',ns).text,'45')
            self.assertEqual(sheet.find('.//s:c[@r="C82"]/s:v',ns).text,'48')


if __name__=='__main__':unittest.main()

