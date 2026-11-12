"""在全新 Python 进程中拒绝窗口库导入，验证接收端仍可启动并出表。

模拟设备；不连接串口、不打开 JK808、不改时钟或网卡、不运行真实负载。
"""
import importlib.abc
import json
from pathlib import Path
import sys
import tempfile
from contextlib import nullcontext
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
blocked_names = {'pythoncom', 'pywinauto', 'win32com', 'win32api', 'win32gui', 'win32ui', 'pywintypes'}
attempts = []


class DenyWindowLibraries(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in blocked_names:
            attempts.append(fullname)
            raise ImportError('应用程序控制策略已阻止此文件（测试模拟）：'+fullname)
        return None


assert not (blocked_names & set(sys.modules)), 'probe must start in a fresh interpreter'
sys.meta_path.insert(0, DenyWindowLibraries())
import runtime_check
import environment_setup
import windows_temperature_test as app
import 集中联调 as commissioning

mode = sys.argv[1]
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    config = root/'config.json'
    settings=json.loads((ROOT/'config.example.json').read_text('utf-8-sig'))
    settings['serial'].update(host_port='COM8',receiver_port='COM8')
    original=json.dumps(settings,ensure_ascii=False).encode('utf-8')
    config.write_bytes(original)
    if mode in ('receiver', 'unified', 'exit', 'host'):
        arguments = ['集中联调.py'] + (['2'] if mode == 'receiver' else ['1'] if mode == 'host' else [])
        answers = ['2', '0'] if mode == 'unified' else ['0']
        with patch.object(sys, 'argv', arguments), patch('builtins.input', side_effect=answers), \
             patch.object(app, 'ROOT', root), patch.object(app, 'CONFIG', config), \
             patch.object(environment_setup, 'restart_as_administrator', return_value=False), \
             patch.object(environment_setup, 'require_installers_idle'), \
             patch.object(environment_setup, 'ensure_ftdi_driver', return_value={'ports':['COM8']}), \
             patch.object(app, 'require_administrator') as admin, \
             patch.object(app, 'single_instance', return_value=nullcontext()), \
             patch.object(app, 'run_host') as host, \
             patch.object(commissioning, 'prepare_configuration') as prepare:
            if mode == 'host':
                try:
                    commissioning.main()
                except RuntimeError as exc:
                    assert 'pythoncom' in str(exc) and '检查未通过' in str(exc), str(exc)
                else:
                    raise AssertionError('host must retain its required window dependency check')
                admin.assert_not_called()
                assert 'pythoncom' in attempts
            else:
                commissioning.main()
                assert not attempts, attempts
                if mode == 'exit': admin.assert_not_called()
            host.assert_not_called()
            prepare.assert_not_called()
        assert config.read_bytes() == original
        if mode in ('receiver', 'unified'):
            check = json.loads((root/'部署记录/最近运行库检查_receiver.json').read_text('utf-8'))
            assert check['passed'] and not check['dlls']
            assert 'pythoncom' not in check['modules']
    elif mode == 'reports':
        from test_revision_037 import write_fixture, good_host_status
        assert runtime_check.inspect_runtime('receiver')['passed']
        output = root/'result'
        host = output/'手工复测/主机数据'
        for stage in app.ORDER:
            cfg, raw = write_fixture(host/stage, stage)
        good_host_status(host)
        # 各模拟阶段使用同一固定时段，统一 JK808 原始记录足够覆盖每项。
        _, raw = write_fixture(host/'idle', 'idle')
        (output/'手工复测/jk808.jsonl').write_text(raw, 'utf-8')
        results = app.finish_reports(cfg, output, app.ORDER)
        for stage, count in zip(app.ORDER, (20, 10, 20, 120)):
            statistics = json.loads((host/stage/'statistics.json').read_text('utf-8'))
            assert statistics['selected_count'] == count, statistics
        assert len(list(output.glob('Windows_*.png'))) == 4
        assert len(list(output.glob('Windows_*.html'))) == 4
        assert (output/app.report_filename(cfg,'本次结果','.html')).is_file()
        with zipfile.ZipFile(output/app.report_filename(cfg,'温度汇总','.xlsx')) as workbook:
            assert workbook.testzip() is None
            pictures = [n for n in workbook.namelist() if n.startswith('xl/media/')]
            assert len(pictures) == 4, pictures
        assert not attempts, attempts
    else:
        raise ValueError(mode)
print(json.dumps({'mode': mode, 'passed': True, 'window_import_attempts': attempts}, ensure_ascii=False))
