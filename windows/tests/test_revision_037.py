"""整段流程回归：设备/窗口/时钟用测试桩，真实执行文件回传、选点和Excel填表。"""
import copy
import csv
import json
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import windows_temperature_test as app
import temperature_report as report
import company_template as template


def configuration():
    cfg=json.loads((app.ROOT/'config.example.json').read_text('utf-8-sig'))
    cfg['report']['capacity']='1T'
    hw=cfg['hwinfo']
    hw.pop('expected_header',None);hw.pop('expected_sensor_group',None)
    hw.update(encoding='utf-8-sig',delimiter=',',date_column='Date',time_column='Time',
        datetime_format='%d.%m.%Y %H:%M:%S.%f',decimal_mark='.',
        columns={'Composite':'T1','CPU':'T2','NAND':'T3'})
    return cfg


class Clock:
    def __init__(self):self.now=0.0
    def monotonic(self):return self.now
    def time(self):return 1700000000+self.now
    def advance(self,seconds):self.now+=seconds


class BrowserTests(unittest.TestCase):
    def wait_quality(self, side_effect, *, commissioning=True, visible=True, cancelled=False):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        clock=Clock();page=Mock();video=page.locator.return_value.first
        video.is_visible.return_value=visible
        state={'ready':False}
        video.evaluate.side_effect=lambda *a: {'width':1920,'height':1080} if state['ready'] else {'width':640,'height':360}
        page.wait_for_timeout.side_effect=lambda ms:clock.advance(ms/1000)
        cfg=configuration();cfg['_commissioning']=commissioning
        stop=Mock();stop.is_set.side_effect=lambda:cancelled or clock.now>=600
        responses=iter(side_effect) if isinstance(side_effect,list) else None
        def choose(*a,**k):
            result=next(responses) if responses is not None else side_effect
            if isinstance(result,Exception):raise result
            state['ready']=True
        with patch.object(app.time,'monotonic',clock.monotonic),\
             patch.object(app,'bilibili_quality_state',return_value={'selected':'80','label':'1080P'}),\
             patch.object(app,'select_bilibili_1080',side_effect=choose) as select:
            result=app.wait_for_bilibili_quality(page,cfg,stop,{})
        return result,video,select,clock

    def test_login_overlay_on_first_selection_can_clear_and_continue(self):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        result,video,select,clock=self.wait_quality([BrowserTimeout('overlay'),None])
        self.assertIs(result,video);self.assertEqual(select.call_count,2)
        self.assertGreaterEqual(clock.now,1)
        self.assertLess(clock.now,3)

    def test_visible_default_video_does_not_bypass_quality_selection(self):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        with self.assertRaises(InterruptedError):
            self.wait_quality(BrowserTimeout('blocked'),commissioning=False)

    def test_unloaded_video_waits_until_cancelled(self):
        with self.assertRaises(InterruptedError):
            self.wait_quality(None,commissioning=False,visible=False)
        with self.assertRaises(InterruptedError):self.wait_quality(None,cancelled=True)

    def test_changed_quality_option_is_not_hidden_by_retry(self):
        with self.assertRaisesRegex(RuntimeError,'page changed'):
            self.wait_quality(RuntimeError('page changed'))

    def test_minimize_uses_owned_window_pid_and_handle(self):
        cfg=configuration();cfg['hwinfo']['ui']['_logging_session']={'pid':12,'handle':345}
        desktop=Mock()
        with patch('pywinauto.Desktop',return_value=desktop),patch.object(app,'ui_window') as arbitrary:
            app.minimize_owned_hwinfo(cfg)
        desktop.window.assert_called_once_with(handle=345,process=12)
        desktop.window.return_value.minimize.assert_called_once();arbitrary.assert_not_called()


