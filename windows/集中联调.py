"""一次检查双机和四项负载。短测只验功能，不生成正式温度表。

实际测试仍在 windows_temperature_test.py；这里依次调用同一套测试代码，
汇总全部错误和原始记录。已有 config.json 原位备份后补齐，不搬用其他目录配置。
"""
import copy
from datetime import datetime
import html
import json
import math
import os
import sys
from pathlib import Path
import threading
import time
import traceback
import uuid
import zipfile

import windows_temperature_test as app
from temperature_report import check_surface_gaps, finite

SHORT_SECONDS = {'idle':185, 'local_video':125, 'online_video':125, 'bit':120}


def short_configuration(cfg):
    result = copy.deepcopy(cfg)
    result['_commissioning'] = True
    for mode in app.ORDER:
        result['tests'][mode]['duration_s'] = SHORT_SECONDS[mode]
        result['tests'][mode]['wait_before_s'] = 0
    return result


def automatic_port(cfg, role, ports):
    """仅识别唯一FTDI串口。多根FTDI线时保留已填且仍有效的端口。"""
    candidates = [p.device for p in ports if p.vid == 0x0403]
    from environment_setup import select_ftdi_port
    return select_ftdi_port(cfg['serial'].get(role+'_port'),candidates)


def enter_project_batch(cfg):
    """测试人员只输入项目和批次；容量、型号、序列号由主机自动提供。"""
    cfg = copy.deepcopy(cfg)
    for key, label in (('project','项目'),('batch','批次')):
        previous = str(cfg['report'].get(key) or '').strip()
        while True:
            value = input(label+(' ['+previous+'，回车沿用]' if previous else '')+'：').strip() or previous
            if value:
                cfg['report'][key] = value
                break
            print(label+'不能为空，请填写。')
    app.save_setup_config(cfg,'正式测试项目批次', {k:cfg['report'][k] for k in ('project','batch')})
    return cfg


