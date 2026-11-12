"""批次统一表：多容量/单项/旧任务/续测/失败更新，不连接设备或执行负载。"""
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

import batch_report as batch
import company_template as template
import windows_temperature_test as app
from test_revision_0411 import config, fixture_task


PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6KxkAAAAASUVORK5CYII=')


def measurement(count, value=45, ok=True):
    return dict(status='有效选点' if ok else '负载异常，统计仅供复核',selected_count=count,
        required_count=count,statistics={name:dict(max=value,mean=value,min=value,count=count)
         for name in ('Composite','CPU','NAND','CPU_Surface','NAND_Surface','AMB')},
        workload_ok=ok,data_issue_count=0,simulated=False)


def add_run(folder, capacity, modes, *, token='a', epoch=1, value=45, serial=None, ok=True):
    cfg=config();cfg['report'].update(capacity=capacity,system_disk_model='XP-'+capacity,
        expected_serial=serial or 'SN-'+capacity)
    manual=folder/'手工复测';manual.mkdir(parents=True)
    app.save_json(manual/'本次任务.json',dict(job_id=token*32,config=cfg,modes=list(modes),created_epoch=epoch))
    results={mode:measurement(cfg['tests'][mode]['points'],value,ok) for mode in modes}
    app.save_json(manual/'结果汇总.json',results)
    for mode in modes:
        stem=app.report_filename(cfg,app.LABELS[mode],'')
        (folder/(stem+'.png')).write_bytes(PNG)
        (folder/(stem+'.html')).write_text('source','utf-8')
    return cfg


