"""网页错误重试、不同容量文件名、真实CSV续测合并与换盘拦截（不运行硬件负载）。"""
import copy
from contextlib import ExitStack
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import windows_temperature_test as app
import resume_flow as flow
import temperature_report as report
from test_revision_037 import write_fixture, configuration, good_host_status


LEGACY_ORDER = ('idle', 'local_video', 'online_video', 'bit')

def config():
    cfg=configuration()
    cfg['report'].update(project='2260',batch='10-21',capacity='1T',
                         system_disk_model='XP2260MD001T',expected_serial='DISK-001')
    return cfg


class RetryTests(unittest.TestCase):
    def page(self):
        page=Mock()
        page.url='https://www.bilibili.com/video/test'
        page.is_closed.return_value=False
        page.goto.return_value=Mock(status=200)
        page.reload.return_value=Mock(status=200)
        page.evaluate.return_value={'url':page.url,'title':'视频','text':'视频页面','player':True}
        return page

    def test_first_failure_then_refresh_succeeds_without_formal_timer(self):
        from playwright.sync_api import TimeoutError
        page=self.page();page.goto.side_effect=TimeoutError('net timeout');state={}
        app.open_online_video_page(page,config(),status=state)
        self.assertEqual(page.goto.call_count,1);self.assertEqual(page.reload.call_count,1)
        self.assertFalse(state['waiting_for_user']);self.assertNotIn('start_epoch',state)

    def test_exactly_five_retries_after_first_failure_then_sixth_attempt_succeeds(self):
        from playwright.sync_api import Error
        page=self.page();page.goto.side_effect=Error('net failed')
        page.reload.side_effect=[Error('net failed')]*4+[Mock(status=200)]
        cfg=config();cfg['online_video']['page_retry_count']=1
        with tempfile.TemporaryDirectory() as directory:
            cfg['_diagnostic_folder']=directory
            app.open_online_video_page(page,cfg)
            history=flow.read_json(Path(directory)/'网页打开重试.json')
        self.assertEqual(page.reload.call_count,5)
        self.assertEqual(len(history),6);self.assertTrue(history[-1]['passed'])

    def test_all_six_fail_preserves_last_error(self):
        from playwright.sync_api import Error
        page=self.page();page.goto.side_effect=page.reload.side_effect=Error('ERR_CONNECTION_RESET')
        with self.assertRaisesRegex(RuntimeError,'5次刷新重试均失败'):
            app.open_online_video_page(page,config())
        self.assertEqual(page.reload.call_count,5)

    def test_http_503_and_browser_error_document_are_retried(self):
        for http in (True,False):
            with self.subTest(http=http):
                page=self.page()
                if http:page.goto.return_value.status=503
                else:page.evaluate.side_effect=[dict(url=page.url,title='无法访问此网站',text='ERR_FAILED',player=False),
                                               dict(url=page.url,title='视频',text='登录',player=False)]
                app.open_online_video_page(page,config())
                self.assertEqual(page.reload.call_count,1)

    def test_failed_initial_navigation_about_blank_uses_goto_again(self):
        from playwright.sync_api import Error
        page=self.page();page.url='about:blank';page.goto.side_effect=[Error('failed'),Mock(status=200)]
        app.open_online_video_page(page,config())
        self.assertEqual(page.goto.call_count,2);page.reload.assert_not_called()

    def test_login_page_does_not_refresh_or_start_timer(self):
        page=self.page();page.evaluate.return_value.update(player=False,text='请扫码登录')
        app.open_online_video_page(page,config())
        page.reload.assert_not_called()

    def test_cancel_during_retry_wait_stops_without_another_navigation(self):
        from playwright.sync_api import Error
        page=self.page();page.goto.side_effect=Error('failed')
        stop=Mock();stop.is_set.return_value=False;stop.wait.return_value=True
        with self.assertRaises(InterruptedError):app.open_online_video_page(page,config(),stop)
        page.reload.assert_not_called()

    def test_cancel_before_open_and_closed_browser_are_not_hidden(self):
        page=self.page();stop=threading.Event();stop.set()
        with self.assertRaises(InterruptedError):app.open_online_video_page(page,config(),stop)
        page.goto.assert_not_called()
        from playwright.sync_api import Error
        page.goto.side_effect=Error('closed');page.is_closed.return_value=True
        with self.assertRaisesRegex(RuntimeError,'浏览器已关闭'):app.open_online_video_page(page,config())
        page.reload.assert_not_called()