def prepare_configuration(cfg, role):
    """已知控件和配置文件自动接入；设备绑定及已验证证据保留。逐项收集问题。"""
    from serial.tools import list_ports
    cfg = copy.deepcopy(cfg)
    issues = []
    def attempt(label, operation):
        try:
            operation()
        except Exception as exc:
            issues.append(label+'：'+str(exc))
    def port():
        cfg['serial'][role+'_port'] = automatic_port(cfg,role,list(list_ports.comports()))
    attempt('USB互连线',port)
    if role == 'host':
        local = cfg['local_video']
        local['adapter'] = 'windows_media_player'
        local['ui'].update(backend='uia',window={'title_re':'.*媒体播放器.*','class_name':'ApplicationFrameWindow'},
            position_control={'auto_id':'TimeElapsedElement','control_type':'Text'})
        cfg['online_video']['adapter'] = 'bilibili_1080'
        def bit():
            from 接入并验证BIT import confirmed_bit_configuration
            old_proof = cfg['burnintest'].get('script_verification')
            updated = confirmed_bit_configuration(cfg)
            cfg['burnintest'] = updated['burnintest']
            if old_proof:
                cfg['burnintest']['script_verification'] = old_proof
        attempt('BIT配置',bit)
        def hwinfo():
            hw = cfg['hwinfo']
            hw['ui'].update(adapter='hwinfo_834',backend='win32',
                window={'title_re':r'HWiNFO.*32 v8\.34-5870 Sensors Status.*','class_name':'#32770'})
            hw['ui'].pop('_logging_session',None)
            if not hw.get('usb'):
                found=[]
                for volume in app.usb_storage_volumes():
                    try:
                        executable=app.hwinfo_on_usb(volume,'HWiNFO32/HWiNFO32.exe')
                        found.append((volume,executable))
                    except FileNotFoundError:
                        pass
                if len(found)!=1:
                    raise RuntimeError('U盘HWiNFO32/HWiNFO32.exe不唯一或未找到，不能自动绑定')
                volume,executable=found[0]
                hw['source']='usb'
                hw['usb']={'volume_serial':volume['serial'],'relative_executable':'HWiNFO32/HWiNFO32.exe',
                           'exe_sha256':app.sha256(executable)}
            hw['executable'] = str(app.resolve_hwinfo_executable(hw))
        attempt('U盘HWiNFO',hwinfo)
        def disk():
            app.refresh_system_disk(cfg)
        attempt('系统盘身份',disk)
        if any(issue.startswith('系统盘身份：') for issue in issues):
            # 本次读取失败时不能继续把U盘里上一张盘的信息当成当前盘。
            cfg.pop('detected_disk',None)
            cfg['report'].update(capacity='',system_disk_model='',expected_serial='')
            cfg['hwinfo'].pop('system_disk_binding',None)
            cfg['hwinfo'].pop('logging_verification',None)
            cfg['hwinfo'].update(expected_sensor_group=None,smart_refresh_verified=False)
        def network():
            from environment_setup import select_online_adapter
            adapters = app.network_snapshot()
            wanted = cfg['network']['online_adapter_guid']
            selected = select_online_adapter(cfg['network'],adapters)
            cfg['network'].update(online_adapter_guid=selected['InterfaceGuid'],
                online_adapter_description=selected.get('InterfaceDescription',''))
            if wanted.strip('{}').lower() != selected['InterfaceGuid'].strip('{}').lower():
                print('已重新绑定在线视频网卡：'+selected.get('Name',selected['InterfaceGuid']),flush=True)
        attempt('在线视频网卡',network)
        if not Path(local['file']).is_file():
            issues.append('本地视频：未找到 '+local['file'])
    else:
        # 首次使用提供公网示例；已经填写的 NTP 地址原样保留。
        clock=cfg.setdefault('clock_sync',{})
        servers=clock.get('ntp_servers',[])
        if not servers:
            clock['ntp_servers']=['ntp.aliyun.com','ntp1.aliyun.com']
        print('本次校时服务器：'+', '.join(clock['ntp_servers']),flush=True)
        def thermometer():
            import hid
            devices = hid.enumerate(0x0483,0x5757)
            current = cfg['thermometer'].get('hid_path_hex')
            paths = [d['path'].hex() for d in devices]
            if current not in paths:
                if len(paths)!=1:
                    raise RuntimeError('JK808未连接或不唯一')
                cfg['thermometer']['hid_path_hex'] = paths[0]
            # 通道对应已经由测试人员明确确认；实际协议仍由实读校验。
            probe = app.JK808Thermometer(cfg['thermometer'],lambda *_:None)
            try:
                values,_,_ = probe.read()
                if any(values.get(c) is None or not math.isfinite(values[c])
                       for c in cfg['thermometer']['channel_map'].values()):
                    raise RuntimeError('JK808三个通道实读有空值')
                cfg['thermometer']['protocol_verified'] = True
            finally:
                probe.close()
        attempt('JK808实读',thermometer)
    app.save_setup_config(cfg,'集中接入_'+role,{'issues':issues,'version':app.VERSION})
    return cfg,issues


def inspect_stage(folder, cfg, mode):
    conditions = json.loads((folder/'conditions.json').read_text('utf-8'))
    if conditions['workload_status'] != '完成计时':
        raise RuntimeError(conditions['workload_status'])
    rows,errors = app.read_hwinfo(folder/'hwinfo.csv',cfg['hwinfo'])
    if errors:
        raise RuntimeError('HWiNFO原始记录解析异常：'+str(errors[:3]))
    rows = [r for r in rows if conditions['start_epoch']<=r['epoch']<conditions['end_epoch']]
    minimum = 3 if mode in ('idle','bit') else 2
    if len(rows)<minimum:
        raise RuntimeError(f'负载期间仅{len(rows)}个有效SMART采样，至少需要{minimum}个')
    if any(row.get(key) is None for row in rows for key in ('Composite','CPU','NAND')):
        raise RuntimeError('Composite/CPU/NAND有缺值')
    interval = cfg['tests'][mode]['interval_s']
    gaps = [b['epoch']-a['epoch'] for a,b in zip(rows,rows[1:])]
    if any(abs(gap-interval)>2 for gap in gaps):
        raise RuntimeError('SMART没有按计划连续采样：'+str(gaps))
    # 首尾同样检查，不能只验两行之间的间隔而漏掉整段头尾数据。
    if rows[0]['epoch']-conditions['start_epoch']>interval+2 or conditions['end_epoch']-rows[-1]['epoch']>interval+2:
        raise RuntimeError('SMART首段或末段缺失')
    return {'passed':True,'rows':len(rows),'gaps_s':gaps,'interval_s':interval,
            'start_epoch':conditions['start_epoch'],'end_epoch':conditions['end_epoch'],
            'csv_sha256':app.sha256(folder/'hwinfo.csv')}


