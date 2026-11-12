"""真实PowerShell执行FTDI过滤表达式，设备数据替换为样本；不安装或操作真实硬件。"""
from contextlib import nullcontext
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import environment_setup as env
import runtime_check
import windows_temperature_test as app
import 集中联调 as combined
from platform_fixtures import fixture_platform_refresh


class DeviceQueryTests(unittest.TestCase):
    def query(self,devices,ports):
        # 使用内置powershell.exe实际解析并执行原查询，只替换Get-PnpDevice的结果。
        actual_run=subprocess.run
        records=json.dumps(devices,ensure_ascii=False).replace("'","''")
        prefix="function Get-PnpDevice { param([switch]$PresentOnly) foreach ($item in (ConvertFrom-Json -InputObject '"+records+"')) { Write-Output $item } }; "
        def execute(arguments,**kwargs):
            self.assertEqual(arguments[-1],env.FTDI_QUERY)
            return actual_run([*arguments[:-1],prefix+arguments[-1]],**kwargs)
        with patch.object(env.subprocess,'run',side_effect=execute), \
             patch('serial.tools.list_ports.comports',return_value=ports):
            return env.ftdi_state()

    def test_actual_powershell_recognizes_ftdi_bus_and_com_hardware_ids(self):
        devices=[{'InstanceId':r'USB\VID_0403&PID_6001\FT123','Status':'OK'},
                 {'InstanceId':r'FTDIBUS\VID_0403+PID_6001+FT123\0000','Status':'OK'},
                 {'InstanceId':r'USB\VID_0483&PID_5757\JK808','Status':'OK'},
                 {'InstanceId':r'ROOT\OTHER','Status':'Error'}]
        ports=[SimpleNamespace(device='COM7',vid=0x0403),SimpleNamespace(device='COM2',vid=0x1234)]
        state=self.query(devices,ports)
        self.assertTrue(state['healthy']);self.assertEqual(state['ports'],['COM7'])
        self.assertEqual(len(state['devices']),2)

    def test_uninstalled_ftdi_is_detected_without_com_port(self):
        state=self.query([{'InstanceId':r'USB\VID_0403&PID_6001\FT123','Status':'Error'}],[])
        self.assertFalse(state['healthy']);self.assertEqual(len(state['devices']),1)

    def test_no_ftdi_query_empty_output_is_valid(self):
        state=self.query([{'InstanceId':r'USB\VID_0483&PID_5757\JK808','Status':'OK'}],[])
        self.assertEqual(state,{'healthy':False,'ports':[],'devices':[]})

    def test_healthy_com_does_not_hide_a_device_error(self):
        state=self.query([{'InstanceId':r'FTDIBUS\VID_0403+PID_6001+FT123\0000','Status':'Error'}],
                         [SimpleNamespace(device='COM7',vid=0x0403)])
        self.assertFalse(state['healthy'])

    def test_query_failure_is_not_misreported_as_missing_cable(self):
        with patch.object(env.subprocess,'run',return_value=Mock(returncode=1,stderr='PnP unavailable')):
            with self.assertRaisesRegex(RuntimeError,'读取FTDI设备状态失败'):env.ftdi_state()

    def test_null_pnp_status_is_not_accepted_as_healthy(self):
        state=self.query([{'InstanceId':r'USB\VID_0403&PID_6001\FT123','Status':None}],
                         [SimpleNamespace(device='COM7',vid=0x0403)])
        self.assertFalse(state['healthy'])