def fixture_task(root,modes,passed,*,cfg=None,token='a',surface=48):
    cfg=copy.deepcopy(cfg or config());manual=root/'手工复测';host=manual/'主机数据'
    for mode in modes:
        _,raw=write_fixture(host/mode,mode)
        raw=raw.replace('"CPU_Surface": 48','"CPU_Surface": '+str(surface))
        # The idle fixture spans all stage durations and supplies full raw thermometer coverage.
        if mode not in passed:
            path=host/mode/'conditions.json';data=flow.read_json(path)
            data['workload_status']='未完成：页面打开失败';app.save_json(path,data)
    _,raw=write_fixture(manual/'fixture_idle')
    import shutil
    assert (manual/'fixture_idle').resolve().is_relative_to(manual.resolve())
    shutil.rmtree(manual/'fixture_idle')  # unittest-owned temporary directory only.
    raw=raw.replace('"CPU_Surface": 48','"CPU_Surface": '+str(surface))
    (manual/'jk808.jsonl').write_text(raw,'utf-8')
    good_host_status(host)
    app.save_json(manual/'本次任务.json',{'job_id':token*32,'config':cfg,'modes':list(modes)})
    app.save_json(host/'task.json',{'job_id':token*32,'station':cfg['station'],'finished':True})
    app.save_json(host/'config_snapshot.json',cfg)
    app.save_json(host/'文件校验.json',[])
    app.save_json(manual/'接收进度.json',{'phase':'received','job_id':token*32})
    if set(modes)-set(passed):app.save_json(host/'status.json',{'phase':'未完成','message':'页面打开失败'})
    return cfg


