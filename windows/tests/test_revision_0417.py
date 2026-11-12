"""全量原始记录/统一图表/整套首项免等待，均使用离线数据和设备测试桩。"""
import copy
import csv
import json
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import windows_temperature_test as app
import temperature_report as report
import company_template as template
import resume_flow as flow
import test_revision_037 as legacy
from platform_fixtures import fixture_platform_refresh
from test_revision_0411 import fixture_task


def raw_fixture(folder):
    folder.mkdir(parents=True, exist_ok=True)
    settings = legacy.configuration()['hwinfo']
    settings['expected_sensor_group'] = 'S.M.A.R.T.: test disk'
    start = 1700000000
    conditions = dict(mode='bit', start_epoch=start, end_epoch=start+901, interval_s=5,
                      plot_interval_s=5, points=120, window_s=600, hwinfo=settings, workload_status='完成计时')
    app.save_json(folder/'conditions.json', conditions)
    header = ['Date','Time','T1','T2','T3','Other SMART']
    with (folder/'hwinfo.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream);writer.writerow(header)
        for i, second in enumerate(range(-20, 911, 5)):
            when = datetime.fromtimestamp(start+second, app.BJ)
            writer.writerow([when.strftime('%d.%m.%Y'),when.strftime('%H:%M:%S.%f'),45+i%2,47,42,'RAW-'+str(i)])
        writer.writerow(header)
        writer.writerow(['','','S.M.A.R.T.: test disk','S.M.A.R.T.: test disk','S.M.A.R.T.: test disk',''])
    # 兼容旧版本：无source_row，并包含正式段前后的每秒记录。
    rows = [dict(epoch=start+second, slot=second+25, CPU_Surface=48, NAND_Surface=43, AMB=24)
            for second in range(-25, 921)]
    (folder/'jk808.jsonl').write_bytes(('\r\n'.join(json.dumps(row) for row in rows)+'\r\n').encode('utf-8'))
    return conditions


class RawDataTests(unittest.TestCase):
    def test_all_187_smart_rows_formal_181_selected_120_and_raw_bytes(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);stage=root/'bit';conditions=raw_fixture(stage)
            before={name:(stage/name).read_bytes() for name in ('hwinfo.csv','jk808.jsonl','conditions.json')}
            result,png=report.export_stage(stage,root)
            self.assertEqual(result['selected_count'],120)
            raw=result['raw_data']
            self.assertEqual((raw['SMART全部原始记录'],raw['SMART正式段记录'],raw['SMART选中点']),(187,181,120))
            self.assertEqual((raw['JK808全程原始记录'],raw['JK808本项原始记录'],raw['JK808正式段记录']),(946,931,901))
            with (stage/'HWiNFO_SMART全部原始记录_中文.csv').open(encoding='utf-8-sig') as stream:
                rows=list(csv.DictReader(stream))
            self.assertEqual(len(rows),187)
            self.assertEqual(rows[0]['时段'],'测试前');self.assertEqual(rows[-1]['时段'],'测试后')
            self.assertEqual(rows[-1]['原始列6：Other SMART'],'RAW-186')
            with (stage/'JK808全程原始记录_中文.csv').open(encoding='utf-8-sig') as stream:
                surface=list(csv.DictReader(stream))
            marked=[row for row in surface if row['选中绘图统计点']=='是']
            self.assertEqual(len(marked),120)
            self.assertEqual(float(marked[0]['Unix时间戳']),conditions['start_epoch'])
            self.assertEqual(float(marked[-1]['Unix时间戳']),conditions['start_epoch']+595)
            html=(stage/'原始数据查看.html').read_text('utf-8')
            self.assertLess(html.index('<h2>HWiNFO SMART'),html.index('<h2>JK808'))
            self.assertIn('Drive Temperature 2',html);self.assertIn('SSD主控',html)
            self.assertIn('全部 187 条 / 正式段 181 条 / 选中 120 点',html)
            self.assertIn('原始数据查看.html',(root/'Windows_BIT.html').read_text('utf-8'))
            self.assertEqual(before,{name:(stage/name).read_bytes() for name in before})
            with Image.open(png) as image:self.assertEqual(image.size,(3000,1800))
            book=root/'book.xlsx';template.create_empty_company_template(app.ROOT/'温度模版_原始.xlsx',book)
            template.fill_windows_result(book,'1T','bit',result['statistics'],{},png,120,0)
            self.assertEqual(template.read_xlsx_parts(book)['xl/media/auto_windows_2_bit.png'],png.read_bytes())

    def test_startup_failure_still_shows_existing_full_smart_without_formal_fabrication(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);stage=root/'bit';conditions=raw_fixture(stage)
            (stage/'conditions.json').unlink()
            app.save_json(root/'config_snapshot.json',{'hwinfo':conditions['hwinfo']})
            app.save_json(stage/'stage_failure.json',dict(mode='bit',formal_started=False,error='启动取消'))
            result,png=report.export_stage(stage,root)
            self.assertEqual(result['status'],'启动失败');self.assertIsNone(png)
            self.assertEqual(result['raw_data']['SMART全部原始记录'],187)
            self.assertEqual(result['raw_data']['SMART正式段记录'],0)
            self.assertEqual(result['raw_data']['SMART选中点'],0)
            self.assertIn('正式开始',result['raw_data'])
            self.assertIn('正式时段未知',(stage/'原始数据查看.html').read_text('utf-8'))

    def test_missing_raw_and_bad_json_are_identified_without_fake_measurements(self):
        with tempfile.TemporaryDirectory() as directory:
            stage=Path(directory)
            (stage/'jk808.jsonl').write_text('{broken}\n','utf-8')
            raw=report.write_readable_raw_data(stage)
            self.assertEqual(raw['SMART全部原始记录'],0)
            self.assertEqual(raw['JK808全程原始记录'],1)
            self.assertEqual(raw['JK808本项原始记录'],0)
            self.assertTrue(any('不能补造SMART' in issue['问题'] for issue in raw['问题']))
            self.assertTrue(any('无法解析' in issue['问题'] for issue in raw['问题']))

    def test_readable_copy_write_failure_keeps_completed_statistics(self):
        with tempfile.TemporaryDirectory() as directory:
            stage=Path(directory)/'bit';raw_fixture(stage)
            with patch.object(report,'write_readable_raw_data',side_effect=OSError('disk full')):
                result,_=report.export_stage(stage,stage/'复算结果')
            self.assertTrue(result['workload_ok']);self.assertEqual(result['selected_count'],120)
            self.assertIn('disk full',result['raw_data_error'])
            self.assertEqual(result['data_issue_count'],0)

    def test_atomic_copy_repairs_partial_stage_and_preserves_canonical_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cfg=fixture_task(root,['idle'],['idle'])
            source=root/'手工复测/jk808.jsonl';stage=root/'手工复测/主机数据/idle/jk808.jsonl'
            before=source.read_bytes();stage.write_bytes(before[:100])
            plan=flow.plan(root)
            self.assertEqual(plan[-1],[])
            self.assertEqual(stage.read_bytes(),before);self.assertEqual(source.read_bytes(),before)
            stamp=stage.stat().st_mtime_ns;report.sync_surface_source(source,stage)
            self.assertEqual(stage.stat().st_mtime_ns,stamp)


class FirstWaitTests(unittest.TestCase):
    def test_job_flag_only_new_formal_full_order_and_no_persistent_config_changes(self):
        for modes,kind,resume,expected in [(list(app.ORDER),'formal',False,True),
                (list(app.ORDER),'formal',True,False),(['online_video'],'formal',False,False),
                (list(app.ORDER),'commissioning',False,False)]:
            with self.subTest(modes=modes,kind=kind,resume=resume),tempfile.TemporaryDirectory() as directory:
                cfg=legacy.configuration();cfg['host_workspace']=directory
                cfg['tests']['online_video']['wait_before_s']=200
                before=copy.deepcopy(cfg);host=app.HostController(cfg)
                with patch.object(app,'preflight_host',return_value=[]),patch.object(app,'workload_window_problems',return_value=[]),\
                        patch.object(app,'recover_pending_network'),patch.object(app.threading,'Thread') as thread,\
                        patch.object(app,'refresh_test_platform',side_effect=fixture_platform_refresh):
                    host.handle('start',dict(job_id='a'*32,config=cfg,modes=modes,kind=kind,resume=resume))
                self.assertEqual(thread.call_args.kwargs['args'][2],expected)
                self.assertEqual(cfg,before)

    def test_skipped_wait_still_completes_hwinfo_and_actual_playback_readiness_before_clock(self):
        for skip in (False,True):
            with self.subTest(skip=skip),tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
                cfg=legacy.configuration();cfg['tests']['online_video']['duration_s']=0
                cfg['tests']['online_video']['wait_before_s']=200
                original=copy.deepcopy(cfg['tests']);clock=legacy.Clock();actions=[]
                csv_path=Path(directory)/'hw.csv';csv_path.write_text('sample','utf-8')
                def wait(seconds,*args):actions.append('wait');clock.advance(seconds)
                def logging(*args):actions.append('logging');clock.advance(7);return csv_path
                def ready(*args,**kwargs):actions.append('actual_playback_ready');clock.advance(13);return {'page':Mock()}
                replacements={'apply_network_state':Mock(return_value=[]),'wait_seconds':Mock(side_effect=wait),
                    'launch_hwinfo':Mock(side_effect=lambda *args:actions.append('launch')),
                    'start_hwinfo_logging':Mock(side_effect=logging),'minimize_owned_hwinfo':Mock(),
                    'start_online_video':Mock(side_effect=ready),'stop_online_video':Mock(),'stop_hwinfo_logging':Mock()}
                for name,value in replacements.items():stack.enter_context(patch.object(app,name,value))
                stack.enter_context(patch.object(app.time,'time',clock.time));stack.enter_context(patch.object(app.time,'monotonic',clock.monotonic))
                folder=Path(directory)/'online_video'
                app.execute_stage(cfg,'online_video',folder,threading.Event(),{},[],skip_prepare_wait=skip)
                conditions=json.loads((folder/'conditions.json').read_text('utf-8'))
                self.assertEqual(conditions['start_epoch'],1700000000+20+(0 if skip else 200))
                self.assertEqual(conditions['prepare_wait_s'],0 if skip else 200)
                self.assertEqual(actions,(['wait'] if not skip else [])+['launch','logging','actual_playback_ready'])
                self.assertEqual(cfg['tests'],original)

    def test_full_suite_timing_and_real_transfer_reports_with_first_wait_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg,clock,actions,host=legacy.WholeFlowTests().simulate(directory,skip_first_wait=True)
            self.assertEqual(host.status['phase'],'完成')
            self.assertEqual([a[1] for a in actions if a[0]=='wait'],[120,120,120])
            self.assertEqual(clock.now,4800+3*120)
            class Link:
                def request(self,command,data=None,**kwargs):return host.handle(command,data or {})
            root=Path(directory)/'receiver';manual=root/'手工复测'
            app.receive_files(Link(),manual/'主机数据',host.job_id)
            (manual/'jk808.jsonl').write_text(''.join(json.dumps(dict(epoch=1700000000+i+.25,slot=i,
                CPU_Surface=48,NAND_Surface=43,AMB=24))+'\n' for i in range(int(clock.now))),'utf-8')
            results=app.finish_reports(cfg,root,list(app.ORDER))
            self.assertEqual([results[m]['selected_count'] for m in app.ORDER],[20,10,20,120])
            self.assertTrue(all(results[m]['workload_ok'] for m in app.ORDER))
            for mode in app.ORDER:
                self.assertTrue((manual/'主机数据'/mode/'原始数据查看.html').is_file())


if __name__=='__main__':unittest.main()