def run_host_checks(controller, cfg, original):
    """四项都尝试；缺少一项不会丢掉其他诊断。收尾不安全则终止后续负载。"""
    cfg = short_configuration(cfg)
    results = {}
    for mode in app.ORDER:
        if controller.stop.is_set():
            break
        folder = controller.folder/mode
        cfg['_diagnostic_folder'] = str(folder)
        controller.status.update(phase='集中联调',message=app.LABELS[mode]+'：自动设置、启动、记录和停止')
        try:
            app.execute_stage(cfg,mode,folder,controller.stop,controller.status,original)
            results[mode] = inspect_stage(folder,cfg,mode)
        except (Exception,KeyboardInterrupt) as exc:
            test_started=(folder/'conditions.json').is_file()
            error=str(exc) if test_started else '尚未开始测试计时；'+str(exc)
            results[mode] = {'passed':False,'error':error,'test_started':test_started,
                             'last_step':controller.status.get('message','')}
            # 不等大文件回传才告诉接收端失败；换下一项也保留结果，防止轮询错过。
            controller.status['stage_results']=copy.deepcopy(results)
            controller.status.update(phase='联调单项失败',message=app.LABELS[mode]+'未通过：'+error)
            if isinstance(exc, app.WorkloadCleanupError):
                controller.stop.set()  # 即使错误记录无法写盘，也不能继续叠加下一项负载。
            folder.mkdir(parents=True,exist_ok=True)
            (folder/'异常.txt').write_text(traceback.format_exc(),'utf-8-sig')
            try:
                # 一并保留控件树，后续无需再向操作员索取一次“窗口补采”。
                app.inspect_ui(folder/'窗口现场')
            except Exception as capture_error:
                results[mode]['capture_error'] = str(capture_error)
        finally:
            try:
                restored=app.restore_network(original)
                app.save_json(folder/'network_after_restore.json',restored)
            except Exception as exc:
                results[mode].update(passed=False,restore_error=str(exc))
                controller.stop.set()
            controller.status['stage_results']=copy.deepcopy(results)
            app.save_json(controller.folder/'集中联调主机结果.json',results)
        if (folder/'cleanup_errors.json').exists():
            controller.stop.set()  # 未停止的负载不能与下一项叠加。
    for mode in app.ORDER:
        results.setdefault(mode,{'passed':False,'error':'取消或前项收尾未完成，未执行'})
    controller.status['stage_results']=copy.deepcopy(results)
    # 只有真实CSV及BIT报告通过后才落盘证明；不把接入配置等同于通过。
    save_cfg = copy.deepcopy(controller.cfg)
    if cfg['hwinfo'].get('system_disk_binding'):
        save_cfg['hwinfo'] = copy.deepcopy(cfg['hwinfo'])
        save_cfg['hwinfo']['ui'].pop('_logging_session', None)
        save_cfg['report']['expected_serial'] = cfg['report']['expected_serial']
    verified = set(save_cfg['hwinfo'].get('logging_verification',{}).get('verified_intervals_s',[]))
    for mode in ('idle','bit'):
        if results[mode]['passed']:
            verified.add(cfg['tests'][mode]['interval_s'])
    newly_verified = any(results[mode]['passed'] for mode in ('idle','bit'))
    if newly_verified:
        hw = save_cfg['hwinfo']
        hw['smart_refresh_verified'] = True
        hw['logging_verification'] = {'verified_intervals_s':sorted(verified),
            'sensor_group':hw['expected_sensor_group'],'executable_sha256':app.sha256(hw['executable']),
            'evidence_folder':str(controller.folder)}
    if results['bit']['passed']:
        bit = save_cfg['burnintest']
        bit['script_verification'] = {'passed':True,'configuration_sha256':bit['configuration_sha256'],
            'executable_sha256':app.sha256(bit['executable']),'duration_s':120,
            'evidence_folder':str(controller.folder)}
    app.save_setup_config(save_cfg,'集中联调证据',results)
    controller.cfg = save_cfg
    app.save_json(controller.folder/'集中联调主机结果.json',results)
    return all(v['passed'] for v in results.values())


