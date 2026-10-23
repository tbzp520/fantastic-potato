"""按本机角色检查运行库。只加载依赖，不操作窗口、网卡、时钟或设备。"""
import argparse
import ctypes
import importlib
import json
from pathlib import Path
import platform
import sys
import traceback
import warnings


ROLE_NAMES = {'host': '测试主机', 'receiver': '接收电脑'}
ROLE_MODULES = {
    'host': ('serial', 'pythoncom', 'win32api', 'win32gui', 'win32ui',
             'pywinauto', 'playwright.sync_api'),
    'receiver': ('serial', 'serial.tools.list_ports', 'hid', 'numpy',
                 'PIL.Image', 'matplotlib.backends.backend_agg'),
}


def inspect_runtime(role='host'):
    if role not in ROLE_NAMES:
        raise ValueError('运行库检查角色应为 host 或 receiver')
    result = {'python': sys.version, 'executable': sys.executable,
              'architecture': platform.machine(), 'pointer_bits': ctypes.sizeof(ctypes.c_void_p)*8,
              'role': role, 'modules': {}, 'dlls': {}}
    for name in ROLE_MODULES[role]:
        try:
            with warnings.catch_warnings(record=True) as notices:
                warnings.simplefilter('always')
                module = importlib.import_module(name)
                if name == 'pywinauto':
                    getattr(module, 'Desktop')  # 仅验证类可加载，不枚举真实窗口。
            result['modules'][name] = {'ok': True, 'path': getattr(module, '__file__', ''),
                                       'warnings': [str(item.message) for item in notices]}
        except Exception as exc:
            result['modules'][name] = {'ok': False, 'error': str(exc), 'traceback': traceback.format_exc()}
    # 窗口自动化 DLL 只属于测试主机，接收端既不检查也不加载。
    dlls = ('mfc140u.dll', 'msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'pywintypes312.dll') if role == 'host' else ()
    for name in dlls:
        try:
            library = ctypes.WinDLL(name)
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
            buffer = ctypes.create_unicode_buffer(32768)
            kernel.GetModuleFileNameW(library._handle, buffer, len(buffer))
            result['dlls'][name] = {'ok': True, 'path': buffer.value}
        except Exception as exc:
            result['dlls'][name] = {'ok': False, 'error': str(exc)}
    result['passed'] = all(item['ok'] for item in result['modules'].values())
    return result


def write_result(result, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')


def require_runtime(role, root):
    """选择角色后才检查；主机缺少VC DLL时尝试一次离线修复，再复查。"""
    from environment_setup import require_installers_idle
    require_installers_idle()
    result = inspect_runtime(role)
    output = Path(root)/'部署记录'/('最近运行库检查_'+role+'.json')
    write_result(result, output)
    if role == 'host' and not result['passed'] and missing_vc_runtime(result):
        from environment_setup import run_installation
        print('测试主机缺少窗口控制运行库，正在自动安装包内微软运行库……', flush=True)
        script = Path(root)/'repair_runtime.ps1'
        if not script.is_file(): raise RuntimeError('缺少运行库修复程序，请完整解压新版包。')
        code = run_installation(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
            '-File',str(script),'-Role','host'], output.with_suffix('.log'))
        if code == 3010:
            result.update(repair_exit_code=code, restart_required=True)
            write_result(result, output)
            raise RuntimeError('运行库安装完成，Windows要求重启。请手动重启后从U盘重新启动；尚未开始测试。')
        result = inspect_runtime(role)
        result['repair_exit_code'] = code
        write_result(result, output)
        if code != 0:
            raise RuntimeError('微软运行库自动配置未完成，请查看：'+str(output.with_suffix('.log')))
    if not result['passed']:
        failed = ['{}：{}'.format(name, status['error'])
                  for name, status in result['modules'].items() if not status['ok']]
        raise RuntimeError(ROLE_NAMES[role]+'运行库检查未通过：\n'+'\n'.join(failed)+'\n检查记录：'+str(output))
    print(ROLE_NAMES[role]+'运行库检查通过。', flush=True)
    if role == 'receiver':
        print('接收端已检查串口、JK808和出表依赖，不加载测试主机的窗口控制库。', flush=True)
    return result


def missing_vc_runtime(result):
    """仅修复明确缺少的VC/MFC依赖；策略阻止、模块缺失等不反复安装。"""
    details = json.dumps(result,ensure_ascii=False).lower()
    if any(word in details for word in ('应用程序控制','application control','blocked by','策略','政策')):
        return False
    vc_names = ('mfc140u.dll','msvcp140.dll','vcruntime140.dll','vcruntime140_1.dll')
    return any(not status.get('ok') and any(word in status.get('error','').lower()
        for word in ('winerror 126','找不到指定的模块','specified module could not be found','could not find module'))
        for name,status in result.get('dlls',{}).items() if name in vc_names)


def main():
    parser = argparse.ArgumentParser(description='按设备角色进行运行库只读自检')
    parser.add_argument('--role', choices=tuple(ROLE_NAMES), default='host')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = inspect_runtime(args.role)
    write_result(result, args.output)
    for name, status in result['modules'].items():
        print(name+': '+('通过' if status['ok'] else '失败 — '+status['error']))
    print(ROLE_NAMES[args.role]+'依赖：'+('可加载' if result['passed'] else '未通过'))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
