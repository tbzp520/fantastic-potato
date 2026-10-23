"""换盘归属与跨容量汇总：假盘、临时文件，不连接 SSH 或操作实际设备。"""
from test_program import program, ROOT, EPOCH
import test_program as baseline
from pathlib import Path
from contextlib import ExitStack, nullcontext
from unittest.mock import patch
import copy
import json
import tempfile
import unittest


class DiskAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.cfg=baseline.ConfigurationTests().configured_receiver()
        self.cfg['company_report'].update(project='同项目',batch='9.24',output_dir=str(self.root/'结果'))
        self.set_disk('512G','SERIAL512','F512')

    def set_disk(self,capacity,serial,firmware):
        self.cfg['company_report'].update(capacity=capacity,expected_serial=serial,test_version=firmware)
        self.cfg['detected_disk']=dict(device='/dev/nvme0n1',pci='0000:02:00.0',model='测试型号',
                                       capacity=capacity,serial=serial,firmware=firmware)

    def confirm(self,answers):
        previous=copy.deepcopy(self.cfg['company_report'])
        with patch('builtins.input',side_effect=answers),patch('builtins.print'):
            program.confirm_disk_project_and_batch(self.cfg,previous)

    def register(self):
        folder=program.get_report_batch_folder(self.cfg)
        folder.mkdir(parents=True,exist_ok=True)
        program.check_batch_disk_identity(self.cfg,folder)
        return folder

    def test_three_capacities_share_one_batch_after_each_explicit_confirmation(self):
        folders=[]
        for capacity,serial,firmware in [('512G','SERIAL512','F512'),('1T','SERIAL1T','F1T'),('2T','SERIAL2T','F2T')]:
            self.set_disk(capacity,serial,firmware)
            self.confirm(['Y'])
            program.validate_test_configuration(self.cfg,'read_1m')
            folders.append(self.register())
        self.assertEqual(len(set(folders)),1)
        saved=json.loads((folders[0]/'汇总对象记录.json').read_text('utf-8'))
        self.assertEqual({key:value['serial'] for key,value in saved.items()},
                         {'512G':'SERIAL512','1T':'SERIAL1T','2T':'SERIAL2T'})

    def test_new_batch_and_existing_batch_both_require_final_yes(self):
        original=self.register()
        (original/'温度模版.xlsx').write_bytes(b'original workbook')
        old_record=(original/'汇总对象记录.json').read_bytes()
        self.confirm(['N','N','另一个项目','另一批次','Y'])
        self.assertEqual(self.cfg['company_report']['project'],'另一个项目')
        self.assertFalse(program.get_report_batch_folder(self.cfg).exists())
        self.confirm(['N','1','Y'])
        self.assertEqual(program.get_report_batch_folder(self.cfg),original)
        self.assertEqual((original/'温度模版.xlsx').read_bytes(),b'original workbook')
        self.assertEqual((original/'汇总对象记录.json').read_bytes(),old_record)

    def test_blank_is_not_consent_and_cancel_preserves_configuration(self):
        before=copy.deepcopy(self.cfg)
        with self.assertRaisesRegex(InterruptedError,'取消'):
            self.confirm(['','N','N','新项目','新批次','0'])
        self.assertEqual(self.cfg,before)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_first_deployment_without_group_can_select_existing_result_root(self):
        original=self.register()
        self.cfg['company_report'].update(project='',batch='')
        self.confirm(['1','Y'])
        self.assertEqual(program.get_report_batch_folder(self.cfg),original)
        program.validate_confirmed_disk_assignment(self.cfg)

    def test_first_deployment_new_group_validates_folder_names(self):
        self.cfg['company_report'].update(project='',batch='')
        self.confirm(['N','../wrong','9.24','N','项目 A','9.24','Y'])
        self.assertEqual(self.cfg['company_report']['project'],'项目 A')
        self.assertEqual(self.cfg['company_report']['batch'],'9.24')
        self.assertFalse((self.root/'结果').exists())

    def test_same_capacity_conflict_can_choose_new_batch_without_overwriting_old(self):
        original=self.register()
        before=(original/'汇总对象记录.json').read_bytes()
        self.set_disk('512G','OTHER_SERIAL','OTHER_FW')
        self.confirm(['Y','N','同项目','9.24_复测','Y'])
        self.assertEqual(self.cfg['company_report']['batch'],'9.24_复测')
        self.assertEqual((original/'汇总对象记录.json').read_bytes(),before)
        self.assertFalse(program.get_report_batch_folder(self.cfg).exists())

    def test_existing_firmware_or_report_version_change_requires_another_batch(self):
        original=self.register()
        before=(original/'汇总对象记录.json').read_bytes()
        for field in ('firmware','test_version'):
            cfg=copy.deepcopy(self.cfg)
            if field=='firmware':cfg['detected_disk'][field]='new-firmware'
            else:cfg['company_report'][field]='new-report-version'
            with self.assertRaisesRegex(ValueError,'不一致'):
                program.check_report_batch_disk_conflict(cfg,original)
        self.assertEqual((original/'汇总对象记录.json').read_bytes(),before)

    def test_corrupt_registry_and_unregistered_workbook_remain_untouched(self):
        folder=self.register()
        path=folder/'汇总对象记录.json'
        for contents in ('{broken','[]','{"512G": null}','{"512G": {"serial": "old"}}'):
            path.write_text(contents,'utf-8')
            with self.assertRaisesRegex(ValueError,'损坏'):
                program.check_report_batch_disk_conflict(self.cfg,folder)
            self.assertEqual(path.read_text('utf-8'),contents)
        path.unlink()
        (folder/'温度模版.xlsx').write_bytes(b'original')
        with self.assertRaisesRegex(ValueError,'缺少'):
            program.check_report_batch_disk_conflict(self.cfg,folder)
        self.assertFalse(path.exists())
        self.assertEqual((folder/'温度模版.xlsx').read_bytes(),b'original')

    def test_unconfirmed_or_changed_affiliation_blocks_b_before_network_and_files(self):
        self.confirm(['Y'])
        variants=[]
        legacy=copy.deepcopy(self.cfg);legacy.pop('sample_assignment');variants.append(legacy)
        for key in ('project','batch','capacity','expected_serial','test_version','output_dir'):
            changed=copy.deepcopy(self.cfg)
            changed['company_report'][key]='1T' if key=='capacity' else 'changed'
            variants.append(changed)
        changed=copy.deepcopy(self.cfg);changed['detected_disk']['firmware']='new';variants.append(changed)
        with patch.object(program,'LinuxConnection') as connection,patch.object(program,'create_test_result_folder') as create:
            for changed in variants:
                for mode in ('l12_ps4','l0_ps4','write_1m','read_1m'):
                    with self.subTest(mode=mode,changed=changed['company_report']),self.assertRaisesRegex(ValueError,'先选 A'):
                        program.execute_selected_test(changed,mode)
            connection.assert_not_called();create.assert_not_called()

    def test_unrelated_changes_do_not_require_reconfirm_and_confirmation_keeps_resume_key(self):
        self.confirm(['Y'])
        key=program.workflow_configuration_key(self.cfg)
        self.cfg['sample_assignment']['confirmed_beijing']='2026-09-25T09:00:00+08:00'
        self.cfg['ssh']['password']='a-new-test-only-password'
        self.cfg['ssh']['allow_destructive']=True
        self.cfg['clock_sync']['ntp_servers']=['internal.example']
        self.cfg['bios_profile']='all_off'
        program.validate_test_configuration(self.cfg,'read_1m')
        self.assertEqual(program.workflow_configuration_key(self.cfg),key)
        snapshot=program.configuration_without_password(self.cfg)
        self.assertEqual(snapshot['sample_assignment'],self.cfg['sample_assignment'])
        self.assertNotIn('a-new-test-only-password',json.dumps(snapshot))

    def test_a_cancellation_or_later_probe_failure_does_not_save_new_disk_or_password(self):
        self.confirm(['Y'])
        self.cfg['deployment_complete']=True
        self.cfg['ssh']['password']='old-test-password'
        config_path=self.root/'config.json'
        program.save_json(config_path,self.cfg)
        before=config_path.read_bytes()
        def detect(configuration,connection):
            configuration['detected_disk']=dict(self.cfg['detected_disk'],serial='NEW1T',capacity='1T')
            configuration['company_report'].update(expected_serial='NEW1T',capacity='1T')
        for answer in ('0','Y'):
            with self.subTest(answer=answer),ExitStack() as stack:
                stack.enter_context(patch.object(program,'PROGRAM_FOLDER',self.root))
                stack.enter_context(patch.object(program,'CONFIGURATION_FILE',config_path))
                stack.enter_context(patch.object(program,'ask_text',side_effect=lambda label,current:current))
                stack.enter_context(patch.object(program.getpass,'getpass',return_value='new-test-password'))
                stack.enter_context(patch.object(program,'lock_receiver_during_test',return_value=nullcontext()))
                connection=stack.enter_context(patch.object(program,'LinuxConnection')).return_value
                connection.execute.return_value=(0,'fio-3.33','')
                stack.enter_context(patch.object(program,'detect_and_fill_nvme_information',side_effect=detect))
                probe=stack.enter_context(patch.object(program,'identify_and_verify_jk808',side_effect=ValueError('探针未核对')))
                stack.enter_context(patch('builtins.input',return_value=answer))
                stack.enter_context(patch('builtins.print'))
                with self.assertRaises((InterruptedError,ValueError)):
                    program.first_deployment()
                if answer=='0':probe.assert_not_called()
                self.assertEqual(config_path.read_bytes(),before)
                connection.close.assert_called_once()

    def test_a_success_saves_new_capacity_and_same_group_together(self):
        self.cfg['deployment_complete']=True
        path=self.root/'config.json'
        program.save_json(path,self.cfg)
        def detect(configuration,connection):
            configuration['detected_disk']=dict(self.cfg['detected_disk'],serial='NEW1T',capacity='1T')
            configuration['company_report'].update(expected_serial='NEW1T',capacity='1T')
        with ExitStack() as stack:
            stack.enter_context(patch.object(program,'PROGRAM_FOLDER',self.root))
            stack.enter_context(patch.object(program,'CONFIGURATION_FILE',path))
            stack.enter_context(patch.object(program,'ask_text',side_effect=lambda label,current:current))
            stack.enter_context(patch.object(program.getpass,'getpass',return_value='test-password'))
            stack.enter_context(patch.object(program,'lock_receiver_during_test',return_value=nullcontext()))
            connection=stack.enter_context(patch.object(program,'LinuxConnection')).return_value
            connection.execute.return_value=(0,'fio-3.33','')
            stack.enter_context(patch.object(program,'detect_and_fill_nvme_information',side_effect=detect))
            stack.enter_context(patch.object(program,'identify_and_verify_jk808'))
            stack.enter_context(patch('builtins.input',return_value='Y'))
            stack.enter_context(patch('builtins.print'))
            program.first_deployment()
        saved=json.loads(path.read_text('utf-8'))
        self.assertEqual(saved['company_report']['expected_serial'],'NEW1T')
        self.assertEqual(saved['company_report']['project'],'同项目')
        self.assertEqual(saved['company_report']['batch'],'9.24')
        self.assertEqual(saved['sample_assignment']['capacity'],'1T')
        program.validate_confirmed_disk_assignment(saved)

    def test_real_report_writer_keeps_three_capacities_in_one_original_template(self):
        import openpyxl
        import xml.etree.ElementTree as ET
        paths=[]
        for number,capacity in enumerate(('512G','1T','2T')):
            self.set_disk(capacity,'SN_'+capacity,'FW_'+capacity)
            self.confirm(['Y'])
            folder=self.register()/capacity/'离线模拟_读'
            folder.mkdir(parents=True)
            value=40+number*10
            surface=[dict(Second=s,CPU_Surface=value+2,NAND_Surface=value-2,AMB=25) for s in range(900)]
            smart=[dict(Second=s,Composite=value,CPU=value+1,NAND=value-1) for s in range(0,900,60)]
            bandwidth=[{'Second':s,'BW(MiB/s)':6000} for s in range(900)]
            state=dict(mode='read_1m',start_epoch=EPOCH,duration_s=900,smart_interval_s=60,
                       completed=True,errors=['离线模拟数据，不可上交'],identity={'firmware':'FW_'+capacity})
            with patch('builtins.print'):
                program.generate_all_test_results(self.cfg,state,folder,surface,smart,bandwidth)
            paths.append(folder.parent.parent/'温度模版.xlsx')
        self.assertEqual(len(set(paths)),1)
        with paths[0].open('rb') as file:
            book=openpyxl.load_workbook(file,data_only=True,read_only=True)
            for number,capacity in enumerate(('512G','1T','2T')):
                sheet=book[capacity+'-温度']
                self.assertEqual(sheet['B47'].value,capacity)
                self.assertEqual(sheet['B48'].value,'FW_'+capacity)
                for col in 'CDE':self.assertEqual(sheet[f'{col}80'].value,40+number*10)
                self.assertIn(capacity+'_读_基础数据',book.sheetnames)
                self.assertEqual(sheet['C59'].value,'未测试')
            book.close()
        original=program.read_xlsx_parts(ROOT/'温度模版_原始.xlsx')
        merged=program.read_xlsx_parts(paths[0])
        for index in (1,2,3):
            old=ET.fromstring(original[f'xl/worksheets/sheet{index}.xml'])
            new=ET.fromstring(merged[f'xl/worksheets/sheet{index}.xml'])
            for tag in ('mergeCells','cols'):
                self.assertEqual(ET.tostring(old.find(f'{{{program.S}}}{tag}')),ET.tostring(new.find(f'{{{program.S}}}{tag}')))


if __name__=='__main__':unittest.main()