def write_fixture(folder,mode='idle'):
    cfg=configuration();setting=cfg['tests'][mode];start=1700000000
    folder.mkdir(parents=True,exist_ok=True)
    conditions={'mode':mode,'start_epoch':start,'end_epoch':start+setting['duration_s'],
        'interval_s':setting['interval_s'],'points':setting['points'],
        'hwinfo':cfg['hwinfo'],'workload_status':'完成计时'}
    app.save_json(folder/'conditions.json',conditions)
    with (folder/'hwinfo.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['Date','Time','T1','T2','T3'])
        for second in range(0,setting['duration_s'],setting['interval_s']):
            when=datetime.fromtimestamp(start+second,app.BJ)
            writer.writerow([when.strftime('%d.%m.%Y'),when.strftime('%H:%M:%S.%f'),45,47,42])
    raw=''.join(json.dumps({'epoch':start+i,'slot':i,'CPU_Surface':48,'NAND_Surface':43,'AMB':24})+'\n'
        for i in range(setting['duration_s']))
    (folder/'jk808.jsonl').write_text(raw,'utf-8')
    return cfg,raw


def good_host_status(folder):
    app.save_json(folder/'status.json',{'phase':'完成','message':'本批次完成'})
    app.save_json(folder/'task.json',{'finished':True})
    app.save_json(folder/'network_after_restore.json',{'restored':True})


class ReportTests(unittest.TestCase):
    def test_corrupt_csv_invalidates_existing_chart_html_and_statistics(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);stage=root/'idle';write_fixture(stage)
            report.export_stage(stage,root)
            original=(stage/'hwinfo.csv').read_bytes()
            (stage/'hwinfo.csv').write_text('wrong,column\n','utf-8')
            with self.assertRaises(ValueError):report.export_stage(stage,root)
            self.assertFalse((root/'Windows_待机.png').exists())
            self.assertNotIn('<img', (root/'Windows_待机.html').read_text('utf-8'))
            result=json.loads((stage/'statistics.json').read_text('utf-8'))
            self.assertEqual(result['statistics'],{})
            self.assertFalse((stage/'选中绘图及统计点.csv').exists())
            self.assertEqual((stage/'hwinfo.csv').read_text('utf-8'),'wrong,column\n')
            (stage/'hwinfo.csv').write_bytes(original)
            self.assertEqual(report.export_stage(stage,root)[0]['selected_count'],10)

    def test_missing_conditions_removes_only_this_stage_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);stage=root/'idle';write_fixture(stage)
            report.export_stage(stage,root)
            other=root/'Windows_BIT.png';other.write_bytes(b'other stage')
            (stage/'conditions.json').unlink()
            with self.assertRaises(FileNotFoundError):report.export_stage(stage,root)
            self.assertFalse((root/'Windows_待机.png').exists())
            self.assertEqual(other.read_bytes(),b'other stage')

    def test_failed_rerun_clears_only_target_cells_and_image_in_real_xlsx(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);host=root/'手工复测/主机数据'
            cfg,raw=write_fixture(host/'idle');good_host_status(host)
            (root/'手工复测/jk808.jsonl').write_text(raw,'utf-8')
            app.finish_reports(cfg,root,['idle'])
            workbook=root/app.report_filename(cfg,'温度汇总','.xlsx')
            template.fill_windows_result(workbook,'1T','local_video',{'Composite':{'max':55,'mean':54,'min':53}},cfg['report'],None,12,0)
            (host/'idle/hwinfo.csv').unlink()
            result=app.finish_reports(cfg,root,['idle'])
            self.assertEqual(result['idle']['status'],'报告生成失败')
            parts=template.read_xlsx_parts(workbook)
            sheet=ET.fromstring(parts['xl/worksheets/sheet2.xml']);ns={'s':template.S}
            self.assertIsNone(sheet.find('.//s:c[@r="C11"]/s:v',ns))
            self.assertEqual(sheet.find('.//s:c[@r="C20"]/s:v',ns).text,'55')
            self.assertNotIn('xl/media/auto_windows_2_idle.png',parts)
            self.assertIn('报告生成失败',(root/app.report_filename(cfg,'本次结果','.html')).read_text('utf-8'))

    def test_excel_busy_is_reported_while_raw_data_and_html_remain(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);host=root/'手工复测/主机数据'
            cfg,raw=write_fixture(host/'idle');good_host_status(host)
            (root/'手工复测/jk808.jsonl').write_text(raw,'utf-8')
            with patch.object(template,'fill_windows_result',side_effect=PermissionError('Excel busy')):
                result=app.finish_reports(cfg,root,['idle'])
            self.assertEqual(result['idle']['selected_count'],10)
            self.assertIn('Excel未更新',(root/app.report_filename(cfg,'本次结果','.html')).read_text('utf-8'))
            self.assertTrue((host/'idle/hwinfo.csv').is_file())

    def test_task_cannot_pass_with_pending_network_or_unfinished_record(self):
        good={'idle':{'status':'有效选点','workload_ok':True,'selected_count':10,'data_issue_count':0}}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);host=root/'手工复测/主机数据';good_host_status(host)
            cfg=configuration()
            self.assertTrue(app.write_task_report(cfg,root,good)['completed'])
            app.save_json(host/'network_restore_pending.json',{})
            self.assertFalse(app.write_task_report(cfg,root,good)['completed'])
            (host/'network_restore_pending.json').unlink()
            app.save_json(host/'task.json',{'finished':False})
            self.assertFalse(app.write_task_report(cfg,root,good)['completed'])

    def test_missing_or_corrupt_host_status_is_not_silently_successful(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);host=root/'手工复测/主机数据';host.mkdir(parents=True)
            for text in ('','{broken'):
                (host/'status.json').write_text(text,'utf-8')
                self.assertFalse(app.write_task_report(configuration(),root,{})['completed'])


