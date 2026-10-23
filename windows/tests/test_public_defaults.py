"""Published defaults preserve local NTP settings without contacting hardware."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import windows_temperature_test as app
import 集中联调 as menu

class PublicDefaultsTests(unittest.TestCase):
    def test_receiver_preserves_existing_ntp_and_fills_only_empty_default(self):
        default=['ntp.aliyun.com','ntp1.aliyun.com']
        for servers in ([],default,['ntp.example.internal']):
            with self.subTest(servers=servers):
                cfg=json.loads((app.ROOT/'config.example.json').read_text('utf-8'))
                cfg['clock_sync']['ntp_servers']=servers
                before=copy.deepcopy(cfg)
                device=Mock();device.read.return_value=({1:40,2:38,3:24},None,None)
                with patch('serial.tools.list_ports.comports',return_value=[SimpleNamespace(device='COM3',vid=0x403)]), \
                     patch('hid.enumerate',return_value=[{'path':b'test'}]), \
                     patch.object(app,'JK808Thermometer',return_value=device), \
                     patch.object(app,'save_setup_config'):
                    result,issues=menu.prepare_configuration(cfg,'receiver')
                self.assertEqual(issues,[])
                self.assertEqual(result['clock_sync']['ntp_servers'],servers or default)
                self.assertEqual(cfg,before)

if __name__=='__main__':unittest.main()