def write_combined_report(folder, checks):
    """只打包本轮资料，不放入配置密码、浏览器登录数据、其他历史结果。"""
    passed = bool(checks) and all(v.get('passed') is True for v in checks.values())
    result = {'version':app.VERSION,'formal_test':False,'passed':passed,'checks':checks}
    app.save_json(folder/'集中联调结果.json',result)
    rows = ''.join('<tr><td>'+html.escape(name)+'</td><td>'+('通过' if r.get('passed') else '待处理')+
                   '</td><td>'+html.escape(str(r.get('error',r.get('detail',''))))+'</td></tr>' for name,r in checks.items())
    (folder/'集中联调结果.html').write_text('<!doctype html><meta charset="utf-8"><title>集中联调结果</title>'
        '<style>body{font:18px sans-serif;max-width:1100px;margin:40px auto}td,th{padding:12px;border:1px solid #bbb}'
        'table{border-collapse:collapse;width:100%}</style><h1>集中联调：'+('通过' if passed else '有待处理项目')+
        '</h1><p>短测仅验证自动启动、采样、停止与传输，不能代替正式温度测试。</p>'
        '<table><tr><th>项目</th><th>结果</th><th>说明</th></tr>'+rows+'</table>',encoding='utf-8')
    archive = folder.with_suffix('.zip')
    excluded = {'config_snapshot.json','本次任务.json'}
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(folder.rglob('*')):
            if p.is_file() and p.name not in excluded:
                z.write(p,folder.name+'/'+p.relative_to(folder).as_posix())
    with zipfile.ZipFile(archive) as z:
        if z.testzip():
            raise RuntimeError('集中联调包校验失败')
    print('集中联调'+('通过。' if passed else '有待处理项目。')+'结果：'+str(folder),flush=True)
    print('需要反馈时只发这一个文件：'+str(archive),flush=True)
    return result


def finish_combined_report(folder, checks):
    """某个日志损坏也应交付已有诊断，不让最后打包步骤丢掉整轮证据。"""
    try:
        return collect_combined_report(folder, checks)
    except Exception as exc:
        checks['结果文件解析']={'passed':False,'error':f'{type(exc).__name__}: {exc}'}
        app.save_json(folder/'接收端检查.json',checks)
        return write_combined_report(folder,checks)


