"""v2.3.3真实出表、全原始导出、复算、双机共享入口；全离线。"""
import copy
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_program import program, CONFIG, EPOCH
from test_revision_210 import manual
from test_revision_231 import inputs
from test_revision_230 import dual
from report_sampling import PROFILE
from raw_host_export import export_raw_host_data


def read_rows(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def png_size(data):
    import struct
    return struct.unpack('>II', data[16:24])


def sources(folder, smart, surface):
    folder.mkdir(parents=True, exist_ok=True)
    for name,rows,columns in [('SMART计划采样数据.csv',smart,['Time','ActualTime','Second','Error','Composite','CPU','NAND']),
                              ('JK808逐秒原始数据.csv',surface,['Time','Second','CPU_Surface','NAND_Surface','AMB','CH008_Raw'])]:
        program.save_csv_table(folder/name,columns,rows)
    records = [json.dumps(dict(Second=row['Second'],ActualTime=program.utc_timestamp(EPOCH+row['Second']),
                            Error='响应晚到' if row['Second']==5 else '',
                            raw_response=json.dumps(dict(temperature=318,temperature_sensor_1=320,
                                temperature_sensor_2=315,percentage_used=3,custom_vendor_field={'raw':[1,2]}))))
               for row in smart]
    (folder/'SMART完整原始响应.jsonl').write_bytes(('\r\n'.join(records)+'\r\n').encode('utf-8'))
    (folder/'JK808原始报文.jsonl').write_bytes(b'{"hex":"00 ff"}\r\n')
    return {name:path.read_bytes() for name in ('SMART完整原始响应.jsonl','SMART计划采样数据.csv','JK808逐秒原始数据.csv','JK808原始报文.jsonl')
            if (path:=folder/name).exists()}


class RawExportTests(unittest.TestCase):
    def test_all_180_smart_rows_custom_fields_late_record_and_source_bytes_survive(self):
        surface,smart,bw=inputs();surface[0]['CH008_Raw']=65535
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);originals=sources(folder,smart,surface)
            meta=export_raw_host_data(folder,smart,surface,smart[-20:],{})
            out=folder/'原始主机数据'
            rows=read_rows(out/'SMART全部原始记录_中文.csv')
            self.assertEqual(list(rows[0])[:7],['北京时间','正式测试相对秒','SMART综合℃','主控传感器℃','NAND传感器℃','采集错误','记录来源'])
            self.assertEqual([rows[0][key] for key in ('SMART综合℃','主控传感器℃','NAND传感器℃')],['45','47','42'])
            self.assertEqual(len(rows),180)
            self.assertEqual([float(r['正式测试相对秒']) for r in rows],list(range(0,900,5)))
            self.assertEqual(rows[1]['采集错误'],'响应晚到')
            self.assertEqual(json.loads(rows[0]['SMART原始字段：custom_vendor_field']),{'raw':[1,2]})
            self.assertEqual(len(read_rows(out/'JK808每秒原始记录_中文.csv')),900)
            self.assertEqual(list(read_rows(out/'JK808每秒原始记录_中文.csv')[0])[:5],['北京时间','正式测试相对秒','主控表面℃','NAND表面℃','环境℃'])
            self.assertEqual(read_rows(out/'JK808每秒原始记录_中文.csv')[0]['JK808通道008原始字值'],'65535')
            self.assertIn('temperature',meta['已有SMART原始字段'])
            for name,data in originals.items():
                self.assertEqual((folder/name).read_bytes(),data)
                self.assertEqual((out/'原文件_字节保留'/name).read_bytes(),data)
                self.assertIn(hashlib.sha256(data).hexdigest(),(out/'原文件校验.json').read_text('utf-8'))
            document=(out/'原始数据与选点统计.html').read_text('utf-8')
            self.assertNotIn('<details open>',document)
            first_table=document.split('<table>',1)[1].split('</table>',1)[0]
            self.assertIn('SMART综合℃',first_table)
            self.assertNotIn('SMART原始响应全文',first_table)
            for anchor in ('smart','jk','selected','stats'):
                self.assertIn('id="'+anchor+'"',document)

    def test_missing_raw_response_invalid_json_and_unscheduled_actual_records_are_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            records=[{'Second':0,'raw_response':'bad'}, {'Second':5,'raw_response':'{"temperature":310}'},
                     {'Second':7.4,'raw_response':'{"vendor_extra":99}'}]
            (folder/'SMART完整原始响应.jsonl').write_text('\n'.join(json.dumps(r) for r in records)+'\ninvalid json\n','utf-8')
            export_raw_host_data(folder,[{'Second':0,'Composite':None},{'Second':10,'Composite':40}],[],[],{})
            rows=read_rows(folder/'原始主机数据/SMART全部原始记录_中文.csv')
            self.assertEqual(len(rows),5)  # 4原文件记录 + 1只有采样表的实际记录。
            self.assertIn('bad',rows[0]['SMART原始响应全文'])
            self.assertEqual(rows[2]['正式测试相对秒'],'7.4')
            self.assertEqual(rows[3]['原始记录文本'],'invalid json')
            self.assertEqual(rows[-1]['正式测试相对秒'],'10')
            self.assertEqual(rows[0]['采样表：SMART综合℃'],'')

    def test_raw_only_temperatures_use_known_original_unit_and_rounding_without_changing_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            record=dict(Second=5,raw_response=json.dumps(dict(temperature=318,temperature_sensor_1=320,temperature_sensor_2=315)))
            (folder/'SMART完整原始响应.jsonl').write_text(json.dumps(record),'utf-8')
            export_raw_host_data(folder,metadata={'SMART原始温度单位':'K','SMART摄氏度向上取整':True,'start_epoch':EPOCH})
            row=read_rows(folder/'原始主机数据/SMART全部原始记录_中文.csv')[0]
            self.assertEqual([row[key] for key in ('SMART综合℃','主控传感器℃','NAND传感器℃')],['45','47','42'])
            self.assertIn('+08:00',row['北京时间'])
            self.assertEqual(read_rows(folder/'原始主机数据/对齐选点_中文.csv'),[])
            export_raw_host_data(folder)
            row=read_rows(folder/'原始主机数据/SMART全部原始记录_中文.csv')[0]
            self.assertEqual(row['SMART综合℃'],'')


    def test_conflicting_planned_value_is_flagged_without_changing_raw_or_statistics(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            record=dict(Second=0,raw_response=json.dumps(dict(temperature=318,temperature_sensor_1=320,temperature_sensor_2=315)))
            raw=(json.dumps(record)+'\n').encode('utf-8')
            (folder/'SMART完整原始响应.jsonl').write_bytes(raw)
            plan=[dict(Second=0,Composite=90,CPU=47,NAND=42)]
            summary={'Composite':dict(max=90,min=90,mean=90,count=1)}
            meta=export_raw_host_data(folder,plan,[],plan,summary,
                {'SMART原始温度单位':'K','SMART摄氏度向上取整':True,'start_epoch':EPOCH})
            out=folder/'原始主机数据'
            row=read_rows(out/'SMART全部原始记录_中文.csv')[0]
            self.assertEqual(row['SMART综合℃'],'90')
            self.assertEqual(row['原始响应换算：SMART综合℃'],'45')
            self.assertIn('不一致',row['数据核对提示'])
            self.assertEqual(meta['SMART计划解析与原始响应换算不一致条数'],1)
            self.assertIn('数据核对提示',(out/'原始数据与选点统计.html').read_text('utf-8'))
            self.assertEqual((folder/'SMART完整原始响应.jsonl').read_bytes(),raw)
            self.assertEqual(summary['Composite']['max'],90)


class FigureAndReportTests(unittest.TestCase):
    def assert_style(self, figure):
        expected={'CPU表面':('#FF0000',.9), 'NAND表面':('#4A91FA',.9), '环境温度':('#228B22',.9),
                  '复合温度':('#7209B7',1.7),'CPU':('#FF0000',1.7),'NAND':('#4A91FA',1.7)}
        self.assertEqual(list(figure.get_size_inches()),[10,6])
        self.assertEqual(figure.axes[0].get_xlabel(),'时间（秒）')
        self.assertEqual(figure.axes[0].get_ylabel(),'温度（℃）')
        for axis in figure.axes:
            self.assertFalse(axis.collections)
            self.assertFalse(axis.texts)
            for line in axis.lines:
                self.assertEqual(line.get_linestyle(),'-')
                self.assertIn(line.get_marker(),('None',None,''))
                if line.get_label() in expected:
                    color,width=expected[line.get_label()]
                    self.assertEqual(line.get_color(),color)
                    self.assertEqual(line.get_linewidth(),width)

    def test_four_modes_report_excel_raw_export_rebuild_and_standalone_recalculation(self):
        surface,smart,bw=inputs()
        original_create=program.create_company_temperature_figure
        captured=[]
        def capture(*args,**kwargs):
            fig=original_create(*args,**kwargs);captured.append(fig);return fig
        with tempfile.TemporaryDirectory() as directory,patch('builtins.print'),patch.object(program,'create_company_temperature_figure',side_effect=capture):
            root=Path(directory)
            for mode,interval,duration in [('read_1m',5,900),('write_1m',5,900),('l0_ps4',60,1500),('l12_ps4',1500,2000)]:
                cfg=copy.deepcopy(CONFIG);cfg['company_report'].update(capacity='128G',test_version='synthetic',expected_serial='OFFLINE')
                rows=smart if interval==5 else [dict(Time=program.utc_timestamp(EPOCH+s),Second=s,Composite=45,CPU=47,NAND=42) for s in range(0,duration,interval)]
                surf=surface if duration==900 else [dict(Second=s,CPU_Surface=48,NAND_Surface=43,AMB=24) for s in range(duration)]
                state=dict(mode=mode,start_epoch=EPOCH,duration_s=duration,smart_interval_s=interval,
                           errors=[],completed=True,simulated=False,fio_offset_s=.12)
                if interval==5:
                    state.update(sampling_profile=PROFILE,report_interval_s=30,report_points=20)
                folder=root/'128G'/mode;originals=sources(folder,rows,surf)
                program.save_csv_table(folder/'带宽原始换算数据.csv',['Second','BW(MiB/s)'],bw if interval==5 else [])
                program.save_json(folder/'本次运行记录.json',dict(state,configuration=cfg))
                program.generate_all_test_results(cfg,state,folder,surf,rows,bw if interval==5 else [])
                self.assert_style(captured[-1])
                self.assertEqual(len(captured[-1].axes),2 if interval==5 else 1)
                chart=folder/'【最终结果+双轴图表】_双Y轴图表.png'
                self.assertEqual(png_size(chart.read_bytes()),(3000,1800))
                data=folder/'手工复测';out=data/'原始主机数据'
                self.assertEqual(len(read_rows(out/'SMART全部原始记录_中文.csv')),len(rows))
                self.assertEqual(len(read_rows(out/'JK808每秒原始记录_中文.csv')),duration)
                stats=json.loads((data/'统计结果.json').read_text('utf-8'))
                self.assertEqual(manual.recalculate(data),stats['statistics'])
                for name,content in originals.items():
                    self.assertEqual((data/name).read_bytes(),content)
                # 只带本测次手工复测目录，在隔离模式下运行随附脚本；不从工程/tmp补依赖。
                result=subprocess.run([sys.executable,'-E','-s','-X','utf8',str(data/'人工复算代码/复算温度.py'),str(data)],
                                      cwd=directory,capture_output=True,text=True,encoding='utf-8')
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                recalc=json.loads((data/'人工复算结果/复算统计.json').read_text('utf-8'))
                self.assertEqual(recalc,stats['statistics'])
                program.rebuild_saved_test_results(folder)
                self.assert_style(captured[-1])
                self.assertEqual(json.loads((data/'统计结果.json').read_text('utf-8'))['statistics'],stats['statistics'])
                parts=program.read_xlsx_parts(root/'温度模版.xlsx')
                from capacity_template import capacity_sheet_index
                index=capacity_sheet_index(parts,'128G')
                self.assertEqual(png_size(parts[f'xl/media/auto_linux_{index}_{mode}.png']),(3000,1800))
            # 双机隔离引擎使用同一绘图和导出实现，A/B路径互不覆盖。
            for lane in ('A','B'):
                engine=dual.isolated_engine(lane,root/lane)
                self.assertEqual(engine.STYLES,program.STYLES)
                self.assertEqual(engine.export_raw_host_data,program.export_raw_host_data)
                state=dict(mode='read_1m',start_epoch=EPOCH,duration_s=900,smart_interval_s=5,
                           errors=[],completed=True,simulated=True,fio_offset_s=.12,
                           sampling_profile=PROFILE,report_interval_s=30,report_points=20)
                lane_folder=root/lane/'run'
                originals=sources(lane_folder,smart,surface)
                with patch.object(engine,'create_company_temperature_figure',side_effect=capture):
                    engine.generate_all_test_results(cfg,state,lane_folder,surface,smart,bw)
                self.assert_style(captured[-1])
                out=lane_folder/'手工复测/原始主机数据'
                self.assertEqual(len(read_rows(out/'SMART全部原始记录_中文.csv')),180)
                self.assertEqual(len(read_rows(out/'对齐选点_中文.csv')),20)
                for name,data in originals.items():
                    self.assertEqual((out/'原文件_字节保留'/name).read_bytes(),data)

    def test_view_copy_failure_does_not_erase_completed_report_or_independent_statistics(self):
        surface,smart,bw=inputs();cfg=copy.deepcopy(CONFIG)
        state=dict(mode='read_1m',start_epoch=EPOCH,duration_s=900,smart_interval_s=5,
                   errors=[],completed=True,simulated=True,fio_offset_s=.12,
                   sampling_profile=PROFILE,report_interval_s=30,report_points=20)
        with tempfile.TemporaryDirectory() as directory,patch('builtins.print'):
            folder=Path(directory)/'run';folder.mkdir()
            with patch.object(program,'export_raw_host_data',side_effect=OSError('模拟副本写入失败')):
                program.generate_all_test_results(cfg,state,folder,surface,smart,bw)
            data=folder/'手工复测'
            self.assertTrue((folder/'测试报告.html').is_file())
            self.assertIn('副本未完整生成',(folder/'测试报告.html').read_text('utf-8'))
            self.assertTrue((data/'原始数据导出提示.txt').is_file())
            with patch.object(manual,'export_raw_host_data',side_effect=OSError('模拟副本写入失败')):
                stats=manual.recalculate(data)
            self.assertEqual(stats,json.loads((data/'人工复算结果/复算统计.json').read_text('utf-8')))
            self.assertIn('副本未完整生成',(data/'人工复算结果/复算说明与对照.txt').read_text('utf-8-sig'))


if __name__=='__main__':
    unittest.main()
