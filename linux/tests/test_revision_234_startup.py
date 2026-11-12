"""Windows接收端真实启动器回归；不启动测试、UAC、SSH或仪器。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
PS=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
CMD=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/cmd.exe'

@unittest.skipUnless(os.name=='nt','只验证Windows启动入口')
class StartupTests(unittest.TestCase):
    def fixture(self,folder,code="print('MENU_READY'); input('Choice: ')"):
        root=Path(folder)/'中文 管理员 & (启动)'
        root.mkdir()
        for name in ('开始测试.cmd','start_linux.ps1'):
            shutil.copyfile(ROOT/name,root/name)
        (root/'dual_linux.py').write_text(code,'utf-8')
        (root/'config.json').write_bytes(b'{"keep":"original"}\r\n')
        (root/'results').mkdir();(root/'results/original.txt').write_bytes(b'original result')
        return root

    def run_ps(self,root,check=False):
        args=[str(PS),'-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',str(root/'start_linux.ps1')]
        if check:args.append('-CheckOnly')
        return subprocess.run(args,cwd=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32',
            input='0\n',capture_output=True,encoding='utf-8',errors='replace',timeout=45)

    def test_admin_launch_from_system32_preserves_interactive_input_and_chinese_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.fixture(folder)
            result=self.run_ps(root)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('MENU_READY',result.stdout)
            record=json.loads((root/'启动记录/最近启动检查.json').read_text('utf-8-sig'))
            self.assertEqual(Path(record['root']),root)
            self.assertEqual(record['selected']['details']['version'][:2],[3,12])
            self.assertEqual(record['selected']['details']['bits'],64)
            self.assertEqual((root/'config.json').read_bytes(),b'{"keep":"original"}\r\n')
            self.assertEqual((root/'results/original.txt').read_bytes(),b'original result')

    def test_broken_venv_is_read_only_during_check_and_backed_up_before_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.fixture(folder)
            venv=root/'.venv';(venv/'Scripts').mkdir(parents=True)
            # 这个程序不是Python，代表旧环境/损坏启动文件，不会执行测试。
            shutil.copyfile(Path(os.environ['SystemRoot'])/'System32/where.exe',venv/'Scripts/python.exe')
            (venv/'preserve.txt').write_bytes(b'old environment retained')
            checked=self.run_ps(root,check=True)
            self.assertEqual(checked.returncode,0,checked.stdout+checked.stderr)
            self.assertTrue((venv/'preserve.txt').exists())
            self.assertFalse(list(root.glob('.venv_失效备份_*')))
            result=self.run_ps(root)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            backups=list(root.glob('.venv_失效备份_*'))
            self.assertEqual(len(backups),1)
            self.assertEqual((backups[0]/'preserve.txt').read_bytes(),b'old environment retained')
            self.assertFalse(venv.exists())
            self.assertIn('MENU_READY',result.stdout)

    def test_native_negative_exit_is_reported_and_cmd_does_not_close_without_pause(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.fixture(folder,"import os; os._exit(-1073741515)")
            result=self.run_ps(root)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('0xC0000135',result.stdout)
            record=json.loads((root/'启动记录/最近启动检查.json').read_text('utf-8-sig'))
            self.assertEqual(record['exit_code'],-1073741515)
            data=(root/'开始测试.cmd').read_bytes()
            self.assertNotIn(b'\x0b',data)
            self.assertNotIn(b'if errorlevel 1 pause',data)
            self.assertIn(b'pause\r\n:finish',data)

    def test_cmd_check_only_accepts_quoted_ampersand_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root=self.fixture(folder,"raise RuntimeError('must not enter app in check-only')")
            command=f'"{CMD}" /d /s /c ""{root / "开始测试.cmd"}" -CheckOnly"'
            result=subprocess.run(command,cwd=os.environ.get('SystemRoot','C:/Windows'),
                                  capture_output=True,encoding='utf-8',errors='replace',timeout=45)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            record=json.loads((root/'启动记录/最近启动检查.json').read_text('utf-8-sig'))
            self.assertTrue(record['selected']['passed'])
            self.assertIsNone(record['exit_code'])

if __name__=='__main__':unittest.main()
