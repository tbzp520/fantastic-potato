"""Linux小容量识别、双机配置、真实出表与原三容量表升级。无真实硬件。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from test_program import program, CONFIG, EPOCH
from test_revision_230 import dual, ready_config
from test_revision_231 import inputs
from report_sampling import PROFILE
import capacity_template as capacities


class SmallCapacityTests(unittest.TestCase):
    def test_disk_scan_identifies_128_and_256_gigabytes(self):
        for size,capacity in [(128035676160,'128G'),(256060514304,'256G')]:
            remote=Mock();remote.execute.side_effect=[(0,json.dumps({'sn':'SN-'+capacity,'mn':'XP-'+capacity,'fr':'F1'}),''),
                (0,'/sys/devices/pci0000:00/0000:01:00.0',''),(0,str(size),'')]
            identity=program.read_disk_identity(remote,'/dev/nvme0n1')
            self.assertEqual(identity['capacity'],capacity)
            self.assertEqual(identity['capacity_bytes'],size)

    def test_dual_lane_configurations_accept_separate_small_capacities(self):
        cfg=ready_config()
        for lane,capacity in [('A','128G'),('B','256G')]:
            station=cfg['stations'][lane];station['company_report']['capacity']=capacity
            station['detected_disk']['capacity']=capacity
            materialized=dual.station_configuration(cfg,lane)
            station['sample_assignment']=dict(dual.base.disk_assignment_fields(materialized),confirmed_beijing='offline')
            dual.base.validate_test_configuration(dual.station_configuration(cfg,lane),'read_1m')
        dual.validate_dual_configuration(cfg)

    def test_actual_small_capacity_reports_fill_separate_pages_with_twenty_points(self):
        surface,smart,bw=inputs()
        with tempfile.TemporaryDirectory() as directory,patch('builtins.print'):
            root=Path(directory)
            for capacity in ('128G','256G'):
                cfg=copy.deepcopy(CONFIG);cfg['company_report'].update(capacity=capacity,
                    expected_serial='SYNTHETIC-'+capacity,test_version='offline')
                state=dict(mode='read_1m',start_epoch=EPOCH,duration_s=900,smart_interval_s=5,errors=[],
                    completed=True,simulated=False,fio_offset_s=.12,sampling_profile=PROFILE,report_interval_s=30,report_points=20)
                folder=root/capacity/'run';folder.mkdir(parents=True)
                program.generate_all_test_results(cfg,state,folder,surface,smart,bw)
                result=json.loads((folder/'手工复测/统计结果.json').read_text('utf-8'))
                self.assertEqual(result['statistics']['Composite']['count'],20)
            parts=program.read_xlsx_parts(root/'温度模版.xlsx')
            for capacity in ('128G','256G'):
                index=capacities.capacity_sheet_index(parts,capacity)
                sheet=ET.fromstring(parts[f'xl/worksheets/sheet{index}.xml'])
                self.assertEqual(sheet.find(f'.//{{{program.S}}}c[@r="C80"]/{{{program.S}}}v').text,'45')
                self.assertIn(f'xl/media/auto_linux_{index}_read_1m.png',parts)

    def test_legacy_table_expansion_preserves_all_original_capacity_parts(self):
        # 仓库保留干净的原三容量模板，测试无需依赖其他release目录。
        source=program.PROGRAM_FOLDER/'温度模版_原始.xlsx'
        parts=program.read_xlsx_parts(source);before=dict(parts)
        self.assertTrue(capacities.ensure_capacity_sheets(parts,program.replace_template_cells))
        for name,value in before.items():
            if name not in ('xl/workbook.xml','xl/_rels/workbook.xml.rels','[Content_Types].xml'):
                self.assertEqual(parts[name],value,name)
        program.validate_company_template(parts)


if __name__=='__main__':unittest.main()
