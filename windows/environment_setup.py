"""U盘启动前补齐本机环境；不操作测试负载、网卡、系统时钟或磁盘数据。"""
import ctypes
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import zipfile

FTDI_SHA256 = '19abc6449d5be2fd61baa689d7488d31d2e0618606f09b07599383a14300323e'
FTDI_QUERY = r"""$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new();
@(Get-PnpDevice -PresentOnly | Where-Object {
    $_.InstanceId -like 'USB\VID_0403&*' -or $_.InstanceId -like 'FTDIBUS\*'
} | Select-Object InstanceId,Status,Class,FriendlyName) | ConvertTo-Json -Compress
"""
INSTALLER_QUERY = """$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new();
@(Get-CimInstance Win32_Process -Filter "Name='VC_redist.x64.exe' OR Name='pnputil.exe' OR Name='powershell.exe'" |
Select-Object Name,ProcessId,CommandLine) | ConvertTo-Json -Compress
"""


def require_installers_idle():
    """上次窗口退出后，系统安装进程可能还在运行；重新启动也不能叠加安装。"""
    completed = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', INSTALLER_QUERY],
        capture_output=True, encoding='utf-8', errors='replace', timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if completed.returncode:
        raise RuntimeError('无法检查环境安装进程，尚未开始配置：'+completed.stderr.strip())
    processes = json.loads(completed.stdout.strip() or '[]')
    if isinstance(processes, dict): processes = [processes]
    active = []
    for process in processes:
        name = str(process.get('Name') or '').lower()
        command = str(process.get('CommandLine') or '').lower()
        repair_script = False
        if name == 'powershell.exe':
            # 只认实际的-File入口；查询/诊断命令里提及脚本名不代表正在安装。
            arguments = re.findall(r'"[^"]*"|\S+', command)
            for index, argument in enumerate(arguments):
                if argument in ('-command', '-encodedcommand', '-c', '-enc'):
                    break
                if argument == '-file':
                    if index+1 < len(arguments):
                        repair_script = Path(arguments[index+1].strip('"')).name == 'repair_runtime.ps1'
                    break
        if (name == 'vc_redist.x64.exe'
                or repair_script
                or (name == 'pnputil.exe' and '/add-driver' in command and 'ftdi_driver_' in command)):
            active.append(f"{name}（PID {process['ProcessId']}）")
    if active:
        raise RuntimeError('环境安装仍在运行：'+', '.join(active)+'。请等安装结束后再启动；本次未重复安装或开始测试。')


@contextmanager
def installation_lock(directory):
    """两个角色从同一个U盘目录启动时，安装操作只能有一个。"""
    import msvcrt
    lock_path = Path(directory)/'environment_install.lock'
    with lock_path.open('a+b') as lock:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b'0'); lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError('另一个窗口正在配置环境，请等待该窗口完成后再启动。') from exc
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def select_ftdi_port(current, ports):
    """保留仍有效的COM；仅有一个新候选时自动更新，多个候选不能猜选。"""
    if current in ports:
        return current
    if len(ports) == 1:
        return ports[0]
    raise RuntimeError('没有找到唯一FTDI COM口；当前候选：'+str(ports)+'。请接好线并核对端口')


def select_online_adapter(network, adapters):
    """保留有效绑定；重装后优先匹配同型号网卡，其次仅接纳唯一已连接物理网卡。"""
    wanted = str(network.get('online_adapter_guid','')).strip('{}').lower()
    matched = [a for a in adapters if a['InterfaceGuid'].strip('{}').lower() == wanted]
    if len(matched) == 1:
        return matched[0]
    physical = [a for a in adapters if a.get('HardwareInterface') is True]
    description = network.get('online_adapter_description')
    same = [a for a in physical if description and a.get('InterfaceDescription') == description]
    if len(same) == 1:
        return same[0]
    connected = [a for a in physical if a.get('Enabled') is True and str(a.get('Status','')).lower() == 'up']
    if len(connected) == 1:
        return connected[0]
    raise RuntimeError('原网卡标识已失效，未找到唯一可用的在线视频网卡；请连接所需网卡后重试，或填写其InterfaceGuid。')


def restart_as_administrator(role, root, setup_only=False):
    """普通双击时由Windows请求授权，在管理员配置窗口继续；取消则不配置。"""
    if ctypes.windll.shell32.IsUserAnAdmin():
        return False
    root = Path(root).resolve()
    args = ['-B', '-u', str(root/'集中联调.py'), '1' if role == 'host' else '2', '--elevated']
    if setup_only:
        args.append('--setup-only')
    launch = ctypes.windll.shell32.ShellExecuteW
    launch.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                       ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
    launch.restype = ctypes.c_void_p
    result = launch(None, 'runas', str(root/'runtime/python.exe'),
                    subprocess.list2cmdline(args), str(root), 1)
    if not result or result <= 32:
        raise PermissionError('未取得管理员授权，环境尚未配置；请重新启动并允许Windows授权。')
    print('已在管理员窗口继续配置，请查看新窗口。', flush=True)
    return True