def collect_combined_report(folder, checks):
    host = folder/'手工复测'/'主机数据'
    path = host/'集中联调主机结果.json'
    results = json.loads(path.read_text('utf-8')) if path.exists() else {}
    for mode in app.ORDER:
        checks[app.LABELS[mode]] = results.get(mode,{'passed':False,'error':'未取得主机本项结果'})
    status = host/'status.json'
    if status.exists():
        state = json.loads(status.read_text('utf-8'))
        evidence_errors = []
        if state.get('phase')!='完成': evidence_errors.append('主机未确认任务完成：'+state.get('message',''))
        if not (host/'network_before.json').is_file(): evidence_errors.append('缺少测试前网卡记录')
        if (host/'network_restore_pending.json').exists(): evidence_errors.append('仍有网卡待恢复记录')
        for filename,flag in (('task.json','finished'),('network_after_restore.json','restored')):
            try:
                proof=json.loads((host/filename).read_text('utf-8'))
                if proof.get(flag) is not True: evidence_errors.append(filename+'未确认完成')
            except (OSError,ValueError,AttributeError):
                evidence_errors.append('缺少或无法读取'+filename)
        checks['主机收尾和网卡恢复'] = {'passed':not evidence_errors,
            'detail':'；'.join(evidence_errors) if evidence_errors else state.get('message','')}
    else:
        checks['主机收尾和网卡恢复'] = {'passed':False,'error':'缺少主机结束状态'}
    jk_path = folder/'手工复测'/'jk808.jsonl'
    if jk_path.exists():
        records = [json.loads(line) for line in jk_path.read_text('utf-8').splitlines() if line.strip()]
        issues = []; checked_stages = 0
        for mode,result in results.items():
            if not result.get('passed'):
                continue
            checked_stages += 1
            part = [r for r in records if result['start_epoch']<=r['epoch']<result['end_epoch']]
            gaps=check_surface_gaps(records,result['start_epoch'],result['end_epoch'])
            if gaps:
                issues.append({'mode':mode,'missing_or_invalid':gaps})
            try:
                smart,parse_errors = app.read_hwinfo(host/mode/'hwinfo.csv',
                    json.loads((host/mode/'conditions.json').read_text('utf-8'))['hwinfo'])
                unmatched = [row['epoch'] for row in smart if result['start_epoch']<=row['epoch']<result['end_epoch']
                    and not any(abs(r['epoch']-row['epoch'])<=.75 and not r.get('error')
                        and all(finite(r.get(k)) for k in ('CPU_Surface','NAND_Surface','AMB')) for r in part)]
                if unmatched or parse_errors:
                    issues.append({'mode':mode,'unmatched_hwinfo_times':unmatched,'csv_errors':parse_errors})
            except Exception as exc:
                issues.append({'mode':mode,'alignment_error':str(exc)})
        checks['JK808全程每秒记录'] = {'passed':bool(records) and not issues and checked_stages>0,
                                     'detail':issues if checked_stages else '没有完成的阶段可供核对'}
    else:
        checks['JK808全程每秒记录'] = {'passed':False,'error':'没有JK808记录'}
    app.save_json(folder/'接收端检查.json',checks)
    return write_combined_report(folder,checks)


