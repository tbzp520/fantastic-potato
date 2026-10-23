"""Windows温度测试主流程。

host：本机执行四项负载、控制网卡、保存HWiNFO；不依赖网络连接控制端。
receiver：USB串口下发任务、JK808每秒采集、回收文件、选点出表。
demo：只生成明确标为模拟的数据，不启停网卡、不启动负载、不设置时间。
"""
import argparse
import base64
import copy
import csv
import ctypes
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from html import escape
from contextlib import contextmanager, redirect_stdout

from serial_link import SerialConnection, RequestServer
from hardware_support import JK808Thermometer, obtain_internet_standard_time, standard_time_now, set_windows_system_time
from temperature_report import read_hwinfo, export_stage, write_stage_failure, write_csv, LABELS, artifact_stem
from media_session import read_media_sessions, window_app_ids
from resume_flow import MODES as ORDER

VERSION = '0.4.12 在线视频优先版'
ROOT = Path(__file__).resolve().parent
CONFIG = ROOT/'config.json'
BJ = timezone(timedelta(hours=8))
HOST_TOOL_SECTIONS = ('network','hwinfo','local_video','online_video','burnintest')
CONFIRMED_BIT_CONFIG_SHA256 = '7b2ee76b7b77d88ad9ee5d978d27e5993f10712094010a2e220e697f34a0ea92'


class WorkloadCleanupError(RuntimeError):
    """本轮启动失败且窗口/负载未退出，禁止继续叠加下一项。"""


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    temporary.replace(path)


def append_record(path, record):
    with Path(path).open('a',encoding='utf-8') as stream:
        stream.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n')
        stream.flush()


def load_config():
    if not CONFIG.exists():
        # 完整包不携带 config.json，覆盖升级不会覆盖现场配置。
        shutil.copy2(ROOT/'config.example.json', CONFIG)
    cfg = json.loads(CONFIG.read_text(encoding='utf-8-sig'))
    migrate_test_waits(cfg)
    return cfg


def migrate_test_waits(cfg):
    """按本次要求升级旧配置一次；保留项目、设备绑定、采样周期和正式时长。"""
    key = 'wait_before_120s_20261021'
    if cfg.get('config_updates', {}).get(key):
        return
    tests = cfg.get('tests', {})
    if not all(isinstance(tests.get(mode), dict) for mode in ORDER):
        return  # 配置不完整由原有检查给出原因，不补造测试参数。
    original = CONFIG.read_bytes()
    backup = ROOT/'部署记录'/('等待时间升级_'+datetime.now(BJ).strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6])
    backup.mkdir(parents=True, exist_ok=False)
    (backup/'config.before.json').write_bytes(original)
    for mode in ORDER:
        tests[mode]['wait_before_s'] = 120
    cfg.setdefault('config_updates', {})[key] = True
    save_json(CONFIG, cfg)
    print('已将四项正式测试的开始前等待统一设为120秒；采样周期和测试时长保留。', flush=True)


def safe_name(value):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')[:80]
    if not cleaned:
        return '未命名'
    if cleaned.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
        cleaned = '_'+cleaned
    return cleaned


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def powershell_error_text(raw):
    """Windows PowerShell可能把错误装在CLIXML里，提取错误正文而非模块进度。"""
    text=raw.decode('utf-8-sig',errors='replace').strip()
    if text.startswith('#< CLIXML'):
        try:
            root=ET.fromstring(text[text.index('<Objs'):])
            messages=[item.text or '' for item in root.iter() if item.tag.rsplit('}',1)[-1]=='S'
                      and item.get('S')=='Error']
            if messages:
                text=''.join(messages)
                text=re.sub(r'_x([0-9a-fA-F]{4})_',lambda m:chr(int(m[1],16)),text)
        except (ValueError,ET.ParseError):
            pass
    return text[:4000].strip()


def powershell(script, timeout=30):
    """脚本文本固定；动态值通过JSON数据传递，禁止拼接shell命令。"""
    script="$ProgressPreference='SilentlyContinue'; "+script
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    process = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded],
        capture_output=True,timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    output = process.stdout.decode('utf-8-sig',errors='replace').strip()
    if process.returncode:
        raise RuntimeError(powershell_error_text(process.stderr) or output)
    return output


def ps_with_data(body, data):
    token = base64.b64encode(json.dumps(data).encode('utf-8')).decode('ascii')
    return powershell("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
        "$d=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"+token+"'))|ConvertFrom-Json; "+body)


def require_administrator():
    if os.name != 'nt' or not ctypes.windll.shell32.IsUserAnAdmin():
        raise PermissionError('正式操作请右键启动文件，以管理员身份运行')


@contextmanager
def keep_windows_awake():
    """采集、回传期间保持本机唤醒；退出时还原本线程先前的电源请求。"""
    previous = ctypes.windll.kernel32.SetThreadExecutionState(0x80000003)
    if not previous:
        raise OSError('无法启用采集期间防休眠，请检查Windows电源设置')
    try:
        yield
    finally:
        ctypes.windll.kernel32.SetThreadExecutionState(previous & 0xffffffff)


def ensure_beijing_timezone():
    """HWiNFO CSV采用本地时间，故双机校时同时固定为中国标准时间。"""
    actual = ps_with_data("if((Get-TimeZone).Id -ne 'China Standard Time'){Set-TimeZone -Id 'China Standard Time'}; "
                         "(Get-TimeZone).Id", {})
    if actual != 'China Standard Time':
        raise RuntimeError('未能将Windows时区设为北京时间：'+actual)
    return actual


@contextmanager
def single_instance(role):
    import msvcrt
    path = ROOT/(role+'.lock')
    with path.open('a+b') as stream:
        if path.stat().st_size == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            raise RuntimeError('本目录已经有同角色程序运行') from None
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)


def network_snapshot():
    text = ps_with_data('Get-NetAdapter -IncludeHidden | Select-Object Name,InterfaceGuid,InterfaceDescription,'
        'HardwareInterface,@{n="Enabled";e={[int]$_.AdminStatus -eq 1}},Status | ConvertTo-Json -Compress',{})
    value = json.loads(text)
    return value if isinstance(value,list) else [value]


def set_adapter_enabled(guid, enabled):
    if not re.fullmatch(r'\{?[0-9A-Fa-f-]{36}\}?',guid):
        raise ValueError('无效网卡GUID')
    ps_with_data('$a=@(Get-NetAdapter -IncludeHidden|Where-Object {$_.InterfaceGuid.ToString().Trim("{}") -eq $d.guid.Trim("{}")}); '
        'if($a.Count -ne 1){throw "找不到唯一网卡"}; if($d.enabled){$a|Enable-NetAdapter -Confirm:$false} '
        'else{$a|Disable-NetAdapter -Confirm:$false}',{'guid':guid,'enabled':bool(enabled)})


def is_wifi_direct_adapter(adapter):
    """已在现场识别的WLAN附属虚拟端口；驱动停止时会删除、重启时可重新创建。"""
    return not adapter.get('HardwareInterface') and bool(re.fullmatch(
        r'Microsoft Wi-Fi Direct Virtual Adapter(?: #\d+)?',adapter.get('InterfaceDescription',''),re.I))


def normalized_guid(adapter):
    return adapter['InterfaceGuid'].strip('{}').lower()


def read_wifi_connections(interface_guids):
    """读取指定无线接口的连接状态和配置名；不读取或导出Wi-Fi密码。

    用Windows原生结构读取，避免解析中英文netsh输出。这里只读取结构中
    已公开的连接状态/配置名头部，后面的SSID、BSSID、安全信息不使用。
    """
    wanted={str(uuid.UUID(g.strip('{}'))) for g in interface_guids}
    if not wanted:return {}
    class InterfaceInfo(ctypes.Structure):
        _fields_=[('guid',ctypes.c_ubyte*16),('description',ctypes.c_wchar*256),('state',ctypes.c_int)]
    class ConnectionHeader(ctypes.Structure):
        _fields_=[('state',ctypes.c_int),('mode',ctypes.c_int),('profile',ctypes.c_wchar*256)]
    api=ctypes.WinDLL('wlanapi.dll')
    pointer=ctypes.c_void_p; dword=ctypes.c_uint32
    signatures={
        'WlanOpenHandle':([dword,pointer,ctypes.POINTER(dword),ctypes.POINTER(pointer)],dword),
        'WlanEnumInterfaces':([pointer,pointer,ctypes.POINTER(pointer)],dword),
        'WlanQueryInterface':([pointer,pointer,ctypes.c_int,pointer,ctypes.POINTER(dword),ctypes.POINTER(pointer),pointer],dword),
        'WlanFreeMemory':([pointer],None),'WlanCloseHandle':([pointer,pointer],dword)}
    for name,(args,result) in signatures.items():
        function=getattr(api,name); function.argtypes=args; function.restype=result
    def check(code,operation):
        if code:
            detail='；请检查Windows位置权限是否允许桌面程序读取Wi-Fi连接' if code==5 else ''
            raise OSError(code,operation+'失败'+detail)
    handle=pointer(); version=dword(); listing=pointer()
    code=api.WlanOpenHandle(2,None,ctypes.byref(version),ctypes.byref(handle))
    if code==1062:return {}  # WLAN服务未运行，没有可记住的无线连接（例如仅有线主机）。
    check(code,'打开WLAN服务')
    try:
        check(api.WlanEnumInterfaces(handle,None,ctypes.byref(listing)),'枚举无线接口')
        if not listing.value:raise RuntimeError('WLAN接口列表为空指针')
        count=dword.from_address(listing.value).value
        if count>256:raise RuntimeError('WLAN接口数量异常')
        result={}
        for index in range(count):
            info=InterfaceInfo.from_address(listing.value+8+index*ctypes.sizeof(InterfaceInfo))
            guid=str(uuid.UUID(bytes_le=bytes(info.guid)))
            if guid not in wanted:continue
            connection={'state':info.state,'profile':''}
            if info.state==1:  # wlan_interface_state_connected
                data=pointer(); size=dword()
                try:
                    code=api.WlanQueryInterface(handle,ctypes.byref(info.guid),7,None,
                        ctypes.byref(size),ctypes.byref(data),None)
                    if code==5023:  # 查询途中断开：ERROR_INVALID_STATE
                        connection['state']=4
                    else:
                        check(code,'读取当前Wi-Fi配置')
                        if not data.value or size.value<ctypes.sizeof(ConnectionHeader):
                            raise RuntimeError('WLAN连接数据长度异常')
                        header=ConnectionHeader.from_address(data.value)
                        connection={'state':header.state,'profile':header.profile}
                finally:
                    if data.value:api.WlanFreeMemory(data)
            result[guid]=connection
        return result
    finally:
        if listing.value:api.WlanFreeMemory(listing)
        api.WlanCloseHandle(handle,None)


def remember_wifi_profiles(adapters):
    """禁用网卡前记住正在连接的配置，随本任务的网卡恢复记录保存。"""
    saved=copy.deepcopy(adapters)
    candidates=[a for a in saved if a.get('HardwareInterface') and a.get('Enabled')]
    connections=read_wifi_connections([a['InterfaceGuid'] for a in candidates])
    for adapter in candidates:
        connection=connections.get(normalized_guid(adapter))
        if connection is None:continue  # 有线网卡不在WLAN接口列表中。
        adapter['WifiProfile']=''
        if connection['state']==1 and connection['profile']:
            adapter['WifiProfile']=connection['profile']
        elif adapter.get('Status','').lower()=='up':
            raise RuntimeError('无法记住已连接Wi-Fi的配置，请保持测试前网络连接后重试；尚未禁用网卡')
    return saved