def run_installation(arguments, log_path, timeout=600):
    """参数直接传递；后台安装时每15秒提示进度，不弹出额外命令行窗口。"""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with installation_lock(log_path.parent):
        # 先检查再打开日志，保留仍在安装进程写入的旧日志。
        require_installers_idle()
        started = time.monotonic()
        with log_path.open('wb') as output:
            process = subprocess.Popen(arguments, stdout=output, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            while True:
                try:
                    return process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    if time.monotonic()-started >= timeout:
                        # 不强杀系统安装进程。下次启动会再次检查未结束的安装器。
                        raise RuntimeError(f'安装进程尚未结束（PID {process.pid}）；请等待安装结束后再启动。记录：{log_path}')
                    print('环境安装仍在进行，请稍候……', flush=True)


def ftdi_state():
    """没有驱动时仍从PnP硬件ID识别FTDI；COM口以安装后实际枚举为准。"""
    from serial.tools import list_ports
    # PowerShell通配符中的反斜杠是普通字符，避免Python/正则表达式的双重转义。
    completed = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',FTDI_QUERY],
        capture_output=True, encoding='utf-8', errors='replace', timeout=45,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if completed.returncode:
        raise RuntimeError('读取FTDI设备状态失败：'+completed.stderr.strip())
    devices = json.loads(completed.stdout.strip() or '[]')
    if isinstance(devices, dict): devices = [devices]
    ports = sorted(p.device for p in list_ports.comports() if p.vid == 0x0403)
    if ports and not devices:
        raise RuntimeError('FTDI串口与USB设备状态暂不一致，请确认数据线连接稳定后重试；本次不重装驱动。')
    healthy = bool(ports) and all(str(d.get('Status') or '').upper() == 'OK' for d in devices)
    return {'devices':devices, 'ports':ports, 'healthy':healthy}


def extract_ftdi_driver(installer, destination):
    """提取用户提供的已签名FTDI包内原始INF/CAT，不运行交互式安装向导。"""
    installer, destination = Path(installer), Path(destination).resolve()
    if not installer.is_file() or hashlib.sha256(installer.read_bytes()).hexdigest() != FTDI_SHA256:
        raise RuntimeError('FTDI离线驱动缺少或校验不符，请完整解压新版程序包。')
    with zipfile.ZipFile(installer) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('FTDI驱动压缩数据损坏')
        for name in archive.namelist():
            if not (destination/name).resolve().is_relative_to(destination):
                raise RuntimeError('FTDI驱动内部路径无效')
        archive.extractall(destination)
    for name in ('ftdibus.inf','ftdiport.inf','ftdibus.cat','ftdiport.cat'):
        if not (destination/name).is_file(): raise RuntimeError('FTDI驱动包不完整：'+name)


def ensure_ftdi_driver(root, role):
    """驱动正常则跳过；缺少时安装总线及串口两层驱动，复查COM后才算完成。"""
    root = Path(root).resolve()
    record = root/'部署记录'/('最近FTDI配置_'+role+'.json')
    record.parent.mkdir(parents=True, exist_ok=True)
    result = {'passed':False, 'installed':False, 'role':role, 'program_dir':str(root)}
    try:
        require_installers_idle()
        state = ftdi_state()
        result['before'] = state
        if state['healthy']:
            print('FTDI驱动已正常，串口：'+', '.join(state['ports']), flush=True)
        else:
            if not state['devices'] and not state['ports']:
                raise RuntimeError('没有检测到FTDI USB互连线，请插好本机这一端后重新启动；未安装无关驱动。')
            if not ctypes.windll.shell32.IsUserAnAdmin():
                raise PermissionError('安装FTDI驱动需要管理员权限，请以管理员身份重新运行。')
            print('检测到FTDI设备但串口未就绪，正在自动安装包内驱动……', flush=True)
            pnputil = str(Path(os.environ['SystemRoot'])/'System32/pnputil.exe')
            # 安装超时也保留源文件，防止系统安装进程仍在读取时被临时目录清理。
            driver_dir = record.parent/('FTDI_driver_'+FTDI_SHA256[:12])
            extract_ftdi_driver(root/'drivers/FTDI_setup.exe', driver_dir)
            result['install_codes'] = {}
            for name in ('ftdibus.inf','ftdiport.inf'):
                code = run_installation([pnputil,'/add-driver',str(driver_dir/name),'/install'],
                    record.parent/(role+'_'+name+'.log'))
                result['install_codes'][name] = code
                if code == 3010:
                    result['restart_required'] = True
                    raise RuntimeError('FTDI驱动已安装，Windows要求重启；请手动重启本机，再从程序目录启动。')
                if code != 0:
                    raise RuntimeError(f'FTDI驱动安装未通过（{name}，退出码{code}），请查看部署记录。')
            result['installed'] = True
            # 新串口由Windows枚举，等待它真正出现；不凭安装退出码宣布成功。
            for attempt in range(16):
                state = ftdi_state()
                if state['healthy']: break
                if attempt < 15: time.sleep(2)
            result['after'] = state
            if not state['healthy']:
                raise RuntimeError('FTDI驱动安装结束，但仍未出现正常COM口；请检查接线或部署记录中的设备状态。')
            print('FTDI驱动配置完成，串口：'+', '.join(state['ports']), flush=True)
        result.update(passed=True, ports=state['ports'])
        return result
    except Exception as exc:
        result['error'] = str(exc)
        raise
    finally:
        record.write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