class ResumeTests(unittest.TestCase):
    def setUp(self):
        quiet=patch('sys.stdout',io.StringIO());quiet.start();self.addCleanup(quiet.stop)

    def test_resume_keeps_first_two_stages_and_retries_online_then_bit(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cfg=fixture_task(root,LEGACY_ORDER,LEGACY_ORDER[:2])
            def run(retry,modes,*,resume_folder):
                self.assertEqual(modes,['online_video','bit']);self.assertEqual(resume_folder,root)
                child=root/'手工复测/续测记录/second'
                fixture_task(child,modes,modes,cfg=retry,token='b',surface=70)
                flow.add_attempt(root,child,app.save_json)
            original=(root/'手工复测/主机数据/idle/hwinfo.csv').read_bytes()
            with patch.object(app,'run_receiver',side_effect=run) as start,patch.object(app,'recover_result'):
                app.resume_failed_flow(cfg,root)
            start.assert_called_once()
            stats=flow.read_json(root/'手工复测/结果汇总.json')
            self.assertEqual([stats[m]['selected_count'] for m in LEGACY_ORDER],[10,12,12,20])
            self.assertEqual(stats['idle']['statistics']['CPU_Surface']['mean'],48)
            self.assertEqual(stats['online_video']['statistics']['CPU_Surface']['mean'],70)
            self.assertTrue(flow.read_json(root/'手工复测/任务结果.json')['completed'])
            self.assertEqual((root/'手工复测/主机数据/idle/hwinfo.csv').read_bytes(),original)
            self.assertIn('续测数据来源',(root/'本次结果_1T.html').read_text('utf-8'))
            self.assertTrue((root/'温度汇总_1T.xlsx').exists())

    def test_second_failed_attempt_keeps_newly_completed_stage_for_next_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cfg=fixture_task(root,LEGACY_ORDER,['idle'])
            child=root/'手工复测/续测记录/second'
            fixture_task(child,LEGACY_ORDER[1:],['local_video'],cfg=cfg,token='b')
            flow.add_attempt(root,child,app.save_json)
            task,paths,sources,pending=flow.plan(root)
            self.assertEqual(pending,['online_video','bit']);self.assertEqual(sources['idle'],root)
            self.assertEqual(sources['local_video'],child)

    def test_failed_new_attempt_does_not_replace_good_prior_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cfg=fixture_task(root,['idle'],['idle'])
            child=root/'手工复测/续测记录/second'
            fixture_task(child,['idle'],[],cfg=cfg,token='b')
            flow.add_attempt(root,child,app.save_json)
            _,_,sources,pending=flow.plan(root)
            self.assertEqual(pending,[]);self.assertEqual(sources['idle'],root)

    def test_transfer_or_excel_only_failure_does_not_rerun_completed_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cfg=fixture_task(root,['idle'],['idle'])
            with patch.object(app,'run_receiver') as run,patch.object(app,'recover_result') as recover:
                app.resume_failed_flow(cfg,root)
            run.assert_not_called();recover.assert_called_once_with(cfg,root,raw_only=True)

    def test_incomplete_transfer_is_not_considered_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);fixture_task(root,['idle'],['idle'])
            (root/'手工复测/主机数据/文件校验.json').unlink()
            self.assertEqual(flow.plan(root)[-1],['idle'])
            summaries,pending=app.finish_resumed_reports(root)
            self.assertFalse(summaries['idle']['workload_ok'])
            self.assertFalse(flow.read_json(root/'手工复测/任务结果.json')['completed'])

    def test_corrupt_data_or_missing_surface_requires_retry_even_with_old_good_summary(self):
        for path in ('主机数据/idle/hwinfo.csv','jk808.jsonl'):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);fixture_task(root,['idle'],['idle'])
                (root/'手工复测'/path).unlink()
                self.assertEqual(flow.plan(root)[-1],['idle'])

    def test_wrong_disk_same_capacity_and_wrong_project_or_batch_are_blocked(self):
        original=config()['report']
        for key in flow.IDENTITY:
            other=copy.deepcopy(original);other[key]+='different'
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'身份不一致'):
                flow.check_identity(original,other)

    def test_receiver_refuses_changed_physical_disk_before_clock_or_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cfg=fixture_task(root,['idle'],[])
            link=Mock()
            link.request.side_effect=[dict(protocol=2,version=app.VERSION,station=cfg['station'],busy=False),
                {'sample':{**cfg['report'],'expected_serial':'DIFFERENT'}}]
            with patch.object(app,'require_administrator'),patch.object(app,'SerialConnection',return_value=link), \
                 patch.object(app,'synchronize_two_clocks') as clock,patch.object(app,'adopt_current_sample') as adopt:
                with self.assertRaisesRegex(ValueError,'身份不一致'):
                    app.run_receiver(cfg,['idle'],resume_folder=root)
            clock.assert_not_called();adopt.assert_not_called();link.close.assert_called_once()
            self.assertNotIn('start',[c.args[0] for c in link.request.call_args_list])

    def test_forged_job_or_host_disk_binding_is_rejected(self):
        for field in ('job','disk'):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);fixture_task(root,['idle'],['idle'])
                if field=='job':
                    path=root/'手工复测/主机数据/task.json';data=flow.read_json(path);data['job_id']='c'*32
                else:
                    path=root/'手工复测/主机数据/config_snapshot.json';data=flow.read_json(path);data['report']['expected_serial']='OTHER'
                app.save_json(path,data)
                with self.assertRaises(ValueError):flow.plan(root)

    def test_path_escape_or_duplicate_attempt_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);fixture_task(root,['idle'],[])
            app.save_json(root/'手工复测/续测记录.json',{'attempts':['../elsewhere']})
            with self.assertRaisesRegex(ValueError,'路径'):flow.attempts(root)

    def test_wrong_current_project_blocks_before_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cfg=fixture_task(root,['idle'],[]);cfg['report']['project']='other'
            with patch.object(app,'recover_result') as recover,self.assertRaisesRegex(ValueError,'project'):
                app.resume_failed_flow(cfg,root)
            recover.assert_not_called()

    def test_recent_task_default_and_only_requested_single_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cfg=fixture_task(root/'task',['bit'],[])
            app.save_json(root/'部署记录/最近正式任务.json',{'folder':str(root/'task')})
            with patch.object(app,'ROOT',root),patch.object(app,'recover_result'), \
                 patch.object(app,'finish_resumed_reports',return_value=({},['bit'])),patch.object(app,'run_receiver') as run:
                app.resume_failed_flow(cfg)
            self.assertEqual(run.call_args.args[1],['bit'])
            self.assertTrue(all(x['wait_before_s']==120 for x in run.call_args.args[0]['tests'].values()))

    def test_cleanup_failure_is_still_reported_after_data_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);fixture_task(root,['idle'],['idle'])
            app.save_json(root/'手工复测/主机数据/network_restore_pending.json',{})
            app.finish_resumed_reports(root)
            self.assertFalse(flow.read_json(root/'手工复测/任务结果.json')['completed'])

    def test_actual_receiver_dispatch_transfer_and_aggregate_use_only_pending_modes(self):
        with tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
            base=Path(directory);root=base/'original'
            import shutil
            for name in ('temperature_report.py','温度模版_原始.xlsx','config.example.json'):
                shutil.copy2(app.ROOT/name,base/name)
            cfg=fixture_task(root,LEGACY_ORDER,LEGACY_ORDER[:2]);cfg['host_workspace']=str(base/'remote')
            cfg['thermometer'].update(protocol_verified=True,hid_path_hex='00')
            controller=app.HostController(copy.deepcopy(cfg));calls=[];ran=[]
            raw=(root/'手工复测/jk808.jsonl').read_text('utf-8').replace('"CPU_Surface": 48','"CPU_Surface": 70')
            def remote_run(active,modes):
                ran.extend(modes)
                for mode in modes:write_fixture(controller.folder/mode,mode)
                app.save_json(controller.folder/'config_snapshot.json',active)
                app.save_json(controller.folder/'network_after_restore.json',{'restored':True})
                task=flow.read_json(controller.folder/'task.json');task['finished']=True
                app.save_json(controller.folder/'task.json',task)
                controller.status={'phase':'完成','message':'test stub completed'}
                app.save_json(controller.folder/'status.json',controller.status)
            controller.run_job=remote_run
            class Thread:
                def __init__(self,target,args=(),**kwargs):self.target=target;self.args=args
                def start(self):
                    if self.target==app.collect_jk808:self.args[1].write_text(raw,'utf-8')
                    else:self.target(*self.args)
                def join(self,**kwargs):pass
                def is_alive(self):return False
            class Link:
                def request(self,command,data=None,**kwargs):
                    calls.append(command);return controller.handle(command,data or {})
                def close(self):pass
            instrument=Mock();instrument.read.return_value=({1:40,2:38,3:24},None,None)
            replacements={'require_administrator':Mock(),'ROOT':base,'save_setup_config':Mock(),
                'SerialConnection':Mock(return_value=Link()),'JK808Thermometer':Mock(return_value=instrument),
                'synchronize_two_clocks':Mock(),'network_snapshot':Mock(return_value=[]),
                'preflight_host':Mock(return_value=[]),'workload_window_problems':Mock(return_value=[]),
                'recover_pending_network':Mock()}
            controller.cfg['detected_disk']={'model':cfg['report']['system_disk_model']}
            for key,value in replacements.items():stack.enter_context(patch.object(app,key,value))
            stack.enter_context(patch.object(controller,'refresh_sample'))
            stack.enter_context(patch.object(app.threading,'Thread',Thread))
            app.resume_failed_flow(cfg,root)
            self.assertEqual(ran,['online_video','bit']);self.assertEqual(calls.count('start'),1)
            paths=flow.attempts(root)
            self.assertEqual(len(paths),2);self.assertTrue(paths[-1].name.startswith('1T_'))
            self.assertTrue((paths[-1]/'手工复测/主机数据/文件校验.json').exists())
            self.assertTrue(flow.read_json(root/'手工复测/任务结果.json')['completed'])
            self.assertEqual(flow.read_json(base/'部署记录/最近正式任务.json')['folder'],str(root))
            self.assertTrue(all('/续测记录/' not in p.as_posix() for p in root.iterdir()))

    def test_missing_raw_transfer_is_recovered_before_deciding_to_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cfg=fixture_task(root,['idle'],['idle'])
            (root/'手工复测/主机数据/文件校验.json').unlink()
            link=Mock();link.request.side_effect=[
                dict(protocol=2,version=app.VERSION,station=cfg['station'],busy=False),
                {'exists':True},{'busy':False,'recovered':True}]
            def receive(*args):app.save_json(root/'手工复测/主机数据/文件校验.json',[])
            with patch.object(app,'SerialConnection',return_value=link), \
                 patch.object(app,'receive_files',side_effect=receive),patch.object(app,'run_receiver') as run:
                app.resume_failed_flow(cfg,root)
            self.assertEqual([c.args[0] for c in link.request.call_args_list],['hello','job_exists','open_job'])
            run.assert_not_called()

    def test_force_recovery_refreshes_already_received_cleanup_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cfg=fixture_task(root,['idle'],['idle'])
            link=Mock();link.request.side_effect=[
                dict(protocol=2,version=app.VERSION,station=cfg['station'],busy=False),{'exists':True},{'recovered':True}]
            with patch.object(app,'SerialConnection',return_value=link),patch.object(app,'receive_files') as receive:
                app.recover_result(cfg,root,raw_only=True,force=True)
            receive.assert_called_once()

    def test_busy_host_never_resumes_during_previous_transfer_or_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cfg=fixture_task(root,['idle'],[])
            (root/'手工复测/主机数据/文件校验.json').unlink()
            link=Mock();link.request.return_value=dict(protocol=2,version=app.VERSION,station=cfg['station'],busy=True)
            with patch.object(app,'SerialConnection',return_value=link),patch.object(app,'run_receiver') as run:
                with self.assertRaisesRegex(ValueError,'等待主机任务结束'):app.resume_failed_flow(cfg,root)
            run.assert_not_called()

    def test_wrong_duration_or_wrong_mode_cannot_be_preserved_as_completed(self):
        for field in ('duration','mode'):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);fixture_task(root,['idle'],['idle'])
                path=root/'手工复测/主机数据/idle/conditions.json';data=flow.read_json(path)
                if field=='duration':data['end_epoch']=data['start_epoch']+185
                else:data['mode']='bit'
                app.save_json(path,data)
                self.assertEqual(flow.plan(root)[-1],['idle'])


class CapacityTests(unittest.TestCase):
    def test_png_html_xlsx_have_real_capacity_for_all_three_sizes(self):
        for capacity in ('512G','1T','2T'):
            with self.subTest(capacity=capacity),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);cfg=fixture_task(root,['idle'],['idle']);cfg['report']['capacity']=capacity
                app.finish_reports(cfg,root,['idle'])
                self.assertEqual(sorted(p.name for p in root.iterdir() if p.is_file()),
                    sorted(['Windows_待机_'+capacity+'.png','Windows_待机_'+capacity+'.html',
                            '本次结果_'+capacity+'.html','温度汇总_'+capacity+'.xlsx']))
                self.assertIn('Windows_待机_'+capacity+'.html',(root/('本次结果_'+capacity+'.html')).read_text('utf-8'))

    def test_invalid_capacity_cannot_create_path(self):
        with self.assertRaises(ValueError):report.artifact_stem('test','../other')


if __name__=='__main__':unittest.main()