def cell(workbook, index, address):
    sheet=ET.fromstring(template.read_xlsx_parts(workbook)[f'xl/worksheets/sheet{index}.xml'])
    found=sheet.find(f'.//{{{template.S}}}c[@r="{address}"]')
    return ''.join(found.itertext()) if found is not None else ''


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)/'2260'/'10-21';self.root.mkdir(parents=True)
        self.template=app.ROOT/'温度模版_原始.xlsx'
        self.enterContext(patch('builtins.print'))

    def rebuild(self):
        return batch.rebuild_batch(self.root,'2260','10-21',self.template)

    def test_three_capacities_and_four_stages_fill_one_workbook(self):
        originals={}
        for i,capacity in enumerate(batch.CAPACITIES):
            folder=self.root/(capacity+'_run')
            add_run(folder,capacity,app.ORDER,token=chr(97+i),epoch=100+i,value=45+i)
            originals[folder]=(folder/'手工复测/结果汇总.json').read_bytes()
        result=self.rebuild();self.assertEqual(result['stages'],len(batch.CAPACITIES)*4)
        book=self.root/batch.BOOK
        for index,value in enumerate((45,46,47),1):
            for address in ('C11','D20','E29','C38'):
                self.assertEqual(cell(book,index,address),str(value))
        parts=template.read_xlsx_parts(book)
        self.assertEqual(len([name for name in parts if name.startswith('xl/media/auto_windows_')]),len(batch.CAPACITIES)*4)
        self.assertTrue(all((p/'手工复测/结果汇总.json').read_bytes()==raw for p,raw in originals.items()))
        text=(self.root/batch.PAGE).read_text('utf-8')
        for capacity in batch.CAPACITIES:self.assertIn('SN-'+capacity,text)

    def test_single_stages_accumulate_and_missing_stages_remain_untested(self):
        add_run(self.root/'one','512G',['idle'],epoch=10)
        add_run(self.root/'two','512G',['bit'],epoch=20,token='b',value=51)
        result=self.rebuild();self.assertEqual(result['stages'],2)
        self.assertEqual(cell(self.root/batch.BOOK,1,'C11'),'45')
        self.assertEqual(cell(self.root/batch.BOOK,1,'C38'),'51')
        self.assertEqual(cell(self.root/batch.BOOK,1,'C17'),'未测试')
        self.assertEqual(cell(self.root/batch.BOOK,2,'C11'),'')

    def test_recovery_of_old_run_does_not_override_a_newer_stage(self):
        old=self.root/'old';new=self.root/'new'
        add_run(old,'1T',['idle'],epoch=10,value=41)
        add_run(new,'1T',['idle'],epoch=20,token='b',value=52)
        self.rebuild()
        results=batch.read_json(old/'手工复测/结果汇总.json');results['idle']=measurement(10,99)
        app.save_json(old/'手工复测/结果汇总.json',results)
        self.rebuild()
        self.assertEqual(cell(self.root/batch.BOOK,2,'C11'),'52')
        self.assertEqual(batch.read_json(self.root/batch.RECORD)['sources']['1T']['idle']['folder'],'new')

    def test_new_failure_clears_prior_numbers_and_picture_for_this_stage_only(self):
        add_run(self.root/'first','512G',['idle','bit'],epoch=10)
        cfg=add_run(self.root/'second','512G',['idle'],epoch=20,token='b')
        self.rebuild()
        app.save_json(self.root/'second/手工复测/结果汇总.json',{'idle':dict(status='启动失败',selected_count=0,
            statistics={},data_issue_count=1,workload_ok=False,simulated=False)})
        self.rebuild()
        book=self.root/batch.BOOK
        self.assertEqual(cell(book,1,'C11'),'');self.assertEqual(cell(book,1,'C38'),'45')
        parts=template.read_xlsx_parts(book)
        self.assertNotIn('xl/media/auto_windows_1_idle.png',parts)
        self.assertIn('xl/media/auto_windows_1_bit.png',parts)
        self.assertIn('待处理：启动失败',(self.root/batch.PAGE).read_text('utf-8'))

    def test_same_capacity_new_serial_is_blocked_before_overwriting(self):
        cfg=add_run(self.root/'first','512G',['idle']);self.rebuild()
        book=self.root/batch.BOOK;before=book.read_bytes();cfg['report']['expected_serial']='OTHER'
        with self.assertRaisesRegex(ValueError,'原盘数据不会覆盖'):batch.check_batch_target(self.root,cfg['report'])
        add_run(self.root/'other','512G',['bit'],token='b',serial='OTHER')
        with self.assertRaises(ValueError):self.rebuild()
        self.assertEqual(book.read_bytes(),before)

    def test_mixed_project_is_not_merged_even_if_folder_names_collide(self):
        cfg=add_run(self.root/'first','512G',['idle'])
        path=self.root/'first/手工复测/本次任务.json';task=batch.read_json(path)
        task['config']['report']['project']='different';app.save_json(path,task)
        with self.assertRaisesRegex(ValueError,'项目/批次'):self.rebuild()
        self.assertFalse((self.root/batch.BOOK).exists())

    def test_simulated_or_commissioning_run_cannot_supply_formal_result(self):
        for simulated in (True,False):
            folder=self.root/str(simulated);add_run(folder,'512G',['idle'],token='a' if simulated else 'b')
            path=folder/'手工复测/本次任务.json';task=batch.read_json(path)
            if simulated:task['config']['_simulated']=True
            else:task['kind']='commissioning'
            app.save_json(path,task)
        with self.assertRaisesRegex(ValueError,'没有正式测次'):self.rebuild()
        self.assertFalse((self.root/batch.BOOK).exists())

    def test_manual_version_and_other_sheet_area_are_preserved(self):
        add_run(self.root/'first','512G',['idle']);self.rebuild()
        book=self.root/batch.BOOK;parts=template.read_xlsx_parts(book)
        parts['xl/worksheets/sheet1.xml']=template.replace_template_cells(parts['xl/worksheets/sheet1.xml'],
            {'B5':'人工版本V2','C80':88})
        template.save_xlsx_parts(parts,book)
        self.rebuild()
        self.assertEqual(cell(book,1,'B5'),'人工版本V2')
        self.assertEqual(cell(book,1,'C80'),'88')

    def test_excel_busy_keeps_existing_book_page_and_registry_unchanged(self):
        add_run(self.root/'first','512G',['idle']);self.rebuild()
        targets=[self.root/name for name in (batch.BOOK,batch.PAGE,batch.RECORD)]
        before=[p.read_bytes() for p in targets];real=Path.replace
        def busy(source,target):
            if Path(target)==self.root/batch.BOOK:raise PermissionError('Excel busy')
            return real(source,target)
        with patch.object(Path,'replace',busy),self.assertRaises(PermissionError):self.rebuild()
        self.assertEqual([p.read_bytes() for p in targets],before)

    def test_corrupt_registry_or_unclaimed_workbook_is_not_overwritten(self):
        add_run(self.root/'first','512G',['idle']);self.rebuild()
        book=self.root/batch.BOOK;before=book.read_bytes()
        (self.root/batch.RECORD).write_text('{broken','utf-8')
        with self.assertRaises(ValueError):self.rebuild()
        (self.root/batch.RECORD).unlink()
        with self.assertRaisesRegex(ValueError,'缺少批次汇总记录'):self.rebuild()
        self.assertEqual(book.read_bytes(),before)

    def test_missing_previously_summarized_capacity_is_not_silently_erased(self):
        add_run(self.root/'first','512G',['idle']);self.rebuild()
        before=(self.root/batch.BOOK).read_bytes()
        (self.root/'first/手工复测/结果汇总.json').unlink()
        add_run(self.root/'second','1T',['bit'],token='b')
        with self.assertRaisesRegex(ValueError,'测次结果缺失'):self.rebuild()
        self.assertEqual((self.root/batch.BOOK).read_bytes(),before)

    def test_legacy_timestamps_order_without_relying_on_later_file_mtime(self):
        for name,token,value in [('512G_20261020_010000_aaaaaaaa','a',40),('512G_20261021_010000_bbbbbbbb','b',50)]:
            add_run(self.root/name,'512G',['idle'],token=token,value=value)
            path=self.root/name/'手工复测/本次任务.json';task=batch.read_json(path)
            task.pop('created_epoch');app.save_json(path,task)
        self.rebuild();self.assertEqual(cell(self.root/batch.BOOK,1,'C11'),'50')

    def test_explicit_old_directory_uses_its_identity_without_serial_or_devices(self):
        add_run(self.root/'first','512G',['idle'])
        cfg=config();cfg['report'].update(project='当前另一个项目',batch='别的批次')
        before=copy.deepcopy(cfg)
        with patch.object(app,'SerialConnection') as serial,patch.object(app,'JK808Thermometer') as instrument,patch.object(app,'run_receiver') as run:
            result=app.rebuild_selected_batch(cfg,self.root)
        self.assertTrue(result['passed']);self.assertEqual(cfg,before)
        serial.assert_not_called();instrument.assert_not_called();run.assert_not_called()

    def test_actual_reports_auto_merge_capacities_and_local_results_stay_independent(self):
        one=self.root/'512G_20261108_010000_aaaaaaaa';two=self.root/'1T_20261108_020000_bbbbbbbb'
        cfg=config();cfg['report'].update(capacity='512G',expected_serial='SN-512G',system_disk_model='XP-512G')
        fixture_task(one,['idle'],['idle'],cfg=cfg)
        app.finish_reports(cfg,one,['idle'])
        cfg2=copy.deepcopy(cfg);cfg2['report'].update(capacity='1T',expected_serial='SN-1T',system_disk_model='XP-1T')
        fixture_task(two,['local_video'],['local_video'],cfg=cfg2,token='b')
        app.finish_reports(cfg2,two,['local_video'])
        book=self.root/batch.BOOK
        self.assertEqual(cell(book,1,'C11'),'45.0');self.assertEqual(cell(book,2,'C20'),'45.0')
        self.assertTrue((one/app.report_filename(cfg,'温度汇总','.xlsx')).is_file())
        self.assertTrue((two/app.report_filename(cfg2,'温度汇总','.xlsx')).is_file())
        self.assertTrue(batch.read_json(two/'手工复测/批次汇总状态.json')['passed'])
        self.assertIn('同项目、同批次',(two/app.report_filename(cfg2,'本次结果','.html')).read_text('utf-8'))

    def test_conflicting_disk_is_rejected_before_jk808_clock_or_workload(self):
        cfg=add_run(self.root/'first','512G',['idle']);self.rebuild()
        other=copy.deepcopy(cfg);other['report']['expected_serial']='OTHER'
        link=Mock();link.request.side_effect=[dict(protocol=2,version=app.VERSION,station=cfg['station'],busy=False),
            dict(sample=other['report'],host_tools={},problems=[])]
        with patch.object(app,'require_administrator'),patch.object(app,'SerialConnection',return_value=link),\
             patch.object(app,'adopt_current_sample',return_value=other),patch.object(app,'report_batch_folder',return_value=self.root),\
             patch.object(app.ctypes.windll.kernel32,'SetThreadExecutionState',return_value=1),\
             patch.object(app,'JK808Thermometer') as instrument,patch.object(app,'synchronize_two_clocks') as clock:
            with self.assertRaisesRegex(ValueError,'原盘数据不会覆盖'):app.run_receiver(cfg,['idle'])
        self.assertEqual([c.args[0] for c in link.request.call_args_list],['hello','preflight'])
        instrument.assert_not_called();clock.assert_not_called();link.close.assert_called_once()

    def test_missing_image_or_mismatched_host_snapshot_does_not_change_previous_book(self):
        cfg=add_run(self.root/'first','512G',['idle']);self.rebuild()
        before=(self.root/batch.BOOK).read_bytes()
        image=self.root/'first'/app.report_filename(cfg,app.LABELS['idle'],'.png');image.unlink()
        with self.assertRaisesRegex(ValueError,'缺少曲线图'):self.rebuild()
        image.write_bytes(PNG)
        app.save_json(self.root/'first/手工复测/主机数据/config_snapshot.json',{'report':{'expected_serial':'OTHER'}})
        with self.assertRaisesRegex(ValueError,'盘身份不一致'):self.rebuild()
        self.assertEqual((self.root/batch.BOOK).read_bytes(),before)

    def test_batch_failure_keeps_local_data_and_reports_retry_without_repeating_load(self):
        folder=self.root/'512G_20261108_010000_aaaaaaaa'
        cfg=config();cfg['report'].update(capacity='512G',expected_serial='SN-512G',system_disk_model='XP-512G')
        fixture_task(folder,['idle'],['idle'],cfg=cfg)
        app.finish_reports(cfg,folder,['idle']);before=(self.root/batch.BOOK).read_bytes()
        with patch.object(batch,'rebuild_batch',side_effect=PermissionError('Excel busy')):
            summaries=app.finish_reports(cfg,folder,['idle'])
        self.assertEqual(summaries['idle']['selected_count'],10)
        self.assertEqual((self.root/batch.BOOK).read_bytes(),before)
        self.assertTrue((folder/'手工复测/主机数据/idle/hwinfo.csv').exists())
        task=batch.read_json(folder/'手工复测/任务结果.json')
        self.assertFalse(task['completed']);self.assertTrue(any('选E' in issue for issue in task['issues']))

    def test_task_restore_warning_remains_visible_in_batch_page(self):
        add_run(self.root/'first','512G',['idle'])
        app.save_json(self.root/'first/手工复测/任务结果.json',{'completed':False,'issues':['主机网卡尚未恢复']})
        self.rebuild()
        self.assertIn('主机网卡尚未恢复',(self.root/batch.PAGE).read_text('utf-8'))

    def test_legacy_sort_uses_synced_formal_start_instead_of_wrong_folder_clock(self):
        for name,token,value,start in [('512G_20990101_010000_aaaaaaaa','a',40,100),('512G_20261021_010000_bbbbbbbb','b',50,200)]:
            folder=self.root/name;add_run(folder,'512G',['idle'],token=token,value=value)
            path=folder/'手工复测/本次任务.json';task=batch.read_json(path);task.pop('created_epoch');app.save_json(path,task)
            app.save_json(folder/'手工复测/主机数据/idle/conditions.json',{'start_epoch':start})
        self.rebuild();self.assertEqual(cell(self.root/batch.BOOK,1,'C11'),'50')

    def test_float_roundoff_in_mean_is_accepted_but_invalid_numbers_are_not(self):
        result=measurement(20,42.2)
        result['statistics']['AMB']['mean']=sum([42.2]*20)/20
        batch.validate_result(result)
        result['statistics']['AMB']['mean']=float('nan')
        with self.assertRaises(ValueError):batch.validate_result(result)

    def test_legacy_images_without_capacity_suffix_are_grouped_by_saved_disk_identity(self):
        folder=self.root/'old';cfg=add_run(folder,'512G',['idle'])
        for suffix in ('.png','.html'):
            (folder/app.report_filename(cfg,app.LABELS['idle'],suffix)).replace(folder/(app.LABELS['idle']+suffix))
        self.rebuild()
        self.assertIn('xl/media/auto_windows_1_idle.png',template.read_xlsx_parts(self.root/batch.BOOK))


if __name__=='__main__':unittest.main()