@app.keep_windows_awake()
def run_receiver_checks(cfg):
    app.require_administrator()
    folder = app.ROOT/'联调资料'/('集中联调_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6])
    manual = folder/'手工复测'; manual.mkdir(parents=True)
    checks = {}; link = None; collector = None; task = None; submitted = False
    collector_errors = []
    stop = threading.Event()
    try:
        cfg,issues = prepare_configuration(cfg,'receiver')
        checks['接收端设备接入'] = {'passed':not issues,'detail':issues}
        link = app.SerialConnection(cfg['serial']['receiver_port'],cfg['serial']['baudrate'])
        hello = link.request('hello')
        versions={'receiver':{'version':app.VERSION,'program_dir':str(app.ROOT),
                              'main_sha256':app.sha256(Path(app.__file__))},
                  'host':{k:hello.get(k) for k in ('version','program_dir','main_sha256')}}
        app.save_json(manual/'程序版本核对.json',versions)
        checks['两端程序版本']={'passed':hello.get('version')==app.VERSION,'detail':versions}
        if hello.get('version')!=app.VERSION:
            raise RuntimeError('两端版本不同：接收端 '+app.VERSION+'；主机 '+str(hello.get('version'))+
                               '。请关闭两台电脑的旧窗口，完整覆盖更新后重开')
        if hello.get('version')!=app.VERSION or hello.get('station')!=cfg['station'] or hello.get('busy'):
            raise RuntimeError('请先把两端都更新为'+app.VERSION+'，测试主机运行统一入口1，且没有其他任务')
        for n in range(5):
            token = uuid.uuid4().hex
            if link.request('echo',{'text':token})['text'] != token:
                raise RuntimeError('USB回传内容不一致')
        checks['USB双向通信'] = {'passed':True,'detail':'5轮带校验往返通过'}
        info = link.request('commissioning-info',timeout=30)
        checks['测试主机配置接入'] = {'passed':not info['issues'],'detail':info['issues']}
        cfg.update(copy.deepcopy(info['host_tools']))
        cfg['report'].update(info['sample'])
        app.save_setup_config(cfg,'接收端接入主机盘信息',info['sample'])
        windows = link.request('workload-windows', timeout=30)
        checks['测试主机旧负载窗口'] = {'passed':not windows['problems'], 'detail':windows['problems']}
        if windows['problems']:
            raise RuntimeError('尚未开始整轮测试，请在测试主机处理：\n'+'\n'.join(windows['problems']))
        try:
            app.synchronize_two_clocks(cfg,link,manual)
            checks['双机校时'] = {'passed':True}
        except Exception as exc:
            checks['双机校时'] = {'passed':False,'error':str(exc)}
        # 校时/仪器失败仍执行主机软件检查，但整套不能显示通过。
        if cfg['thermometer'].get('protocol_verified'):
            collector = threading.Thread(target=app.collect_jk808,args=(cfg,manual/'jk808.jsonl',stop,collector_errors),daemon=False)
            collector.start()
            if collector_errors:
                raise OSError('JK808采集文件无法保存：'+collector_errors[0])
        task = uuid.uuid4().hex
        app.save_json(manual/'本次任务.json',{'job_id':task,'kind':'commissioning','config':cfg,'modes':list(app.ORDER)})
        print('集中联调开始，运行约10～15分钟；扫码等待和文件回传另计。需要登录时会一直等待，可按Ctrl+C取消。',flush=True)
        submitted = True  # 回包丢失时也要保留任务编号，不能启动第二次。
        link.request('start',{'job_id':task,'kind':'commissioning','config':cfg,'modes':list(app.ORDER)},timeout=180)
        deadline = time.monotonic()+1800; last = None; previous_poll = None; displayed_results = {}
        while time.monotonic()<deadline:
            if collector_errors:
                raise OSError('JK808采集文件无法保存：'+collector_errors[0])
            try:
                state = link.request('status')
                if state['job_id'] != task:
                    raise RuntimeError('主机任务编号改变，拒绝混入另一轮数据')
                deadline,previous_poll = app.extend_deadline_for_user_wait(deadline,previous_poll,state)
                app.print_stage_results(state['status'],displayed_results)
                message = state['status']['message']
                if message!=last:
                    print(message,flush=True); last=message
                if not state['busy']:
                    break
            except (TimeoutError,OSError) as exc:
                previous_poll = None
                app.append_record(manual/'通信异常.jsonl',{'epoch':time.time(),'error':str(exc)})
            time.sleep(1)
        else:
            raise TimeoutError('联调运行超过30分钟（不含扫码等待），保留任务编号，请用同一入口C补取结果')
        stop.set()
        if collector: collector.join(5)
        if collector_errors:
            raise OSError('JK808采集文件无法保存：'+collector_errors[0])
        app.receive_files(link,manual/'主机数据',task)
        checks['原始文件回传'] = {'passed':True,'detail':'每文件SHA256校验通过'}
        submitted = False
    except (Exception,KeyboardInterrupt) as exc:
        checks['集中联调执行'] = {'passed':False,'error':f'{type(exc).__name__}: {exc}'}
        if submitted and link and task:
            try:
                link.request('cancel',{'job_id':task})
                print('已请求停止并恢复网卡；等待主机结束后可在统一入口C补取本轮结果。',flush=True)
            except Exception:
                print('停止指令未确认，请在测试主机按Ctrl+C停止；原始数据仍在主机。',flush=True)
        try:
            (folder/'异常.txt').write_text(traceback.format_exc(),'utf-8-sig')
        except OSError as log_error:
            print('联调异常记录未保存：'+str(log_error)+'；原始错误：'+str(exc),flush=True)
    finally:
        stop.set()
        if collector: collector.join(5)
        if link: link.close()
    return finish_combined_report(folder,checks)


def main():
    from 启动校验 import verify_release
    verify_release(Path(__file__).resolve().parent,loaded_version=app.VERSION,loaded_program=app.__file__)
    print('JK808 Windows '+app.VERSION)
    if len(sys.argv)>1 and sys.argv[1] in ('1','2'):
        choice=sys.argv[1]
    else:
        print('1. 测试主机（插U盘和FTDI线）\n2. 接收电脑（连接JK808和FTDI线）\n0. 退出')
        choice = input('本机角色：').strip()
    if choice=='0': return
    if choice not in ('1','2'): raise ValueError('请选择1或2')
    role = 'host' if choice=='1' else 'receiver'
    setup_only = '--setup-only' in sys.argv
    from environment_setup import restart_as_administrator, ensure_ftdi_driver, select_ftdi_port
    if restart_as_administrator(role, app.ROOT, setup_only): return
    from runtime_check import require_runtime
    with app.single_instance(role):
        require_runtime(role, app.ROOT)
        app.require_administrator()
        print('正在配置本机环境；程序与配置位置：'+str(app.ROOT), flush=True)
        driver = ensure_ftdi_driver(app.ROOT, role)
        cfg = app.load_config()
        # 两个角色都更新端口。接收端直接选择B时也不能继续使用失效的旧COM。
        port_key = role+'_port'
        selected_port = select_ftdi_port(cfg['serial'].get(port_key),driver['ports'])
        if selected_port != cfg['serial'].get(port_key):
            cfg['serial'][port_key] = selected_port
            app.save_setup_config(cfg,'自动更新串口_'+role,{'port':selected_port})
        print('本机FTDI端口：'+selected_port,flush=True)
        if role=='host' or setup_only:
            cfg,issues = prepare_configuration(cfg,role)
            print('配置检查已完成，已有人工设置保留。',flush=True)
            for issue in issues: print('待处理：'+issue)
            if role=='host' and any(issue.startswith(('系统盘身份：','USB互连线：')) for issue in issues):
                raise RuntimeError('当前系统盘或USB通信配置未通过，尚未启动主机执行端。请处理上述问题后重新启动。')
            if role=='host' and cfg.get('detected_disk'):
                print('当前系统盘：'+str(cfg['detected_disk']['model'])+'；容量：'+cfg['report']['capacity']+
                      '；序列号：'+(cfg['report'].get('expected_serial') or '将在HWiNFO Sensors中核对'),flush=True)
            if setup_only:
                if issues: raise RuntimeError('环境仍有上述待处理项，尚未启动测试；检查记录已保存在U盘部署记录中。')
                print('环境配置完成。接下来运行本机的开始_测试主机.cmd或开始_接收电脑.cmd。',flush=True)
                return
            print('请停止HWiNFO旧日志，关闭播放器和BIT旧窗口。')
            print('正在启动测试主机；如有历史网卡记录，将在本窗口处理，完成后等待接收电脑任务。',flush=True)
            app.run_host(cfg)
            return
        while True:
            print('\nA. 可选联调检查（四项短测）\nB. 自动识别盘片并正式测试（无需先跑A）\nC. 补取上次结果\nD. 继续上次未完成测试（保留已完成项）\n0. 退出')
            action = input('选择：').strip().upper()
            if action=='0':return
            try:
                cfg=app.load_config()
                if action=='A': run_receiver_checks(cfg)
                elif action=='B':
                    cfg,issues=prepare_configuration(cfg,'receiver')
                    if issues: raise RuntimeError('；'.join(issues))
                    cfg=enter_project_batch(cfg)
                    print('1. 整套：在线视频 → 待机 → 本地视频 → BIT\n2. 在线视频\n3. 待机\n4. 本地视频\n5. BIT')
                    selected=input('选择：').strip()
                    modes=app.ORDER if selected=='1' else (app.ORDER[int(selected)-2],) if selected in ('2','3','4','5') else ()
                    if modes: app.run_receiver(cfg,modes)
                elif action=='C': app.recover_result(cfg,input('上次结果文件夹路径：').strip().strip('"'))
                elif action=='D': app.resume_failed_flow(cfg,input('原结果文件夹路径（回车使用最近正式任务）：').strip().strip('"'))
            except Exception as exc:
                print('未完成：'+str(exc))


if __name__=='__main__':
    exit_code = 0
    try:
        main()
    except (Exception,KeyboardInterrupt) as exc:
        print('操作结束：'+str(exc))
        exit_code = 1
    finally:
        # UAC新开的Python控制台没有外层CMD的pause，必须留下结果供操作员查看。
        if '--elevated' in sys.argv:
            try: input('按回车关闭此窗口：')
            except (EOFError, KeyboardInterrupt): pass
    sys.exit(exit_code)
