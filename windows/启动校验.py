"""启动前只读核对本包代码，防止旧代码混进新目录；config.json不参与校验。"""
import hashlib
import json
from pathlib import Path
import re
import sys

EXPECTED_VERSION='0.4.12 在线视频优先版'
REQUIRED_FILES={'windows_temperature_test.py','集中联调.py','serial_link.py','hardware_support.py',
                'temperature_report.py','runtime_check.py','启动校验.py','统一启动.cmd',
                '开始_测试主机.cmd','开始_接收电脑.cmd','repair_runtime.ps1','media_session.py',
                'environment_setup.py','配置环境.cmd'}


def verify_release(root=None, loaded_version=None, loaded_program=None):
    """核对版本清单和代码哈希；失败时不运行测试，不改动配置或历史数据。"""
    root=Path(root or Path(__file__).parent).resolve()
    manifest_path=root/'发布信息.json'
    try:
        manifest=json.loads(manifest_path.read_text('utf-8-sig'))
    except (OSError,ValueError) as exc:
        raise RuntimeError('缺少或无法读取发布信息.json，请完整解压本包到程序目录') from exc
    if not isinstance(manifest,dict) or manifest.get('version')!=EXPECTED_VERSION:
        raise RuntimeError('发布清单版本不符，应为 '+EXPECTED_VERSION+'，请覆盖整包后重开窗口')
    files=manifest.get('files')
    if not isinstance(files,dict) or not REQUIRED_FILES.issubset(files):
        raise RuntimeError('发布清单不完整，请重新完整解压本包')
    failures=[]
    for name,expected in files.items():
        # 清单是数据，不能用绝对路径或目录穿越读取包外内容。
        if not isinstance(name,str) or '/' in name or '\\' in name or ':' in name or name in ('','.', '..') \
                or not isinstance(expected,str) or not re.fullmatch('[a-f0-9]{64}',expected):
            raise RuntimeError('发布清单包含无效文件名或校验值')
        path=(root/name).resolve()
        if not path.is_relative_to(root):raise RuntimeError('发布清单文件超出程序目录')
        if not path.is_file():
            failures.append(name+'（缺少）')
        elif hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            failures.append(name+'（不是本版文件，可能未覆盖或被修改）')
    if loaded_version is not None and loaded_version!=EXPECTED_VERSION:
        failures.append('当前已加载代码版本 '+str(loaded_version)+'，应为 '+EXPECTED_VERSION)
    if loaded_program is not None and Path(loaded_program).resolve()!=root/'windows_temperature_test.py':
        failures.append('当前加载了其他目录的测试代码：'+str(loaded_program))
    if failures:
        raise RuntimeError('程序更新校验未通过：\n'+'\n'.join(failures)+
            '\n请关闭两端旧CMD，将本包全部内容覆盖到当前目录；只保留原config.json，然后重新启动。')
    return {'passed':True,'version':EXPECTED_VERSION,'program_dir':str(root),'files_checked':len(files),
            'main_sha256':files['windows_temperature_test.py']}


def main():
    root=Path(__file__).resolve().parent
    print('本次运行目录：'+str(root),flush=True)
    try:
        result=verify_release(root)
        print('程序文件校验通过：'+result['version'],flush=True)
        return 0
    except Exception as exc:
        print(str(exc),flush=True)
        print('尚未进入测试；配置和历史结果保持原样。',flush=True)
        return 2


if __name__=='__main__':sys.exit(main())