class StartupReviewTests(unittest.TestCase):
    def setUp(self):
        output=patch('sys.stdout',io.StringIO());output.start();self.addCleanup(output.stop)

    def test_receiver_startup_updates_changed_com_before_menu(self):
        cfg={'serial':{'host_port':'COM3','receiver_port':'COM4'},'report':{'project':'2260','batch':'10.19'}}
        with patch('启动校验.verify_release'),patch.object(sys,'argv',['集中联调.py','2']), \
             patch('builtins.input',return_value='0'), \
             patch.object(env,'restart_as_administrator',return_value=False), \
             patch.object(env,'ensure_ftdi_driver',return_value={'ports':['COM9']}), \
             patch.object(runtime_check,'require_runtime'),patch.object(app,'require_administrator'), \
             patch.object(app,'single_instance',return_value=nullcontext()), \
             patch.object(app,'load_config',return_value=cfg),patch.object(app,'save_setup_config') as save, \
             patch.object(combined,'prepare_configuration') as prepare:
            combined.main()
            self.assertEqual(cfg['serial'],{'host_port':'COM3','receiver_port':'COM9'})
            self.assertEqual(cfg['report'],{'project':'2260','batch':'10.19'})
            save.assert_called_once();prepare.assert_not_called()

    def test_multiple_ftdi_ports_cannot_silently_select_one(self):
        with self.assertRaisesRegex(RuntimeError,'唯一FTDI'):
            env.select_ftdi_port('COM1',['COM8','COM9'])
        self.assertEqual(env.select_ftdi_port('COM8',['COM8','COM9']),'COM8')

    def test_disk_read_failure_does_not_leave_old_disk_or_verification_in_config(self):
        cfg=json.loads((ROOT/'config.example.json').read_text('utf-8-sig'))
        cfg['report'].update(project='2260',batch='10.19',test_version='人工版本')
        cfg['detected_disk']={'serial':'previous disk'}
        cfg['hwinfo'].update(usb={'volume_serial':'USB123'},smart_refresh_verified=True,
            logging_verification={'sensor_group':'previous disk'},system_disk_binding={'serial':'previous disk'})
        with patch('serial.tools.list_ports.comports',return_value=[SimpleNamespace(device='COM8',vid=0x0403)]), \
             patch('接入并验证BIT.confirmed_bit_configuration',side_effect=copy.deepcopy), \
             patch('portable_tools.rebind_bit_executable',return_value={'passed':True,'rebound':False}), \
             patch('portable_tools.rebind_local_video',return_value={'passed':True,'rebound':False}), \
             patch.object(app,'resolve_hwinfo_executable',return_value=Path('F:/HWiNFO32/HWiNFO32.exe')), \
             patch.object(app,'powershell',side_effect=RuntimeError('Get-Disk read failed')), \
             patch.object(app,'refresh_test_platform',side_effect=fixture_platform_refresh), \
             patch.object(app,'network_snapshot',return_value=[{'InterfaceGuid':cfg['network']['online_adapter_guid']}]), \
             patch.object(app,'save_setup_config'):
            updated,issues=combined.prepare_configuration(cfg,'host')
        self.assertTrue(any(s.startswith('系统盘身份：') for s in issues))
        self.assertNotIn('detected_disk',updated)
        self.assertNotIn('system_disk_binding',updated['hwinfo'])
        self.assertNotIn('logging_verification',updated['hwinfo'])
        self.assertIsNone(updated['hwinfo']['expected_sensor_group'])
        self.assertEqual(updated['report']['expected_serial'],'')
        self.assertEqual(updated['report']['capacity'],'')
        for key in ('project','batch','test_version'): self.assertEqual(updated['report'][key],cfg['report'][key])
        self.assertIn('detected_disk',cfg)  # 输入对象及磁盘历史备份不变。

    def test_disk_failure_stops_normal_host_start(self):
        cfg={'serial':{'host_port':'COM8'}}
        with patch('启动校验.verify_release'),patch.object(sys,'argv',['集中联调.py','1']), \
             patch.object(env,'restart_as_administrator',return_value=False), \
             patch.object(env,'ensure_ftdi_driver',return_value={'ports':['COM8']}), \
             patch.object(runtime_check,'require_runtime'),patch.object(app,'require_administrator'), \
             patch.object(app,'single_instance',return_value=nullcontext()), \
             patch.object(app,'load_config',return_value=cfg),patch.object(app,'run_host') as host, \
             patch.object(combined,'prepare_configuration',return_value=(cfg,['系统盘身份：read failed'])):
            with self.assertRaisesRegex(RuntimeError,'尚未启动主机执行端'):combined.main()
            host.assert_not_called()


if __name__=='__main__':unittest.main()