class WholeFlowTests(unittest.TestCase):
    def simulate(self, directory, *, cleanup_failure=False, cancel=False, restore_failure=False, online_fps=30, online_quality=None):
        cfg=configuration();clock=Clock();actions=[];logging={}
        if online_quality is not None: cfg['online_video']['adapter']='bilibili_1080'
        cfg['host_workspace']=str(Path(directory)/'jobs')
        host=app.HostController(cfg);host.job_id='d'*32
        host.folder=Path(cfg['host_workspace'])/host.job_id
        app.save_json(host.folder/'task.json',{'finished':False,'station':cfg['station'],'job_id':host.job_id})
        def flush_csv():
            if not logging:return
            with logging['path'].open('a',encoding='utf-8',newline='') as stream:
                writer=csv.writer(stream)
                while logging['next']<clock.now:
                    stamp=datetime.fromtimestamp(1700000000+logging['next'],app.BJ)
                    writer.writerow([stamp.strftime('%d.%m.%Y'),stamp.strftime('%H:%M:%S.%f'),45,47,42])
                    logging['next']+=logging['interval']
        def wait(seconds):
            clock.advance(5);flush_csv()
            return cancel and clock.now>=30
        host.stop=Mock();host.stop.wait.side_effect=wait;host.stop.is_set.return_value=False
        def start_csv(c,folder,interval):
            path=folder/'hwinfo.csv';path.write_text('Date,Time,T1,T2,T3\n','utf-8')
            logging.update(path=path,next=clock.now+.25,interval=interval)
            actions.append(('log_start',folder.name,interval));return path
        def stop_csv(c):
            actions.append(('log_stop',));logging.clear()
            if cleanup_failure:raise RuntimeError('stop failed')
        def start_bit(c,folder,duration,**kwargs):
            actions.append(('bit_start',duration))
            start=clock.now;process=Mock();process.poll.side_effect=lambda:0 if clock.now>=start+duration else None
            return {'adapter':'bit9_script','process':process,'start_epoch':clock.time(),
                    'start_mono':clock.now,'end_epoch':clock.time()+duration}
        def stop_video(workload):actions.append(('video_stop',))
        fake_com=types.SimpleNamespace(CoInitialize=lambda:None,CoUninitialize=lambda:None)
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules,{'pythoncom':fake_com}))
            stack.enter_context(patch.object(app.ctypes.windll.kernel32,'SetThreadExecutionState',return_value=1))
            replacements={
                'network_snapshot':Mock(return_value=[]),'remember_wifi_profiles':lambda x:x,
                'network_state_problems':Mock(return_value=[]),
                'apply_network_state':lambda state,*args:actions.append(('network',state)) or [],
                'restore_network':Mock(side_effect=RuntimeError('restore failed')) if restore_failure else Mock(return_value={'restored':True,'adapters':[]}),
                'wait_seconds':lambda seconds,*args:(actions.append(('wait',seconds)),clock.advance(seconds)),
                'launch_hwinfo':Mock(),'minimize_owned_hwinfo':Mock(),
                'start_hwinfo_logging':start_csv,'stop_hwinfo_logging':stop_csv,
                'start_local_video':lambda c,**kw:actions.append(('local_start',)) or {'profile':{}},
                'start_online_video':lambda *a,**k:actions.append(('online_start',)) or {'page':Mock()},
                'local_video_window_is_open':lambda *a:True,
                'online_video_status':lambda *a:dict(position=clock.now,width=1920,height=1080,
                    observed_fps=online_fps,playback_rate=1,paused=False,ended=False,
                    quality=online_quality,frame_span_s=2,
                    fullscreen={'active':True,'contains_video':True,'fills_viewport':True}),
                'stop_local_video':stop_video,'stop_online_video':stop_video,
                'start_bit':start_bit,'stop_bit_script':lambda w:actions.append(('bit_stop',)),
                'collect_bit_result':lambda w,**k:{'start_epoch':w['start_epoch'],'end_epoch':w['end_epoch'],'passed':True}}
            for name,value in replacements.items():stack.enter_context(patch.object(app,name,value))
            stack.enter_context(patch.object(app.time,'time',clock.time))
            stack.enter_context(patch.object(app.time,'monotonic',clock.monotonic))
            host.run_job(cfg,list(app.ORDER))
        return cfg,clock,actions,host

    def test_four_stages_full_timing_transfer_and_company_workbook(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg,clock,actions,host=self.simulate(directory)
            self.assertEqual(host.status['phase'],'完成')
            self.assertEqual([a[1] for a in actions if a[0]=='network'],['online','offline','offline','offline'])
            self.assertEqual([a[1] for a in actions if a[0]=='log_start'],['online_video','idle','local_video','bit'])
            self.assertEqual([a[1] for a in actions if a[0]=='wait'],[120,120,120,120])
            self.assertEqual([a[2] for a in actions if a[0]=='log_start'],[60,60,60,30])
            self.assertEqual(len([a for a in actions if a[0]=='log_stop']),4)
            self.assertEqual(clock.now,1500+1200+1200+900+4*120)
            class Link:
                def request(self,command,data=None,**kwargs):return host.handle(command,data or {})
            result=Path(directory)/'receiver';manual=result/'手工复测'
            app.receive_files(Link(),manual/'主机数据',host.job_id)
            raw=''.join(json.dumps({'epoch':1700000000+i+.25,'slot':i,
                'CPU_Surface':48,'NAND_Surface':43,'AMB':24})+'\n' for i in range(int(clock.now)))
            (manual/'jk808.jsonl').write_text(raw,'utf-8')
            summaries=app.finish_reports(cfg,result,list(app.ORDER))
            self.assertEqual([summaries[m]['selected_count'] for m in app.ORDER],[12,10,12,20])
            self.assertTrue(json.loads((manual/'任务结果.json').read_text('utf-8'))['completed'])
            parts=template.read_xlsx_parts(result/app.report_filename(cfg,'温度汇总','.xlsx'))
            for mode in app.ORDER:
                self.assertIn(f'xl/media/auto_windows_2_{mode}.png',parts)
                self.assertEqual(summaries[mode]['statistics']['Composite']['mean'],45)
                with (manual/'主机数据'/mode/'选中绘图及统计点.csv').open(encoding='utf-8-sig') as stream:
                    rows=list(csv.DictReader(stream))
                self.assertEqual(float(rows[0]['plot_second']),0)
                self.assertEqual(float(rows[-1]['plot_second']), (len(rows)-1)*cfg['tests'][mode]['interval_s'])
            self.assertEqual(sorted(p.suffix for p in result.iterdir() if p.is_file()),['.html']*5+['.png']*4+['.xlsx'])

    def test_stop_failure_prevents_second_load_and_preserves_restore_record(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg,clock,actions,host=self.simulate(directory,cleanup_failure=True)
            self.assertEqual(host.status['phase'],'未完成')
            self.assertFalse((host.folder/'local_video').exists())
            self.assertTrue((host.folder/'online_video/cleanup_errors.json').exists())
            self.assertFalse((host.folder/'network_restore_pending.json').exists())

    def test_cancel_stops_current_logging_and_restores_network(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg,clock,actions,host=self.simulate(directory,cancel=True)
            self.assertEqual(host.status['phase'],'未完成')
            self.assertIn(('log_stop',),actions)
            self.assertFalse((host.folder/'local_video').exists())
            self.assertFalse((host.folder/'network_restore_pending.json').exists())

    def test_final_restore_failure_preserves_pending_even_after_all_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg,clock,actions,host=self.simulate(directory,restore_failure=True)
            self.assertEqual(host.status['phase'],'未完成')
            self.assertEqual(len([a for a in actions if a[0]=='log_stop']),4)
            self.assertTrue((host.folder/'network_restore_pending.json').exists())


if __name__=='__main__':unittest.main()


# Historical single-video fixtures isolate current-video selection.
def setUpModule():
    global _video_selector
    _video_selector=patch.object(app, "active_online_video", side_effect=lambda page:page.locator("video").first)
    _video_selector.start()
    global _play_request
    _play_request=patch.object(app,"request_online_playback",return_value="fixture request")
    _play_request.start()

def tearDownModule():
    _video_selector.stop()
    _play_request.stop()