def reconnect_saved_wifi(saved, timeout=60):
    """只重连任务开始时的Wi-Fi；请求发出后核对配置名和真实关联状态。"""
    profile=saved.get('WifiProfile')
    if not profile:return None
    if not isinstance(profile,str) or len(profile)>255 or '\x00' in profile:
        raise ValueError('保存的Wi-Fi配置名无效')
    guid=normalized_guid(saved)
    deadline=time.monotonic()+timeout; next_request=0; attempts=0; last_error='尚未连接'
    while time.monotonic()<deadline:
        current=read_wifi_connections([saved['InterfaceGuid']]).get(guid,{})
        if current.get('state')==1 and current.get('profile')==profile:
            return {'interface_guid':saved['InterfaceGuid'],'profile':profile,'connected':True,'requests':attempts}
        if time.monotonic()>=next_request:
            adapters={normalized_guid(a):a for a in network_snapshot()}
            adapter=adapters.get(guid)
            if not adapter or not adapter['Enabled']:
                last_error='原无线接口尚未启用或暂未枚举到'
            else:
                # 参数列表直接传递中文、空格等；不经shell，不修改保存的密码或自动连接策略。
                process=subprocess.run(['netsh.exe','wlan','connect','name='+profile,
                    'interface='+adapter['Name']],capture_output=True,timeout=15,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                attempts+=1
                output=(process.stdout+process.stderr).decode('mbcs',errors='replace').strip()
                last_error=f'连接请求返回{process.returncode}；'+output[:600]
            next_request=time.monotonic()+15
        time.sleep(1)
    raise RuntimeError(f'无线网卡已启用，但{timeout}秒内未重新连接原Wi-Fi配置“{profile}”：{last_error}')


def offline_adapter_ids(adapters):
    # 禁用物理网卡、蓝牙PAN和其他已启用的虚拟网卡；Wi-Fi Direct由WLAN驱动管理。
    return [a['InterfaceGuid'] for a in adapters if not is_wifi_direct_adapter(a) and (
            a['HardwareInterface'] or 'personal area network' in a['InterfaceDescription'].lower() or
            (a['Enabled'] and a['Status'] not in ('Not Present','Disabled') and 'WAN Miniport' not in a['InterfaceDescription']))]


def network_state_problems(state, current, online_guid):
    """按当前枚举复查，包括中途新出现的网卡；断网不以Ping失败为证据。"""
    wanted=online_guid.strip('{}').lower()
    controlled={g.strip('{}').lower() for g in offline_adapter_ids(current)}
    problems=[]
    for adapter in current:
        key=normalized_guid(adapter)
        if key in controlled and adapter['Enabled'] and not (state=='online' and key==wanted):
            problems.append('接口仍启用：'+adapter['Name'])
        if state=='offline' and is_wifi_direct_adapter(adapter) and adapter['Status'].lower()=='up':
            problems.append('Wi-Fi Direct仍有活动连接：'+adapter['Name'])
    if state=='online' and not any(normalized_guid(a)==wanted and a['Enabled'] for a in current):
        problems.append('指定在线视频网卡未启用')
    return problems


def apply_network_state(state, original, online_guid):
    if state not in ('offline','online'):
        raise ValueError('联网状态只能是offline或online')
    controlled = offline_adapter_ids(original)
    if state == 'online' and online_guid.strip('{}').lower() not in {x.strip('{}').lower() for x in controlled}:
        raise ValueError('在线视频网卡GUID未在这台主机中找到；请先部署识别')
    for guid in controlled:
        # 每次操作重新枚举，前一个网卡启停可能使附属端口出现或消失。
        current = {normalized_guid(a):a for a in network_snapshot()}
        desired = state == 'online' and guid.strip('{}').lower() == online_guid.strip('{}').lower()
        now=current.get(guid.strip('{}').lower())
        if now is None:
            if desired:
                raise RuntimeError('指定在线视频网卡不存在：'+guid)
            continue  # 已不存在的接口不能联网，不向过期GUID发送禁用命令。
        if now['Enabled'] != desired:
            set_adapter_enabled(guid,desired)
    verify = network_snapshot()
    problems=network_state_problems(state,verify,online_guid)
    if problems:
        raise RuntimeError('网络状态复查未通过：'+'；'.join(problems))
    if state == 'online':
        selected=next(a for a in original if normalized_guid(a)==online_guid.strip('{}').lower())
        wifi_result=reconnect_saved_wifi(selected)
        deadline = time.monotonic()+60
        while time.monotonic()<deadline:
            try:
                with socket.create_connection(('www.bilibili.com',443),timeout=2):
                    final=network_snapshot()
                    problems=network_state_problems(state,final,online_guid)
                    if problems:raise RuntimeError('联网后接口状态改变：'+'；'.join(problems))
                    if wifi_result:
                        next(a for a in final if normalized_guid(a)==normalized_guid(selected))['WifiConnection']=wifi_result
                    return final
            except OSError:
                time.sleep(1)
        raise RuntimeError('网卡已启用，但60秒内未能连接B站443端口；请检查该网络的DNS和B站访问权限')
    return verify


def restorable_adapters(original):
    """恢复和历史记录比较采用相同范围，不把驱动临时端口当成独立网卡。"""
    return [a for a in original if not is_wifi_direct_adapter(a) and not (
        not a.get('HardwareInterface') and not a['Enabled'] and a.get('Status')=='Not Present')]


def restore_network(original):
    """先恢复物理设备，保留原来的启用状态；不要求驱动重建相同Wi-Fi Direct GUID。"""
    managed=restorable_adapters(original)
    managed.sort(key=lambda a:not a.get('HardwareInterface',False))
    for attempt in range(3):
        failures=[]
        current={normalized_guid(a):a for a in network_snapshot()}
        for saved in managed:
            now=current.get(normalized_guid(saved))
            if now and bool(now['Enabled'])!=bool(saved['Enabled']):
                try:
                    set_adapter_enabled(saved['InterfaceGuid'],saved['Enabled'])
                except Exception as exc:
                    failures.append(saved['Name']+': '+str(exc))
        after=network_snapshot()
        verified={normalized_guid(a):a for a in after}
        mismatches=[a['Name'] for a in managed if normalized_guid(a) not in verified
                    or bool(verified[normalized_guid(a)]['Enabled'])!=bool(a['Enabled'])]
        if not failures and not mismatches:
            wifi=[reconnect_saved_wifi(a) for a in managed if a['Enabled'] and a.get('WifiProfile')]
            return {'adapters':network_snapshot() if wifi else after,'restored':True,'wifi_connections':wifi,
                    'driver_managed_wifi_direct':[a['Name'] for a in original if is_wifi_direct_adapter(a)]}
        if attempt<2:time.sleep(1)
    raise RuntimeError('网卡恢复后复查未通过：'+'；'.join(failures+mismatches))


def network_recovery_signature(adapters):
    """比较接口、启用状态和已记录Wi-Fi配置，忽略名称、瞬时状态和排列顺序。"""
    if not isinstance(adapters,list) or not adapters:
        raise ValueError('网卡原始记录为空或格式错误')
    identities=set()
    for adapter in adapters:
        if not isinstance(adapter,dict) or not isinstance(adapter.get('InterfaceGuid'),str) or not adapter['InterfaceGuid'] \
                or not isinstance(adapter.get('Name'),str) or not isinstance(adapter.get('Enabled'),bool):
            raise ValueError('网卡原始记录缺少接口身份、名称或启用状态')
        identity=normalized_guid(adapter)
        if identity in identities:raise ValueError('网卡原始记录存在重复接口')
        identities.add(identity)
        profile=adapter.get('WifiProfile','')
        if not isinstance(profile,str) or len(profile)>255 or '\x00' in profile:
            raise ValueError('网卡原始记录中的Wi-Fi配置名无效')
    return tuple(sorted((normalized_guid(a),a['Enabled'],a.get('WifiProfile','')) for a in restorable_adapters(adapters)))


def choose_network_recovery(records, interactive):
    """相同状态自动合并；只有实际启用状态或设备身份不同时才在主机窗口选一次。"""
    choices={}
    for item in records:
        signature=network_recovery_signature(item['record']['adapters'])
        choices.setdefault(signature,[]).append(item)
    if len(choices)==1:
        print(f'发现{len(records)}份待恢复记录，网卡目标状态一致，正在合并恢复。',flush=True)
        return records[0],'相同目标自动合并'
    if not interactive:
        raise RuntimeError('历史网卡记录的启用状态不一致；请在测试主机关闭并重开统一启动.cmd，选1，在窗口选择恢复状态后继续。未创建新任务')
    print('\n历史网卡记录的状态不一致，请选择本机测试前应保留的状态。程序不会按日期猜选。')
    groups=list(choices.values())
    for index,items in enumerate(groups,1):
        print(f'{index}. 对应{len(items)}份记录（任务 {items[0]["record"]["job_id"][:8]}）')
        for adapter in restorable_adapters(items[0]['record']['adapters']):
            print('   '+adapter['Name']+'：'+('启用' if adapter['Enabled'] else '禁用')+
                  '  ['+normalized_guid(adapter)[-8:]+']'+(' Wi-Fi：'+adapter['WifiProfile'] if adapter.get('WifiProfile') else ''))
    print('通常应选择测试前使用的无线/有线网卡启用、Wi-Fi名称正确的那组；原本禁用的网卡保持禁用。')
    while True:
        answer=input('输入恢复状态编号，0取消：').strip()
        if answer=='0':raise InterruptedError('已取消恢复，历史记录保留，未启动测试')
        if answer.isdigit() and 1<=int(answer)<=len(groups):
            return groups[int(answer)-1][0],'测试人员选择目标状态'
        print('编号无效，请按上面的状态选择。')


def recover_pending_network(cfg, interactive=True):
    """恢复所有有效历史记录；先保留处理计划，复查并存档后才清除待恢复标记。"""
    root=Path(cfg['host_workspace']).resolve()
    pending=sorted(p for p in root.glob('*/network_restore_pending.json')
                   if re.fullmatch(r'[a-f0-9]{32}',p.parent.name))
    plan_path=root/'network_recovery_plan.json'
    if not pending:
        if plan_path.exists():
            # 所有标记已清除但计划尚未来得及删除：核对归档收据后完成收尾。
            plan=json.loads(plan_path.read_text('utf-8'))
            if plan.get('station')!=cfg['station'] or not plan.get('records'):
                raise RuntimeError('恢复计划无法核对：'+str(plan_path))
            for job_id,digest in plan['records'].items():
                if not re.fullmatch(r'[a-f0-9]{32}',job_id):raise RuntimeError('恢复计划任务编号无效')
                receipt=json.loads((root/job_id/'network_recovery.json').read_text('utf-8'))
                if receipt.get('pending_sha256')!=digest or receipt.get('selected_job_id')!=plan['selected_job_id']:
                    raise RuntimeError('恢复归档收据不匹配：'+job_id)
            plan_path.unlink()
        return
    records=[]
    for marker in pending:
        try:
            record=json.loads(marker.read_text('utf-8-sig'))
            if not isinstance(record,dict):raise ValueError('记录不是JSON对象')
            network_recovery_signature(record.get('adapters'))
        except (ValueError,TypeError,KeyError) as exc:
            raise RuntimeError(f'待恢复记录无效：{marker}；{exc}。原文件保留，未操作网卡') from exc
        if record.get('station')!=cfg['station'] or record.get('job_id')!=marker.parent.name:
            raise RuntimeError(f'待恢复记录的工位或任务编号不符：{marker}；未操作网卡')
        records.append({'marker':marker,'record':record,'sha256':sha256(marker)})
    # 清除多个标记的中途可能断电；已确认的目标不能因只剩一部分记录而改变。
    if plan_path.exists():
        plan=json.loads(plan_path.read_text('utf-8'))
        if plan.get('station')!=cfg['station'] or any(
                plan.get('records',{}).get(item['record']['job_id'])!=item['sha256'] for item in records):
            raise RuntimeError('恢复计划与现有记录不匹配，已保留全部文件：'+str(plan_path))
        network_recovery_signature(plan.get('adapters'))
        print('继续上次已确定的网卡恢复方案。',flush=True)
    else:
        chosen,source=choose_network_recovery(records,interactive)
        plan={'station':cfg['station'],'source':source,'selected_job_id':chosen['record']['job_id'],
              'adapters':chosen['record']['adapters'],
              'records':{item['record']['job_id']:item['sha256'] for item in records}}
        save_json(plan_path,plan)
    restored=restore_network(plan['adapters'])
    selected_signature=network_recovery_signature(plan['adapters'])
    for item in records:
        folder=item['marker'].parent
        save_json(folder/'network_after_restore.json',restored)
        save_json(folder/'network_recovery.json',{'recovered_epoch':time.time(),'source':plan['source'],
            'selected_job_id':plan['selected_job_id'],
            'pending_sha256':item['sha256'],
            'original_target_matched':network_recovery_signature(item['record']['adapters'])==selected_signature,
            'note':'只处理网卡恢复，不改变原测试成败，不重跑负载'})
        save_json(folder/'network_restore_resolved.json',item['record'])
    for item in records:
        item['marker'].unlink()
    plan_path.unlink()
    print(f'已恢复并复查网卡，归档{len(records)}份历史记录；原测试数据保留，历史测试不会重跑。',flush=True)


def restore_network_record(path):
    """人工选定原始记录后恢复；成功复查才清除同任务的待恢复标记。"""
    path=Path(path).resolve()
    original=json.loads(path.read_text('utf-8'))
    if path.name!='network_before.json' or not isinstance(original,list) or not original:
        raise ValueError('请选择本轮主机任务目录里的 network_before.json')
    restored=restore_network(original)
    save_json(path.with_name('network_after_restore.json'),restored)
    marker=path.with_name('network_restore_pending.json')
    if marker.is_file():
        record=json.loads(marker.read_text('utf-8'))
        if record.get('adapters')==original and record.get('job_id')==path.parent.name:
            save_json(path.with_name('network_recovery.json'),
                      {'recovered_epoch':time.time(),'source':'测试人员选择原始记录恢复'})
            marker.unlink()
    print('已恢复并复查记录中的网卡启用状态。')


def ui_window(profile):
    from pywinauto import Desktop
    criteria = dict(profile['window'])
    # 本轮启动的 BIT 按进程定位；同名旧窗口和其他软件弹窗都不能被误操作。
    if profile.get('_process_id') is not None:
        criteria['process'] = profile['_process_id']
    # pywinauto直接传handle会跳过process筛选。先按进程查，再匹配句柄，
    # 避免原窗口关闭、句柄被其他进程复用后，误读或误关别的窗口。
    if criteria.get('handle') is not None and criteria.get('process') is not None:
        handle = criteria.pop('handle')
        criteria['predicate_func'] = lambda element: element.handle == handle
    return Desktop(backend=profile.get('backend','uia')).window(**criteria)


def run_ui_actions(profile, key, variables=None):
    """只执行已配置的控件动作，不使用固定屏幕坐标，不执行外部shell。"""
    variables = variables or {}
    for action in profile.get(key,[]):
        kind = action['action']
        if kind == 'wait':
            time.sleep(min(float(action['seconds']),10))
            continue
        # 保存日志等弹窗与主窗口不同，允许逐个动作指定真实窗口控件。
        window = ui_window({**profile, 'window':action.get('window',profile['window'])})
        window.wait('visible',timeout=20)
        control = window.child_window(**action['control']) if action.get('control') else window
        value = str(action.get('value',''))
        for name, replacement in variables.items():
            value = value.replace('{'+name+'}',str(replacement))
        if kind == 'click':
            control.click_input()
        elif kind == 'set_text':
            control.set_edit_text(value)
        elif kind == 'keys':
            control.type_keys(value,with_spaces=True)
        elif kind == 'verify_text':
            if not re.search(value,control.window_text()):
                raise RuntimeError('界面状态不符合预期：'+control.window_text())
        else:
            raise ValueError('未知界面动作：'+kind)


def wait_seconds(seconds, stop, status, label):
    end = time.monotonic()+seconds
    while time.monotonic()<end:
        status['message'] = f'{label}：还剩{math.ceil(end-time.monotonic())}秒'
        if stop.wait(min(.2,max(0,end-time.monotonic()))):
            raise InterruptedError('测试人员取消')


def usb_storage_volumes():
    """只读列出可移动卷，以及被Windows识别为固定磁盘的USB卷。"""
    value=powershell(r"""$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
$usbLetters=@(Get-Disk | Where-Object {$_.BusType -eq 'USB'} |
    Get-Partition | Where-Object {$_.DriveLetter} | ForEach-Object {"$($_.DriveLetter):"})
$volumes=@(Get-CimInstance Win32_LogicalDisk | Where-Object {
    ($_.DriveType -eq 2 -or $usbLetters -contains $_.DeviceID) -and
    $_.DeviceID -ne $env:SystemDrive -and $_.VolumeSerialNumber
} | ForEach-Object {
    [pscustomobject]@{root=($_.DeviceID+'\'); serial=$_.VolumeSerialNumber; label=$_.VolumeName}
})
ConvertTo-Json -InputObject $volumes -Compress
""")
    return json.loads(value or '[]')


def hwinfo_on_usb(volume,relative_path):
    """只允许绑定卷内的HWiNFO32.exe，不能跳到系统盘或网络路径。"""
    relative=Path(relative_path)
    if relative.is_absolute() or relative.drive or '..' in relative.parts or ':' in str(relative):
        raise ValueError('HWiNFO的U盘相对路径无效')
    root=Path(volume['root']).resolve()
    executable=(root/relative).resolve()
    if not executable.is_relative_to(root) or executable.name.lower()!='hwinfo32.exe':
        raise ValueError('请选择U盘内的HWiNFO32.exe')
    if not executable.is_file():
        raise FileNotFoundError('U盘内未找到HWiNFO32：'+str(executable))
    return executable


def resolve_hwinfo_executable(settings,volumes=None):
    """每次开始前按卷标识重找盘符，绝不把U盘程序复制到系统盘。"""
    if settings.get('source','path')!='usb':
        path=Path(settings['executable'])
        if not path.is_file():raise FileNotFoundError('未找到HWiNFO程序：'+str(path))
        return path.resolve()
    binding=settings.get('usb',{})
    if not binding.get('volume_serial') or not binding.get('relative_executable') or not binding.get('exe_sha256'):
        raise ValueError('请在测试主机插入U盘，先运行“绑定U盘HWiNFO.cmd”')
    available=usb_storage_volumes() if volumes is None else volumes
    matched=[v for v in available if str(v['serial']).upper()==binding['volume_serial'].upper()]
    if len(matched)!=1:
        raise ValueError('未找到已绑定的HWiNFO U盘，或存在重复卷标识；请插回原U盘或重新绑定')
    executable=hwinfo_on_usb(matched[0],binding['relative_executable'])
    if sha256(executable)!=binding['exe_sha256']:
        raise ValueError('U盘HWiNFO32程序已更换，请运行“绑定U盘HWiNFO.cmd”重新核对')
    return executable


def bind_hwinfo_usb(cfg,path=None):
    """测试主机首次绑定U盘；只写本程序config.json，不改U盘INI或安装BIT。"""
    volumes=usb_storage_volumes()
    if not volumes:raise ValueError('未发现U盘，请插入后重试（测试主机以管理员身份运行）')
    if path is None:
        candidates=[]
        # 仅在外部卷浅层找程序，不全盘递归扫描；目录较深可直接输入完整路径。
        for volume in volumes:
            root=Path(volume['root']).resolve()
            directories=[root]
            visited=0
            while directories and visited<2000:
                directory=directories.pop();visited+=1
                try:
                    for entry in directory.iterdir():
                        if entry.is_symlink() or entry.is_junction():continue
                        if entry.is_file() and entry.name.lower()=='hwinfo32.exe':candidates.append(entry)
                        elif entry.is_dir() and not entry.name.startswith(('$','.')) and len(entry.relative_to(root).parts)<4:
                            directories.append(entry)
                except OSError:
                    continue
        candidates=sorted(set(candidates))
        if len(candidates)==1:
            path=candidates[0]
            print('找到U盘HWiNFO32：'+str(path))
        else:
            for index,item in enumerate(candidates,1):print(index,str(item))
            answer=input('选择程序编号，或输入U盘HWiNFO32.exe完整路径：').strip().strip('"')
            if answer.isdigit() and 1<=int(answer)<=len(candidates):path=candidates[int(answer)-1]
            else:path=answer
    executable=Path(path).resolve()
    matched=[v for v in volumes if executable.is_relative_to(Path(v['root']).resolve())]
    if len(matched)!=1:raise ValueError('程序必须位于已识别的U盘，不能选择系统盘里的副本')
    volume=matched[0]
    relative=executable.relative_to(Path(volume['root']).resolve()).as_posix()
    executable=hwinfo_on_usb(volume,relative)
    updated=copy.deepcopy(cfg)
    updated['hwinfo'].update(source='usb',executable=str(executable),usb={
        'volume_serial':str(volume['serial']),'relative_executable':relative,'exe_sha256':sha256(executable)})
    save_setup_config(updated,'U盘HWiNFO绑定',{
        'executable':str(executable),'volume_serial':volume['serial'],'relative_executable':relative,
        'exe_sha256':sha256(executable),'ini_exists':executable.with_suffix('.ini').is_file()})
    print('U盘绑定已保存，盘符变化会自动重找。请保留同目录的INI配置，测试期间保持U盘插入。')
    print('BurnInTest继续使用系统已安装路径：'+cfg['burnintest']['executable'])
    return updated


def preflight_host(cfg, modes):
    problems = []
    if any(mode not in ORDER for mode in modes) or not modes or len(set(modes))!=len(modes):
        return ['测试项目无效']
    hw = cfg['hwinfo']
    if cfg['report']['capacity'] not in ('512G','1T','2T'):
        problems.append('公司模板容量应为512G、1T或2T')
    try:
        hw['executable']=str(resolve_hwinfo_executable(hw))
    except (OSError,ValueError,RuntimeError) as exc:
        problems.append('HWiNFO程序检查：'+str(exc))
    if not hw['mapping_verified'] or not hw['columns'].get('Composite'):
        problems.append('尚未核对真实HWiNFO CSV与被测SSD列映射')
    # 已适配的HWiNFO每项开始时都会设置并回读采样参数，不要求历史短测记录。
    if hw['ui'].get('adapter') != 'hwinfo_834':
        if not hw.get('smart_refresh_verified',False):
            problems.append('尚未核对自定义HWiNFO适配的SMART刷新周期')
        for action in ('start_logging_actions','stop_logging_actions','interval_actions'):
            if not hw['ui'].get(action):
                problems.append('HWiNFO界面动作待适配：'+action)
    for mode in modes:
        item = cfg['tests'][mode]
        if (any(type(item.get(k)) not in (int,float) or not math.isfinite(item[k]) or item[k]<=0
                for k in ('duration_s','interval_s')) or type(item.get('points')) is not int or item['points']<2
                or type(item.get('wait_before_s')) not in (int,float)
                or not math.isfinite(item['wait_before_s']) or item['wait_before_s']<0):
            problems.append(mode+'的时长/间隔/点数无效')
        elif item['duration_s']<(item['points']-1)*item['interval_s']:
            problems.append(mode+'的时长不足以取得所需连续点')
        if item.get('network')!=('online' if mode=='online_video' else 'offline'):
            problems.append(mode+'的联网状态与已确定测试要求不符')
    if 'local_video' in modes:
        video = cfg['local_video']
        if not Path(video['file']).is_file():
            problems.append('未找到本地视频')
        if not video['ui'].get('window'):
            problems.append('本地播放器窗口待适配')
    if 'online_video' in modes and not cfg['online_video'].get('quality_selector') and cfg['online_video'].get('adapter') != 'bilibili_1080':
        problems.append('B站1080p清晰度控件待实机适配')
    if 'bit' in modes:
        bit = cfg['burnintest']
        path = Path(bit['configuration'])
        if not Path(bit['executable']).is_file():
            problems.append('未找到BurnInTest程序')
        if not path.is_file() or path.suffix.lower() != '.bitcfg':
            problems.append('需要真实.bitcfg，不能使用.lnk快捷方式')
        elif sha256(path).lower() != bit.get('configuration_sha256','').lower():
            problems.append('BIT配置内容尚未核对或与记录的SHA256不符')
        if not bit.get('system_disk_file_test_verified'):
            problems.append('尚未核对BIT系统盘测试文件范围及负载项目')
        if bit.get('adapter') == 'bit9_script':
            if cfg.get('detected_disk') and cfg['detected_disk'].get('drive') != 'C:':
                problems.append('当前系统盘不是C盘，与已确认的BIT C盘文件测试配置不符')
            if bit.get('configuration_sha256','').lower() != CONFIRMED_BIT_CONFIG_SHA256:
                problems.append('BIT脚本适配对应已收集的C盘配置，当前配置内容不同')
            # 版本、配置文件、启动确认及实际运行证据在本次start_bit_script中核验。
        elif (not bit['ui'].get('running_control') or not bit['ui'].get('stop_actions')
                or not bit['ui'].get('running_text_re')):
            problems.append('BIT开始运行识别与停止动作待适配')
        if bit.get('adapter') != 'bit9_script' and not bit['ui'].get('save_result_actions'):
            problems.append('BIT测试日志保存动作待适配')
    return problems


def launch_hwinfo(cfg):
    from pywinauto import Application
    executable = str(resolve_hwinfo_executable(cfg['hwinfo']))
    cfg['hwinfo']['executable']=executable
    try:
        app = Application(backend='uia').connect(path=executable)
    except Exception:
        app = Application(backend='uia').start('"'+executable+'"',work_dir=str(Path(executable).parent))
        if cfg['hwinfo']['ui'].get('adapter') != 'hwinfo_834':
            run_ui_actions(cfg['hwinfo']['ui'],'startup_actions')
    # 无论刚启动，还是程序已停在Start页，都要完成启动并等到Sensors真实出现。
    if cfg['hwinfo']['ui'].get('adapter') == 'hwinfo_834':
        hwinfo_sensor_window(cfg)
    return app


def start_hwinfo_logging(cfg, folder, interval):
    if cfg['hwinfo']['ui'].get('adapter') == 'hwinfo_834':
        return start_hwinfo_834_logging(cfg, folder, interval)
    profile = cfg['hwinfo']['ui']
    path = folder/'hwinfo.csv'
    run_ui_actions(profile,'interval_actions',{'interval_ms':interval*1000})
    run_ui_actions(profile,'start_logging_actions',{'csv_path':path})
    end = time.monotonic()+20
    while time.monotonic()<end:
        if path.is_file() and path.stat().st_size:
            return path
        time.sleep(.2)
    raise RuntimeError('HWiNFO没有创建本次CSV，尚未开始正式计时')


def hwinfo_sensor_window(cfg):
    """仅操作绑定U盘的8.34进程：Start启动页→主窗口→Sensors，兼容已经启动的情况。"""
    from pywinauto import Desktop
    executable = resolve_hwinfo_executable(cfg['hwinfo']).resolve()
    desktop=Desktop(backend='win32')
    def bound_windows(title_re, **filters):
        candidates=[]
        for window in desktop.windows(title_re=title_re,visible_only=True,**filters):
            try:
                actual=Path(process_executable_path(window.element_info.process_id)).resolve()
                if os.path.normcase(str(actual))==os.path.normcase(str(executable)):
                    candidates.append(window)
            except OSError:
                continue
        return candidates
    deadline=time.monotonic()+90; opened=False; start_clicked=False
    record={'passed':False,'actions':[]}
    if cfg.get('_diagnostic_folder'):
        previous=Path(cfg['_diagnostic_folder'])/'HWiNFO程序启动.json'
        if previous.is_file():
            record=json.loads(previous.read_text('utf-8'))
    record['passed']=False
    def record_action(action, window):
        record['actions'].append({'epoch':time.time(),'action':action,'handle':window.handle,
                                  'pid':window.element_info.process_id})
        if cfg.get('_diagnostic_folder'):
            save_json(Path(cfg['_diagnostic_folder'])/'HWiNFO程序启动.json',record)
    while True:
        for warning in bound_windows(r'^Warning$',class_name='#32770'):
            dialog=desktop.window(handle=warning.handle,process=warning.element_info.process_id)
            message=dialog.child_window(control_id=65535,class_name='Static').window_text()
            expected='This is a 64-bit system and HWiNFO32 might not work properly here. Use HWiNFO64 instead.'
            if ' '.join(message.split()) != expected:
                raise RuntimeError('HWiNFO出现其他Warning提示，未自动确认：'+message)
            # 已用用户提供的程序核对：此兼容性提示的“确定”是ID 2，不是Start页的ID 1。
            button=dialog.child_window(control_id=2,class_name='Button')
            if button.window_text().replace('&','').strip() not in ('确定','OK'):
                raise RuntimeError('HWiNFO兼容性提示的确定按钮与已核对界面不符')
            button.wait('visible enabled',timeout=5)
            record_action('确认HWiNFO32在64位系统上的兼容性提示',warning)
            button.click()
            dialog.wait_not('visible',timeout=5)
        # Full mode会同时弹出System Summary；Sensors已打开时也可能被它盖住。
        # 只关闭绑定程序的独立摘要窗口，不关闭主窗口，更不退出HWiNFO。
        for summary in bound_windows(r'HWiNFO.*32 v8\.34-5870.* - System Summary\s*$'):
            record_action('关闭System Summary摘要窗口', summary)
            try:
                summary.close(wait_time=5)
            except Exception as exc:
                record['error'] = 'System Summary未能关闭：'+str(exc)
                if cfg.get('_diagnostic_folder'):
                    save_json(Path(cfg['_diagnostic_folder'])/'HWiNFO程序启动.json',record)
                raise RuntimeError(record['error']) from exc
        candidates=bound_windows(r'HWiNFO.*32 v8\.34-5870 Sensors Status.*',class_name='#32770')
        if len(candidates)>1:raise RuntimeError('绑定U盘的HWiNFO存在多个Sensors窗口，请只保留一个')
        if candidates:break
        if not opened:
            main=bound_windows(r'HWiNFO.*32 v8\.34-5870\s*$')
            if len(main)>1:raise RuntimeError('绑定U盘的HWiNFO存在多个主窗口，请只保留一个')
            if main:
                window=main[0]
                if window.is_minimized():window.restore()
                uia=Desktop(backend='uia').window(handle=window.handle,process=window.element_info.process_id)
                toolbar=uia.child_window(auto_id='59392',control_type='ToolBar')
                button=toolbar.child_window(title='Sensors',control_type='Button')
                button.wait('visible enabled',timeout=5)
                from pywinauto.uia_defines import NoPatternInterfaceError
                record_action('点击主窗口Sensors按钮',window)
                try:button.invoke()
                except NoPatternInterfaceError:button.click_input()
                opened=True
            elif not start_clicked:
                startup=bound_windows(r'HWiNFO.*32\s*$',class_name='#32770')
                if len(startup)>1:raise RuntimeError('绑定U盘的HWiNFO存在多个启动窗口，请只保留一个')
                if startup:
                    window=startup[0]
                    # 控件ID来自用户提供的8.34-5870程序实际启动页，不点击许可或其他弹窗。
                    dialog=desktop.window(handle=window.handle,process=window.element_info.process_id)
                    version_label=dialog.child_window(control_id=1317,class_name='Static')
                    start=dialog.child_window(control_id=1,class_name='Button')
                    # 已运行程序可能正在扫描；同标题窗口缺少Start结构时只等待，不猜按钮。
                    if version_label.exists(timeout=0) and start.exists(timeout=0):
                        version=version_label.window_text()
                        if '8.34-5870' not in version or start.window_text().replace('&','').strip()!='Start':
                            raise RuntimeError('HWiNFO弹窗不是已适配的Start启动页，未自动点击')
                        mode=dialog.child_window(control_id=2746,class_name='ComboBox')
                        if 'summary' in mode.window_text().lower():
                            mode.select('Full mode')
                        if window.is_minimized():window.restore()
                        start.wait('visible enabled',timeout=5)
                        record_action('点击Start，等待扫描与Sensors',window)
                        start.click()
                        start_clicked=True
        if time.monotonic()>=deadline:
            record['error']='90秒内未进入Sensors；程序可能仍在扫描、存在许可弹窗或已经退出'
            if cfg.get('_diagnostic_folder'):
                save_json(Path(cfg['_diagnostic_folder'])/'HWiNFO程序启动.json',record)
            raise RuntimeError('未能自动打开已绑定U盘的HWiNFO Sensors；'+record['error'])
        time.sleep(.25)
    window = candidates[0]
    if window.is_minimized():
        window.restore()
    record['passed']=True
    record_action('确认Sensors已出现',window)
    return desktop.window(handle=window.handle,process=window.element_info.process_id)


def hwinfo_log_button_state(sensor):
    """窗口恢复及停止后文字可能短暂为空；只读同一个按钮，绝不猜测并反复切换。"""
    from pywinauto import Desktop
    deadline = time.monotonic()+5
    detail = ''
    while True:
        try:
            button = sensor.child_window(control_id=1437, class_name='Button')
            label = button.window_text().replace('&', '').strip()
            if not label:
                # 实机现场UIA仍有Log Start，而原生window_text暂时返回空。
                wrapper = button.wrapper_object()
                label = Desktop(backend='uia').window(
                    handle=wrapper.handle, process=wrapper.element_info.process_id
                ).window_text().replace('&', '').strip()
            if label == 'Log Start':
                return 'stopped'
            if label in ('Log Stop', 'Stop Logging'):
                return 'logging'
            detail = repr(label)
        except Exception as exc:
            detail = f'{type(exc).__name__}: {exc}'
        if time.monotonic() >= deadline:
            raise RuntimeError('无法确认HWiNFO日志按钮状态，未点击：'+detail)
        time.sleep(.2)


def refresh_system_disk(cfg):
    """读取本机Windows系统盘；只更新自动字段，项目/批次/人工版本保留。"""
    raw = powershell("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
        "$p=Get-Partition -DriveLetter ($env:SystemDrive.TrimEnd(':')); $d=$p|Get-Disk; "
        "[pscustomobject]@{model=$d.FriendlyName;serial=$d.SerialNumber;bytes=$d.Size;drive=$env:SystemDrive}|ConvertTo-Json -Compress")
    detected = json.loads(raw)
    model, serial = detected['model'].strip(), detected['serial'].strip()
    if not model or not serial:
        raise RuntimeError('系统盘型号或序列号为空')
    detected.update(model=model, serial=serial, drive=detected.get('drive','C:').upper())
    capacity = {512:'512G',1000:'1T',2000:'2T'}
    match = [label for gb,label in capacity.items() if abs(detected['bytes']/1e9-gb)<gb*.08]
    if len(match)!=1:
        raise RuntimeError('系统盘容量不在当前公司模板512G/1T/2T范围')
    cfg['detected_disk'] = detected
    cfg['report'].update(capacity=match[0],system_disk_model=model,expected_serial=serial)
    expected = f'S.M.A.R.T.: {model} ({serial}) [{detected.get("drive","C:")}]'
    if storage_identifier_is_eui(serial):
        binding = cfg['hwinfo'].get('system_disk_binding', {})
        if (binding.get('storage_identifier') == serial and binding.get('model') == model
                and binding.get('bytes') == detected['bytes']
                and binding.get('drive') == detected.get('drive','C:')):
            expected = binding['sensor_group']
            cfg['report']['expected_serial'] = binding['serial']
        else:
            # 首轮到Sensors读取唯一系统盘SMART组，不能从EUI猜硬件序列号。
            expected = None
            cfg['hwinfo'].pop('system_disk_binding', None)
            cfg['report']['expected_serial'] = ''
    if cfg['hwinfo'].get('expected_sensor_group') != expected:
        cfg['hwinfo'].pop('logging_verification',None)
        cfg['hwinfo']['smart_refresh_verified'] = False
        cfg['hwinfo']['expected_sensor_group'] = expected


def storage_identifier_is_eui(serial):
    """部分NVMe驱动给Get-Disk返回EUI标识；它不能当成HWiNFO显示的硬件序列号。"""
    return re.fullmatch(r'[0-9A-Fa-f]{4}(?:_[0-9A-Fa-f]{4}){7}\.?', str(serial).strip()) is not None


def select_system_smart_group(labels, disk):
    """EUI场景按已查到的系统盘型号和系统盘符，限定唯一SMART组。"""
    drive = disk.get('drive', 'C:').upper()
    matches = set()
    for label in labels:
        match = re.fullmatch(r'S\.M\.A\.R\.T\.: (.+) \(([^()]+)\) \[([^\]]+)\]', label.strip())
        if match and match[1].strip().casefold() == disk['model'].strip().casefold():
            drives = re.findall(r'[A-Za-z]:', match[3].upper())
            if drive in drives:
                matches.add(label.strip())
    if len(matches) != 1:
        raise RuntimeError('未找到唯一的系统盘SMART组，请核对HWiNFO系统盘传感器：'+str(sorted(matches)))
    group = matches.pop()
    serial = re.fullmatch(r'S\.M\.A\.R\.T\.: (.+) \(([^()]+)\) \[([^\]]+)\]', group)[2]
    if storage_identifier_is_eui(serial):
        raise RuntimeError('HWiNFO也未提供硬件序列号，不能把EUI标识填入盘序列号')
    return group, serial


def bind_system_smart_group(cfg, sensor, folder):
    disk = cfg.get('detected_disk', {})
    if not storage_identifier_is_eui(disk.get('serial', '')):
        return
    from pywinauto import Desktop
    wrapper = sensor.wrapper_object()
    window = Desktop(backend='uia').window(handle=wrapper.handle, process=wrapper.element_info.process_id)
    items = window.child_window(auto_id='2301', control_type='List').descendants(control_type='ListItem')
    group, serial = select_system_smart_group([item.window_text() for item in items], disk)
    hw = cfg['hwinfo']
    proof = hw.get('logging_verification', {})
    if proof and proof.get('sensor_group') != group:
        hw.pop('logging_verification', None)
        hw['smart_refresh_verified'] = False
    hw['expected_sensor_group'] = group
    hw['system_disk_binding'] = {'storage_identifier':disk['serial'], 'model':disk['model'],
        'bytes':disk['bytes'], 'drive':disk.get('drive','C:'), 'sensor_group':group, 'serial':serial}
    cfg['report']['expected_serial'] = serial
    save_json(folder/'系统盘SMART对应.json', hw['system_disk_binding'])


def select_hwinfo_rows(native, index=None):
    """直接设置原生列表选择状态，不依赖Ctrl+Home/Ctrl+Shift+End的键盘锚点。

    index=None选择全部行；指定下标时先清除全部选择和焦点，再仅选择该行。
    LVITEM由目标控件提供（32/64位结构不同），数据写入目标进程内存。
    调用方必须读回选中范围，才能发送监测启停指令。
    """
    from pywinauto import win32defines
    from pywinauto.remote_memory_block import RemoteMemoryBlock
    native.verify_actionable()
    if index is not None and (type(index) is not int or not 0 <= index < native.item_count()):
        raise ValueError('SMART列表下标已失效，未更改选择')

    def set_state(row, state, mask):
        item = native.LVITEM()
        item.mask = win32defines.LVIF_STATE
        item.state = state
        item.stateMask = mask
        remote = RemoteMemoryBlock(native)
        try:
            remote.Write(item, size=ctypes.sizeof(item))
            if not native.send_message(win32defines.LVM_SETITEMSTATE, row, remote):
                raise RuntimeError('HWiNFO原生列表拒绝更新选择状态')
        finally:
            del remote

    selected = win32defines.LVIS_SELECTED
    focused = win32defines.LVIS_FOCUSED
    if index is None:
        set_state(-1, selected, selected)
    else:
        set_state(-1, 0, selected | focused)
        set_state(index, selected | focused, selected | focused)


def configure_hwinfo_smart_only(cfg, sensor, folder):
    """每次记录前按当前盘重新设置，不依赖旧Windows注册表里的传感器状态。

    8.34的传感器列表为2301。原生列表选择并读回后，用Del停用监测，
    再用Insert启用当前盘SMART组；不使用Shift+Del隐藏项目。
    最后还须通过新CSV表头检查，才可进入负载与正式计时。
    """
    from pywinauto import Desktop
    record_path = Path(folder)/'HWiNFO监测范围.json'
    record = {'policy': 'current_system_disk_smart_only', 'actions_completed': False,
              'csv_verified': False}
    save_json(record_path, record)
    try:
        if hwinfo_log_button_state(sensor) != 'stopped':
            raise RuntimeError('HWiNFO仍在记录旧日志，未修改传感器设置')
        wrapper = sensor.wrapper_object()
        record.update(pid=wrapper.element_info.process_id, handle=wrapper.handle)
        window = Desktop(backend='uia').window(handle=wrapper.handle, process=record['pid'])
        listing = window.child_window(auto_id='2301', control_type='List')
        native = sensor.child_window(control_id=2301, class_name='SysListView32').wrapper_object()

        def read_items():
            items = listing.descendants(control_type='ListItem')
            labels = [item.window_text().strip() for item in items]
            if not labels or len(labels) != native.item_count():
                raise RuntimeError('HWiNFO传感器列表读取不完整，未按错误行号操作')
            return items, labels

        def select_one(label):
            items, labels = read_items()
            indexes = [i for i, text in enumerate(labels) if text == label]
            if len(indexes) != 1:
                raise RuntimeError('HWiNFO传感器项目不是唯一项：'+label)
            index = indexes[0]
            native.get_item(index).ensure_visible()
            if native.get_item(index).text().strip() != label:
                raise RuntimeError('HWiNFO原生列表与读取的SMART行不一致，未发送启用指令')
            select_hwinfo_rows(native, index)
            if native.get_selected_count() != 1 or not native.get_item(index).is_selected():
                raise RuntimeError('未能单独选中当前SMART项目，未发送启用指令')

        _, before = read_items()
        report = cfg['report']
        disk = {'model': report['system_disk_model'],
                'drive': cfg.get('detected_disk', {}).get('drive', 'C:')}
        group, serial = select_system_smart_group(before, disk)
        if (group != cfg['hwinfo'].get('expected_sensor_group')
                or serial != report.get('expected_serial')):
            raise RuntimeError('Sensors当前系统盘与本轮盘片信息不一致，未更改监测范围')
        record.update(sensor_group=group, before=before, step='停用其他监测')
        save_json(record_path, record)
        if sensor.is_minimized():
            sensor.restore()
        sensor.set_focus()
        native.set_focus()
        select_hwinfo_rows(native)
        # 空白分隔行可能不可选；所有有名称的行都必须已选中。
        missing = [label for i, label in enumerate(before)
                   if label and not native.get_item(i).is_selected()]
        record['selection_method'] = 'LVM_SETITEMSTATE'
        record['unselected_rows'] = missing
        if missing:
            raise RuntimeError('原生列表未确认全部传感器选中，未发送停用指令：'+str(missing[:8]))
        native.type_keys('{DELETE}', pause=.05)
        select_one(group)  # 停用后列表可能折叠，重新按名称定位，绝不复用旧下标。
        native.type_keys('{INSERT}', pause=.05)
        record['step'] = '已启用当前盘SMART，等待CSV核对'
        record['after'] = read_items()[1]
        record['actions_completed'] = True
        print('HWiNFO已设置只监测当前盘SMART，正在核对新日志。', flush=True)
        return record
    except BaseException as exc:
        record['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        save_json(record_path, record)


def verify_hwinfo_start_header(cfg, path, folder):
    """在测试开始前发现多余传感器/缺少温度列，不能等跑完才发现表头错误。"""
    settings = cfg['hwinfo']
    with Path(path).open(encoding=settings['encoding'], newline='') as stream:
        first_line = stream.readline(1024*1024)
    if not first_line.endswith(('\n', '\r')):
        return False  # 写入尚未完成，继续等同一次日志。
    header = next(csv.reader([first_line], delimiter=settings['delimiter'], strict=True))
    expected = settings.get('expected_header')
    record_path = Path(folder)/'HWiNFO监测范围.json'
    record = json.loads(record_path.read_text('utf-8'))
    record['actual_header'] = header
    record['csv_verified'] = bool(expected and header == expected)
    save_json(record_path, record)
    if not record['csv_verified']:
        raise RuntimeError('HWiNFO新日志仍包含其他传感器或缺少SMART列，本项尚未开始计时；'
                           '请查看HWiNFO监测范围.json中的实际表头')
    return True


def read_hwinfo_polling(dialog):
    """真实设置页：全局毫秒 × SMART周期倍数，才是SMART刷新间隔。"""
    return {'global_ms': int(dialog.child_window(control_id=2158, class_name='Edit').window_text()),
            'smart_cycles': int(dialog.child_window(control_id=2162, class_name='Edit').window_text()),
            'direct_to_disk': dialog.child_window(control_id=2181, class_name='Button').get_check_state() == 1}


def open_hwinfo_settings(sensor):
    """恢复本轮Sensors；普通点击未打开时补一次实际按钮点击，再固定设置页句柄。"""
    from pywinauto import Desktop
    from pywinauto.timings import TimeoutError as WindowTimeout
    pid = sensor.wrapper_object().element_info.process_id
    desktop = Desktop(backend='win32')
    dialog = desktop.window(
        process=pid, class_name='#32770', title_re=r'HWiNFO.*Sensor Settings')
    if dialog.exists(timeout=.2) and dialog.is_visible():
        raise RuntimeError('HWiNFO已有设置窗口，请先取消该窗口；未修改或关闭旧设置')
    if sensor.is_minimized(): sensor.restore()
    sensor.set_focus()
    button = sensor.child_window(control_id=1436, class_name='Button')
    button.wait('visible enabled', timeout=5)
    button.click()
    try:
        dialog.wait('visible enabled', timeout=3)
    except WindowTimeout:
        # 已出现但仍忙的设置页只等待，不能重复点击造成关闭或误操作。
        if not (dialog.exists(timeout=.2) and dialog.is_visible()):
            sensor.set_focus()
            button.click_input()
        try:
            dialog.wait('visible enabled', timeout=12)
        except WindowTimeout as exc:
            raise RuntimeError('HWiNFO设置页未打开；已恢复Sensors并重试设置按钮，本项尚未开始计时') from exc
    return desktop.window(handle=dialog.wrapper_object().handle, process=pid)


def select_hwinfo_general_page(dialog):
    """常规控件已显示就直接使用，否则通过实机采集成功的UIA TabItem切页。"""
    from pywinauto import Desktop
    controls = [dialog.child_window(control_id=control_id, class_name=class_name)
                for control_id, class_name in ((2158, 'Edit'), (2162, 'Edit'),
                                               (2159, 'Button'), (2181, 'Button'),
                                               (1172, 'Button'), (2506, 'Edit'))]
    if all(control.exists(timeout=.2) and control.is_visible() and control.is_enabled()
           for control in controls):
        return 'General控件已就绪，无需切页'
    window = dialog.wrapper_object()
    uia_dialog = Desktop(backend='uia').window(
        handle=window.handle, process=window.element_info.process_id)
    # 不使用SysTabControl32.select('General')：现场原生文本枚举返回过0个标签。
    general = uia_dialog.child_window(title='General', control_type='TabItem')
    general.wait('visible enabled', timeout=10)
    general.select()
    for control in controls:
        control.wait('visible enabled', timeout=10)
    return 'UIA General标签已选择，常规控件已就绪'


def save_hwinfo_failure_context(pid, folder):
    """仅采集已确认的HWiNFO进程；采集失败也必须留下原因。"""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    result = {'pid': pid if isinstance(pid, int) else None, 'completed': False}
    save_json(folder/'现场采集结果.json', result)
    try:
        if not isinstance(pid, int) or pid <= 0:
            raise ValueError('未获得HWiNFO进程ID，未读取其他程序窗口')
        from 继续软件适配 import save_window_diagnostics
        save_window_diagnostics(pid, folder, str(pid))
        windows = json.loads((folder/f'{pid}_窗口清单.json').read_text('utf-8-sig'))
        result['window_count'] = len(windows)
        if not windows:
            raise RuntimeError('该HWiNFO进程已无窗口，可能已退出；未改采其他进程')
        result['completed'] = True
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        save_json(folder/'现场采集结果.json', result)
    return result


def configure_hwinfo_sampling(sensor, interval_s, record_path):
    """每项测试开始前设置并重新打开核对，防止上次的30秒或100倍周期残留。"""
    if interval_s not in (30, 60):
        raise ValueError('当前已适配的HWiNFO刷新间隔为30秒或60秒')
    if hwinfo_log_button_state(sensor) != 'stopped':
        raise RuntimeError('HWiNFO已有日志正在记录，请先结束该记录；未更改其设置')
    dialog = None
    record = {'requested_interval_s': interval_s, 'passed': False, 'step': '打开设置'}
    save_json(record_path, record)
    try:
        dialog = open_hwinfo_settings(sensor)
        record['step'] = '定位General采样控件'
        record['initial_page'] = select_hwinfo_general_page(dialog)
        record['before'] = read_hwinfo_polling(dialog)
        record['step'] = '写入刷新设置'
        dialog.child_window(control_id=2158, class_name='Edit').set_edit_text(str(int(interval_s*1000)))
        dialog.child_window(control_id=2162, class_name='Edit').set_edit_text('1')
        dialog.child_window(control_id=2159, class_name='Button').click()  # Polling Period的Set
        # 每个样本及时写入CSV；不采用内存缓冲，便于检查缺失与传输。
        dialog.child_window(control_id=2181, class_name='Button').check_by_click()
        dialog.child_window(control_id=1172, class_name='Button').check_by_click()  # Celsius
        dialog.child_window(control_id=2506, class_name='Edit').set_edit_text('')  # 由任务控制停止
        dialog.child_window(control_id=1, class_name='Button').click()
        dialog.wait_not('visible', timeout=10)
        dialog = None
        record['step'] = '重新打开设置核对'
        dialog = open_hwinfo_settings(sensor)
        record['readback_page'] = select_hwinfo_general_page(dialog)
        record['readback'] = read_hwinfo_polling(dialog)
        record['passed'] = record['readback'] == {
            'global_ms': int(interval_s*1000), 'smart_cycles': 1, 'direct_to_disk': True}
        if not record['passed']:
            raise RuntimeError('HWiNFO设置回读不一致：'+json.dumps(record['readback'], ensure_ascii=False))
        record['step'] = '刷新设置回读通过'
        return record
    except BaseException as exc:
        record['passed'] = False
        record['error'] = f'{type(exc).__name__}: {exc}'
        save_json(record_path, record)
        try:
            print('正在保存HWiNFO设置失败现场……', flush=True)
            pid = sensor.wrapper_object().element_info.process_id
            record['diagnostics'] = save_hwinfo_failure_context(
                pid, Path(record_path).parent/'设置失败现场')
        except Exception as capture_error:
            record['diagnostics_error'] = str(capture_error)
        raise
    finally:
        try:
            # 只关闭本次打开并取得了确切句柄的设置页；不误关预先存在的弹窗。
            if dialog is not None and dialog.exists() and dialog.is_visible():
                dialog.child_window(control_id=2, class_name='Button').click()
                dialog.wait_not('visible', timeout=10)
        except Exception as cleanup_error:
            record['passed'] = False
            record['cleanup_error'] = str(cleanup_error)
            if 'error' not in record:
                raise RuntimeError('HWiNFO设置已读取，但设置窗口未能关闭，请手动取消') from cleanup_error
        finally:
            save_json(record_path, record)


def start_hwinfo_834_logging(cfg, folder, interval):
    """在本次任务目录自动保存CSV；设置、保存窗口和停止操作都限定同一PID。"""
    from pywinauto import Desktop
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder/'hwinfo.csv'
    if path.exists():
        raise RuntimeError('本次HWiNFO文件已经存在，未覆盖：'+str(path))
    sensor = hwinfo_sensor_window(cfg)
    bind_system_smart_group(cfg, sensor, folder)
    save_json(folder/'HWiNFO窗口定位.json', {
        'pid': sensor.wrapper_object().element_info.process_id,
        'sensor_handle': sensor.wrapper_object().handle, 'interval_s': interval})
    configure_hwinfo_smart_only(cfg, sensor, folder)
    configure_hwinfo_sampling(sensor, interval, folder/'HWiNFO刷新设置.json')
    pid = sensor.wrapper_object().element_info.process_id
    save_dialog = Desktop(backend='win32').window(process=pid, class_name='#32770', title='另存为')
    if save_dialog.exists() and save_dialog.is_visible():
        raise RuntimeError('HWiNFO还有旧保存弹窗，请先取消，未操作旧弹窗')
    if hwinfo_log_button_state(sensor) != 'stopped':
        raise RuntimeError('HWiNFO已有记录，未重新启动或覆盖')
    session = {'pid': pid, 'handle': sensor.wrapper_object().handle, 'csv_path': str(path),
               'save_requested': False}
    cfg['hwinfo']['ui']['_logging_session'] = session
    sensor.child_window(control_id=1437, class_name='Button').click()
    try:
        save_dialog.wait('visible enabled', timeout=15)
        save_dialog.child_window(control_id=1152, class_name='Edit').set_edit_text(str(path))
        session['save_requested'] = True
        save_dialog.child_window(control_id=1, class_name='Button').click()
        save_dialog.wait_not('visible', timeout=15)
        deadline = time.monotonic()+interval+20
        while time.monotonic()<deadline:
            if (hwinfo_log_button_state(sensor) == 'logging' and path.is_file() and path.stat().st_size
                    and verify_hwinfo_start_header(cfg, path, folder)):
                save_json(folder/'HWiNFO日志启动.json', {'pid': pid, 'path': str(path),
                    'interval_s': interval, 'start_epoch': time.time()})
                return path
            time.sleep(.2)
        raise RuntimeError('HWiNFO未确认开始记录或未生成本次CSV')
    except BaseException:
        # 仅取消本次打开的、同进程中的保存对话框；不关闭传感器主窗口。
        if save_dialog.exists() and save_dialog.is_visible():
            save_dialog.child_window(control_id=2, class_name='Button').click()
            save_dialog.wait_not('visible', timeout=10)
        stop_hwinfo_logging(cfg)
        raise


def desktop_diagnostics():
    """只读当前执行端的桌面名称；读取失败不代表已锁屏，也不能妨碍收尾。"""
    result = {'role':'host', 'pid':os.getpid(), 'epoch':time.time()}
    try:
        import win32api
        import win32con
        import win32service
        desktop = win32service.GetThreadDesktop(win32api.GetCurrentThreadId())
        result['thread_desktop'] = win32service.GetUserObjectInformation(desktop, win32con.UOI_NAME)
        current = win32service.OpenInputDesktop(0, False, win32con.DESKTOP_READOBJECTS)
        try:
            result['input_desktop'] = win32service.GetUserObjectInformation(current, win32con.UOI_NAME)
        finally:
            current.CloseDesktop()
    except Exception as exc:
        result['read_error'] = f'{type(exc).__name__}: {exc}'
    return result


def stop_hwinfo_logging(cfg):
    profile = cfg['hwinfo']['ui']
    if profile.get('adapter') != 'hwinfo_834':
        return run_ui_actions(profile, 'stop_logging_actions')
    session = profile.get('_logging_session')
    if not session:
        return  # 未启动过本次日志，不能误停测试人员原有记录。
    from pywinauto import Desktop
    sensor = Desktop(backend='win32').window(handle=session['handle'], process=session['pid'])
    if session['save_requested']:
        record = {'pid':session['pid'], 'handle':session['handle'], 'clicked_stop':False, 'passed':False}
        record_path = Path(session['csv_path']).parent/'HWiNFO日志停止.json' if session.get('csv_path') else None
        try:
            if sensor.is_minimized():
                sensor.restore()
            sensor.wait('exists visible enabled',timeout=10)
            record['before'] = hwinfo_log_button_state(sensor)
            if record['before'] == 'logging':
                button=sensor.child_window(control_id=1437, class_name='Button')
                button.wait('visible enabled',timeout=5)
                button.verify_actionable()
                input_attempted = False
                try:
                    sensor.set_focus()
                    input_attempted = True
                    button.click_input()
                    record['clicked_stop'] = True
                    record['click_method'] = 'focused_button_center'
                except Exception as exc:
                    record['input_error'] = str(exc)
                    record['desktop'] = desktop_diagnostics()
                    # SetCursorPos失败发生在鼠标按下之前；此时允许向同一按钮发原生消息。
                    # 其他输入异常可能已点中按钮，不能盲目再点，把停止变成重新开始。
                    record['after_input_error'] = hwinfo_log_button_state(sensor)
                    if record['after_input_error'] != 'stopped':
                        if input_attempted and 'There is no active desktop required for moving mouse cursor' not in str(exc):
                            raise
                        button.click(coords=button.client_rect().mid_point())
                        record['clicked_stop'] = True
                        record['click_method'] = 'native_button_message'
            deadline = time.monotonic()+10
            while time.monotonic()<deadline:
                record['after'] = hwinfo_log_button_state(sensor)
                if record['after'] == 'stopped':
                    record['passed'] = True
                    profile.pop('_logging_session', None)
                    return
                time.sleep(.2)
            raise RuntimeError('HWiNFO日志尚未停止，请结束记录后保存本次CSV')
        except Exception as exc:
            record['error'] = str(exc)
            record.setdefault('desktop', desktop_diagnostics())
            raise
        finally:
            if record_path:
                save_json(record_path, record)
    profile.pop('_logging_session', None)


def playback_seconds(text):
    """读取已用时间：支持 31:47、1:02:03，以及系统播放器的中文时分秒。"""
    original = text
    text = re.sub(r'^(?:已用时间|Elapsed time)\s*[:：]?\s*', '', text.strip(), flags=re.I)
    colon = re.fullmatch(r'(\d+):([0-5]\d)(?::([0-5]\d))?', text)
    if colon:
        parts = [int(value) for value in colon.groups() if value is not None]
        result = 0
        for value in parts:
            result = result*60+value
        return result
    chinese = re.fullmatch(
        r'(?:(\d+)\s*(?:小时|时)\s*)?(?:(\d+)\s*(?:分钟|分)\s*)?(?:(\d+)\s*秒)?', text)
    if chinese and any(value is not None for value in chinese.groups()):
        hours, minutes, seconds = (int(value or 0) for value in chinese.groups())
        return hours*3600+minutes*60+seconds
    raise RuntimeError('未能从播放器读取已用时间：'+original)


def local_video_duration(path):
    """读取系统媒体属性中的真实时长（100纳秒单位），用于时间标签为空时换算进度条。"""
    from win32com.client import Dispatch
    shell=Dispatch('Shell.Application')
    folder=shell.NameSpace(str(path.parent))
    item=folder.ParseName(path.name) if folder is not None else None
    value=item.ExtendedProperty('System.Media.Duration') if item is not None else None
    seconds=float(value)/10_000_000
    if not math.isfinite(seconds) or seconds<=0:
        raise RuntimeError('Windows未提供有效视频时长')
    return seconds


def reveal_local_video_controls(profile):
    """播放中控制栏会隐藏；仅移动鼠标唤出，不点击画面、不切换暂停。"""
    window = ui_window(profile).wrapper_object()
    if window.is_minimized():
        window.restore()
        window.maximize()
    window.set_focus()
    rect = window.rectangle()
    if rect.width() <= 0 or rect.height() <= 0:
        raise RuntimeError('本次播放器没有可见播放区域')
    # 位置相对本轮窗口尺寸；两次移动保证鼠标原本停在窗口内时也会唤出控制栏。
    for fraction in (.70, .80):
        window.move_mouse_input(coords=(rect.left+int(rect.width()*.5), rect.top+int(rect.height()*fraction)), absolute=True)


def local_video_control(window, selector, refresh_controls):
    control = window.child_window(**selector)
    if refresh_controls and not control.exists(timeout=.4):
        raise RuntimeError('播放器控制栏尚未出现：'+str(selector.get('auto_id', '已用时间')))
    return control


class MediaSessionIdentityError(RuntimeError):
    """系统已返回媒体，但不是本轮唯一的视频；不能再借界面读数绕过。"""


def select_local_media_session(sessions, profile):
    """绑定本轮应用与视频；不能拿浏览器或另一文件的进度代替本地视频。"""
    binding = profile.get('_media_binding')
    duration = profile.get('_duration_s')
    names = {Path(profile['_media_file']).name.casefold(), Path(profile['_media_file']).stem.casefold()}
    candidates = []
    usable_timeline = False
    for item in sessions:
        if item.get('error') or item.get('app_id') not in profile['_media_app_ids']:
            continue
        values = [item.get(key) for key in ('position_s','start_s','end_s','updated_epoch')]
        if any(isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) for value in values):
            continue
        position, start, end, updated = values
        if not 0 <= start <= position <= end or end <= start: continue
        title = str(item.get('title') or '').strip()
        if not title: continue
        usable_timeline = True
        if not profile['_opened_epoch']-2 <= updated <= time.time()+2: continue
        if binding:
            matches = item['app_id'] == binding['app_id'] and title == binding['title'] and abs(end-binding['end_s']) <= .1
        else:
            same_title = title.casefold() in names
            same_duration = isinstance(duration,(int,float)) and math.isfinite(duration) and abs(end-start-duration) <= 2
            matches = bool(title) and (same_title or same_duration)
        if matches: candidates.append(item)
    if len(candidates) != 1:
        if not usable_timeline:
            raise RuntimeError('播放器媒体会话尚未提供有效视频标题和时间线')
        raise MediaSessionIdentityError('本轮播放器没有唯一匹配视频的媒体会话（候选'+str(len(candidates))+'个）')
    selected = candidates[0]
    if not binding:
        profile['_media_binding'] = {key:selected[key] for key in ('app_id','title','end_s')}
    return selected


def read_local_media_position(profile):
    """读取播放器主动报告的位置，不按Playing状态或系统时间推算进度。"""
    window = ui_window(profile).wrapper_object()  # 每次核实本轮窗口仍存在。
    profile['_media_app_ids'] = window_app_ids(window.handle, window.process_id())
    sessions = read_media_sessions(profile['_media_app_ids'])
    profile['_media_snapshot'] = sessions
    if not sessions or all(item.get('error') for item in sessions):
        raise RuntimeError('播放器尚未提供可读媒体会话')
    return select_local_media_session(sessions, profile)


def read_local_video_position(profile):
    """旧版进度读取，仅保留诊断兼容；本地视频计时与验收不再调用。"""
    text=''
    profile.pop('_position_observation',None)
    # 系统媒体会话不会随UWP播放控件隐藏而从窗口树消失，优先读取这条通道。
    if profile.get('_media_file'):
        try:
            media = read_local_media_position(profile)
        except MediaSessionIdentityError:
            raise
        except Exception as exc:
            profile['_media_read_error'] = str(exc)
        else:
            profile.pop('_media_read_error', None)
            profile['_position_observation'] = {'source':'windows_media_session', **media}
            if media['status'] != 'Playing':
                raise RuntimeError('本轮播放器实际状态为'+media['status']+'，尚未确认正在播放')
            return media['position_s']
    window=ui_window(profile)
    refresh_controls = profile.get('_reveal_controls', False)
    if refresh_controls:
        reveal_local_video_controls(profile)
    try:
        text=local_video_control(window,profile['position_control'],refresh_controls).window_text()
        position=playback_seconds(text)
        profile['_position_observation']={'source':'elapsed_label','label':text}
        return position
    except Exception as exc:
        label_error=str(exc)
        duration=profile.get('_duration_s')
        if not isinstance(duration,(int,float)) or not math.isfinite(duration) or duration<=0:
            raise RuntimeError('无法读取播放器已用时间，且没有有效视频时长可换算进度条：'+label_error) from exc
    # 时间标签为空或暂未出现时改读真实进度条；不能拿程序计时冒充播放进度。
    try:
        slider=local_video_control(window,{'auto_id':'ProgressSlider','control_type':'Slider'},refresh_controls).wrapper_object().iface_range_value
    except Exception as exc:
        if profile.get('_media_read_error'):
            raise RuntimeError('系统媒体会话：'+profile['_media_read_error']+'；界面进度：'+str(exc)) from exc
        raise
    low,high,value=map(float,(slider.CurrentMinimum,slider.CurrentMaximum,slider.CurrentValue))
    if not all(math.isfinite(v) for v in (low,high,value)) or high<=low or not low<=value<=high:
        raise RuntimeError('播放器时间标签为空，进度条也没有有效值')
    profile['_position_observation']={'source':'progress_slider','label':text,'label_error':label_error,'duration_s':duration,
                                     'minimum':low,'maximum':high,'value':value}
    return duration*(value-low)/(high-low)


def check_test_cancelled(stop):
    if stop is not None and stop.is_set():
        raise InterruptedError('测试人员取消，停止启动负载')


def start_local_video(cfg, status=None, stop=None):
    check_test_cancelled(stop)
    if cfg['local_video'].get('adapter') == 'windows_media_player':
        return start_windows_media_player(cfg, status=status, stop=stop)
    from pywinauto import Desktop
    profile = copy.deepcopy(cfg['local_video']['ui'])
    if Desktop(backend=profile.get('backend','uia')).windows(**{**profile['window'], 'visible_only':False}):
        raise RuntimeError('本地播放器已有窗口，请先关闭；最小化不算关闭')
    os.startfile(str(Path(cfg['local_video']['file']).resolve()))
    workload = None
    try:
        window = ui_window(profile)
        window.wait('visible',timeout=40)
        wrapper = window.wrapper_object()
        profile['window'] = {'handle':wrapper.handle, 'process':wrapper.process_id()}
        workload = {'profile':profile}
        check_test_cancelled(stop)
        maximize_local_video_window(wrapper)
        check_test_cancelled(stop)
        run_ui_actions(profile,'play_actions')
        check_test_cancelled(stop)
        workload['timer_source'] = 'program_monotonic'
        workload['playback_evidence'] = 'configured_play_actions_completed'
        return workload
    except BaseException as exc:
        if workload:
            try:
                stop_local_video(workload)
            except Exception as cleanup:
                raise WorkloadCleanupError(str(exc)+'；播放器未能关闭：'+str(cleanup)) from exc
        else:
            raise WorkloadCleanupError(str(exc)+'；已打开视频但未能定位本次窗口，请关闭播放器后重试') from exc
        raise


def maximize_local_video_window(window):
    """最大化系统播放器的外层窗口，回读成功后才允许开始本项计时。"""
    window.maximize()
    window.set_focus()
    deadline = time.monotonic()+5
    while time.monotonic()<deadline:
        if window.is_maximized(): return
        time.sleep(.2)
    raise RuntimeError('本地播放器窗口未能最大化，本项尚未开始计时')


def stop_local_video(workload):
    # 只关闭本次新打开的窗口，不能按进程名杀死其他播放器。
    if not workload['profile']['window'].get('handle'):
        raise WorkloadCleanupError('缺少本次播放器窗口句柄，不能确认关闭目标')
    window = ui_window(workload['profile'])
    if window.exists(timeout=1):
        window.close()
        window.wait_not('exists', timeout=15)


def local_video_window_is_open(workload):
    """只检查本轮播放器窗口仍存在，不读取播放位置。"""
    return ui_window(workload['profile']).exists(timeout=.2)


def workload_window_problems(cfg):
    """只检查旧负载窗口，不关闭窗口；待机前也要排除已有播放器和 BIT。"""
    from pywinauto import Desktop
    problems = []
    player = dict(cfg['local_video']['ui']['window'])
    player['visible_only'] = False
    for label, backend, selector in (
        ('媒体播放器', cfg['local_video']['ui'].get('backend', 'uia'), player),
        ('BurnInTest', 'win32', {'class_name': 'bit', 'visible_only': False}),
    ):
        try:
            windows = Desktop(backend=backend).windows(**selector)
            if windows:
                problems.append(f'测试主机仍有 {len(windows)} 个{label}窗口；请真正关闭窗口，最小化不算关闭。')
        except Exception as exc:
            problems.append(label+'启动前窗口检查失败：'+str(exc))
    return problems


def start_windows_media_player(cfg, status=None, stop=None):
    """打开本轮播放器、最大化并发出播放指令；程序独立计时，结束关闭窗口。"""
    from pywinauto import Desktop
    check_test_cancelled(stop)
    video = cfg['local_video']
    profile = copy.deepcopy(video['ui'])
    profile['_reveal_controls'] = True
    existing = Desktop(backend='uia').windows(**{**profile['window'], 'visible_only':False})
    if existing:
        raise RuntimeError('本地播放器已有窗口，请在集中联调开始前关闭；避免操作正在播放的其他文件')
    path = Path(video['file']).resolve(strict=True)
    if path.drive.upper() != os.environ.get('SystemDrive','C:').upper():
        raise RuntimeError('本地视频必须在被测系统盘：'+str(path))
    if status is not None:
        status.update(phase='本地视频启动检查',message='本地视频：正在打开播放器，尚未开始测试计时')
    check_test_cancelled(stop)
    profile['_media_file'] = str(path)
    profile['_opened_epoch'] = time.time()
    os.startfile(str(path))
    workload = None
    record = {'file':str(path), 'passed':False, 'observations':[]}
    try:
        window = ui_window(profile)
        window.wait('visible', timeout=40)
        wrapper = window.wrapper_object()
        profile['window'] = {'handle':wrapper.handle, 'process':wrapper.process_id()}
        workload = {'adapter':'windows_media_player', 'profile':profile, 'file':str(path)}
        record['window'] = profile['window']
        check_test_cancelled(stop)
        if status is not None:
            status['message']='本地视频：正在最大化窗口并启动播放，尚未开始测试计时'
        maximize_local_video_window(wrapper)
        check_test_cancelled(stop)
        reveal_local_video_controls(profile)
        progress = ui_window(profile).child_window(auto_id='ProgressSlider',control_type='Slider')
        workload.update(prepare_local_video_playback(profile, progress, record, status=status, stop=stop))
        check_test_cancelled(stop)
        record['passed'] = True
        record['media_binding'] = profile.get('_media_binding')
        if cfg.get('_diagnostic_folder'):
            save_json(Path(cfg['_diagnostic_folder'])/'本地视频启动检查.json', record)
        return workload
    except BaseException as exc:
        record['error'] = str(exc)
        record['media_binding'] = profile.get('_media_binding')
        record['media_snapshot'] = profile.get('_media_snapshot')
        record['media_read_error'] = profile.get('_media_read_error')
        if workload:
            # 先保留仍然存在的播放器控件，再关闭；否则联调末尾只能采到HWiNFO窗口。
            if cfg.get('_diagnostic_folder'):
                try:
                    with io.StringIO() as text, redirect_stdout(text):
                        ui_window(profile).print_control_identifiers(depth=6)
                        (Path(cfg['_diagnostic_folder'])/'本地视频失败窗口.txt').write_text(text.getvalue(),'utf-8')
                except Exception as capture_error:
                    record['window_capture_error'] = str(capture_error)
            try:
                stop_local_video(workload)
            except Exception as cleanup:
                raise WorkloadCleanupError(str(exc)+'；播放器未能关闭：'+str(cleanup)) from exc
        else:
            raise WorkloadCleanupError(str(exc)+'；已发出打开视频指令，但未能定位本次窗口，请关闭播放器后重试') from exc
        raise
    finally:
        if not record['passed'] and cfg.get('_diagnostic_folder'):
            # 失败时先关闭本次窗口，再保存记录；写盘失败不能跳过关窗。
            try:
                save_json(Path(cfg['_diagnostic_folder'])/'本地视频启动检查.json', record)
            except Exception:
                pass


def prepare_local_video_playback(profile, progress, record, status=None, stop=None):
    """只启动播放，不读取或比较播放进度。计时由execute_stage的单调时钟负责。"""
    check_test_cancelled(stop)
    record.update(timer_source='program_monotonic', position_monitoring=False)
    if status is not None:
        status['message']='本地视频：正在启动播放，随后由程序独立计时'
    # 能找到进度条时尽量从片头开始；控件缺失/读数错误不阻止本项测试。
    try:
        if progress.exists(timeout=.5):
            slider = progress.wrapper_object().iface_range_value
            low, high = float(slider.CurrentMinimum), float(slider.CurrentMaximum)
            if math.isfinite(low) and math.isfinite(high) and high > low:
                check_test_cancelled(stop)
                slider.SetValue(low)
                record['seek'] = {'requested':True, 'minimum':low, 'maximum':high,
                                  'readback_required':False}
    except InterruptedError:
        raise
    except Exception as exc:
        record['seek_note'] = '未能回到片头，继续启动播放：'+str(exc)

    deadline = time.monotonic()+15
    last_error = ''
    while time.monotonic()<deadline:
        check_test_cancelled(stop)
        window = ui_window(profile)
        window.wait('exists visible', timeout=2)
        try:
            play = window.child_window(auto_id='MTCPlayPauseButton.Play', control_type='Button')
            pause = window.child_window(auto_id='MTCPlayPauseButton.Pause', control_type='Button')
            if play.exists(timeout=.2):
                check_test_cancelled(stop)
                play.invoke()  # 明确的播放按钮，不用空格键切换，避免误暂停。
                evidence = 'play_button_invoked'
            elif pause.exists(timeout=.2):
                evidence = 'already_playing_pause_button'
            else:
                # 控制栏隐藏时只读Playing状态，不调用时间线、不等进度前进。
                wrapper = window.wrapper_object()
                app_ids = window_app_ids(wrapper.handle, wrapper.process_id())
                sessions = read_media_sessions(app_ids, include_timeline=False)
                owned = [item for item in sessions if item.get('app_id') in app_ids and not item.get('error')]
                if len(owned) != 1 or owned[0].get('status') != 'Playing':
                    raise RuntimeError('暂未找到播放按钮或正在播放状态')
                record['media_playback'] = owned[0]
                evidence = 'media_status_playing'
            check_test_cancelled(stop)
            record['observations'].append({'epoch':time.time(), 'action':evidence})
            record['playback_evidence'] = evidence
            return {'timer_source':'program_monotonic', 'playback_evidence':evidence,
                    'position_monitoring':False}
        except InterruptedError:
            raise
        except Exception as exc:
            last_error = str(exc)
        time.sleep(.5)
    raise RuntimeError('播放器已打开，但未能发出播放指令或确认正在播放状态：'+last_error)


def select_bilibili_1080(page, timeout=10000, force=False):
    """不足1080P时选择1080P档；已有更高清晰度时不主动降档。"""
    if not force and bilibili_quality_at_least_1080(bilibili_quality_state(page)):
        return
    quality = page.get_by_role('button',name='清晰度',exact=True)
    option = quality.locator('li[data-value="80"]')
    # 菜单已经展开时不能再点击按钮，否则会把菜单关掉。
    if not option.is_visible():
        quality.click(timeout=timeout)
    label = option.inner_text(timeout=timeout)
    if '1080P' not in label or '自动' in label:
        raise RuntimeError('B站清晰度页面已变化，未找到1080P高清选项')
    option.click(timeout=timeout)


def bilibili_quality_state(page):
    """读取页面显示的选中项与档位名；不调用播放器私有接口、不修改播放流。"""
    return page.evaluate('''() => {
      const menus = document.querySelectorAll('[role="button"][aria-label="清晰度"]');
      if (menus.length !== 1) return {selected:null, label:'', choices:[]};
      const menu=menus[0], active=menu.querySelectorAll('li.bpx-state-active');
      return {
        selected: active.length===1 ? active[0].getAttribute('data-value') : null,
        label: menu.querySelector('.bpx-player-ctrl-quality-result')?.textContent.trim() || '',
        choices: Array.from(menu.querySelectorAll('li[data-value]'), n=>({
          value:n.getAttribute('data-value'), label:n.textContent.trim()}))};
    }''')


def bilibili_quality_at_least_1080(quality):
    """菜单只用于选择清晰度；通过与否以实际视频尺寸和帧率为准。"""
    if not isinstance(quality, dict): return False
    label = str(quality.get('label', '')).upper()
    return (quality.get('selected') not in (None,'0') and '自动' not in label
            and any(token in label for token in ('1080P','1440P','2160P','2K','4K','8K')))


def video_resolution_is_sufficient(sample):
    return sample.get('width',0) >= 1920 and sample.get('height',0) >= 1080


def restore_bilibili_quality(page, sample, attempts):
    """检测偏离后最多重选3次，间隔15秒；不重置视频进度、不暂停测试计时。"""
    if (sample.get('readyState',4)<3 or (sample.get('frame_age_s') or 0)>2
            or sample.get('frame_span_s',1)<1):
        return None
    pixels_bad = not video_resolution_is_sufficient(sample)
    fps = sample.get('observed_fps')
    fps_bad = (sample.get('frame_span_s', 0) >= 1 and isinstance(fps, (int, float))
               and math.isfinite(fps) and fps < 29.5)
    if not (pixels_bad or fps_bad): return None
    now = time.monotonic()
    if attempts.get('count', 0) >= 3 or now < attempts.get('next_at', 0): return None
    attempts['count'] = attempts.get('count', 0)+1
    attempts['next_at'] = now+15
    record = {'epoch':time.time(), 'attempt':attempts['count'],
              'before':sample.get('quality'), 'action':'实际画面低于最低要求，重新请求1080P'}
    try:
        select_bilibili_1080(page, timeout=1000, force=True)
        record['result'] = '已请求，等待后续实际档位/分辨率/帧率验证'
    except Exception as exc:
        record['result'] = '重选失败：'+str(exc)
    return record


def stop_online_video(workload):
    """即使浏览器窗口关闭报错，也释放本轮Playwright运行时。"""
    try:
        if workload.get('context'):
            workload['context'].close()
    finally:
        workload['runtime'].stop()


def maximize_online_video_window(context, page):
    """用本次页面的窗口ID最大化Edge；不发F11、不按标题操作其他浏览器。"""
    session = context.new_cdp_session(page)
    try:
        page.bring_to_front()
        current = session.send('Browser.getWindowForTarget')
        window_id = current['windowId']
        state = current['bounds'].get('windowState')
        if state in ('minimized','fullscreen'):
            session.send('Browser.setWindowBounds', {'windowId':window_id,'bounds':{'windowState':'normal'}})
        if state != 'maximized':
            session.send('Browser.setWindowBounds', {'windowId':window_id,'bounds':{'windowState':'maximized'}})
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            bounds = session.send('Browser.getWindowBounds', {'windowId':window_id})['bounds']
            if bounds.get('windowState') == 'maximized': return bounds
            page.wait_for_timeout(200)
        raise RuntimeError('在线视频浏览器窗口未能最大化，本项尚未开始计时')
    finally:
        session.detach()


VIDEO_FULLSCREEN_SCRIPT = '''v => {
    const element=document.fullscreenElement, rect=element?.getBoundingClientRect();
    return {active:!!element, contains_video:!!element && element.contains(v),
      fills_viewport:!!rect && Math.abs(rect.width-innerWidth)<=4 && Math.abs(rect.height-innerHeight)<=4,
      width:rect?.width || 0, height:rect?.height || 0};
}'''


class OnlineVideoUnavailable(RuntimeError):
    """网页仍在，但切流时暂时没有可读取的视频；与浏览器退出区分。"""


def active_online_video(page):
    """切换画质会同时留下旧video和新video；选择画面前层，而不是固定第一个元素。"""
    videos = page.locator('video')
    index = videos.evaluate_all(ACTIVE_VIDEO_SCRIPT)
    if index < 0:
        raise OnlineVideoUnavailable('当前页面没有可见的视频画面')
    return videos.nth(index)


ACTIVE_VIDEO_SCRIPT = '''videos => {
  const visible=videos.map((v,i)=>{
    const r=v.getBoundingClientRect(), s=getComputedStyle(v);
    return {v,i,r,area:r.width*r.height,visible:r.width>1&&r.height>1&&
      s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)!==0};
  }).filter(x=>x.visible);
  if(!visible.length) return -1;
  visible.sort((a,b)=>b.area-a.area || b.i-a.i);
  const main=visible[0], r=main.r;
  // 控制栏/清晰度菜单可以挡在上方，找同一位置最前面的video即可，不要求hover命中。
  const x=Math.max(0,Math.min(innerWidth-1,r.left+r.width*.5));
  const y=Math.max(0,Math.min(innerHeight-1,r.top+r.height*.4));
  for(const e of document.elementsFromPoint(x,y)) {
    const match=visible.find(v=>v.v===e && v.area>=main.area*.8);
    if(match)return match.i;
  }
  return main.i;
}'''


def reveal_online_controls(page):
    """移动到实际画面内以收起画质菜单，不对被遮挡的旧video执行hover。"""
    box = active_online_video(page).bounding_box()
    if box:
        page.mouse.move(box['x']+box['width']*.5, box['y']+box['height']*.4)


def request_online_playback(page, cfg):
    """只在暂停时请求播放；异步play不阻塞，不把一次按钮点击当成播放成功。"""
    video = active_online_video(page)
    if not video.evaluate('v=>v.paused'):
        return '已经播放，不切换暂停按钮'
    # play()是幂等请求；被浏览器自动播放策略拒绝后，再用真实按钮点击提供用户手势。
    video.evaluate('v=>{v.playbackRate=1; if(v.paused){v.play().catch(()=>{});}}')
    page.wait_for_timeout(150)
    if active_online_video(page).evaluate('v=>v.paused'):
        reveal_online_controls(page)
        if cfg['online_video'].get('adapter') == 'bilibili_1080':
            page.get_by_role('button', name='播放/暂停', exact=True).click(timeout=1000)
        else:
            active_online_video(page).click(timeout=1000)
        # 切流与按钮操作可能竞争；幂等play可以纠正按钮恰好把自动播放切成暂停的情况。
        active_online_video(page).evaluate('v=>{if(v.paused){v.play().catch(()=>{});}}')
        return '已请求播放并点击播放按钮'
    return '已请求播放'




def online_video_runtime_findings(sample, previous_position=None):
    """运行中缓冲/停顿是提示，不判负载失败；真实暂停、退出、低画质仍单独报告。"""
    issues, notices = [], []
    if sample.get('paused'): issues.append('视频处于暂停状态')
    if sample.get('ended'): issues.append('视频已播放结束')
    if sample.get('playback_rate',1)!=1: issues.append('视频未按正常1倍速度播放')
    stalled = previous_position is not None and sample.get('position',0)<=previous_position
    buffering = (sample.get('readyState',4)<3 or stalled or
                 (sample.get('frame_age_s') or 0)>2 or sample.get('frame_span_s',0)<1)
    if buffering:
        notices.append('缓冲或画面暂未更新；计时和采样继续，不判失败')
    width, height = sample.get('width',0), sample.get('height',0)
    if width>0 and height>0 and not video_resolution_is_sufficient(sample):
        issues.append(f'实际分辨率 {width}×{height}，要求至少1920×1080')
    elif not width or not height:
        notices.append('切流/缓冲时尚无视频尺寸，等待下次读取')
    fps=sample.get('observed_fps')
    if not buffering and isinstance(fps,(int,float)) and math.isfinite(fps) and fps<29.5:
        issues.append(f'实际帧率 {fps:.1f} 帧/秒，要求至少30帧/秒')
    elif not buffering and (not isinstance(fps,(int,float)) or not math.isfinite(fps)):
        notices.append('本次帧率未读到，等待下次读取')
    full=sample.get('fullscreen') or {}
    if not (full.get('active') and full.get('contains_video') and full.get('fills_viewport')):
        issues.append('视频画面未保持全屏播放')
    return issues, notices


def start_online_playback_fullscreen(page, cfg, stop=None):
    """画质就绪后立即请求播放和全屏；不等进度条，也不重置切流中的currentTime。"""
    from playwright.sync_api import TimeoutError as BrowserTimeout
    record = {'passed':False, 'actions':[]}
    try:
        page.bring_to_front()
        deadline = time.monotonic()+15
        next_play = 0
        last_error = ''
        while time.monotonic()<deadline:
            check_test_cancelled(stop)
            try:
                if time.monotonic()>=next_play:
                    record['actions'].append(request_online_playback(page,cfg))
                    next_play = time.monotonic()+1
                state = active_online_video(page).evaluate(VIDEO_FULLSCREEN_SCRIPT)
                if not (state['active'] and state['contains_video'] and state['fills_viewport']):
                    reveal_online_controls(page)
                    page.get_by_role('button', name='全屏', exact=True).click(timeout=1000)
                    record['actions'].append('点击视频全屏按钮')
                state = active_online_video(page).evaluate(VIDEO_FULLSCREEN_SCRIPT)
                record['fullscreen'] = state
                if state['active'] and state['contains_video'] and state['fills_viewport']:
                    record['passed'] = True
                    return record
            except (BrowserTimeout, OnlineVideoUnavailable) as exc:
                last_error = str(exc)
            page.wait_for_timeout(250)
        raise RuntimeError('视频未进入全屏播放，本项尚未开始计时：'+last_error)
    except BaseException as exc:
        record['error'] = str(exc)
        raise
    finally:
        if cfg.get('_diagnostic_folder'):
            save_json(Path(cfg['_diagnostic_folder'])/'在线视频播放与全屏.json', record)


def wait_for_bilibili_quality(page, cfg, stop=None, status=None):
    """扫码准备不设时限；收到取消或浏览器实际报错才退出，不占正式测试时间。"""
    from playwright.sync_api import TimeoutError as BrowserTimeout
    next_selection = 0.0
    next_record = 0.0
    message = '等待B站扫码/播放准备：请在测试主机Edge中登录；不限等待时间，尚未开始正式计时。可按Ctrl+C取消。'
    print(message,flush=True)
    if status is not None:
        status.update(phase='等待B站播放准备',message=message,waiting_for_user=True)
    try:
        while True:
            if stop is not None and stop.is_set():
                raise InterruptedError('测试人员取消')
            try:
                video = active_online_video(page)
                if video.is_visible():
                    size = video.evaluate('v=>({width:v.videoWidth,height:v.videoHeight})')
                    quality = bilibili_quality_state(page)
                    ready = video_resolution_is_sufficient(size)
                    # 长时间离开时每10秒追加一次准备记录，不反复重写越来越大的列表。
                    if cfg.get('_diagnostic_folder') and (ready or time.monotonic()>=next_record):
                        append_record(Path(cfg['_diagnostic_folder'])/'B站选档检查.jsonl',
                                      {'epoch':time.time(), **size, 'quality':quality, 'ready':ready})
                        next_record = time.monotonic()+10
                    if ready:
                        return video
                if time.monotonic()>=next_selection:
                    select_bilibili_1080(page, timeout=1000)
                    request_online_playback(page,cfg)
                    next_selection = time.monotonic()+1
            except RuntimeError as exc:
                if '没有可见的视频' not in str(exc): raise
            except BrowserTimeout:
                # 登录遮罩挡住选项时等操作员登录，不点击扫码弹窗或绕过登录。
                next_selection = time.monotonic()+1
            page.wait_for_timeout(500)
    finally:
        if status is not None:
            status['waiting_for_user'] = False


def extend_deadline_for_user_wait(deadline, previous_poll, state):
    """只扣除连续收到主机等待状态的时间；断线、卡死不能无限续期。"""
    now = time.monotonic()
    waiting = (state.get('busy') is True and
               state.get('status',{}).get('waiting_for_user') is True)
    if previous_poll is not None:
        previous_time, previously_waiting = previous_poll
        elapsed = now-previous_time
        if waiting and previously_waiting and 0<=elapsed<=10:
            deadline += elapsed
    return deadline, (now,waiting)


def print_stage_results(status, displayed):
    """单项结果随状态持续回传；即使已进入下一项，也能显示前项失败原因。"""
    results=status.get('stage_results',{})
    for mode in ORDER:
        result=results.get(mode)
        if not isinstance(result,dict):
            continue
        signature=json.dumps(result,ensure_ascii=False,sort_keys=True)
        if displayed.get(mode)==signature:
            continue
        if result.get('passed') is True:
            detail='本项短测通过'
            if isinstance(result.get('start_epoch'),(int,float)) and isinstance(result.get('end_epoch'),(int,float)):
                detail+=f'，实际测试{result["end_epoch"]-result["start_epoch"]:.1f}秒'
            prefix='通过'
        else:
            detail=result.get('error','本项未通过')
            if result.get('restore_error'):
                detail+='；网络恢复失败：'+result['restore_error']
            prefix='未通过'
        print(f'【{prefix}】{LABELS[mode]}：{detail}',flush=True)
        displayed[mode]=signature


def online_video_issues(sample, previous_position=None, require_bilibili=False, require_fullscreen=False):
    """把暂停、缓冲、分辨率和帧率分开说明，不用一个笼统错误代替实际原因。"""
    issues = []
    if sample['paused']: issues.append('视频处于暂停状态')
    if sample['ended']: issues.append('视频已播放结束')
    if sample.get('readyState', 4) < 3: issues.append('视频正在缓冲，尚无足够后续画面')
    if previous_position is not None and sample['position'] <= previous_position:
        issues.append('视频播放进度未前进或已倒退')
    if not video_resolution_is_sufficient(sample):
        issues.append(f'实际分辨率 {sample["width"]}×{sample["height"]}，要求至少 1920×1080')
    if sample.get('playback_rate', 1) != 1:
        issues.append(f'实际播放速度 {sample["playback_rate"]} 倍，要求正常速度 1 倍')
    fps = sample['observed_fps']
    if not isinstance(fps, (int, float)) or not math.isfinite(fps):
        issues.append('实际帧率尚不能确认')
    elif fps < 29.5:
        # 29.97fps属于30fps档；为帧回调短窗测量保留0.5fps容差，无上限。
        issues.append(f'实际帧率 {fps:.1f} 帧/秒，要求至少 30 帧/秒（测量容差0.5）')
    if sample.get('frame_span_s', 1) < 1:
        issues.append('帧率观察跨度不足1秒')
    if (sample.get('frame_age_s') or 0) > 2:
        issues.append('视频帧观测已超过2秒未更新')
    full = sample.get('fullscreen') or {}
    if require_fullscreen and not (full.get('active') and full.get('contains_video') and full.get('fills_viewport')):
        issues.append('视频画面未保持全屏播放')
    return issues


def confirm_online_video_playback(page, cfg, stop=None, status=None, quality_attempts=None):
    """连续2秒满足至少1080P、30帧再开始正式计时；保留每次观察证据。"""
    deadline = time.monotonic()+40
    stable_since = None
    previous_position = None
    history = []
    last_issues = ['尚未观察到播放数据']
    last_message = None
    require_bilibili = cfg['online_video'].get('adapter') == 'bilibili_1080'
    quality_attempts = quality_attempts if quality_attempts is not None else {}
    try:
        while time.monotonic() < deadline:
            if stop is not None and stop.is_set():
                raise InterruptedError('测试人员取消')
            page.wait_for_timeout(2000)
            try:
                sample = online_video_status(page)
            except OnlineVideoUnavailable as exc:
                # 换流不能把一次旧控件丢失当作整个测试失败；仍保留40秒准备上限。
                stable_since = None
                previous_position = None
                last_issues = [str(exc)]
                if status is not None:
                    status.update(message='在线视频正在切换画面，尚未开始计时')
                continue
            now = time.monotonic()
            if sample.get('ended'):
                raise RuntimeError('在线视频已播放结束，尚未开始正式测试；请使用剩余时长足够的视频')
            last_issues = online_video_issues(sample, previous_position, require_bilibili, require_fullscreen=True)
            waiting_for_buffer = sample.get('readyState',4)<3 or sample.get('paused')
            if waiting_for_buffer:
                # 登录/网络缓冲期间主机持续响应接收端，不进入正式计时，也不按超时判失败。
                deadline = now+40
            if status is not None:
                status['waiting_for_user'] = bool(waiting_for_buffer)
            if sample.get('paused') and not sample.get('ended'):
                from playwright.sync_api import TimeoutError as BrowserTimeout
                try: request_online_playback(page,cfg)
                except (BrowserTimeout, OnlineVideoUnavailable): pass
            previous_position = sample['position']
            observation = {'epoch':time.time(), 'sample':sample, 'issues':list(last_issues)}
            history.append(observation)
            if cfg.get('_diagnostic_folder'):
                append_record(Path(cfg['_diagnostic_folder'])/'在线视频准备观察.jsonl', observation)
            if len(history)>200: del history[:-200]
            if require_bilibili:
                action = restore_bilibili_quality(page, sample, quality_attempts)
                if action: observation['quality_correction'] = action
            if last_issues:
                stable_since = None
            else:
                if stable_since is None: stable_since = now
                if now-stable_since >= 2:
                    return sample
            message = ('B站播放准备：'+'；'.join(last_issues) if last_issues else
                       'B站播放准备：正在确认至少1080P、30帧持续稳定2秒')
            if message != last_message:
                print(message, flush=True)
                if status is not None: status.update(phase='等待B站播放准备', message=message)
                last_message = message
        if not last_issues: last_issues = ['至少1080P、30帧尚未持续稳定2秒']
        raise RuntimeError('在线视频尚未开始正式计时：'+'；'.join(last_issues)+
                           '。允许2K、4K和60帧；保持正常播放速度。')
    finally:
        if status is not None: status['waiting_for_user'] = False
        if cfg.get('_diagnostic_folder'):
            folder = Path(cfg['_diagnostic_folder'])
            folder.mkdir(parents=True, exist_ok=True)
            save_json(folder/'在线视频启动检查.json', history)


def open_online_video_page(page, cfg, stop=None, status=None):
    """首次打开后至少补试五次。网页错误页也重试，登录页正常交给扫码准备。"""
    from playwright.sync_api import Error as BrowserError
    settings = cfg['online_video']
    retries = max(5, min(20, int(settings.get('page_retry_count', 5))))
    history = []
    try:
        for attempt in range(retries+1):
            if stop is not None and stop.is_set():
                raise InterruptedError('测试人员取消')
            message = ('正在打开在线视频页面' if attempt == 0 else
                       f'在线视频页面打开失败，正在刷新重试 {attempt}/{retries}')+'，尚未开始测试计时。'
            print(message, flush=True)
            if status is not None:
                status.update(phase='打开在线视频页面', message=message, waiting_for_user=True)
            try:
                if attempt and page.url.startswith(('https://', 'http://')):
                    response = page.reload(wait_until='domcontentloaded', timeout=45000)
                else:
                    response = page.goto(settings['url'], wait_until='domcontentloaded', timeout=45000)
                if response is not None and response.status >= 400:
                    raise RuntimeError('网页返回HTTP '+str(response.status))
                page.wait_for_function("() => document.body && (document.body.innerText.trim().length > 0 || document.querySelector('video'))", timeout=15000)
                state = page.evaluate("""() => ({url:location.href, title:document.title,
                    text:(document.body.innerText || '').slice(0,3000),
                    player:!!document.querySelector('video,.bpx-player-container,.bilibili-player')})""")
                text = state['title']+'\n'+state['text']
                if (state['url'].startswith(('chrome-error:', 'edge-error:', 'about:')) or
                    (not state['player'] and re.search(
                        r'ERR_[A-Z_]+|无法访问此(网站|网页)|无法打开此页面|网络连接失败|页面加载失败|'
                        r'502 Bad Gateway|503 Service Unavailable|504 Gateway|页面不存在|页面走丢|'
                        r'网络不给力|页面出错|加载失败.*刷新', text))):
                    raise RuntimeError('网站返回错误页面：'+text[:300])
                history.append({'attempt':attempt, 'passed':True, 'url':state['url']})
                return
            except (BrowserError, RuntimeError) as exc:
                history.append({'attempt':attempt, 'passed':False, 'error':str(exc)})
                if page.is_closed():
                    raise RuntimeError('在线视频浏览器已关闭，无法刷新') from exc
                if attempt >= retries:
                    raise RuntimeError(f'在线视频页面首次打开及{retries}次刷新重试均失败：{exc}；可选D继续本项及后续测试。') from exc
                # 等待可取消；正式计时尚未开始。
                if stop is not None:
                    if stop.wait(2):
                        raise InterruptedError('测试人员取消')
                else:
                    page.wait_for_timeout(2000)
    finally:
        if status is not None:
            status['waiting_for_user'] = False
        if cfg.get('_diagnostic_folder'):
            save_json(Path(cfg['_diagnostic_folder'])/'网页打开重试.json', history)


def start_online_video(cfg, stop=None, status=None):
    from playwright.sync_api import sync_playwright
    settings = cfg['online_video']
    runtime = sync_playwright().start()
    context = None
    try:
        context = runtime.chromium.launch_persistent_context(str(ROOT/settings['profile']),
            channel=settings['browser'],headless=False,viewport=None,args=['--start-maximized'])
        page = context.pages[0] if context.pages else context.new_page()
        open_online_video_page(page,cfg,stop,status)
        bounds = maximize_online_video_window(context, page)
        if cfg.get('_diagnostic_folder'):
            save_json(Path(cfg['_diagnostic_folder'])/'在线视频窗口.json', bounds)
        if settings.get('adapter') == 'bilibili_1080':
            video = wait_for_bilibili_quality(page,cfg,stop,status)
        else:
            if settings.get('quality_open_selector'):
                page.locator(settings['quality_open_selector']).hover(timeout=15000)
            page.locator(settings['quality_selector']).get_by_text(settings['quality_text'],exact=True).click(timeout=15000)
            video = active_online_video(page)
            video.wait_for(state='visible',timeout=15000)
        start_online_playback_fullscreen(page, cfg, stop)
        # 初始化观察器；切流或播放器替换video元素时，状态读取会重新建立观察。
        try:
            online_video_status(page)
        except OnlineVideoUnavailable:
            pass  # 全屏之后恰好切流，由下面的准备检查继续读取。
        quality_attempts = {}
        sample = confirm_online_video_playback(page, cfg, stop, status, quality_attempts)
        return {'runtime':runtime,'context':context,'page':page,'last':sample,
                'quality_attempts':quality_attempts}
    except BaseException as exc:
        if context and cfg.get('_diagnostic_folder'):
            try:
                page.screenshot(path=str(Path(cfg['_diagnostic_folder'])/'在线视频现场.png'))
                save_json(Path(cfg['_diagnostic_folder'])/'在线视频状态.json',online_video_status(page))
            except Exception:
                pass
        try:
            stop_online_video({'context':context,'runtime':runtime})
        except Exception as cleanup:
            raise WorkloadCleanupError(str(exc)+'；浏览器未能正常收尾：'+str(cleanup)) from exc
        raise


def online_video_status(page):
    """在同一次DOM读取中确定视频、帧数与全屏状态，避免切流后混入另一个video。"""
    from playwright.sync_api import Error as BrowserError
    try:
        sample = page.evaluate(ONLINE_VIDEO_SNAPSHOT_SCRIPT)
        if sample is not None:
            sample['quality'] = bilibili_quality_state(page)
    except BrowserError as exc:
        if not page.is_closed() and any(word in str(exc) for word in (
                'Execution context was destroyed', 'Cannot find context with specified id', 'Frame was detached')):
            raise OnlineVideoUnavailable('在线视频页面正在重建，等待下一次读取') from exc
        raise
    if sample is None:
        raise OnlineVideoUnavailable('当前页面没有可见的视频画面')
    return sample


def runtime_online_video_status(page):
    """切流最多补看2.5秒；没有读到时明确返回不可用，绝不编造进度和温度。"""
    observations=[]
    deadline=time.monotonic()+2.5
    while True:
        try:
            sample=online_video_status(page)
        except OnlineVideoUnavailable as exc:
            sample={'observation_available':False,'observation_error':str(exc)}
        observations.append(sample)
        if sample.get('observation_available') is not False and (
                sample.get('frame_span_s',1)>=1 or sample.get('paused') or sample.get('ended')
                or sample.get('readyState',4)<3 or not video_resolution_is_sufficient(sample)
                or (sample.get('frame_age_s') or 0)>2):
            break
        remaining=deadline-time.monotonic()
        if remaining<=0:break
        page.wait_for_timeout(min(250,remaining*1000))
    if len(observations)==1:return sample
    result=dict(sample)
    result['frame_recheck']=observations
    return result


VIDEO_STATUS_SCRIPT = '''v => {
      if (!v.__jk808Frames) {
        const state={frames:[], lastAt:0, size:''}; v.__jk808Frames=state;
        const reset=()=>{state.frames=[]; state.lastAt=0;};
        v.addEventListener('seeking',reset); v.addEventListener('emptied',reset);
        if (v.requestVideoFrameCallback) {
          const observe=(t,m)=>{
            const size=`${v.videoWidth}x${v.videoHeight}`, last=state.frames.at(-1);
            if (size!==state.size || (last && (m.mediaTime<=last.t || m.presentedFrames<=last.n))) reset();
            state.size=size; state.lastAt=performance.now();
            state.frames.push({t:m.mediaTime,n:m.presentedFrames});
            if (state.frames.length>180) state.frames.shift();
            if (v.isConnected) v.requestVideoFrameCallback(observe);
            else delete v.__jk808Frames;
          };
          v.requestVideoFrameCallback(observe);
        }
      }
      const state=v.__jk808Frames, f=state.frames,a=f[0],b=f[f.length-1];
      return {position:v.currentTime,paused:v.paused,ended:v.ended,readyState:v.readyState,
        width:v.videoWidth,height:v.videoHeight,playback_rate:v.playbackRate,
        frame_age_s:state.lastAt ? (performance.now()-state.lastAt)/1000 : null,
        frame_span_s:a&&b?b.t-a.t:0,
        observed_fps:a&&b&&b.t>a.t?(b.n-a.n)/(b.t-a.t):0};}'''


# 同步JavaScript一次完成选取和读取；画质菜单可遮挡视频，但不能使它读到旧元素。
ONLINE_VIDEO_SNAPSHOT_SCRIPT = '''() => {
    const videos=Array.from(document.querySelectorAll('video'));
    const index=('''+ACTIVE_VIDEO_SCRIPT+''')(videos);
    if(index<0)return null;
    const video=videos[index];
    return {...('''+VIDEO_STATUS_SCRIPT+''')(video),
            fullscreen:('''+VIDEO_FULLSCREEN_SCRIPT+''')(video)};
}'''


def bit_program_version(executable):
    import win32api
    info = win32api.GetFileVersionInfo(str(executable), '\\')
    high, low = info['FileVersionMS'], info['FileVersionLS']
    return [high >> 16, high & 65535, low >> 16, low & 65535]


def bit_stop_locations(executable):
    """V9手册规定的两个停止文件位置；尊重系统实际Documents重定向。"""
    from win32com.shell import shell, shellcon
    documents = Path(shell.SHGetFolderPath(0, shellcon.CSIDL_PERSONAL, None, 0))
    return [Path(executable).parent/'BURNINTEST_STOP',
            documents/'PassMark'/'BurnInTest'/'BURNINTEST_STOP']


def check_bit_drive_rows(rows, automatic_all):
    """按实际Disk表的12列检查；C盘卷文件测试与包含C:字样的物理盘不能混淆。"""
    if automatic_all or not rows or any(len(row) != 12 for row in rows):
        raise RuntimeError('BIT磁盘表不完整或启用了自动选择全部磁盘')
    selected = [row for row in rows if row[1].strip() != 'Not Testing']
    if len(selected) != 1 or not re.fullmatch(r'C:\s+.*\[Local drive\]', selected[0][0].strip()):
        raise RuntimeError('BIT必须只选择C盘卷文件测试；不能包含物理盘、U盘或其他盘')
    row = selected[0]
    if (row[1].strip() != 'Default (Cyclic)' or float(row[3]) != 9.95
            or row[4].strip() != 'N' or float(row[5]) != 100
            or row[6].strip() not in ('','100') or row[7].strip() not in ('','0')
            or float(row[10]) != 256 or row[11].strip() not in ('','N','No')):
        raise RuntimeError('BIT磁盘参数与已提供设置不一致，原始读回值已保存')
    return {'drive':'C:', 'mode':'Default (Cyclic)', 'file_percent':9.95,
            'block_kb':256,'seek_count':100,'physical_disk_test':False}


def verify_bit_disk_configuration(executable, config_path, folder):
    """先只载入配置并读取F2磁盘页，关闭后才允许另一个进程执行.bits。"""
    from pywinauto import Desktop
    process = subprocess.Popen([str(executable),'-C',str(config_path)], cwd=config_path.parent)
    dialog, main = None, None
    record = {'pid':process.pid,'passed':False,'configuration_sha256':sha256(config_path)}
    try:
        main = Desktop(backend='win32').window(process=process.pid, class_name='bit')
        main.wait('exists visible enabled', timeout=45)
        located = Desktop(backend='win32').window(process=process.pid,
                            title='BurnInTest Preferences', class_name='#32770')
        dialog = open_bit_preferences_when_ready(main, located, process, record)
        uia = Desktop(backend='uia').window(handle=dialog.handle, process=process.pid)
        uia.child_window(title='Disk', control_type='TabItem').select()
        table = Desktop(backend='win32').window(handle=dialog.handle, process=process.pid).child_window(
                            control_id=1190, class_name='SysListView32', depth=1)
        table.wait('visible enabled', timeout=10)
        rows = [[table.get_item(i,j).text() for j in range(table.column_count())]
                for i in range(table.item_count())]
        automatic_all = Desktop(backend='win32').window(handle=dialog.handle, process=process.pid).child_window(
                            control_id=1224, class_name='Button').get_check_state()
        record.update(rows=rows, automatic_all=automatic_all)
        # 在解释之前先落盘，失败时无需再让操作员导出同一份配置。
        save_json(folder/'BIT配置回读.json',record)
        record['scope'] = check_bit_drive_rows(rows, automatic_all)
        record['passed'] = True
        return record
    except Exception as exc:
        record['error'] = str(exc)
        raise
    finally:
        cleanup_error = None
        try:
            if dialog:
                Desktop(backend='win32').window(handle=dialog.handle, process=process.pid).child_window(
                    control_id=2, class_name='Button').click()
            if process.poll() is None and main is not None and main.exists():
                main.close()
            process.wait(timeout=15)
        except Exception as exc:
            record['passed'] = False
            record['cleanup_error'] = str(exc)
            cleanup_error = exc
        try:
            save_json(folder/'BIT配置回读.json',record)
        finally:
            if cleanup_error:
                raise WorkloadCleanupError('BIT配置检查窗口未正常关闭，不启动负载：'+str(cleanup_error)) from cleanup_error


def open_bit_preferences_when_ready(main, located, process, record):
    """BIT出现主窗口时可能仍在扫描设备；等待可响应，再发送一次成功的F2。"""
    deadline = time.monotonic()+60
    ready_count = 0
    sent = False
    observations = record.setdefault('startup_wait', [])
    while time.monotonic()<deadline:
        if process.poll() is not None:
            raise RuntimeError('BIT在载入测试配置时已退出，退出码：'+str(process.returncode))
        try:
            # 若上一次按键已送达但回读超时，先检查对话框，不重复发送F2。
            if located.exists(timeout=.2) and located.is_visible() and located.is_enabled():
                observations.append({'epoch':time.time(), 'state':'配置窗口已就绪'})
                return located.wrapper_object()
            if not sent:
                main.wait('exists visible enabled', timeout=2)
                main.verify_actionable()  # 包含窗口线程响应检查，不能只检查visible。
                ready_count += 1
                if ready_count >= 2:
                    main.restore()
                    main.type_keys('{F2}')
                    sent = True
                    observations.append({'epoch':time.time(), 'state':'已发送F2'})
        except Exception as exc:
            ready_count = 0
            observations.append({'epoch':time.time(), 'waiting':str(exc)})
        time.sleep(1)
    detail = observations[-1] if observations else {}
    raise RuntimeError('BIT启动等待60秒后仍未能打开配置页；尚未执行测试。'+str(detail))


def build_bit_script(workspace, seconds, powershell):
    """官方V9脚本。只覆盖时限、Disk负载比例和日志位置，使用已核对的C盘文件配置。"""
    workspace = Path(workspace).resolve()
    if not 0 < seconds <= 900 or not math.isfinite(seconds):
        raise ValueError('BIT脚本时长应在0～900秒之间')
    for value in (str(workspace), str(powershell)):
        if not value.isascii() or any(c in value for c in '\r\n"'):
            raise ValueError('BIT V9脚本需要不含引号的英文工作路径')
    marker = workspace/'mark.ps1'
    # 两个标记只表示RUN命令的前后边界；成功还必须查BIT自己的报告及退出码。
    marker.write_text('''param([ValidateSet('before','after')][string]$Phase)
$ErrorActionPreference = 'Stop'
$record = @{phase=$Phase; epoch=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0}
$target = Join-Path $PSScriptRoot ($Phase+'.json')
$temporary = $target+'.tmp'
$record | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding UTF8
Move-Item -LiteralPath $temporary -Destination $target
''', encoding='ascii')
    mark_command = (f'EXECUTEWAIT "{powershell}" -NoProfile -NonInteractive -WindowStyle Hidden '
                    f'-ExecutionPolicy Bypass -File "{marker}"')
    lines = [
        '# Uses the supplied C: file-test configuration. No physical-disk test.',
        f'LOAD "{workspace / "selected.bitcfg"}"',
        f'SETDURATION {seconds/60:g}', 'SETCYCLES 0', 'SETDUTYCYCLE DISK 100',
        f'SETLOG LOG YES NAME "{workspace / "bit_result"}" PREFIX "JK808_" TIME NO',
        'SETLOG REPORT TEXT LOGLEVEL YES TRACELEVEL NO SUM NO LINES 100000',
        'SETLOG PERIODIC 1 FILE NEW', 'STARTLOGGING',
        mark_command+' -Phase before', 'RUN DISK',
        mark_command+' -Phase after', 'EXIT',
    ]
    if any(len(line) > 300 for line in lines):
        raise ValueError('BIT脚本行超过V9支持的300字符，请缩短英文工作路径')
    script = workspace/'run.bits'
    script.write_bytes(('\r\n'.join(lines)+'\r\n').encode('ascii'))
    return script


def read_bit_marker(workspace, phase):
    path = Path(workspace)/(phase+'.json')
    if not path.is_file():
        return None
    marker = json.loads(path.read_text('utf-8-sig'))
    if marker.get('phase') != phase or not isinstance(marker.get('epoch'), (int,float)):
        raise RuntimeError('BIT任务时间标记无效：'+str(path))
    if not math.isfinite(marker['epoch']):
        raise RuntimeError('BIT任务时间标记不是有效时间')
    return marker


def inspect_bit_report(text, seconds):
    """读取V9官方文字报告的最终RESULT SUMMARY；启动了进程不等于磁盘测试成功。"""
    summary = re.split(r'RESULT SUMMARY', text, flags=re.I)[-1]
    duration = re.findall(r'Test Duration:\s*(\d+)h\s*(\d+)m\s*(\d+)s', summary, re.I)
    if not duration:
        raise RuntimeError('BIT报告缺少实际Test Duration')
    hours, minutes, secs = map(int, duration[-1])
    actual = hours*3600+minutes*60+secs
    if not seconds-2 <= actual <= seconds+30:
        raise RuntimeError(f'BIT实际运行{actual}秒，与要求{seconds}秒不符')
    rows = re.findall(
        r'^\s*Disk\s*\(([^)\r\n]+)\)\s+(\d+)\s+'
        r'([\d.,]+(?:\s+(?:Thousand|Million|Billion|Trillion))?)\s+'
        r'(PASS|FAIL|FAILED)\s+(\d+)\b[^\r\n]*', summary, re.I | re.M)
    if len(rows) != 1 or rows[0][0].strip().upper() != 'C:':
        raise RuntimeError('BIT最终报告没有唯一的Disk (C:)结果；保留原始报告供检查')
    drive, cycles, operations, outcome, errors = rows[0]
    operation_count = float(operations.split()[0].replace(',', ''))
    if operation_count <= 0 or outcome.upper() != 'PASS' or int(errors) != 0:
        raise RuntimeError('BIT C盘未完成有效操作或报告了错误：'+' / '.join(rows[0]))
    if 'TEST RUN PASSED' not in summary.upper():
        raise RuntimeError('BIT报告没有最终TEST RUN PASSED标记')
    times={}
    months={name:i+1 for i,name in enumerate('Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split())}
    for phase in ('Start','Stop'):
        values=re.findall(r'^Test '+phase+r' time:[ \t]*([^\r\n]+)',summary,re.I|re.M)
        match=re.fullmatch(r'[A-Za-z]{3}\s+([A-Za-z]{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+(\d{4})',
                          values[-1].strip() if values else '')
        if not match or match[1].title() not in months:
            raise RuntimeError('BIT最终报告缺少有效的Test '+phase+' time，不能把脚本准备时间算成负载')
        month,day,hour,minute,second,year=match.groups()
        times[phase.lower()+'_epoch']=datetime(int(year),months[month.title()],int(day),
            int(hour),int(minute),int(second),tzinfo=BJ).timestamp()
    if abs(times['stop_epoch']-times['start_epoch']-actual)>1:
        raise RuntimeError('BIT报告起止时间与Test Duration不一致')
    return {'actual_duration_s': actual, 'drive': drive.strip(), 'cycles': int(cycles),
            'operations': operations, 'result': outcome.upper(), 'errors': int(errors),
            'start_epoch':times['start_epoch'],'end_epoch':times['stop_epoch']}


def collect_bit_result(workload, *, require_complete):
    """正常、取消、异常都复制本轮日志；只有完整报告+边界标记+退出码才通过。"""
    workspace, folder = workload['workspace'], workload['folder']
    target = folder/'BIT原始记录'
    target.mkdir(exist_ok=True)
    for path in workspace.iterdir():
        if path.is_file():
            shutil.copy2(path, target/path.name)
    result = {'passed': False, 'pid': workload['process'].pid,
              'exit_code': workload['process'].poll(), 'requested_duration_s': workload['seconds'],
              'workspace': str(workspace), 'cancel_requested': workload.get('cancel_requested', False)}
    try:
        if not require_complete:
            result['note'] = '未按完整测试验收，仅保存现场'
            return result
        if result['cancel_requested'] or result['exit_code'] != 0:
            raise RuntimeError('BIT未正常完成：取消状态或非零/未退出的进程状态')
        before, after = read_bit_marker(workspace, 'before'), read_bit_marker(workspace, 'after')
        if not before or not after or after['epoch'] <= before['epoch']:
            raise RuntimeError('BIT缺少本轮脚本的完整起止记录')
        if after['epoch']-before['epoch'] > workload['seconds']+180:
            raise RuntimeError('BIT脚本准备及收尾超过限定时间，保留原始记录')
        reports = [p for p in workspace.glob('*bit_result*') if p.suffix.lower() in ('.log','.txt')]
        if len(reports) != 1 or reports[0].stat().st_size == 0:
            raise RuntimeError('BIT没有生成唯一的本轮文字结果日志')
        raw = reports[0].read_bytes()
        encoding = 'utf-16' if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else (
            'utf-16-le' if b'\x00' in raw[:100] else 'utf-8-sig')
        result.update(inspect_bit_report(raw.decode(encoding), workload['seconds']))
        if not (before['epoch']-1 <= result['start_epoch'] < result['end_epoch'] <= after['epoch']+1):
            raise RuntimeError('BIT报告负载时间不在本轮脚本起止范围内，不能使用其他轮次结果')
        result.update(passed=True, script_before_epoch=before['epoch'], script_after_epoch=after['epoch'],
                      preparation_s=max(0,result['start_epoch']-before['epoch']),
                      finalization_s=max(0,after['epoch']-result['end_epoch']),
                      report_sha256=sha256(reports[0]), report_file=reports[0].name,
                      timing_basis='BIT最终报告的Test Start/Stop time与Duration；脚本标记仅校验本轮边界')
        return result
    except Exception as exc:
        result['error'] = str(exc)
        raise
    finally:
        result_file = folder/'BIT执行结果.json'
        if not require_complete and result_file.is_file():
            # 清理记录不能把刚才严格验收的失败原因覆盖掉。
            result_file = folder/'BIT收尾记录.json'
        save_json(result_file, result)


def stop_bit_script(workload):
    """只给本轮进程发送V9官方停止文件；不使用未核对的快捷键或强杀其他进程。"""
    process = workload['process']
    if process.poll() is not None:
        return
    stop_file = workload['stop_file']
    owner = workload['workspace'].name
    with stop_file.open('x', encoding='ascii') as stream:
        stream.write(owner)
    workload['cancel_requested'] = True
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        # 程序仍在时保留停止信号，避免启动弹窗稍后消失后又开始负载。
        save_json(workload['folder']/'BIT停止待处理.json',
                  {'pid':process.pid,'stop_file':str(stop_file),'owner':owner})
        raise RuntimeError('BIT尚未退出，请关闭该测试窗口；停止信号位置：'+str(stop_file))
    finally:
        if process.poll() is not None and stop_file.is_file() and stop_file.read_text('ascii') == owner:
            stop_file.unlink()


def clear_finished_bit_stop_files(cfg, folder, stop_files):
    """旧停止文件必须匹配本程序任务记录，且BIT进程已退出，才备份清理。"""
    records = None
    for stop_file in stop_files:
        if not stop_file.exists():
            continue
        original = stop_file.read_bytes()
        owner = original.decode('ascii',errors='replace')
        proof = None
        if re.fullmatch(r'[a-f0-9]{32}',owner):
            if records is None:
                records = list(Path(cfg['host_workspace']).rglob('bit_process.json'))
            expected_workspace = (Path('C:/JK808_Windows/bit_runs')/owner).resolve()
            for record_path in records:
                try:
                    record = json.loads(record_path.read_text('utf-8-sig'))
                    command = json.loads(record_path.with_name('bit_command.json').read_text('utf-8-sig'))
                    if (Path(record['workspace']).resolve()==expected_workspace
                            and type(record['pid']) is int and record['pid']>0
                            and isinstance(command,list) and len(command)==4
                            and Path(command[0]).resolve()==Path(cfg['burnintest']['executable']).resolve()
                            and command[1]=='-S' and command[3]=='-T'
                            and Path(command[2]).resolve()==expected_workspace/'run.bits'):
                        proof = {'record_file':str(record_path),'pid':record['pid'],'owner':owner}
                        break
                except (OSError,KeyError,ValueError,TypeError):
                    continue
        if proof is None:
            raise RuntimeError('旧BIT停止文件无法核实所属任务，已保留，请检查：'+str(stop_file))
        active = json.loads(ps_with_data(
            '[bool](@(Get-CimInstance Win32_Process | Where-Object { '
            '$_.ProcessId -eq $d.pid -or $_.Name -eq $d.name }).Count) | ConvertTo-Json -Compress',
            {'pid':proof['pid'],'name':Path(cfg['burnintest']['executable']).name}))
        if active is not False:
            raise RuntimeError('旧BIT进程仍在运行或不能确认退出，停止文件已保留：'+str(stop_file))
        # 先留存内容和归属证据，写盘失败时不删除；删除前再核对内容没有变化。
        backup = folder/('BIT旧停止文件_'+uuid.uuid4().hex[:8]+'.json')
        save_json(backup,{**proof,'stop_file':str(stop_file),'original_base64':base64.b64encode(original).decode('ascii'),
                          'process_exited':True,'removed':False})
        if stop_file.read_bytes()!=original:
            raise RuntimeError('BIT停止文件在检查期间改变，已保留：'+str(stop_file))
        stop_file.unlink()
        save_json(backup,{**proof,'stop_file':str(stop_file),'original_base64':base64.b64encode(original).decode('ascii'),
                          'process_exited':True,'removed':True})
        print('已清理本程序已退出任务的BIT停止标记，记录：'+str(backup),flush=True)


def confirm_bit_start_dialog(process, record, *, cancel=False):
    """只确认本轮BIT进程、已知标题和正文的开始提示；其他弹窗不点击。"""
    from pywinauto import Desktop
    if process.poll() is not None:
        return False
    desktop = Desktop(backend='win32')
    dialogs = desktop.windows(process=process.pid, class_name='#32770', visible_only=True)
    record['visible_dialogs'] = [w.window_text() for w in dialogs]
    for dialog in dialogs:
        if dialog.process_id() != process.pid or dialog.window_text().strip() != 'Getting ready to run Burn in tests':
            continue
        message = ' '.join(' '.join(c.window_text() for c in dialog.descendants(class_name='Static')).split())
        required = ('You are about to start a series of tests that are designed to test the reliability of your computer hardware.',
                    'please ensure you have adequate backups of your data before running these tests.')
        if not all(text in message for text in required):
            raise RuntimeError('BIT开始提示正文与现场截图不符，未自动点击：'+message)
        wanted = ('取消','Cancel') if cancel else ('确定','OK')
        buttons = [b for b in dialog.descendants(class_name='Button')
                   if b.window_text().replace('&','').strip() in wanted and b.is_enabled() and b.is_visible()]
        if len(buttons) != 1:
            raise RuntimeError('BIT开始提示没有唯一可用的'+wanted[0]+'按钮')
        if not cancel and record.get('confirmation_clicked'):
            raise RuntimeError('BIT开始提示重复出现或没有关闭，未重复点击确定')
        record['actions'].append({'epoch':time.time(), 'action':'取消开始提示' if cancel else '确认开始提示',
                                  'pid':process.pid,'handle':dialog.handle,'text':message})
        dialog.set_focus()
        buttons[0].click_input()
        if not cancel:
            record['confirmation_clicked'] = True
        # 不把“点击成功”当作负载成功，后面还要读取本轮BIT实际开始日志。
        ui_window({'backend':'win32','window':{'handle':dialog.handle,'process':process.pid}}).wait_not('visible',timeout=5)
        return True
    return False


def read_bit_running_start(workspace, before_epoch):
    """只接受本轮独立目录中BIT自己的Main Tests started，不接受RUN命令标记。"""
    reports = [p for p in Path(workspace).glob('*bit_result*') if p.suffix.lower() in ('.log','.txt')]
    if len(reports) > 1:
        raise RuntimeError('本轮BIT出现多个文字日志，无法确定实际开始记录')
    if not reports:
        return None
    raw = reports[0].read_bytes()
    encoding = 'utf-16' if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else (
        'utf-16-le' if b'\x00' in raw[:100] else 'utf-8-sig')
    text = '\n'.join(raw.decode(encoding, errors='ignore').splitlines())  # 兼容CRLF；写入末尾可能只有半个字符。
    events = re.findall(r'^LOG NOTE:[ \t]*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),[ \t]*Status,[ \t]*(Main Tests started|Test run stopped)[ \t]*\r?$',text,re.M)
    result = bit_start_from_events(events, before_epoch)
    if result:
        result.update(source='BIT日志：Main Tests started', report_file=reports[0].name)
    return result


def bit_start_from_events(events, before_epoch):
    """文件和界面都必须有本轮原生开始事件；脚本RUN标记和确定按钮不算开始。"""
    starts = [stamp for stamp,event in events if event == 'Main Tests started']
    if not starts:
        return None
    if len(starts) != 1 or any(event == 'Test run stopped' for _,event in events):
        raise RuntimeError('BIT未确认运行前已经停止或重复启动，不能开始计时')
    epoch = datetime.strptime(starts[0],'%Y-%m-%d %H:%M:%S').replace(tzinfo=BJ).timestamp()
    if not before_epoch-1 <= epoch <= time.time()+2:
        raise RuntimeError('BIT实际开始记录不在本轮时间范围内')
    return {'epoch':epoch}


def read_bit_event_rows(process, record):
    """日志被占用时读取BIT自己的Event Log列表，只操作本轮进程的窗口。"""
    from pywinauto import Desktop
    windows = Desktop(backend='win32').windows(process=process.pid, class_name='bit', visible_only=False)
    if len(windows) != 1 or windows[0].process_id() != process.pid:
        raise RuntimeError('未找到本轮唯一BIT主窗口，不能读取事件记录')
    native = windows[0]
    if native.is_minimized(): native.restore()
    window = ui_window({'backend':'uia','window':{'handle':native.handle,'process':process.pid}})
    tab = window.child_window(title='Event Log', control_type='TabItem')
    if not tab.exists(timeout=.3):
        raise RuntimeError('本轮BIT未显示Event Log标签')
    if not tab.is_selected(): tab.select()
    record['event_log_handle'] = native.handle
    lists = [control for control in native.descendants(class_name='SysListView32') if control.is_visible()]
    if len(lists) != 1:
        raise RuntimeError('BIT事件列表尚未显示或不能唯一确定')
    control = lists[0]
    rows, columns = control.item_count(), control.column_count()
    if rows > 500 or not 1 <= columns <= 12:
        raise RuntimeError('BIT事件列表大小超出本轮启动范围')
    return [[control.get_item(row, col).text().strip() for col in range(columns)] for row in range(rows)]


def bit_start_from_ui_rows(rows, before_epoch):
    events = []
    for row in rows:
        messages = [cell for cell in row if cell in ('Main Tests started', 'Test run stopped')]
        if not messages: continue
        # 不推测缺失的日期；否则昨天的事件可能被算进今天。允许日期和时间分列。
        text = ' '.join(row)
        stamps = re.findall(r'\b\d{4}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}:\d{2}\b', text)
        if len(messages) != 1 or len(stamps) != 1 or 'Status' not in row:
            return None  # 字段还未填全或界面格式不同，保留原始行并继续等待。
        stamp = re.sub(r'\s+', ' ', stamps[0].replace('/', '-'))
        events.append((stamp, messages[0]))
    # 原生列表可能按新事件在前显示；按完整时间排序，不能依赖显示方向。
    events.sort(key=lambda item:item[0])
    result = bit_start_from_events(events, before_epoch)
    if result: result.update(source='BIT Event Log：Main Tests started', rows=rows)
    return result


def wait_for_bit_running(workload, stop=None, status=None):
    """确认提示→等待实际开始日志→开始计时。准备等待和取消都有明确出口。"""
    record = {'passed':False,'actions':[],'pid':workload['process'].pid}
    deadline = time.monotonic()+120
    next_ui_read = 0.0
    if status is not None:
        status.update(phase='BIT启动检查',message='BIT：正在启动并确认测试提示，尚未开始测试计时')
    try:
        while time.monotonic() < deadline:
            if stop is not None and stop.is_set():
                raise InterruptedError('测试人员取消BIT启动')
            if workload['process'].poll() is not None:
                raise RuntimeError('BIT在确认实际运行前退出，退出码：'+str(workload['process'].returncode))
            before = read_bit_marker(workload['workspace'],'before')
            if before:
                record['script_before_epoch'] = before['epoch']
                confirm_bit_start_dialog(workload['process'], record)
                check_test_cancelled(stop)
                try:
                    running = read_bit_running_start(workload['workspace'], before['epoch'])
                except OSError as read_error:
                    # 文件可能正在写入或被BIT独占；下轮继续读取，不把读取失败当成已启动。
                    running = None
                    record['log_read_error'] = str(read_error)
                if not running and time.monotonic() >= next_ui_read and not record.get('visible_dialogs'):
                    next_ui_read = time.monotonic()+2
                    try:
                        rows = read_bit_event_rows(workload['process'], record)
                    except Exception as ui_error:
                        record['event_log_read_error'] = str(ui_error)
                    else:
                        record['event_log_rows'] = rows
                        running = bit_start_from_ui_rows(rows, before['epoch'])
                check_test_cancelled(stop)
                if running:
                    workload['start_epoch'] = running['epoch']
                    workload['start_mono'] = time.monotonic()-(time.time()-running['epoch'])
                    workload['running_evidence'] = running
                    record.update(passed=True,running=running)
                    return
                if status is not None:
                    status['message']='BIT：等待程序确认负载实际开始，尚未开始测试计时'
            time.sleep(.25)
        raise RuntimeError('BIT启动120秒内没有实际开始证据；未开始测试计时。窗口：'+str(record.get('visible_dialogs',[]))+
                           '；日志读取：'+str(record.get('log_read_error','未出现开始事件'))+
                           '；界面读取：'+str(record.get('event_log_read_error','未出现开始事件')))
    except BaseException as exc:
        record['error'] = str(exc)
        # 取消仍待确认的本轮提示，防止清理后稍迟点击确定又启动负载。
        try:
            confirm_bit_start_dialog(workload['process'], record, cancel=True)
        except Exception as cancel_error:
            record['cancel_dialog_error'] = str(cancel_error)
        raise
    finally:
        if record.get('event_log_handle') and workload['process'].poll() is None:
            try:
                dashboard = ui_window({'backend':'uia', 'window':{'handle':record['event_log_handle'],
                                       'process':workload['process'].pid}}).child_window(title='Dashboard', control_type='TabItem')
                if dashboard.exists(timeout=.3): dashboard.select()
            except Exception as restore_error:
                record['dashboard_restore_error'] = str(restore_error)
        save_json(workload['folder']/'BIT启动检查.json',record)


def start_bit_script(cfg, folder, seconds, stop=None, status=None):
    """用原始bitcfg+官方.bits脚本启动；所有英文路径由程序创建，不让操作员填写。"""
    check_test_cancelled(stop)
    bit = cfg['burnintest']
    if sha256(bit['configuration']) != CONFIRMED_BIT_CONFIG_SHA256:
        raise RuntimeError('BIT原始配置与已确认文件不符')
    version = bit_program_version(bit['executable'])
    if version[:3] != [9,0,1003]:
        raise RuntimeError('本次适配的BIT版本是9.0 build1003，当前为：'+str(version))
    external_script = Path(bit['executable']).parent/'bit-script-input.txt'
    if external_script.exists():
        raise RuntimeError('BIT目录存在其他程序的配置覆盖脚本，请确认其用途后移走：'+str(external_script))
    stop_files = bit_stop_locations(bit['executable'])
    clear_finished_bit_stop_files(cfg, folder, stop_files)
    workspace = Path('C:/JK808_Windows/bit_runs')/uuid.uuid4().hex
    workspace.mkdir(parents=True, exist_ok=False)
    copied = workspace/'selected.bitcfg'
    shutil.copy2(bit['configuration'], copied)
    if sha256(copied) != CONFIRMED_BIT_CONFIG_SHA256:
        raise RuntimeError('BIT复制配置时内容改变')
    # 原始配置已经按实机磁盘页确认，内容哈希相同即可使用；不再先开一次BIT并卡在F2。
    save_json(folder/'BIT配置核对.json',{'passed':True,'verification':'confirmed_configuration_sha256',
        'configuration_sha256':sha256(copied),'drive':'C:','physical_disk_test':False,
        'note':'使用已确认的C盘文件测试配置；本次未重复打开F2页面'})
    powershell = Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    if not powershell.is_file():
        raise RuntimeError('未找到Windows PowerShell')
    script = build_bit_script(workspace, seconds, powershell)
    command = [str(Path(bit['executable']).resolve()), '-S',str(script), '-T']
    save_json(folder/'bit_command.json',command)
    check_test_cancelled(stop)
    process = subprocess.Popen(command, cwd=workspace)
    workload = {'adapter':'bit9_script','process':process,'workspace':workspace,'folder':folder,
                'seconds':seconds,'stop_file':stop_files[0]}
    try:
        # 启动进程后的所有操作都必须在收尾保护内，包括记录PID时磁盘写满的情况。
        save_json(folder/'bit_process.json',{'pid':process.pid,'version':version,'workspace':str(workspace)})
        wait_for_bit_running(workload, stop=stop, status=status)
        return workload
    except BaseException as exc:
        cleanup = None
        try:
            stop_bit_script(workload)
        except Exception as stop_error:
            cleanup = str(stop_error)
        record_errors=[]
        try:
            collect_bit_result(workload, require_complete=False)
        except Exception as record_error:
            record_errors.append('保存BIT原始记录失败：'+str(record_error))
        try:
            save_json(folder/'BIT启动失败.json',{'error':str(exc),'cleanup_error':cleanup,
                                              'record_errors':record_errors})
        except Exception as record_error:
            record_errors.append('保存BIT启动失败记录失败：'+str(record_error))
        # 留证失败不能掩盖“负载仍未停止”，否则外层可能继续下一项测试。
        if cleanup:
            raise WorkloadCleanupError(str(exc)+'；BIT未能确认停止：'+cleanup+
                                       ('；'+'；'.join(record_errors) if record_errors else '')) from exc
        raise


def start_bit(cfg, folder, seconds, stop=None, status=None):
    """实际负载来自核对过的.bitcfg；CLI只指定配置和15分钟时限。"""
    from pywinauto import Desktop
    if Desktop(backend='win32').windows(class_name='bit',visible_only=False):
        raise RuntimeError('请先关闭已有的 BIT 主窗口，再由程序启动本轮 BIT；避免重复运行负载')
    bit = cfg['burnintest']
    if bit.get('adapter') == 'bit9_script':
        return start_bit_script(cfg, folder, seconds, stop=stop, status=status)
    copied = folder/'selected.bitcfg'
    shutil.copy2(bit['configuration'],copied)
    if sha256(copied) != bit['configuration_sha256'].lower():
        raise RuntimeError('BIT复制配置时内容改变')
    command = [bit['executable'],'-C',str(copied),'-D',str(seconds/60),'-R']
    save_json(folder/'bit_command.json',command)
    process = subprocess.Popen(command,cwd=folder)
    profile = copy.deepcopy(bit['ui'])
    profile['_process_id'] = process.pid
    save_json(folder/'bit_process.json',{'pid':process.pid,'executable':bit['executable']})
    deadline = time.monotonic()+30
    while time.monotonic()<deadline:
        if process.poll() is not None:
            raise RuntimeError('BIT在负载开始前退出')
        try:
            text = ui_window(profile).child_window(**profile['running_control']).window_text()
            if re.search(bit['ui']['running_text_re'],text):
                return {'process':process,'profile':profile}
        except Exception:
            pass
        time.sleep(.5)
    # 只处理本轮窗口；若未能正常停止，明确报告，不假装负载已退出。
    run_ui_actions(profile,'stop_actions')
    raise RuntimeError('BIT未显示已核对的运行状态')


def minimize_owned_hwinfo(cfg):
    """只最小化本项已经开始记录的窗口，避免同名窗口干扰。"""
    session = cfg['hwinfo']['ui'].get('_logging_session')
    if session:
        from pywinauto import Desktop
        window = Desktop(backend='win32').window(handle=session['handle'],process=session['pid'])
    else:
        window = ui_window(cfg['hwinfo']['ui'])
    window.minimize()


def execute_stage(cfg, mode, folder, stop, status, original_network):
    """每个测试的顺序集中在此处，便于测试人员直接阅读。"""
    check_test_cancelled(stop)
    setting = cfg['tests'][mode]
    folder.mkdir(exist_ok=False)
    cfg['_diagnostic_folder'] = str(folder)
    if setting.get('network')!=('online' if mode=='online_video' else 'offline'):
        raise ValueError('测试联网配置不符：只有在线视频联网，其余三项必须断网')
    status.pop('start_epoch', None)
    status.update(mode=mode,phase='准备',message=LABELS[mode])
    try:
        adapters=apply_network_state(setting['network'],original_network,cfg['network']['online_adapter_guid'])
        save_json(folder/'network_after_switch.json',{'requested':setting['network'],'adapters':adapters})
    except Exception as exc:
        detail={'requested':setting['network'],'error':str(exc)}
        try:detail['adapters']=network_snapshot()
        except Exception as snapshot_error:detail['snapshot_error']=str(snapshot_error)
        save_json(folder/'network_switch_error.json',detail)
        raise
    wait_seconds(setting['wait_before_s'],stop,status,'准备等待')
    check_test_cancelled(stop)
    # 等待结束后才打开本轮日志、启动负载并确定正式时间。
    status.update(phase='启动采样',message=LABELS[mode]+'：正在启动HWiNFO，尚未开始测试计时')
    launch_hwinfo(cfg)
    workload, started, conditions = None, False, None
    errors = []
    startup_cleanup_error = None
    try:
        status['message']=LABELS[mode]+'：正在设置采样间隔和CSV记录，尚未开始测试计时'
        csv_path = start_hwinfo_logging(cfg,folder,setting['interval_s'])
        check_test_cancelled(stop)
        minimize_owned_hwinfo(cfg)
        if mode == 'local_video':
            status.update(phase='本地视频启动检查',message='本地视频：正在确认播放器启动，尚未开始测试计时')
            workload = start_local_video(cfg,status=status,stop=stop)
        elif mode == 'online_video':
            workload = start_online_video(cfg, stop=stop, status=status)
        elif mode == 'bit':
            workload = start_bit(cfg,folder,setting['duration_s'],stop=stop,status=status)
        check_test_cancelled(stop)
        start_epoch, start_mono = time.time(), time.monotonic()
        native_bit = mode == 'bit' and workload.get('adapter') == 'bit9_script'
        if native_bit:
            start_epoch, start_mono = workload['start_epoch'], workload['start_mono']
        conditions = {'mode':mode,'start_epoch':start_epoch,'end_epoch':start_epoch+setting['duration_s'],
                      'interval_s':setting['interval_s'],'points':setting['points'],'hwinfo':cfg['hwinfo'],
                      'time_tolerance_s':cfg['report']['time_tolerance_s'],
                      'surface_tolerance_s':cfg['report']['surface_tolerance_s'],
                      'workload_status':'正在运行'}
        if mode == 'local_video':
            conditions.update(timer_source='program_monotonic', position_monitoring=False,
                              playback_evidence=workload.get('playback_evidence'))
        save_json(folder/'conditions.json',conditions)
        timing_label=LABELS[mode]+('联调短测' if cfg.get('_commissioning') else '正式测试')
        status.update(phase='正式测试',start_epoch=start_epoch,
                      message=f'{timing_label}：已开始计时，0/{setting["duration_s"]}秒')
        started = True
        last_check, previous_position, last_file_size, last_network_check = -10, None, 0, -30
        online_unavailable_since = None
        last_file_change_s = 0.0
        # BIT自己的时限控制负载；另外允许有上限的启动准备和日志收尾，不纳入温度选点。
        limit = setting['duration_s']+180 if native_bit else setting['duration_s']
        bit_complete = False
        while time.monotonic()-start_mono < limit:
            elapsed = time.monotonic()-start_mono
            status['message'] = (f'{timing_label}：已到设定时长，等待BIT结束并保存结果（等待{int(elapsed-setting["duration_s"])}秒）'
                                 if native_bit and elapsed>=setting['duration_s'] else
                                 f'{timing_label}：{int(elapsed)}/{setting["duration_s"]}秒')
            if native_bit and workload['process'].poll() is not None:
                result = collect_bit_result(workload, require_complete=True)
                conditions['start_epoch'] = result['start_epoch']
                conditions['end_epoch'] = result['end_epoch']
                conditions['bit_result'] = result
                bit_complete = True
                break
            if elapsed-last_check >= 5:
                last_check = elapsed
                # 在网卡/播放器检查之前成对读取，不能把后续查询耗时当成系统时钟跳变。
                clock_mono_before = time.monotonic()
                clock_epoch = time.time()
                clock_mono_after = time.monotonic()
                clock_elapsed = (clock_mono_before+clock_mono_after)/2-start_mono
                clock_delta = clock_epoch-(start_epoch+clock_elapsed)
                event = {'epoch':clock_epoch,'elapsed_s':clock_elapsed,
                         'clock_offset_s':clock_delta}
                try:
                    if mode == 'online_video':
                        event.update(runtime_online_video_status(workload['page']))
                        if event.get('observation_available') is False:
                            if online_unavailable_since is None: online_unavailable_since = elapsed
                            event['notice']='在线视频切流期间暂未读到画面；计时和采样继续'
                            event['observation_unavailable_s']=elapsed-online_unavailable_since
                            if event['observation_unavailable_s']>=30:
                                event['issue']='连续30秒无法读取视频控件，无法核验播放状态；计时和采样继续'
                            previous_position=None
                        else:
                            online_unavailable_since=None
                            require_bilibili = cfg['online_video'].get('adapter') == 'bilibili_1080'
                            issues, notices = online_video_runtime_findings(event, previous_position)
                            if notices: event['notice']='；'.join(notices)
                            if issues:
                                event['issue']='；'.join(issues)+'（计时继续）'
                            if require_bilibili:
                                action = restore_bilibili_quality(workload['page'], event,
                                    workload.setdefault('quality_attempts', {}))
                                if action: event['quality_correction'] = action
                            previous_position=event['position']
                    elif mode == 'local_video':
                        event.update(timer_source='program_monotonic', position_monitoring=False)
                        # 保留本轮窗口存在检查；不把程序经过时间冒充视频的播放位置。
                        event['player_window_open'] = local_video_window_is_open(workload)
                        if not event['player_window_open']:
                            event['issue']='本轮本地播放器窗口已关闭，计时继续'
                    elif mode == 'bit':
                        if native_bit:
                            # 启动已经核验，完成仍要由BIT最终报告验收。
                            event['bit_process_exit_code'] = workload['process'].poll()
                            event['bit_run_command_issued'] = True
                            event['bit_running_evidence'] = workload.get('running_evidence')
                        elif workload['process'].poll() is not None:
                            raise RuntimeError('BIT负载提前退出')
                        else:
                            profile=workload['profile']
                            event['bit_state']=ui_window(profile).child_window(**profile['running_control']).window_text()
                            if not re.search(profile['running_text_re'],event['bit_state']):
                                event['issue']='BIT界面未处于已核对的运行状态，计时继续'
                except Exception as exc:
                    event['issue']=str(exc)
                # 播放器控件偶尔读不到，仍要检查网卡、系统时钟和CSV更新。
                # 各项独立记录，后发生的异常不能覆盖先前原因。
                try:
                    if setting['network']=='offline' and elapsed-last_network_check>=30:
                        last_network_check=elapsed
                        adapters=network_snapshot()
                        problems=network_state_problems('offline',adapters,cfg['network']['online_adapter_guid'])
                        if problems:
                            raise RuntimeError('离线阶段网络异常：'+'；'.join(problems))
                except Exception as exc:
                    event['issue']='；'.join(filter(None,(event.get('issue'),str(exc))))
                if abs(clock_delta)>1+(clock_mono_after-clock_mono_before)/2:
                    event['issue']='；'.join(filter(None,(event.get('issue'),'正式测试期间系统时钟发生跳变')))
                try:
                    # 文件未更新仅记录，不因为缺一秒数据停止整项测试。
                    size = csv_path.stat().st_size
                    event['hwinfo_bytes']=size
                    sample_elapsed = time.monotonic()-start_mono
                    if size != last_file_size:
                        last_file_change_s = sample_elapsed
                    event['csv_no_update_s'] = sample_elapsed-last_file_change_s
                    if event['csv_no_update_s']>setting['interval_s']+10:
                        event['csv_unchanged']=True
                    last_file_size=size
                except Exception as exc:
                    event['issue']='；'.join(filter(None,(event.get('issue'),'HWiNFO日志检查：'+str(exc))))
                if event.get('notice'):
                    status['message'] += '；在线视频缓冲/观测提示，计时继续'
                append_record(folder/'events.jsonl',event)
                if event.get('issue'):
                    errors.append(event['issue'])
            if stop.wait(.2):
                raise InterruptedError('测试人员取消')
        if native_bit and not bit_complete:
            raise RuntimeError('BIT超过限定时间仍未自动结束，停止并保留现场')
        conditions['workload_status']='完成计时' if not errors else '完成计时但有异常：'+'；'.join(sorted(set(errors)))
    except BaseException as exc:
        if isinstance(exc, WorkloadCleanupError):
            startup_cleanup_error = str(exc)
        if conditions:
            conditions['end_epoch']=time.time()
            conditions['workload_status']='未完成：'+str(exc)
        raise
    finally:
        # 先停止负载和日志，再保存收尾记录；磁盘写满等记录失败不能跳过关窗。
        cleanup = [startup_cleanup_error] if startup_cleanup_error else []
        try:
            if mode=='online_video' and workload:
                stop_online_video(workload)
            elif mode == 'bit' and workload and workload.get('adapter') == 'bit9_script':
                try:
                    stop_bit_script(workload)
                finally:
                    if not (conditions and conditions.get('bit_result')):
                        collect_bit_result(workload, require_complete=False)
            elif mode == 'local_video' and workload:
                stop_local_video(workload)
            elif mode == 'bit' and workload:
                run_ui_actions(workload['profile'],'stop_actions')
                if mode=='bit':
                    result_path=folder/'bit_result.txt'
                    run_ui_actions(workload['profile'],'save_result_actions',{'result_path':result_path})
                    if not result_path.is_file() or result_path.stat().st_size==0:
                        raise RuntimeError('BIT没有保存本次负载结果日志')
        except Exception as exc:
            cleanup.append('停止负载：'+str(exc))
        try:
            stop_hwinfo_logging(cfg)
        except Exception as exc:
            cleanup.append('停止HWiNFO日志：'+str(exc))
        if cleanup:
            if conditions:
                conditions['workload_status']='收尾异常：'+'；'.join(cleanup)
            try:
                save_json(folder/'cleanup_errors.json',cleanup)
                if conditions:
                    save_json(folder/'conditions.json',conditions)
            except Exception as record_error:
                cleanup.append('保存收尾记录失败：'+str(record_error))
            raise WorkloadCleanupError('；'.join(cleanup))
        if conditions:
            save_json(folder/'conditions.json',conditions)
    if not started:
        raise RuntimeError('正式测试未开始')
    status.update(phase='已完成',message=LABELS[mode]+'已保存原始数据')


def copy_transfer_file(source, destination):
    """固定本次读取长度，原日志继续追加也不会把清单长度和回传内容弄乱。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as incoming, destination.open('xb') as outgoing:
        before = os.fstat(incoming.fileno())
        remaining = before.st_size
        while remaining:
            block = incoming.read(min(1024*1024, remaining))
            if not block:
                raise RuntimeError('生成回传副本时源文件缩短，请停止记录后补取：'+source.name)
            outgoing.write(block)
            remaining -= len(block)
        after = os.fstat(incoming.fileno())
    return {'size':before.st_size, 'sha256':sha256(destination),
            'source_changed_during_copy':(after.st_size,after.st_mtime_ns)!=(before.st_size,before.st_mtime_ns)}


class HostController:
    """USB指令仅限健康检查、预检、启动/取消和读取本任务文件。"""
    def __init__(self, cfg):
        self.cfg = cfg
        self.status = {'phase':'空闲','message':'等待接收电脑'}
        self.stop = threading.Event()
        self.worker = None
        self.folder = None
        self.job_id = None
        self.transfer_cache = None
        self.transfer_manifest = {}
        self.transfer_id = None
        self.transfer_job_id = None

    def snapshot_files(self):
        """只把固定副本列入清单；缓存放在任务目录外，原始文件不改动。"""
        cache_root = self.folder.parent/'_transfer_cache'
        cache_root.mkdir(exist_ok=True)
        cache = tempfile.TemporaryDirectory(prefix=self.job_id+'_', dir=cache_root)
        token = uuid.uuid4().hex
        manifest = {}
        root = self.folder.resolve()
        try:
            for source in sorted(self.folder.rglob('*')):
                if not source.is_file():
                    continue
                if not source.resolve().is_relative_to(root):
                    raise ValueError('回传源文件不在本任务目录内')
                name = source.relative_to(self.folder).as_posix()
                entry = copy_transfer_file(source, Path(cache.name)/name)
                manifest[name] = dict(entry, name=name, snapshot_id=token)
        except BaseException:
            cache.cleanup()
            raise
        previous = self.transfer_cache
        self.transfer_cache, self.transfer_manifest = cache, manifest
        self.transfer_id, self.transfer_job_id = token, self.job_id
        if previous is not None:
            previous.cleanup()
        return list(manifest.values())

    def refresh_sample(self):
        """每次正式测试前重新读系统盘；EUI由Sensors补全真实序列号，不跑短测。"""
        try:
            refresh_system_disk(self.cfg)
            if storage_identifier_is_eui(self.cfg['detected_disk']['serial']):
                launch_hwinfo(self.cfg)
                folder = ROOT/'部署记录'/('当前系统盘_'+uuid.uuid4().hex[:8])
                folder.mkdir(parents=True, exist_ok=False)
                bind_system_smart_group(self.cfg, hwinfo_sensor_window(self.cfg), folder)
            if not self.cfg['report'].get('expected_serial'):
                raise RuntimeError('未能取得当前系统盘的真实序列号')
        except Exception:
            self.cfg.pop('detected_disk', None)
            self.cfg['report'].update(capacity='', system_disk_model='', expected_serial='')
            self.cfg['hwinfo'].pop('system_disk_binding', None)
            self.cfg['hwinfo'].pop('logging_verification', None)
            self.cfg['hwinfo'].update(expected_sensor_group=None, smart_refresh_verified=False)
            save_setup_config(self.cfg, '本次系统盘读取失败', {'passed':False})
            raise
        save_setup_config(self.cfg, '正式测试前自动读盘', self.cfg['detected_disk'])

    def effective_config(self, received):
        """工具路径、界面和CSV适配只在主机维护；测试参数及样品命名来自接收端。"""
        cfg=copy.deepcopy(received)
        for key in HOST_TOOL_SECTIONS:
            cfg[key]=copy.deepcopy(self.cfg[key])
        if 'detected_disk' in self.cfg:
            cfg['detected_disk']=copy.deepcopy(self.cfg['detected_disk'])
        for key in ('capacity','system_disk_model','expected_serial'):
            cfg['report'][key]=self.cfg['report'].get(key, '')
        cfg.pop('_commissioning',None)
        cfg.pop('_diagnostic_folder',None)
        return cfg

    def open_saved_job(self,job_id):
        """恢复指定任务的文件访问，不启动线程，也不把中断任务标为成功。"""
        if not isinstance(job_id,str) or not re.fullmatch(r'[a-f0-9]{32}',job_id):
            raise ValueError('任务编号无效')
        if self.worker is not None and self.worker.is_alive():
            raise RuntimeError('主机仍有任务运行，不能切换回收对象')
        root=Path(self.cfg['host_workspace']).resolve()
        folder=(root/job_id).resolve()
        if not folder.is_relative_to(root) or not (folder/'task.json').is_file():
            raise ValueError('未找到该任务记录；v0.1旧任务需手工复制原jobs目录')
        record=json.loads((folder/'task.json').read_text('utf-8'))
        if record.get('job_id')!=job_id or record.get('station')!=self.cfg['station']:
            raise ValueError('任务记录与工位不符')
        status_path=folder/'status.json'
        status=json.loads(status_path.read_text('utf-8')) if status_path.exists() else {}
        if not record.get('finished') or not isinstance(status,dict) or not status.get('phase'):
            status={'phase':'未完成','job_id':job_id,'message':'上次主机执行端中断；只能回收已有数据，不自动重跑负载'}
        self.folder,self.job_id,self.status=folder,job_id,status
        return {'job_id':job_id,'status':dict(status),'busy':False,'recovered':True}

    def check_file_job(self,data):
        if data.get('job_id')!=self.job_id or self.job_id is None:
            raise ValueError('回收任务编号不符，拒绝混入另一个任务的文件')

    def run_job(self,cfg,modes):
        original = None
        import pythoncom
        pythoncom.CoInitialize()
        try:
            original = remember_wifi_profiles(network_snapshot())
            save_json(self.folder/'network_before.json',original)
            save_json(self.folder/'network_restore_pending.json',
                      {'station':self.cfg['station'],'job_id':self.job_id,'adapters':original})
            save_json(self.folder/'config_snapshot.json',cfg)
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000003)
            if cfg.get('_commissioning'):
                from 集中联调 import run_host_checks
                passed = run_host_checks(self,cfg,original)
                self.status.update(phase='完成' if passed else '未完成',
                    message='集中联调已结束，请回收全部结果'+('' if passed else '（有待处理项目）'))
            else:
                for mode in modes:
                    execute_stage(cfg,mode,self.folder/mode,self.stop,self.status,original)
                self.status.update(phase='完成',message='本批次完成，请回收数据')
        except BaseException as exc:
            self.status.update(phase='未完成',message=f'{type(exc).__name__}: {exc}')
        finally:
            try:
                if original is not None:
                    restored=restore_network(original)
                    save_json(self.folder/'network_after_restore.json',restored)
                    (self.folder/'network_restore_pending.json').unlink(missing_ok=True)
            except Exception as exc:
                self.status.update(phase='未完成',message=self.status['message']+'；'+str(exc))
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            save_json(self.folder/'status.json',self.status)
            task_path=self.folder/'task.json'
            if task_path.is_file():
                task=json.loads(task_path.read_text('utf-8'))
                task.update(finished=True,ended_epoch=time.time())
                save_json(task_path,task)
            pythoncom.CoUninitialize()

    def handle(self,command,data):
        busy = self.worker is not None and self.worker.is_alive()
        if command=='hello':
            return {'version':VERSION,'protocol':2,'station':self.cfg['station'],'epoch':time.time(),'busy':busy,
                    'job_id':self.job_id,'status':dict(self.status),
                    'program_dir':str(ROOT),'main_sha256':sha256(Path(__file__))}
        if command=='clock':
            return {'epoch':time.time()}
        if command=='echo':
            text=data.get('text','')
            if not isinstance(text,str) or len(text)>4096:
                raise ValueError('通信自检内容超长')
            return {'text':text,'epoch':time.time()}
        if command=='prepare_clock':
            if busy:
                raise RuntimeError('正式任务进行中不允许修改时区')
            return {'timezone':ensure_beijing_timezone()}
        if command=='set_clock':
            if busy:
                raise RuntimeError('正式任务进行中不允许修改时钟')
            set_windows_system_time(float(data['epoch']))
            return {'epoch':time.time()}
        if command=='preflight':
            if busy:
                raise RuntimeError('主机仍在执行任务，不能重新识别盘片或准备另一轮测试')
            self.refresh_sample()
            cfg=self.effective_config(data['config'])
            return {'problems':preflight_host(cfg,data['modes'])+workload_window_problems(cfg), 'network':network_snapshot(),
                    'network_config':cfg['network'],
                    'detected_disk':copy.deepcopy(self.cfg['detected_disk']),
                    'sample':{k:cfg['report'].get(k,'') for k in ('capacity','system_disk_model','expected_serial')},
                    'host_tools':{key:cfg[key] for key in HOST_TOOL_SECTIONS}}
        if command=='workload-windows':
            if busy:
                raise RuntimeError('主机仍在执行任务，请等待本轮完成')
            return {'problems':workload_window_problems(self.cfg)}
        if command=='commissioning-info':
            if busy:
                raise RuntimeError('主机仍在执行任务')
            from 集中联调 import prepare_configuration
            self.cfg,issues=prepare_configuration(self.cfg,'host')
            return {'issues':issues,'host_tools':{k:self.cfg[k] for k in HOST_TOOL_SECTIONS},
                    'sample':{k:self.cfg['report'].get(k,'') for k in ('capacity','system_disk_model','expected_serial')}}
        if command=='open_job':
            return self.open_saved_job(data.get('job_id'))
        if command=='job_exists':
            job_id=data.get('job_id','')
            if not isinstance(job_id,str) or not re.fullmatch(r'[a-f0-9]{32}',job_id):
                raise ValueError('任务编号无效')
            record=Path(self.cfg['host_workspace'])/job_id/'task.json'
            if not record.is_file(): return {'exists':False}
            saved=json.loads(record.read_text('utf-8'))
            if saved.get('job_id')!=job_id or saved.get('station')!=self.cfg['station']:
                raise ValueError('任务记录与工位不符')
            return {'exists':True}
        if command=='start':
            job_id = data['job_id']
            if not re.fullmatch(r'[a-f0-9]{32}',job_id):
                raise ValueError('任务编号无效')
            # 即使同一任务以新的请求编号重发，也不重复执行负载。
            if job_id==self.job_id:
                return {'job_id':job_id,'status':dict(self.status)}
            if busy:
                raise RuntimeError('主机已有任务运行')
            root = Path(self.cfg['host_workspace']).resolve()
            if (root/job_id).exists():
                return self.open_saved_job(job_id)
            cfg, modes = self.effective_config(data['config']),data['modes']
            kind = data.get('kind','formal')
            if kind not in ('formal','commissioning'):
                raise ValueError('任务类型无效')
            if kind=='commissioning':
                modes=list(ORDER)
                cfg['_commissioning']=True
            elif any(data['config']['report'].get(key) != self.cfg['report'].get(key)
                     for key in ('capacity','system_disk_model','expected_serial')):
                raise RuntimeError('主机盘信息与本次接收端确认的信息不同，请重新选择B自动识别当前盘；未启动测试。')
            problems = preflight_host(cfg,modes) if kind=='formal' else []
            problems.extend(workload_window_problems(cfg))
            if problems:
                raise ValueError('；'.join(problems))
            # 上一任务恢复失败时先解决原记录，不能继续累积失败任务。
            recover_pending_network(self.cfg,interactive=False)
            self.folder = root/job_id
            self.folder.mkdir(parents=True,exist_ok=False)
            save_json(self.folder/'task.json',{'job_id':job_id,'station':self.cfg['station'],
                      'modes':list(modes),'kind':kind,'created_epoch':time.time(),'finished':False})
            self.job_id=job_id
            self.stop.clear()
            self.status={'phase':'准备','job_id':job_id,'message':'即将开始'}
            self.worker=threading.Thread(target=self.run_job,args=(cfg,modes),daemon=False)
            self.worker.start()
            return {'job_id':job_id}
        if command=='status':
            return {'status':dict(self.status),'busy':busy,'job_id':self.job_id}
        if command=='cancel':
            self.check_file_job(data)
            self.stop.set()
            return {'requested':True}
        if command=='files':
            self.check_file_job(data)
            if busy:
                raise RuntimeError('任务仍在写文件，等待结束后校验回收')
            if self.folder is None:
                return []
            return self.snapshot_files()
        if command=='read_file':
            self.check_file_job(data)
            if busy or self.folder is None:
                raise RuntimeError('本任务文件暂不可读取')
            if (self.transfer_cache is None or self.transfer_job_id != self.job_id
                    or data.get('snapshot_id', self.transfer_id) != self.transfer_id):
                raise ValueError('回传副本已失效，请重新获取本任务文件清单后补取')
            name = data.get('name')
            if not isinstance(name, str) or name not in self.transfer_manifest:
                raise ValueError('只能读取本任务回传清单内的文件')
            root = Path(self.transfer_cache.name).resolve()
            target = (root/name).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise ValueError('回传副本路径无效')
            offset=int(data['offset'])
            size = self.transfer_manifest[name]['size']
            length = data.get('length', 12*1024)
            if offset<0 or offset>size or type(length) is not int or not 0<length<=12*1024:
                raise ValueError('文件分块偏移或长度无效')
            with target.open('rb') as stream:
                stream.seek(offset)
                content=stream.read(min(length,size-offset))
            return {'offset':offset,'base64':base64.b64encode(content).decode('ascii')}
        raise ValueError('不支持的USB指令：'+command)


def synchronize_two_clocks(cfg,link,folder):
    print('正在获取NTP标准时间并通过USB校准测试主机……',flush=True)
    reference=obtain_internet_standard_time(cfg)
    ensure_beijing_timezone()
    prepared=link.request('prepare_clock',timeout=30)
    if prepared.get('timezone')!='China Standard Time':
        raise RuntimeError('测试主机未确认北京时间时区')
    set_windows_system_time(standard_time_now(reference))
    measures=[]
    for _ in range(5):
        start=time.monotonic()
        link.request('clock',attempts=1)
        measures.append(time.monotonic()-start)
    rtt=min(measures)
    if rtt>1:
        raise RuntimeError('串口往返超过1秒，校时精度不足')
    # 设置命令只发送一次。重发带旧时间的设置命令可能使时钟回跳。
    link.request('set_clock',{'epoch':standard_time_now(reference)+rtt/2},attempts=1)
    verify=[]
    for _ in range(5):
        begin=time.monotonic(); local=time.time()
        reply=link.request('clock',attempts=1)
        elapsed=time.monotonic()-begin
        verify.append({'rtt_s':elapsed,'offset_s':reply['epoch']-(local+elapsed/2)})
    best=min(verify,key=lambda x:x['rtt_s'])
    save_json(folder/'校时记录.json',{'ntp':reference,'serial':verify,'best':best,'timezone':'China Standard Time'})
    if abs(best['offset_s'])>cfg['clock_sync']['max_offset_s']:
        raise RuntimeError('双机校时未通过，请检查校时记录')


def collect_jk808(cfg,path,stop,fatal_errors=None):
    instrument=JK808Thermometer(cfg['thermometer'],lambda direction,data:
        append_record(path.with_name('jk808_usb.jsonl'),{'epoch':time.time(),'direction':direction,'hex':data.hex()}))
    begin=time.monotonic(); slot=0
    try:
        while not stop.is_set():
            delay=begin+slot-time.monotonic()
            if delay>0 and stop.wait(delay):
                break
            epoch=time.time()
            row={'epoch':epoch,'slot':slot,'source_row':slot+1}
            try:
                values,status,raw=instrument.read()
                row.update({key:values[channel] for key,channel in cfg['thermometer']['channel_map'].items()})
                row['channel_status']=status
            except Exception as exc:
                row['error']=str(exc)
                try:
                    instrument.close()
                except Exception as close_error:
                    row['close_error']=str(close_error)
            append_record(path,row)
            slot=max(slot+1,int(time.monotonic()-begin)+1)
    except Exception as exc:
        # 单次仪器读取失败在上面留空继续；文件已不能保存时，通知接收端停止本轮。
        if fatal_errors is None:
            raise
        fatal_errors.append(f'{type(exc).__name__}: {exc}')
    finally:
        try:
            instrument.close()
        except Exception as close_error:
            print('JK808关闭设备失败：'+str(close_error),flush=True)


def receive_files(link,folder,job_id=None):
    folder=Path(folder).resolve()
    old_manifest_path=folder/'文件校验.json'
    old_manifest=json.loads(old_manifest_path.read_text('utf-8')) if old_manifest_path.is_file() else []
    manifest=link.request('files',{'job_id':job_id},timeout=15)
    print(f'测试已结束，正在回传并校验{len(manifest)}个文件。请等待最终结果提示，不要提前手工打包。',flush=True)
    next_progress=time.monotonic()+10
    names=set()
    for file_number,entry in enumerate(manifest,1):
        if (not isinstance(entry.get('name'),str) or entry['name'] in names
                or type(entry.get('size')) is not int or entry['size']<0
                or not re.fullmatch(r'[a-f0-9]{64}',entry.get('sha256',''))):
            raise ValueError('主机返回了无效或重复的文件清单')
        canonical=entry['name'].replace('\\','/').lower()
        parts=canonical.split('/')
        if (canonical in names or any(p in ('','.','..') or p.rstrip(' .')!=p for p in parts)
                or canonical=='文件校验.json' or ':' in canonical):
            raise ValueError('主机返回了不合法或重复文件路径')
        names.add(canonical)
        target=(folder/entry['name']).resolve()
        if not target.is_relative_to(folder.resolve()) or target==folder.resolve() or ':' in entry['name']:
            raise ValueError('主机返回了不合法文件路径')
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.is_file() and target.stat().st_size==entry['size'] and sha256(target)==entry['sha256']:
            continue
        temporary=target.with_suffix(target.suffix+'.partial')
        offset=temporary.stat().st_size if temporary.exists() else 0
        if offset>entry['size']:
            temporary.unlink(); offset=0
        with temporary.open('ab') as stream:
            while offset<entry['size']:
                request={'job_id':job_id,'name':entry['name'],'offset':offset,
                         'length':min(12*1024,entry['size']-offset)}
                if 'snapshot_id' in entry:
                    request['snapshot_id']=entry['snapshot_id']
                block=link.request('read_file',request)
                content=base64.b64decode(block['base64'],validate=True)
                if block['offset']!=offset or not content or offset+len(content)>entry['size']:
                    raise ValueError(f'文件分块偏移或长度错误：{entry["name"]}；'
                                     f'请求偏移{offset}，返回偏移{block["offset"]}，'
                                     f'返回{len(content)}字节，清单总长{entry["size"]}。'
                                     '请停止HWiNFO日志，并将两端更新后选择C补取。')
                stream.write(content); stream.flush()
                offset+=len(content)
                if time.monotonic()>=next_progress:
                    print(f'回传文件 {file_number}/{len(manifest)}：{entry["name"]}（{offset}/{entry["size"]}字节）',flush=True)
                    next_progress=time.monotonic()+10
        if sha256(temporary)!=entry['sha256']:
            temporary.unlink()
            raise ValueError('回收文件SHA256校验失败：'+entry['name'])
        temporary.replace(target)
    # 只清理由上轮清单证明属于回传、且内容未被人工改过的文件。
    # 例如主机恢复网卡后已删除pending标记，补取时本地不能继续留下旧标记。
    for entry in old_manifest:
        name=entry.get('name','')
        if not isinstance(name,str) or not name or name.replace('\\','/').lower() in names:
            continue
        stale=(folder/name).resolve()
        if (stale.is_relative_to(folder) and ':' not in name and stale.is_file()
                and sha256(stale)==entry.get('sha256')):
            stale.unlink()
    save_json(folder/'文件校验.json',manifest)


def report_filename(cfg, label, suffix):
    return artifact_stem(label, cfg['report'].get('capacity',''), cfg.get('_simulated',False))+suffix


def write_task_report(cfg, folder, summaries, *, final_attempt=None, stage_sources=None, pending_modes=()):
    """把任务结束、网卡恢复和各项统计放在同一页，不能只看到曲线就误认整套成功。"""
    host = (final_attempt or folder)/'手工复测'/'主机数据'
    issues = []
    if pending_modes:
        issues.append('仍需重试或继续：'+'、'.join(LABELS[m] for m in pending_modes)+'；接收端选择D继续')
    if not cfg.get('_simulated'):
        try:
            state = json.loads((host/'status.json').read_text('utf-8'))
            if state.get('phase')!='完成':
                issues.append('主机任务未完整结束：'+state.get('message','未记录原因'))
            task = json.loads((host/'task.json').read_text('utf-8'))
            if task.get('finished') is not True:
                issues.append('任务没有持久化的结束记录，可能中途退出')
        except Exception as exc:
            issues.append('未取得可读取的主机结束状态：'+str(exc))
        if (host/'network_restore_pending.json').exists():
            issues.append('主机网卡尚未恢复；请先在测试主机处理恢复提示，再补取结果')
        else:
            try:
                restored = json.loads((host/'network_after_restore.json').read_text('utf-8'))
                if restored.get('restored') is not True:
                    issues.append('未确认主机网卡恢复成功')
            except Exception:
                issues.append('缺少网卡恢复完成记录')
    for mode,result in summaries.items():
        if (not result.get('workload_ok') or not result.get('selected_count')
                or result.get('data_issue_count') or result.get('excel_error')):
            issues.append(LABELS[mode]+'：'+result['status']+
                          ('；Excel未更新：'+result['excel_error'] if result.get('excel_error') else ''))
    result = {'completed':not issues,'issues':issues,'modes':list(summaries),
              'simulated':bool(cfg.get('_simulated'))}
    save_json(folder/'手工复测'/'任务结果.json',result)
    title = '本次结果'+('（模拟数据）' if cfg.get('_simulated') else '')
    body = '<!doctype html><meta charset="utf-8"><title>'+title+'</title>'
    body += '<style>body{font:17px/1.7 sans-serif;max-width:1000px;margin:32px auto}td,th{padding:10px;border:1px solid #bbb}table{border-collapse:collapse}</style>'
    body += '<h1>'+title+'</h1><p>'+('有待处理事项，请查看下列说明。' if issues else '任务、网卡恢复及所选数据检查完成。')+'</p>'
    body += '<ul>'+''.join('<li>'+escape(v)+'</li>' for v in issues)+'</ul>'
    body += '<table><tr><th>项目</th><th>结果</th><th>查看</th></tr>'
    for mode,part in summaries.items():
        name = report_filename(cfg, LABELS[mode], '.html')
        body += '<tr><td>'+escape(LABELS[mode])+'</td><td>'+escape(part['status'])+'</td><td><a href="'+escape(name)+'">本项报告</a></td></tr>'
    body += '</table><p>原始数据、异常时间和复算代码位于手工复测文件夹。Excel为选点统计，不代表任务全部通过。</p>'
    if stage_sources:
        body += '<h2>续测数据来源</h2><ul>'+''.join('<li>'+escape(LABELS[m])+'：'+
            escape(str(p.relative_to(folder)))+'</li>' for m,p in stage_sources.items())+'</ul>'
        body += '<p>已经完成的项目沿用原始数据；重试数据独立保存在手工复测内，各次失败记录保留。</p>'
    (folder/report_filename(cfg,'本次结果','.html')).write_text(body,'utf-8')
    for issue in issues:
        print('需核对：'+issue,flush=True)
    return result


def finish_reports(cfg,folder,modes, *, stage_sources=None, final_attempt=None, pending_modes=()):
    from company_template import create_empty_company_template, fill_windows_result
    manual=folder/'手工复测'
    # 即使Excel正被占用，旧的“成功”总览也不能继续充当本轮复算结果。
    (folder/report_filename(cfg,'本次结果','.html')).write_text('<!doctype html><meta charset="utf-8"><p>正在重新生成结果，请等待程序结束。旧Excel尚未确认更新。</p>','utf-8')
    workbook=folder/report_filename(cfg,'温度汇总','.xlsx')
    legacy=folder/('温度汇总_模拟数据.xlsx' if cfg.get('_simulated') else '温度汇总.xlsx')
    if not workbook.exists() and legacy.exists():
        shutil.copy2(legacy,workbook)  # 旧报告续测时保留人工填写的测试版本等单元格。
    workbook_error = None
    if not workbook.exists():
        try:
            create_empty_company_template(ROOT/'温度模版_原始.xlsx',workbook)
        except Exception as exc:
            workbook_error = str(exc)
    recalculation=manual/'复算代码'
    recalculation.mkdir(exist_ok=True)
    shutil.copy2(ROOT/'temperature_report.py',recalculation/'复算温度.py')
    (recalculation/'使用方法.txt').write_text(
        '每个项目文件夹包含 conditions.json、hwinfo.csv、jk808.jsonl，以及负载事件。\n'
        '这些文件足以独立复算，不需要连接两台测试电脑。\n'
        '在程序目录用以下方式运行（把路径替换为实际项目数据路径）：\n'
        'runtime\\python.exe temperature_report.py "结果目录\\手工复测\\主机数据\\idle"\n'
        '也可运行本文件夹的复算温度.py，算法完全相同。\n'
        '输出位于该项目的复算结果子目录；不会覆盖外层公司汇总表。\n'
        '选中绘图及统计点.csv 中每行就是一个计算点；max/mean/min按该列直接计算。\n',encoding='utf-8')
    summaries={}
    for mode in modes:
        source=(stage_sources or {}).get(mode,folder)/'手工复测'
        surface_path=source/'jk808.jsonl'
        all_surface=surface_path.read_text(encoding='utf-8') if surface_path.is_file() else ''
        stage=source/'主机数据'/mode
        stage.mkdir(parents=True,exist_ok=True)
        (stage/'jk808.jsonl').write_text(all_surface,encoding='utf-8')
        try:
            if stage_sources is not None:
                from resume_flow import transfer_complete
                if not transfer_complete(source.parent):
                    raise ValueError('本次原始文件尚未完整回传校验，请选C或D补取；不能用部分文件出正式结果')
            result,png=export_stage(stage,folder,mode=mode,simulated=cfg.get('_simulated',False),
                                    capacity=cfg['report'].get('capacity',''))
        except Exception as exc:
            result=write_stage_failure(stage,folder,mode,exc,cfg.get('_simulated',False),cfg['report'].get('capacity',''))
            png=None
        try:
            # 失败也更新为空白结果，移除本项旧数字和旧图片，其他项目照常保留。
            if workbook_error:
                raise RuntimeError(workbook_error)
            fill_windows_result(workbook,cfg['report']['capacity'],mode,result['statistics'],
                cfg['report'],png,result['selected_count'],result['data_issue_count'],result['status'])
        except Exception as exc:
            result['excel_error']=str(exc)
        summaries[mode]=result
    save_json(manual/'结果汇总.json',summaries)
    write_task_report(cfg,folder,summaries,final_attempt=final_attempt,stage_sources=stage_sources,pending_modes=pending_modes)
    # 旧版本固定名称移入原始资料，外层只显示这次按容量命名的报告。
    old_names=['本次结果.html','温度汇总.xlsx']+[LABELS[m]+suffix for m in modes for suffix in ('.png','.html')]
    for name in old_names:
        old=folder/name
        if old.exists() and cfg['report'].get('capacity') and not cfg.get('_simulated'):
            archived=manual/'旧版报告'; archived.mkdir(exist_ok=True)
            destination=archived/name
            if destination.exists(): destination=archived/(uuid.uuid4().hex[:8]+'_'+name)
            old.replace(destination)
    return summaries


def adopt_current_sample(cfg, check):
    """只接受主机本次返回的盘身份，不用接收端保存的上一张盘补缺值。"""
    sample = check.get('sample', {})
    if (sample.get('capacity') not in ('512G','1T','2T')
            or not str(sample.get('system_disk_model') or '').strip()
            or not str(sample.get('expected_serial') or '').strip()):
        raise RuntimeError('主机未返回完整的当前系统盘型号、容量和序列号；未开始测试，请查看主机读盘提示。')
    cfg=copy.deepcopy(cfg)
    cfg.update(copy.deepcopy(check['host_tools']))
    for key in ('capacity','system_disk_model','expected_serial'):
        cfg['report'][key]=sample[key]
    cfg.pop('detected_disk',None)
    if check.get('detected_disk'):
        cfg['detected_disk']=copy.deepcopy(check['detected_disk'])
    save_setup_config(cfg,'接收端自动同步当前盘',sample)
    print('当前测试盘：'+cfg['report']['system_disk_model']+'；容量：'+cfg['report']['capacity']+
          '；序列号：'+cfg['report']['expected_serial'],flush=True)
    print('项目：'+cfg['report']['project']+'；批次：'+cfg['report']['batch'],flush=True)
    return cfg


@keep_windows_awake()
def run_receiver(cfg,modes, *, resume_folder=None):
    require_administrator()
    link=SerialConnection(cfg['serial']['receiver_port'],cfg['serial']['baudrate'])
    stop=threading.Event(); collector=None; task=None; submitted=False; manual=None
    collector_errors=[]
    try:
        hello=link.request('hello')
        if hello.get('protocol')!=2:
            raise ValueError('请将两端都更新到Windows v0.2，当前USB协议不一致')
        if hello.get('version')!=VERSION:
            raise ValueError('两端版本不同：接收端 '+VERSION+'；主机 '+str(hello.get('version'))+
                             '。请关闭两台电脑的旧窗口，完整覆盖更新后重开')
        if hello['station']!=cfg['station'] or hello['busy']:
            raise RuntimeError('主机工位不一致或已有任务；请先查看状态')
        print('正在从测试主机自动读取当前系统盘信息……',flush=True)
        check=link.request('preflight',{'config':cfg,'modes':modes},timeout=180)
        if resume_folder is not None:
            from resume_flow import check_identity, task_record
            original=task_record(resume_folder)['config']
            check_identity(original['report'],cfg['report'])
            check_identity(original['report'],check.get('sample',{}),disk_only=True)
        cfg=adopt_current_sample(cfg,check)
        if check['problems']:
            raise ValueError('\n'.join(check['problems']))
        if not cfg['thermometer']['protocol_verified'] or not cfg['thermometer']['hid_path_hex']:
            raise ValueError('请先部署识别JK808并核对三个通道')
        # 先实读一次，避免仪器没有接好仍在主机上运行完整套负载。
        probe=JK808Thermometer(cfg['thermometer'],lambda *_:None)
        try:
            values,_,_=probe.read()
            if any(values.get(c) is None or not math.isfinite(values[c]) for c in cfg['thermometer']['channel_map'].values()):
                raise ValueError('JK808测试前实读有无效通道，请核对接线')
        finally:
            probe.close()
        task=uuid.uuid4().hex
        label=safe_name(cfg['report']['capacity'])+'_'+datetime.now(BJ).strftime('%Y%m%d_%H%M%S')+'_'+task[:8]
        folder=(Path(resume_folder)/'手工复测/续测记录'/label if resume_folder is not None else
                ROOT/cfg['output_dir']/safe_name(cfg['report']['project'])/safe_name(cfg['report']['batch'])/label)
        manual=folder/'手工复测'; manual.mkdir(parents=True,exist_ok=False)
        save_json(manual/'程序版本核对.json',{'receiver':{'version':VERSION,'program_dir':str(ROOT),
                    'main_sha256':sha256(Path(__file__))},
                    'host':{k:hello.get(k) for k in ('version','program_dir','main_sha256')}})
        save_json(manual/'本次任务.json',{'job_id':task,'config':cfg,'modes':modes})
        save_json(manual/'接收进度.json',{'phase':'not_started','job_id':task})
        if resume_folder is not None:
            from resume_flow import add_attempt
            add_attempt(resume_folder,folder,save_json)
        save_json(ROOT/'部署记录/最近正式任务.json',{'folder':str(Path(resume_folder or folder).resolve())})
        print('本次结果目录：'+str(folder),flush=True)
        synchronize_two_clocks(cfg,link,manual)
        # 先启动采集，避免串口轮询延迟丢掉主机开始后的首秒；等待段保留原始记录但不参与统计。
        collector=threading.Thread(target=collect_jk808,args=(cfg,manual/'jk808.jsonl',stop,collector_errors),daemon=False)
        collector.start()
        if collector_errors:
            raise OSError('JK808采集文件无法保存：'+collector_errors[0])
        save_json(manual/'接收进度.json',{'phase':'start_requested','job_id':task})
        submitted=True  # 启动回包丢失也必须按此任务编号取消，不能漏停。
        link.request('start',{'job_id':task,'config':cfg,'modes':modes},timeout=180)
        last_message=None
        previous_poll=None
        displayed_results={}
        deadline=time.monotonic()+sum(cfg['tests'][m]['duration_s']+cfg['tests'][m]['wait_before_s']+180 for m in modes)+600
        while time.monotonic()<deadline:
            if collector_errors:
                raise OSError('JK808采集文件无法保存：'+collector_errors[0])
            try:
                status=link.request('status')
            except (OSError,TimeoutError) as exc:
                previous_poll=None
                append_record(manual/'通信异常.jsonl',{'epoch':time.time(),'error':str(exc)})
                print('USB通信暂时中断；主机继续本地任务，正在重连。',flush=True)
                time.sleep(1)
                continue
            if status['job_id']!=task:
                raise RuntimeError('主机任务编号改变，不能混用结果')
            deadline,previous_poll=extend_deadline_for_user_wait(deadline,previous_poll,status)
            print_stage_results(status['status'],displayed_results)
            message=status['status']['message']
            # 每10秒输出一次，避免CMD滚屏淹没关键信息。
            bucket=int(time.monotonic()/10)
            if (bucket,status['status']['phase'])!=last_message:
                print(message,flush=True); last_message=(bucket,status['status']['phase'])
            if not status['busy']:
                submitted=False
                break
            time.sleep(.5)
        else:
            raise TimeoutError('整套任务超过预期上限；保留数据，可用补取结果恢复')
        stop.set(); collector.join(timeout=3)
        if collector_errors:
            raise OSError('JK808采集文件无法保存：'+collector_errors[0])
        receive_files(link,manual/'主机数据',task)
        save_json(manual/'接收进度.json',{'phase':'received','job_id':task})
        summaries=finish_reports(cfg,folder,modes)
        for mode,result in summaries.items():
            print(LABELS[mode]+'：'+result['status'])
        print('结果：'+str(folder),flush=True)
        return folder
    except (Exception,KeyboardInterrupt) as exc:
        # 先请求停止负载；磁盘已满时，写错误日志不能妨碍主机收尾。
        if submitted:
            try:
                link.request('cancel',{'job_id':task})
                print('已请求主机停止并恢复网络，原始记录保留；结束后可补取结果。')
            except Exception as cancel_error:
                print('取消指令未确认：'+str(cancel_error)+'；请到主机窗口按Ctrl+C。')
        if manual is not None:
            try:
                save_json(manual/'接收端异常.json',{'error':f'{type(exc).__name__}: {exc}','job_id':task})
            except OSError as log_error:
                print('接收端异常记录未保存：'+str(log_error)+'；原始错误：'+str(exc),flush=True)
        raise
    finally:
        stop.set()
        if collector:
            collector.join(timeout=3)
        link.close()


def run_host(cfg):
    require_administrator()
    recover_pending_network(cfg)
    controller=HostController(cfg)
    stop=threading.Event()
    connection=SerialConnection(cfg['serial']['host_port'],cfg['serial']['baudrate'])
    print('主机执行端已启动；等待USB串口任务。Ctrl+C停止并恢复本轮网络。',flush=True)
    try:
        RequestServer(connection,controller.handle).serve(stop)
    except KeyboardInterrupt:
        pass
    finally:
        # 无论Ctrl+C还是串口服务意外退出，都让执行线程先停止负载、恢复网卡。
        controller.stop.set()
        try:
            if controller.worker:
                controller.worker.join(timeout=60)
                if controller.worker.is_alive():
                    print('主机仍在停止负载和恢复网卡，请保留窗口等待；原始数据已保留。',flush=True)
        finally:
            stop.set(); connection.close()


def deployment(cfg,role):
    from serial.tools import list_ports
    ports=list(list_ports.comports())
    for port in ports:
        print(port.device,port.description,port.hwid)
    current=cfg['serial'][role+'_port']
    choice=input(f'{role}端USB互连COM口 [{current}]：').strip() or current
    if choice not in {p.device for p in ports}:
        raise ValueError('未找到选择的COM口，请接好FTDI线或安装驱动')
    cfg['serial'][role+'_port']=choice
    if role=='receiver':
        import hid
        devices=hid.enumerate(0x0483,0x5757)
        for index,device in enumerate(devices,1):
            print(index,device.get('product_string'),device['path'].hex())
        index=int(input('JK808设备编号：'))-1
        if not 0<=index<len(devices):
            raise ValueError('设备编号不在列表中')
        cfg['thermometer']['hid_path_hex']=devices[index]['path'].hex()
        instrument=JK808Thermometer(cfg['thermometer'],lambda *_:None)
        try:
            print('实读通道温度：',instrument.read()[0])
            cfg['thermometer']['protocol_verified']=input('与仪器读数及探头通道对应，输入Y：').strip().upper()=='Y'
        finally:
            instrument.close()
    else:
        adapters=network_snapshot()
        for index,adapter in enumerate(adapters,1):
            print(index,adapter['Name'],adapter['InterfaceDescription'],adapter['InterfaceGuid'])
        index=int(input('在线视频使用的网卡编号：'))-1
        if not 0<=index<len(adapters):
            raise ValueError('网卡编号不在列表中')
        cfg['network']['online_adapter_guid']=adapters[index]['InterfaceGuid']
        cfg=bind_hwinfo_usb(cfg)
    save_json(CONFIG,cfg)
    print('已保存config.json。两端部署时分别选择各自COM口；测试参数由接收端下发。')


def process_executable_path(process_id):
    """查询真实进程路径，用于匹配已绑定U盘，不能只比较程序名称。"""
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,
                                               wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype=wintypes.BOOL
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.CloseHandle.restype=wintypes.BOOL
    handle=kernel.OpenProcess(0x1000,False,int(process_id))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size=wintypes.DWORD(32768)
        buffer=ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle,0,buffer,ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value
    finally:
        kernel.CloseHandle(handle)


def process_executable_name(process_id):
    return Path(process_executable_path(process_id)).name.lower()


def inspection_software_group(executable,title,cfg):
    """仅匹配实际测试软件进程；播放器采用系统应用宿主时再核对窗口标题。"""
    executable=executable.lower()
    if executable in {'explorer.exe','cmd.exe','powershell.exe','pwsh.exe',
                       'windowsterminal.exe','conhost.exe'}:
        return None
    targets={
        'HWiNFO': {'hwinfo32.exe','hwinfo64.exe'},
        'BurnInTest': {'bit.exe'},
        '播放器': {'wmplayer.exe','music.ui.exe','mediaplayer.exe','microsoft.media.player.exe'},
    }
    for group,section in [('HWiNFO','hwinfo'),('BurnInTest','burnintest')]:
        configured=cfg.get(section,{}).get('executable','')
        if configured:
            targets[group].add(Path(configured).name.lower())
    for group,names in targets.items():
        if executable in names:
            return group
    if executable=='applicationframehost.exe' and re.search(
            r'媒体播放器|Media Player|电影和电视|Movies\s*&\s*TV',title,re.I):
        return '播放器'
    return None


def export_window_controls(window,path):
    """绕开库默认的GBK文件输出，完整保留中文、®及其他Unicode控件名。"""
    captured=io.StringIO()
    try:
        with redirect_stdout(captured):
            window.print_control_identifiers(depth=5)
    finally:
        # 单个窗口中途关闭时，也保留已经采集的内容用于排查。
        Path(path).write_text(captured.getvalue(),encoding='utf-8-sig')


def inspect_ui(output=None):
    from pywinauto import Desktop
    import traceback
    output=Path(output) if output is not None else ROOT/'界面检查'
    output.mkdir(parents=True,exist_ok=True)
    cfg=load_config()
    collected=[]
    failures=[]
    groups=set()
    process_names={}
    unreadable_pids=set()
    desktop=Desktop(backend='uia')
    for window in desktop.windows():
        try:
            pid=window.element_info.process_id
            title=window.window_text()
            if pid not in process_names:
                try:
                    process_names[pid]=process_executable_name(pid)
                except OSError:
                    process_names[pid]=''
                    unreadable_pids.add(pid)
            group=inspection_software_group(process_names[pid],title,cfg)
        except Exception:
            continue  # 非目标窗口可能在枚举后已关闭。
        if group is None:
            continue
        path=output/(datetime.now().strftime('%H%M%S_')+
                     safe_name(title)+'_'+str(window.handle)+'.txt')
        try:
            export_window_controls(desktop.window(handle=window.handle),path)
            collected.append(path.name)
            groups.add(group)
        except Exception:
            error_path=path.with_name(path.stem+'_采集失败.txt')
            error_path.write_text(traceback.format_exc(),encoding='utf-8-sig')
            failures.append({'program':group,'partial_file':path.name,'error_file':error_path.name})
            # 一个窗口失败不影响其余软件采集。
    missing=[name for name in ('HWiNFO','BurnInTest','播放器') if name not in groups]
    save_json(output/'清单.json',collected)
    save_json(output/'采集状态.json',{'exported_files':collected,'failed_windows':failures,
        'missing_programs':missing,'unreadable_process_ids':sorted(unreadable_pids)})
    print('界面信息已保存：'+str(output))
    if missing:
        print('尚未采集到：'+'、'.join(missing)+'。请打开对应软件窗口后补采。')
    if failures or not collected:
        raise RuntimeError('控件采集未全部完成，请查看“界面检查/采集状态.json”和采集失败记录')
    return output


def collect_adaptation():
    """测试人员一次收集真实CSV、BIT配置和目标软件控件，不启停负载或网卡。"""
    import zipfile
    folder=ROOT/'联调资料'/datetime.now().strftime('%Y%m%d_%H%M%S')
    folder.mkdir(parents=True,exist_ok=False)
    for label,suffix,name in [('HWiNFO实际日志CSV','.csv','hwinfo_sample.csv'),
                              ('BIT实际测试配置','.bitcfg','workload.bitcfg')]:
        value=input(label+'完整路径（没有可先回车）：').strip().strip('"')
        if value:
            source=Path(value)
            if not source.is_file() or source.suffix.lower()!=suffix:
                raise ValueError(label+'路径无效')
            shutil.copy2(source,folder/name)
    print('打开HWiNFO传感器窗口、播放器和BIT主窗口；不必开始BIT测试。')
    input('界面准备好后按回车采集控件：')
    ui_error=None
    try:
        inspect_ui(folder/'界面检查')
    except Exception as exc:
        import traceback
        ui_error=f'{type(exc).__name__}: {exc}'
        (folder/'控件采集未完成.txt').write_text(
            ui_error+'\n\n'+traceback.format_exc()+
            '\n若提示 win32ui/DLL 加载失败，请关闭本窗口，运行“配置环境.cmd”后重试。\n',encoding='utf-8-sig')
    save_json(folder/'程序路径.json',{k:load_config()[k] for k in ('hwinfo','local_video','burnintest')})
    missing_files=[]
    if not (folder/'hwinfo_sample.csv').is_file():
        missing_files.append('HWiNFO实际日志CSV')
    if not (folder/'workload.bitcfg').is_file():
        missing_files.append('BIT实际测试配置.bitcfg')
    status_path=folder/'界面检查'/'采集状态.json'
    ui_status=json.loads(status_path.read_text('utf-8')) if status_path.is_file() else {}
    missing_windows=ui_status.get('missing_programs',[])
    save_json(folder/'资料清单.json',{'version':VERSION,'missing_files':missing_files,
        'missing_programs':missing_windows,'ui_error':ui_error,
        'note':'文件提供情况不等于软件适配或正式测试已通过'})
    target=folder.with_suffix('.zip')
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
        for file in folder.rglob('*'):
            if file.is_file():archive.write(file,file.relative_to(folder))
    if ui_error:
        print('控件采集尚未完成，错误和已提供文件仍已打包：'+ui_error)
    if missing_files:
        print('本包尚缺：'+'、'.join(missing_files)+'。可稍后补充。')
    print('请发送：'+str(target))
    return target


def inspect_csv_file(path):
    """只读取日志表头及前200行；编码/分隔符可识别，SSD分组仍由人员核对。"""
    raw=Path(path).read_bytes()
    # 西文HWiNFO的单字节“°C”也能被GB18030解成汉字，不能仅以不报错作为判断。
    legacy=['cp1252','gb18030'] if b'\xb0C' in raw[:65536] or b'\xb0F' in raw[:65536] else ['gb18030','cp1252']
    encodings=['utf-16'] if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else ['utf-8-sig',*legacy]
    for encoding in encodings:
        try:
            text=raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError('无法识别CSV编码，请保留原文件供核对')
    try:
        delimiter=csv.Sniffer().sniff(text[:65536],delimiters=',;\t').delimiter
    except csv.Error:
        # HWiNFO末尾可能有传感器分组说明，先用头部少量数据判断。
        try:
            delimiter=csv.Sniffer().sniff('\n'.join(text.splitlines()[:6]),delimiters=',;\t').delimiter
        except csv.Error:
            raise ValueError('无法识别CSV分隔符，请提供原始HWiNFO日志') from None
    reader=csv.reader(io.StringIO(text),delimiter=delimiter,strict=True)
    header=next(reader,None)
    if not header or len(header)<3:
        raise ValueError('CSV缺少日期、时间和温度列')
    samples=[]
    for values in reader:
        if len(values)==len(header) and values!=header:
            samples.append(values)
        if len(samples)>=200:break
    if not samples:
        raise ValueError('CSV只有表头，没有实际记录')
    return {'encoding':encoding,'delimiter':delimiter,'header':header,'samples':samples}


def csv_column_selector(header,index):
    name=header[index]
    return name if header.count(name)==1 else {'name':name,'occurrence':header[:index+1].count(name)}


def matching_datetime_formats(samples,date_index,time_index):
    candidates=[]
    for date_format in ('%d.%m.%Y','%Y-%m-%d','%Y/%m/%d','%d/%m/%Y','%m/%d/%Y'):
        for time_format in ('%H:%M:%S.%f','%H:%M:%S'):
            fmt=date_format+' '+time_format
            try:
                for row in samples[:20]:
                    datetime.strptime(row[date_index].strip()+' '+row[time_index].strip(),fmt)
            except (ValueError,IndexError):
                continue
            candidates.append(fmt)
    return candidates


def choose_csv_column(header,label,default=None,optional=False):
    hint='；0=没有该传感器' if optional else ''
    number=input(f'{label}列编号 [{default+1 if default is not None else "必填"}]{hint}：').strip()
    if not number and default is not None:return default
    index=int(number)-1
    if optional and index==-1:return None
    if not 0<=index<len(header):raise ValueError(label+'列编号不在表头范围内')
    return index


def save_setup_config(cfg,record_name,record):
    """只维护一个生效config.json，修改前留一份可恢复备份。"""
    folder=ROOT/'部署记录'/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    folder.mkdir(parents=True,exist_ok=False)
    if CONFIG.is_file():shutil.copy2(CONFIG,folder/'修改前配置.json')
    save_json(CONFIG,cfg)
    save_json(folder/(record_name+'.json'),record)
    print('已保存：'+str(CONFIG)+'\n核对记录：'+str(folder))


def apply_confirmed_temperature_mapping(cfg):
    """应用2026-10-08由测试人员确认的512G样品温度对应，其他现场设置保留。"""
    updated=copy.deepcopy(cfg)
    updated['hwinfo'].update(
        encoding='gb18030',delimiter=',',date_column='Date',time_column='Time',
        datetime_format='%d.%m.%Y %H:%M:%S.%f',decimal_mark='.',
        columns={'Composite':'Drive Temperature [°C]','CPU':'Drive Temperature 2 [°C]',
                 'NAND':'Drive Temperature 3 [°C]'},
        required_metrics=['Composite','CPU','NAND'],mapping_verified=True,
        expected_sensor_group='S.M.A.R.T.: XP2260D512G (RFC3824000238) [C:]',
        expected_header=['Date','Time','Drive Temperature [°C]','Drive Temperature 2 [°C]',
            'Drive Temperature 3 [°C]','Drive Remaining Life [%]','Drive Available Spare [%]',
            'Drive Failure [Yes/No]','Drive Warning [Yes/No]','Total Host Writes [GB]',
            'Total Host Reads [GB]',''])
    updated['thermometer']['channel_map']={'CPU_Surface':1,'NAND_Surface':2,'AMB':3}
    updated['thermometer']['active_channels']=[1,2,3]
    # UI动作、SMART实际刷新验证、JK808设备识别是独立事项，不因列对应确认而自动通过。
    save_setup_config(updated,'已确认温度对应',{
        'confirmed_on':'2026-10-08','disk':'XP2260D512G','serial':'RFC3824000238',
        'columns':updated['hwinfo']['columns'],'channel_map':updated['thermometer']['channel_map'],
        'source_csv_sha256':'f9173cf3b06fb8f7deda8baadd32165066d23127d8af51820914259471fc20d8'})
    print('已保存三路SMART和JK808通道对应。换盘后用联调工具7导入新盘完整CSV重新核对。')
    return updated


def import_hwinfo_csv(cfg,path=None):
    """在测试主机导入真实CSV；不启动HWiNFO、不改网卡或采样周期。"""
    path=Path(path or input('HWiNFO实际CSV完整路径：').strip().strip('"')).resolve()
    inspected=inspect_csv_file(path)
    header,samples=inspected['header'],inspected['samples']
    for i,name in enumerate(header):
        print(f'{i+1}. {name}  [同名第{header[:i+1].count(name)}列]  首行值={samples[0][i]}')
    date_default=next((i for i,n in enumerate(header) if n.strip().lower() in ('date','日期')),None)
    time_default=next((i for i,n in enumerate(header) if n.strip().lower() in ('time','时间')),None)
    di=choose_csv_column(header,'日期',date_default)
    ti=choose_csv_column(header,'时间',time_default)
    if di==ti:raise ValueError('日期和时间不能选择同一列')
    formats=matching_datetime_formats(samples,di,ti)
    if not formats:raise ValueError('日期格式不能自动确认，请发送原始CSV供适配')
    if len(formats)==1:
        fmt=formats[0]
    else:
        print('日期存在日/月歧义，请按HWiNFO实际格式选择：')
        for i,fmt in enumerate(formats,1):print(i,fmt)
        index=int(input('日期格式编号：'))-1
        if not 0<=index<len(formats):raise ValueError('日期格式编号无效')
        fmt=formats[index]
    print('请对照HWiNFO系统盘分组选择摄氏温度列；CPU/NAND指SSD内部传感器，不是电脑CPU核心温度。')
    chosen={name:choose_csv_column(header,label,optional=name!='Composite') for name,label in
            [('Composite','Composite复合温度'),('CPU','SSD传感器1'),('NAND','SSD传感器2')]}
    indexes=[v for v in chosen.values() if v is not None]
    if len(set(indexes))!=len(indexes) or di in indexes or ti in indexes:
        raise ValueError('温度列不能重复，不能与日期或时间列重合')
    decimal=input('小数符号，输入点号或逗号 [.]：').strip() or '.'
    if decimal not in ('.',','):raise ValueError('小数符号只能是点号或逗号')
    updated=copy.deepcopy(cfg)
    hw=updated['hwinfo']
    hw.update(encoding=inspected['encoding'],delimiter=inspected['delimiter'],
              date_column=csv_column_selector(header,di),time_column=csv_column_selector(header,ti),
              datetime_format=fmt,decimal_mark=decimal,expected_header=header,
              columns={k:csv_column_selector(header,v) if v is not None else None for k,v in chosen.items()},
              required_metrics=list(chosen_key for chosen_key,index in chosen.items() if index is not None))
    # 换盘时从完整日志末尾读取新盘归属；不能把旧盘的同名温度列静默沿用到新盘。
    previous_group=hw.pop('expected_sensor_group',None)
    groups=set()
    with path.open(encoding=hw['encoding'],newline='') as stream:
        for values in csv.reader(stream,delimiter=hw['delimiter'],strict=True):
            if len(values)==len(header) and not values[di].strip() and not values[ti].strip():
                candidate={values[index].strip() for index in indexes}
                if candidate and all(value.startswith('S.M.A.R.T.:') for value in candidate):
                    groups.update(candidate)
    if len(groups)>1:
        raise ValueError('所选温度列属于不同SMART盘分组，请重新选择同一块系统盘的温度列')
    if groups:
        hw['expected_sensor_group']=next(iter(groups))
        print('本次SMART所属盘：'+hw['expected_sensor_group'])
    elif previous_group:
        raise ValueError('换盘核对需要带SMART盘分组的完整CSV，请停止HWiNFO日志后重新导入')
    rows,errors=read_hwinfo(path,hw)
    usable=[r for r in rows if all(r.get(k) is not None for k in hw['required_metrics'])]
    if len(usable)<3:raise ValueError('至少需要3行有效温度及时间记录，当前导入尚未保存')
    intervals=[b['epoch']-a['epoch'] for a,b in zip(rows,rows[1:])]
    record={'source':str(path),'sha256':sha256(path),'encoding':hw['encoding'],
            'delimiter':hw['delimiter'],'datetime_format':fmt,'columns':hw['columns'],
            'sensor_group':hw.get('expected_sensor_group'),
            'valid_temperature_rows':len(usable),'parsed_rows':len(rows),'parse_errors':errors,
            'observed_interval_median_s':statistics.median(intervals),
            'observed_interval_min_s':min(intervals),'observed_interval_max_s':max(intervals)}
    print(json.dumps(record,ensure_ascii=False,indent=2))
    print('日志行间隔不能证明SMART实际刷新周期；此项仍需在HWiNFO设置里核对。')
    if input('以上确实是被测系统SSD的摄氏温度列，输入Y保存：').strip().upper()!='Y':
        print('未保存配置。');return None
    hw['mapping_verified']=True
    save_setup_config(updated,'HWiNFO导入记录',record)
    return updated


def import_bit_config(cfg,path=None):
    path=Path(path or input('实际BIT .bitcfg完整路径：').strip().strip('"')).resolve()
    if not path.is_file() or path.suffix.lower()!='.bitcfg' or path.stat().st_size==0:
        raise ValueError('请选择BurnInTest实际保存的非空.bitcfg，安装包和快捷方式不能代替')
    print('配置文件：'+str(path)+'\nSHA256：'+sha256(path))
    print('请在BIT里核对该配置的负载项目、占空比、系统盘测试文件路径和大小。')
    if input('已核对系统盘测试范围及负载项目，输入Y保存：').strip().upper()!='Y':
        print('未保存配置。');return None
    updated=copy.deepcopy(cfg)
    updated['burnintest'].update(configuration=str(path),configuration_sha256=sha256(path),
                                system_disk_file_test_verified=True)
    save_setup_config(updated,'BIT导入记录',{'path':str(path),'sha256':sha256(path),
                      'confirmation':'测试人员已核对系统盘测试范围及负载项目；未在此运行负载'})
    return updated


def check_readiness(cfg,role):
    """只读集中检查：未连接串口、未启停软件/网卡、未校时。"""
    from serial.tools import list_ports
    ports=list(list_ports.comports())
    configured=cfg['serial'][role+'_port']
    common=[]
    if configured not in {p.device for p in ports}:
        common.append('FTDI串口尚未就绪：'+(configured or '未配置；线到货后首次部署'))
    details={'version':VERSION,'role':role,'ports':[{'port':p.device,'description':p.description,'hwid':p.hwid} for p in ports]}
    if role=='host':
        details['tests']={m:preflight_host(cfg,[m]) for m in ORDER}
        try:
            adapters=network_snapshot()
            details['adapters']=adapters
            if cfg['network']['online_adapter_guid'].strip('{}').lower() not in {
                    a['InterfaceGuid'].strip('{}').lower() for a in adapters}:
                common.append('在线视频网卡尚未对应本机，请在首次部署中选择')
        except Exception as exc:
            common.append('未取得网卡清单：'+str(exc))
    else:
        if not cfg['thermometer']['protocol_verified'] or not cfg['thermometer']['hid_path_hex']:
            common.append('JK808设备和通道尚未在接收端部署核对')
        details['tests']={}
        details['note']='软件路径、CSV、界面及BIT配置在测试主机检查；接线后正式预检自动读取主机设置。'
    details['common']=common
    details['status']='存在待补项' if common or any(details['tests'].values()) else '静态检查通过，实物及界面联调仍需验证'
    folder=ROOT/'准备检查'/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    save_json(folder/'检查结果.json',details)
    lines=[VERSION+' '+role+'：'+details['status'],*common]
    for mode,problems in details['tests'].items():
        lines.append(LABELS[mode]+'：'+('；'.join(problems) if problems else '静态项目齐全，待实测'))
    lines.append(details.get('note','本检查没有运行负载、启停网卡或设置系统时钟。'))
    (folder/'检查结果.txt').write_text('\n'.join(lines),encoding='utf-8-sig')
    print('\n'.join(lines)+'\n检查记录：'+str(folder))
    return details


@keep_windows_awake()
def recover_result(cfg,folder, *, raw_only=False, force=False):
    folder=Path(folder).resolve()
    if not raw_only and (folder/'手工复测/续测记录.json').exists():
        from resume_flow import attempts
        for attempt in attempts(folder):
            recover_result(cfg,attempt,raw_only=True,force=True)
        return finish_resumed_reports(folder)[0]
    task=json.loads((folder/'手工复测'/'本次任务.json').read_text(encoding='utf-8'))
    if raw_only:
        from resume_flow import transfer_complete
        receipt=folder/'手工复测/接收进度.json'
        if receipt.exists() and json.loads(receipt.read_text('utf-8')).get('phase')=='not_started': return
        if not force and transfer_complete(folder): return
    link=SerialConnection(cfg['serial']['receiver_port'],cfg['serial']['baudrate'])
    try:
        hello=link.request('hello')
        if hello.get('protocol')!=2 or hello['station']!=cfg['station'] or hello['busy']:
            raise ValueError('请确认两端v0.2及同一工位，等待主机任务结束后再补取')
        if task['config']['station']!=cfg['station']:
            raise ValueError('旧任务与当前工位不一致，未回收数据')
        if raw_only:
            if hello.get('version')!=VERSION:
                raise ValueError('续测前请将两端更新为同一版本')
            if not link.request('job_exists',{'job_id':task['job_id']})['exists']:
                raise ValueError('测试主机上已找不到旧任务，无法确认未回传的项目是否完成。请保留任务目录，先恢复原始数据再续测。')
        recovered=link.request('open_job',{'job_id':task['job_id']})
        save_json(folder/'手工复测'/'补取状态.json',recovered)
        receive_files(link,folder/'手工复测'/'主机数据',task['job_id'])
        save_json(folder/'手工复测/接收进度.json',{'phase':'received','job_id':task['job_id']})
        if raw_only: return
        # 保留主机原始文件及校验值。复算按每项conditions判断：已经完成的项目保留，
        # “正在运行”或缺少结束记录的项目只供复核，不能因补取而变成成功。
        if task.get('kind')=='commissioning':
            from 集中联调 import finish_combined_report
            checks_path=folder/'接收端检查.json'
            checks=json.loads(checks_path.read_text('utf-8')) if checks_path.exists() else {}
            checks.pop('集中联调执行',None)
            checks['原始文件回传']={'passed':True,'detail':'补取完成，每文件SHA256校验通过'}
            return finish_combined_report(folder,checks)
        return finish_reports(task['config'],folder,task['modes'])
    finally:
        link.close()


def finish_resumed_reports(folder):
    from resume_flow import plan
    folder=Path(folder).resolve()
    task,paths,sources,pending=plan(folder)
    started=[p for p in paths if (p/'手工复测/主机数据/status.json').exists()]
    final_attempt=started[-1] if started else paths[-1]
    save_json(folder/'手工复测/续测数据来源.json',{
        'sources':{m:p.relative_to(folder).as_posix() for m,p in sources.items()},
        'pending':pending,'final_attempt':final_attempt.relative_to(folder).as_posix()})
    summaries=finish_reports(task['config'],folder,task['modes'],
                            stage_sources=sources,final_attempt=final_attempt,pending_modes=pending)
    return summaries,pending


@keep_windows_awake()
def resume_failed_flow(cfg,folder=''):
    from resume_flow import attempts, task_record
    if not folder:
        pointer=ROOT/'部署记录/最近正式任务.json'
        if not pointer.exists():
            raise ValueError('没有最近正式任务记录；请重新选D并粘贴原结果文件夹路径。')
        folder=json.loads(pointer.read_text('utf-8'))['folder']
    folder=Path(folder).resolve()
    task=task_record(folder)
    original=task['config']
    if original['station']!=cfg['station']:
        raise ValueError('不能续测其他工位的任务')
    for key in ('project','batch'):
        if original['report'].get(key)!=cfg['report'].get(key):
            raise ValueError('当前'+key+'与原任务不同；请核对项目和批次后再续测，换盘请选B。')
    # 上次仅回传/出表失败时先补取和复算，避免已完成负载因文件不足而重跑。
    for attempt in attempts(folder):
        recover_result(cfg,attempt,raw_only=True)
    _,pending=finish_resumed_reports(folder)
    print('续测原任务：'+str(folder),flush=True)
    print('项目：'+original['report']['project']+'；批次：'+original['report']['batch']+
          '；容量：'+original['report']['capacity']+'；序列号：'+original['report']['expected_serial'],flush=True)
    completed=[m for m in task['modes'] if m not in pending]
    print('已完成保留：'+('、'.join(LABELS[m] for m in completed) or '无'),flush=True)
    if not pending:
        print('各项负载及采样已完成，不重复执行。报告已重新生成；若仍有网卡恢复等提示，请按报告处理后选C补取。',flush=True)
        return folder
    print('将依次重试/继续：'+' → '.join(LABELS[m] for m in pending),flush=True)
    retry=copy.deepcopy(original)
    # 项目、盘片和正式测试参数沿用旧任务；当前连接端口、仪器与校时地址可更新。
    for key in ('serial','thermometer','clock_sync'):
        retry[key]=copy.deepcopy(cfg[key])
    for mode in retry['tests']:
        retry['tests'][mode]['wait_before_s']=120
    try:
        run_receiver(retry,pending,resume_folder=folder)
    finally:
        try:
            _,remaining=finish_resumed_reports(folder)
            if remaining:
                print('仍待完成：'+'、'.join(LABELS[m] for m in remaining)+'；处理原因后可再次选D。',flush=True)
            else:
                print('本次续测数据已合并，结果：'+str(folder),flush=True)
        except Exception as exc:
            print('续测汇总未更新：'+str(exc)+'；各次原始数据保留，可选C补取。',flush=True)
    return folder


def link_test(cfg):
    """只测试USB收发，不改网络、时间，也不采集仪器。"""
    connection=SerialConnection(cfg['serial']['receiver_port'],cfg['serial']['baudrate'])
    measurements=[]
    try:
        info=connection.request('hello')
        if info['station']!=cfg['station']:
            raise ValueError('串口另一端不是配置的工位')
        for index in range(20):
            content='USB双机收发验证_'+str(index)+'_'+uuid.uuid4().hex
            begin=time.monotonic()
            answer=connection.request('echo',{'text':content})
            if answer['text']!=content:
                raise ValueError('回传内容不一致')
            measurements.append(time.monotonic()-begin)
        result={'station':info['station'],'rounds':20,'status':'USB收发通过',
                'rtt_min_s':min(measurements),'rtt_max_s':max(measurements)}
        save_json(ROOT/'USB通信检查.json',result)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        connection.close()


def demo(cfg):
    """生成四项完整时长模拟数据，包含一秒JK808缺失，验证告警与选点。"""
    folder=ROOT/'demo_results'/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    manual=folder/'手工复测'; manual.mkdir(parents=True)
    demo_cfg=copy.deepcopy(cfg)
    demo_cfg['_simulated']=True
    demo_cfg['report'].update(project='模拟演练',batch='DEMO',capacity='2T')
    demo_cfg['report']['platform']='模拟演练（非实测）'
    hw=demo_cfg['hwinfo']
    hw.pop('expected_header',None)
    hw.pop('expected_sensor_group',None)
    hw.update(encoding='utf-8-sig',delimiter=',',date_column='Date',time_column='Time',
              datetime_format='%d.%m.%Y %H:%M:%S.%f',decimal_mark='.')
    hw['columns']={'Composite':'Drive Temperature','CPU':'Drive Temperature 2','NAND':'Drive Temperature 3'}
    hw['mapping_verified']=True
    epoch=datetime(2026,9,23,10,0,tzinfo=BJ).timestamp()
    all_surface=[]
    for mode in ORDER:
        item=cfg['tests'][mode]
        epoch+=item['wait_before_s']
        stage=manual/'主机数据'/mode; stage.mkdir(parents=True)
        smart=[]
        for second in range(0,item['duration_s'],item['interval_s']):
            t=datetime.fromtimestamp(epoch+second,BJ)
            c=40+10*(1-math.exp(-second/130))+.2*math.sin(second/80)
            smart.append({'Date':t.strftime('%d.%m.%Y'),'Time':t.strftime('%H:%M:%S.%f'),
                          'Drive Temperature':round(c,2),'Drive Temperature 2':round(c+2,2),
                          'Drive Temperature 3':round(c-4,2)})
        write_csv(stage/'hwinfo.csv',smart,['Date','Time','Drive Temperature','Drive Temperature 2','Drive Temperature 3'])
        for second in range(item['duration_s']):
            if mode=='idle' and second==13:
                continue
            value=42+8*(1-math.exp(-second/110))
            all_surface.append({'epoch':epoch+second+.1,'slot':len(all_surface),
                                'source_row':len(all_surface)+1,'CPU_Surface':value,'NAND_Surface':value-5,'AMB':24})
        save_json(stage/'conditions.json',{'mode':mode,'start_epoch':epoch,'end_epoch':epoch+item['duration_s'],
            'interval_s':item['interval_s'],'points':item['points'],'hwinfo':hw,'simulated':True,
            'workload_status':'模拟数据，未运行真实负载'})
        epoch+=item['duration_s']
    with (manual/'jk808.jsonl').open('w',encoding='utf-8') as stream:
        for row in all_surface:
            stream.write(json.dumps(row)+'\n')
    summaries=finish_reports(demo_cfg,folder,ORDER)
    print(json.dumps(summaries,ensure_ascii=False,indent=2))
    print('模拟演练输出：'+str(folder))
    return folder


def main():
    parser=argparse.ArgumentParser(description='JK808 Windows 温度测试 '+VERSION)
    parser.add_argument('command',choices=['host','receiver','tools','demo','deploy-host','deploy-receiver','inspect-ui','collect-adaptation','columns','recover','ports','link-test','restore-network','import-hwinfo','import-bit','check-host','check-receiver','apply-temperature-mapping','bind-hwinfo-usb'])
    parser.add_argument('path',nargs='?')
    args=parser.parse_args()
    if args.command in ('host', 'receiver', 'deploy-host', 'deploy-receiver'):
        from runtime_check import require_runtime
        require_runtime('host' if args.command.endswith('host') else 'receiver', ROOT)
    cfg=load_config()
    if args.command=='tools':
        while True:
            print('\n1. 检查USB双机通信（接收电脑）\n2. 收集联调资料（测试主机）\n3. 模拟选点出表\n4. 补取结果\n5. 按原始记录恢复网卡\n6. 导出软件控件\n7. 导入HWiNFO日志列（测试主机）\n8. 登记BIT配置（测试主机）\n9. 集中检查准备情况（无需线缆）\n0. 退出')
            choice=input('请选择：').strip()
            if choice=='0':return
            try:
                cfg=load_config()
                if choice=='1':link_test(cfg)
                elif choice=='2':collect_adaptation()
                elif choice=='3':demo(cfg)
                elif choice=='4':recover_result(cfg,input('原结果文件夹完整路径：').strip().strip('"'))
                elif choice=='5':
                    require_administrator()
                    path=Path(input('本轮network_before.json完整路径：').strip().strip('"'))
                    restore_network_record(path)
                elif choice=='6':inspect_ui()
                elif choice=='7':import_hwinfo_csv(cfg)
                elif choice=='8':import_bit_config(cfg)
                elif choice=='9':
                    role=input('本机角色：1测试主机，2接收电脑：').strip()
                    if role not in ('1','2'):raise ValueError('角色应选择1或2')
                    check_readiness(cfg,'host' if role=='1' else 'receiver')
            except Exception as exc:
                print('未完成：'+str(exc))
        return
    if args.command=='demo':
        demo(cfg); return
    if args.command=='link-test':
        link_test(cfg); return
    if args.command=='restore-network':
        require_administrator()
        restore_network_record(args.path)
        return
    if args.command=='import-hwinfo':
        import_hwinfo_csv(cfg,args.path);return
    if args.command=='apply-temperature-mapping':
        apply_confirmed_temperature_mapping(cfg);return
    if args.command=='bind-hwinfo-usb':
        bind_hwinfo_usb(cfg,args.path);return
    if args.command=='import-bit':
        import_bit_config(cfg,args.path);return
    if args.command in ('check-host','check-receiver'):
        check_readiness(cfg,args.command.split('-')[1]);return
    if args.command=='ports':
        from serial.tools import list_ports
        for p in list_ports.comports(): print(p.device,p.description,p.hwid)
        return
    if args.command=='inspect-ui':
        inspect_ui(); return
    if args.command=='collect-adaptation':
        collect_adaptation(); return
    if args.command=='columns':
        with Path(args.path).open(encoding=cfg['hwinfo']['encoding'],newline='') as stream:
            header=next(csv.reader(stream,delimiter=cfg['hwinfo']['delimiter']))
        counts={}
        for n,name in enumerate(header,1):
            counts[name]=counts.get(name,0)+1
            print(n,repr(name),'occurrence='+str(counts[name]))
        return
    if args.command=='recover':
        recover_result(cfg,args.path); return
    role='host' if args.command.endswith('host') else 'receiver'
    with single_instance(role):
        if args.command.startswith('deploy-'):
            deployment(cfg,role); return
        if role=='host':
            run_host(cfg); return
        while True:
            print('\nJK808 Windows '+VERSION+'\nA. 首次部署 / 检查\nB. 执行测试\nD. 继续上次未完成测试\n0. 退出')
            choice=input('请选择：').strip().upper()
            if choice=='0': return
            try:
                if choice=='A':
                    deployment(cfg,'receiver')
                elif choice=='B':
                    os.startfile(str(CONFIG))
                    if input('保存config.json，确认本次样品及测试设置后输入Y：').strip().upper()!='Y': continue
                    cfg=load_config()
                    print('1. 整套：在线视频 → 待机 → 本地视频 → BIT\n2. 仅在线视频\n3. 仅待机\n4. 仅本地视频\n5. 仅BIT')
                    index=input('请选择：').strip()
                    modes=ORDER if index=='1' else (ORDER[int(index)-2],) if index in ('2','3','4','5') else ()
                    if modes: run_receiver(cfg,modes)
                elif choice=='D':
                    resume_failed_flow(cfg,input('原结果文件夹路径（回车使用最近正式任务）：').strip().strip('"'))
            except Exception as exc:
                print('本次操作未完成：'+str(exc))


if __name__=='__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('已请求停止。')
    except Exception as exc:
        print('操作未完成：'+str(exc))
        sys.exit(1)
