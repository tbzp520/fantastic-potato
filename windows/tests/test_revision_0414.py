"""5秒原始采集、独立绘图周期与600秒窗口；只使用合成记录和窗口夹具。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import windows_temperature_test as app
import temperature_report as report
from test_revision_028 import CapturedSettings


def records(duration=1200, cadence=5):
    smart=[dict(epoch=s,source_row=i+2,Composite=45,CPU=47,NAND=42)
           for i,s in enumerate(range(0,duration,cadence))]
    surface=[dict(epoch=s,source_row=s+1,CPU_Surface=48,NAND_Surface=43,AMB=24)
             for s in range(duration)]
    return smart,surface


def select(smart,surface,duration=1200,interval=30,count=20):
    return report.align_and_select(smart,surface,0,duration,interval,count,
        required_smart=('Composite','CPU','NAND'),sample_interval=5,window_s=600)


class SamplingTests(unittest.TestCase):
    def test_video_keeps_240_raw_rows_and_selects_twenty_actual_rows(self):
        smart,surface=records();before=copy.deepcopy((smart,surface))
        aligned,selected,errors=select(smart,surface)
        self.assertEqual((len(aligned),len(selected),len(errors)),(240,20,0))
        self.assertEqual([r['epoch'] for r in selected],list(range(0,600,30)))
        self.assertEqual([r['source_row'] for r in selected],[2+i*6 for i in range(20)])
        self.assertEqual((smart,surface),before)
        self.assertTrue(all(v['count']==20 for v in report.calculate_statistics(selected).values()))

    def test_composite_score_and_all_statistics_use_only_selected_points(self):
        smart,surface=records()
        for r in smart:
            r['Composite']=40+r['epoch']/60 if r['epoch']<600 else 55
            r['CPU']=100 if r['epoch']%30 else 47
        _,chosen,_=select(smart,surface)
        self.assertEqual(chosen[0]['epoch'],600)
        self.assertEqual(report.calculate_statistics(chosen)['CPU']['max'],47)

    def test_missing_raw_between_plot_points_invalidates_affected_window(self):
        smart,surface=records();smart=[r for r in smart if r['epoch']!=5]
        _,selected,errors=select(smart,surface)
        self.assertEqual(selected[0]['epoch'],10)
        self.assertTrue(any(e['epoch']==5 for e in errors))

    def test_last_raw_point_after_twentieth_plot_point_still_required(self):
        smart,surface=records();smart=[r for r in smart if r['epoch']!=595]
        _,selected,_=select(smart,surface)
        self.assertEqual(selected[0]['epoch'],600)

    def test_slow_old_csv_cannot_masquerade_as_five_second_sampling(self):
        smart,surface=records(cadence=30)
        _,selected,errors=select(smart,surface)
        self.assertEqual(selected,[]);self.assertGreater(len(errors),0)

    def test_bit_uses_120_points_within_600_seconds(self):
        smart,surface=records(900)
        _,selected,errors=select(smart,surface,900,5,120)
        self.assertEqual([r['epoch'] for r in selected],list(range(0,600,5)))
        self.assertFalse(errors)
        self.assertEqual(report.calculate_statistics(selected)['Composite']['count'],120)

    def test_bit_gap_shared_by_every_candidate_prevents_success(self):
        smart,surface=records(900);smart=[r for r in smart if r['epoch']!=400]
        self.assertEqual(select(smart,surface,900,5,120)[1],[])

    def test_accumulated_slow_cadence_is_not_a_ten_minute_window(self):
        smart,surface=records(900)
        for r in smart:r['epoch']*=1.2
        self.assertEqual(select(smart,surface,900,5,120)[1],[])

    def test_idle_legacy_geometry_stays_sixty_seconds_and_ten_points(self):
        smart,surface=records(1500,60)
        _,selected,errors=report.align_and_select(smart,surface,0,1500,60,10)
        self.assertEqual([r['epoch'] for r in selected],list(range(0,600,60)))
        self.assertFalse(errors)

    def test_invalid_geometry_does_not_silently_truncate(self):
        for args in [(5,31,20,600),(5,30,20,570),(5,5,120,900),(0,30,20,600)]:
            with self.subTest(args=args),self.assertRaises(ValueError):report.validate_sampling_geometry(*args)


class UpgradeTests(unittest.TestCase):
    def test_resume_of_legacy_job_keeps_its_original_sampling_rules(self):
        import test_revision_037 as fixture
        from test_revision_0411 import fixture_task, config
        current=config();legacy=copy.deepcopy(current)
        for mode in ('local_video','online_video','bit'):
            item=legacy['tests'][mode];item.pop('window_s');item.pop('plot_interval_s')
            item.update(interval_s=30 if mode=='bit' else 60,points=20 if mode=='bit' else 12)
        with tempfile.TemporaryDirectory() as directory,patch.object(fixture,'configuration',return_value=legacy):
            root=Path(directory);fixture_task(root,list(app.ORDER),['idle','local_video'],cfg=legacy)
            with patch.object(app,'recover_result'),patch.object(app,'finish_resumed_reports',return_value=({},['online_video','bit'])),\
                 patch.object(app,'run_receiver') as execute,patch('builtins.print'),\
                 patch.object(app.ctypes.windll.kernel32,'SetThreadExecutionState',return_value=1):
                app.resume_failed_flow(current,root)
            sent=execute.call_args.args[0]
            self.assertEqual(sent['tests'],legacy['tests'])
            self.assertEqual(current['tests']['bit']['interval_s'],5)

    def test_upgrade_preserves_identity_bindings_and_idle_and_backs_up_once(self):
        cfg=json.loads((app.ROOT/'config.example.json').read_text('utf-8-sig'))
        cfg['config_updates'].pop('smart5_plot30_bit120_20261108',None)
        for mode in ('local_video','online_video','bit'):
            cfg['tests'][mode].update(interval_s=60,points=12)
            cfg['tests'][mode].pop('plot_interval_s');cfg['tests'][mode].pop('window_s')
        cfg['report'].update(project='自定义',batch='保留',expected_serial='REAL-SN')
        before=copy.deepcopy(cfg)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'config.json';app.save_json(path,cfg);raw=path.read_bytes()
            with patch.object(app,'ROOT',root),patch.object(app,'CONFIG',path),patch('builtins.print'):
                result=app.load_config();self.assertEqual(result,app.load_config())
            backups=list(root.rglob('config.before.json'))
            self.assertEqual(len(backups),1);self.assertEqual(backups[0].read_bytes(),raw)
        for key in ('serial','hwinfo','report','network','burnintest','clock_sync','local_video','online_video'):
            self.assertEqual(result[key],before[key],key)
        self.assertEqual(result['tests']['idle'],before['tests']['idle'])
        for mode in app.ORDER:
            self.assertEqual(result['tests'][mode]['duration_s'],before['tests'][mode]['duration_s'])
            self.assertEqual(result['tests'][mode]['wait_before_s'],before['tests'][mode]['wait_before_s'])
        self.assertEqual(result['tests']['bit']['points'],120)

    def test_backup_failure_does_not_write_new_settings(self):
        cfg=json.loads((app.ROOT/'config.example.json').read_text('utf-8-sig'))
        cfg['config_updates'].pop('smart5_plot30_bit120_20261108')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.json';app.save_json(path,cfg);before=path.read_bytes()
            with patch.object(app,'ROOT',path.parent),patch.object(app,'CONFIG',path),patch.object(Path,'mkdir',side_effect=OSError('full')):
                with self.assertRaises(OSError):app.load_config()
            self.assertEqual(path.read_bytes(),before)

    def test_five_second_general_settings_are_written_and_read_back(self):
        captured=CapturedSettings()
        with tempfile.TemporaryDirectory() as directory,patch('pywinauto.Desktop',side_effect=captured.desktop):
            result=app.configure_hwinfo_sampling(captured.sensor,5,Path(directory)/'settings.json')
        self.assertTrue(result['passed'])
        self.assertEqual(result['readback'],dict(global_ms=5000,smart_cycles=1,direct_to_disk=True))

    def test_bit_chart_retains_120_true_points_in_fixed_solid_line_chart(self):
        smart,surface=records(900);chosen=select(smart,surface,900,5,120)[1]
        fig=report.create_selected_points_figure(chosen,'Windows_BIT')
        try:
            ax=fig.axes[0]
            self.assertEqual(len(ax.collections),0)
            self.assertEqual(len(ax.texts),0)
            self.assertEqual(fig.get_figwidth(),10)
            self.assertEqual([len(line.get_xdata()) for line in ax.lines],[120]*6)
        finally:
            import matplotlib.pyplot as plt
            plt.close(fig)


if __name__=='__main__':unittest.main()
