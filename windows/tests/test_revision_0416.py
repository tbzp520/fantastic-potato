"""五容量识别/写表/旧表升级。无设备操作，旧容量XML及图片保持原样。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import capacity_template as capacities
import company_template as template
import windows_temperature_test as app
import batch_report as batch
import temperature_report as report
from platform_fixtures import platform_snapshot
from test_revision_0411 import config
from test_revision_0415 import add_run, PNG, cell


LEGACY=app.ROOT/'温度模版_原始.xlsx'  # Published blank three-sheet template


class CapacityTests(unittest.TestCase):
    def test_real_decimal_byte_capacities_and_invalid_sizes(self):
        for size,expected in [(128035676160,'128G'),(256060514304,'256G'),(512110190592,'512G'),
                              (1000204886016,'1T'),(2000398934016,'2T')]:
            self.assertEqual(capacities.capacity_from_bytes(size),expected)
            self.assertEqual(capacities.capacity_from_bytes(size,.85,1.12),expected)
        for value in (64000000000,4000000000000,0,-1,True,float('nan')):
            with self.assertRaises(ValueError):capacities.capacity_from_bytes(value)

    def test_windows_scan_adopt_and_names_support_small_drives(self):
        for size,capacity in [(128035676160,'128G'),(256060514304,'256G')]:
            with self.subTest(capacity=capacity):
                cfg=config();old=copy.deepcopy(cfg['report'])
                disk=dict(model='XP'+capacity,serial='TEST-'+capacity,bytes=size,drive='C:')
                with patch.object(app,'powershell',return_value=json.dumps(disk)):
                    app.refresh_system_disk(cfg)
                self.assertEqual(cfg['report']['capacity'],capacity)
                for key in ('project','batch','test_version'):self.assertEqual(cfg['report'][key],old[key])
                with patch.object(app,'save_setup_config'),patch('builtins.print'):
                    adopted=app.adopt_current_sample(config(),dict(sample=cfg['report'],host_tools={},platform=platform_snapshot()))
                self.assertEqual(adopted['report']['capacity'],capacity)
                self.assertEqual(report.artifact_stem('Windows_BIT',capacity),'Windows_BIT_'+capacity)

    def test_append_does_not_copy_measurements_or_modify_original_parts(self):
        parts=template.read_xlsx_parts(LEGACY);before=dict(parts)
        self.assertTrue(capacities.ensure_capacity_sheets(parts,template.replace_template_cells))
        for key,value in before.items():
            if key not in ('xl/workbook.xml','xl/_rels/workbook.xml.rels','[Content_Types].xml'):
                self.assertEqual(parts[key],value,key)
        for capacity in ('128G','256G'):
            index=capacities.capacity_sheet_index(parts,capacity)
            sheet=ET.fromstring(parts[f'xl/worksheets/sheet{index}.xml'])
            for address in ('B1','B5','C11','D20','E38','C80'):
                target=sheet.find(f'.//{{{template.S}}}c[@r="{address}"]')
                self.assertEqual(''.join(target.itertext()),'',address)
            self.assertEqual(len(ET.fromstring(parts[f'xl/drawings/drawing{index}.xml'])),0)
        before=dict(parts)
        self.assertFalse(capacities.ensure_capacity_sheets(parts,template.replace_template_cells))
        self.assertEqual(parts,before)

    def test_custom_sheet_four_does_not_get_overwritten_or_mistaken_for_128g(self):
        parts=template.read_xlsx_parts(LEGACY)
        parts['xl/worksheets/sheet4.xml']=parts['xl/worksheets/sheet1.xml']
        parts['xl/worksheets/_rels/sheet4.xml.rels']=parts['xl/worksheets/_rels/sheet1.xml.rels']
        parts['xl/workbook.xml']=capacities.append_element(parts['xl/workbook.xml'],'sheets','sheet',
            {'name':'备注','sheetId':100,'xmlns:r':template.R,'r:id':'rNotes'})
        parts['xl/_rels/workbook.xml.rels']=capacities.append_element(parts['xl/_rels/workbook.xml.rels'],
            'Relationships','Relationship',{'Id':'rNotes','Type':template.R+'/worksheet','Target':'worksheets/sheet4.xml'})
        parts['[Content_Types].xml']=capacities.append_element(parts['[Content_Types].xml'],'Types','Override',
            {'PartName':'/xl/worksheets/sheet4.xml','ContentType':'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'})
        note=parts['xl/worksheets/sheet4.xml']
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);book=root/'book.xlsx';png=root/'plot.png';png.write_bytes(PNG)
            template.save_xlsx_parts(parts,book)
            template.fill_windows_result(book,'128G','idle',{'Composite':dict(max=61,mean=60,min=59)},
                                         {},png,10,0)
            new=template.read_xlsx_parts(book)
            self.assertEqual(new['xl/worksheets/sheet4.xml'],note)
            index=capacities.capacity_sheet_index(new,'128G')
            self.assertEqual(index,5);self.assertEqual(cell(book,index,'C11'),'61')
            self.assertIn('xl/media/auto_windows_5_idle.png',new)

    def test_all_five_sheets_write_statistics_and_pictures_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);book=root/'book.xlsx';png=root/'plot.png';png.write_bytes(PNG)
            template.create_empty_company_template(LEGACY,book)
            for i,capacity in enumerate(capacities.CAPACITIES):
                template.fill_windows_result(book,capacity,'bit',{'Composite':dict(max=50+i,mean=49+i,min=48+i)},
                                             {},png,120,0)
            parts=template.read_xlsx_parts(book)
            for i,capacity in enumerate(capacities.CAPACITIES):
                index=capacities.capacity_sheet_index(parts,capacity)
                self.assertEqual(cell(book,index,'C38'),str(50+i))
                self.assertIn(f'xl/media/auto_windows_{index}_bit.png',parts)
            for i,capacity in enumerate(capacities.LEGACY_CAPACITIES,1):
                self.assertEqual(capacities.capacity_sheet_index(parts,capacity),i)

    def test_batch_new_capacities_merge_and_keep_serial_guards(self):
        with tempfile.TemporaryDirectory() as directory,patch('builtins.print'):
            root=Path(directory)/'2260'/'10-21';root.mkdir(parents=True)
            add_run(root/'128','128G',['idle'],token='a')
            cfg=add_run(root/'256','256G',['bit'],token='b')
            result=batch.rebuild_batch(root,'2260','10-21',LEGACY)
            self.assertEqual(set(result['capacities']),{'128G','256G'})
            text=(root/batch.PAGE).read_text('utf-8')
            self.assertIn('SN-128G',text);self.assertIn('SN-256G',text)
            cfg['report']['expected_serial']='OTHER'
            with self.assertRaisesRegex(ValueError,'原盘数据不会覆盖'):batch.check_batch_target(root,cfg['report'])

    def test_excel_removed_empty_drawing_can_be_recreated_without_touching_other_pages(self):
        parts=template.read_xlsx_parts(app.ROOT/'温度模版_原始.xlsx')
        capacities.ensure_capacity_sheets(parts,template.replace_template_cells)
        import re
        parts['xl/worksheets/sheet4.xml']=re.sub(rb'<drawing\b[^>]*/>',b'',parts['xl/worksheets/sheet4.xml'])
        parts.pop('xl/worksheets/_rels/sheet4.xml.rels')
        before=parts['xl/worksheets/sheet1.xml']
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);book=root/'book.xlsx';png=root/'plot.png';png.write_bytes(PNG)
            template.save_xlsx_parts(parts,book)
            template.fill_windows_result(book,'128G','idle',{'Composite':dict(max=60,mean=59,min=58)}, {},png,10,0)
            result=template.read_xlsx_parts(book)
            self.assertEqual(result['xl/worksheets/sheet1.xml'],before)
            self.assertEqual(cell(book,4,'C11'),'60')
            key,_=capacities.drawing_parts(result,4)
            self.assertEqual(len(ET.fromstring(result[key])),1)

    def test_excel_renumbered_drawing_is_resolved_by_sheet_relationship(self):
        parts=template.read_xlsx_parts(app.ROOT/'温度模版_原始.xlsx')
        capacities.ensure_capacity_sheets(parts,template.replace_template_cells)
        parts['xl/drawings/drawing44.xml']=parts.pop('xl/drawings/drawing4.xml')
        parts['xl/drawings/_rels/drawing44.xml.rels']=parts.pop('xl/drawings/_rels/drawing4.xml.rels')
        key='xl/worksheets/_rels/sheet4.xml.rels'
        parts[key]=parts[key].replace(b'drawing4.xml',b'drawing44.xml')
        parts['[Content_Types].xml']=parts['[Content_Types].xml'].replace(b'/drawing4.xml',b'/drawing44.xml')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);book=root/'book.xlsx';png=root/'plot.png';png.write_bytes(PNG)
            template.save_xlsx_parts(parts,book)
            template.fill_windows_result(book,'128G','bit',{}, {},png,120,0)
            result=template.read_xlsx_parts(book)
            self.assertEqual(len(ET.fromstring(result['xl/drawings/drawing44.xml'])),1)
            self.assertNotIn('xl/drawings/drawing4.xml',result)


if __name__=='__main__':unittest.main()
