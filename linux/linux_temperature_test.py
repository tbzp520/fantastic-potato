# -*- coding: utf-8 -*-
"""JK808 + Linux 温度测试。全部测试流程、设备读取、缺失检查、报表都在本文件。

阅读顺序：配置与入口 → 四项测试函数 → 采集/SSH/USB → 基础数据与公司模板。
唯一配置文件：config.json。标准库及 pip 依赖负责底层能力，不包含测试流程。
"""
import base64
import copy
import csv
import ctypes
from ctypes import wintypes
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone, timedelta
import getpass
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

PROGRAM_FOLDER = Path(__file__).resolve().parent
CONFIGURATION_FILE = PROGRAM_FOLDER / 'config.json'
TEST_NAMES = {'read_1m': 'Linux_1M_Read', 'write_1m': 'Linux_1M_Write',
              'l0_ps4': 'Linux_L0_PS4', 'l12_ps4': 'Linux_L1_2_PS4'}
METRICS = ['CPU_Surface', 'NAND_Surface', 'AMB', 'Composite', 'CPU', 'NAND', 'BW(MiB/s)']
DATA_COLUMNS = ['Time'] + METRICS
BEIJING_TIMEZONE = timezone(timedelta(hours=8))
MANUAL_DATA_FOLDER_NAME = '手工复测'

def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', 'utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)

def utc_timestamp(epoch=None):
    return datetime.fromtimestamp(time.time() if epoch is None else epoch, timezone.utc).isoformat(timespec='milliseconds')

def load_configuration():
    configuration = json.loads(CONFIGURATION_FILE.read_text('utf-8-sig'))
    upgrade_read_write_duration(configuration)
    configuration['_config_dir'] = str(PROGRAM_FOLDER)
    return configuration

def upgrade_read_write_duration(configuration):
    """兼容现场旧配置：840 秒读写改为 900 秒，并让 fio 运行同样长。"""
    changed = []
    for mode in ('read_1m','write_1m'):
        settings = configuration.get('tests',{}).get(mode,{})
        if settings.get('duration_s')==840 or (settings.get('duration_s')==900 and settings.get('fio_duration_s') in (780,840)):
            settings.update(duration_s=900,fio_duration_s=900)
            changed.append(TEST_NAMES[mode])
    if changed:
        save_configuration(configuration)
        print('已备份原 config.json；'+ '、'.join(changed)+' 的正式记录和 fio 均已改为 900 秒。',flush=True)

def save_configuration(configuration):
    if CONFIGURATION_FILE.exists():
        folder = PROGRAM_FOLDER/'配置备份'
        folder.mkdir(exist_ok=True)
        shutil.copy2(CONFIGURATION_FILE, folder/(datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.json'))
    save_json(CONFIGURATION_FILE, {key:value for key,value in configuration.items() if not key.startswith('_')})

def quote_shell_argument(value):
    return shlex.quote(str(value))


# ---------- 四项测试：每项的执行顺序直接写在这里 ----------

def run_1m_read_test(configuration, linux_connection, smart_connection, output_folder, stop_event):
    """TRIM → 预写 1GiB → 等 300 秒 → 正式开始采样和 1M 读测试。"""
    trim_selected_nvme_disk(configuration, linux_connection, output_folder)
    run_prewrite_one_gib(configuration, linux_connection, output_folder, stop_event)
    wait_before_starting_test(300, stop_event)
    return collect_during_formal_test(configuration, 'read_1m', linux_connection, smart_connection, output_folder, stop_event)


def run_1m_write_test(configuration, linux_connection, smart_connection, output_folder, stop_event):
    """TRIM → 等 300 秒 → 正式开始采样和 1M 写测试。"""
    trim_selected_nvme_disk(configuration, linux_connection, output_folder)
    wait_before_starting_test(300, stop_event)
    return collect_during_formal_test(configuration, 'write_1m', linux_connection, smart_connection, output_folder, stop_event)


def run_l0_ps4_test(configuration, linux_connection, smart_connection, output_folder, stop_event):
    """不 TRIM、不预写、不启动 fio；等 30 秒 → 正式开始 L0 / PS4 采样。"""
    wait_before_starting_test(30, stop_event)
    return collect_during_formal_test(configuration, 'l0_ps4', linux_connection, smart_connection, output_folder, stop_event)


def run_l12_ps4_test(configuration, linux_connection, smart_connection, output_folder, stop_event):
    """不 TRIM、不预写、不启动 fio；等 30 秒 → 正式开始 L1.2 / PS4 采样。"""
    wait_before_starting_test(30, stop_event)
    return collect_during_formal_test(configuration, 'l12_ps4', linux_connection, smart_connection, output_folder, stop_event)


def build_fio_command(configuration, direction, remote_folder, seconds=None):
    device = configuration['ssh']['device']
    arguments = ['fio','--name=temperature_test',f'--filename={device}',f'--rw={direction}',
                 '--ioengine=libaio','--direct=1','--bs=1M','--iodepth=128','--size=1G',
                 f'--output={remote_folder}/fio_output.txt']
    if seconds is not None:
        arguments += ['--time_based',f'--runtime={seconds}',f'--write_bw_log={remote_folder}/bandwidth',
                      '--log_avg_msec=1000','--log_unix_epoch=1']
    return ' '.join(quote_shell_argument(argument) for argument in arguments)


def start_remote_fio(linux_connection, configuration, output_folder, direction, seconds=None, phase='test'):
    run_name = output_folder.parent.name if output_folder.name==MANUAL_DATA_FOLDER_NAME else output_folder.name
    remote_folder = '/tmp/jk808_simple_'+run_name+'_'+phase
    if not re.fullmatch(r'/tmp/jk808_simple_[A-Za-z0-9_-]+',remote_folder):
        raise ValueError('远端临时目录名称不合法')
    linux_connection.execute('mkdir -m 700 '+quote_shell_argument(remote_folder))
    command = build_fio_command(configuration,direction,remote_folder,seconds)
    # 每次任务独立进程组。重试网络查询不会再次启动 fio。
    script = 'printf "%s\\n" "$$" > '+quote_shell_argument(remote_folder+'/process_group')+'; date +%s.%N > '+quote_shell_argument(remote_folder+'/started_epoch')+'; '+command+'; result=$?; printf "%s\\n" "$result" > '+quote_shell_argument(remote_folder+'/exit_code')+'; exit "$result"'
    launch = 'nohup setsid sh -c '+quote_shell_argument(script)+' >'+quote_shell_argument(remote_folder+'/launcher.txt')+' 2>&1 < /dev/null & echo $!'
    state = {'remote_folder':remote_folder,'command':command,'phase':phase,'process_group':None,'finished':False}
    save_json(output_folder/(phase+'_fio启动记录.json'),state)
    try:
        process_id = linux_connection.execute(launch)[1].strip()
    except Exception as launch_error:
        linux_connection.close()
        try:
            saved_pid = linux_connection.execute('cat '+quote_shell_argument(remote_folder+'/process_group'))[1].strip()
            if not saved_pid.isdigit():
                raise ValueError('远端进程编号无效')
            state['process_group'] = int(saved_pid)
            save_json(output_folder/(phase+'_fio启动记录.json'),state)
            stop_this_fio_process(linux_connection,state)
        except Exception as cleanup_error:
            raise RuntimeError(f'启动响应中断，无法确认远端是否仍在运行。检查 {remote_folder}；不能重复启动。{cleanup_error}') from launch_error
        raise RuntimeError('启动响应中断，已收尾本次远端进程，请检查启动记录') from launch_error
    if not process_id.isdigit():
        raise RuntimeError('没有取得 fio 进程组编号，检查远端启动记录，不能重复启动')
    state['process_group'] = int(process_id)
    save_json(output_folder/(phase+'_fio启动记录.json'),state)
    return state


def get_fio_exit_code(linux_connection, state):
    code, text, _ = linux_connection.execute('cat '+quote_shell_argument(state['remote_folder']+'/exit_code'),check=False)
    if code:
        return None
    result = int(text.strip())
    state['finished'] = True
    return result


def wait_for_fio_completion(linux_connection, state, timeout_s=30):
    """采样到点后不再采集，给稍晚启动的 fio 最多 30 秒正常退出。"""
    deadline = time.monotonic()+timeout_s
    while True:
        result = get_fio_exit_code(linux_connection,state)
        if result is not None:
            return result
        remaining = deadline-time.monotonic()
        if remaining<=0:
            raise TimeoutError('采样结束后等待 fio 正常退出超过 30 秒')
        time.sleep(min(.5,remaining))


def stop_this_fio_process(linux_connection, state):
    if not state or not state.get('process_group') or state.get('finished'):
        return
    if get_fio_exit_code(linux_connection,state) is not None:
        return
    process_id = state['process_group']
    code, command, _ = linux_connection.execute(f'ps -o args= -p {process_id}',check=False)
    if code or state['remote_folder'] not in command:
        raise RuntimeError('不能核对本次 fio 进程身份，请按启动记录检查远端；没有使用 killall')
    linux_connection.execute(f'/bin/kill -TERM -- -{process_id}',check=False)
    for _ in range(10):
        code, _, _ = linux_connection.execute(f'/bin/kill -0 -- -{process_id}',check=False)
        if code:
            return
        time.sleep(.5)
    raise RuntimeError('本次 fio 进程组尚未退出，请现场核查启动记录')


def retrieve_fio_files(linux_connection, state, output_folder):
    if linux_connection.client is None:
        linux_connection.connect()
    destination = output_folder/('fio原始日志_'+state['phase'])
    destination.mkdir(exist_ok=True)
    with linux_connection.client.open_sftp() as transfer:
        for filename in transfer.listdir(state['remote_folder']):
            if re.fullmatch(r'[A-Za-z0-9_.-]+',filename):
                transfer.get(state['remote_folder']+'/'+filename,str(destination/filename))
    return destination


def run_prewrite_one_gib(configuration, linux_connection, output_folder, stop_event):
    state = None
    try:
        print('读测试准备：预写 1GiB。',flush=True)
        state = start_remote_fio(linux_connection,configuration,output_folder,'write',phase='prewrite')
        deadline = time.monotonic()+180
        while True:
            if stop_event.is_set():
                raise InterruptedError('测试人员取消预写')
            result = get_fio_exit_code(linux_connection,state)
            if result is not None:
                if result:
                    raise RuntimeError(f'预写失败，fio 退出码 {result}')
                break
            if time.monotonic()>deadline:
                raise TimeoutError('预写 1GiB 超过 180 秒，尚未开始正式测试')
            stop_event.wait(.5)
    finally:
        if state:
            try:
                stop_this_fio_process(linux_connection,state)
            finally:
                retrieve_fio_files(linux_connection,state,output_folder)


def collect_during_formal_test(configuration, mode, linux_connection, smart_connection, output_folder, stop_event):
    """统一的是采集和收尾。四项的 TRIM / 预写 / 等待步骤在上方逐项展开。"""
    settings = configuration['tests'][mode]
    duration = settings['duration_s']
    surface_rows, smart_rows, bandwidth_rows, errors = [], [], [], []
    start_monotonic = time.monotonic()
    start_epoch = time.time()
    state = {'mode':mode,'started_at':utc_timestamp(start_epoch),'start_epoch':start_epoch,
             'duration_s':duration,'smart_interval_s':settings['smart_interval_s'],
             'identity':configuration.get('_identity',{}),
             'completed':False,'errors':errors,'configuration':configuration_without_password(configuration)}
    save_json(output_folder/'本次运行记录.json',state)
    threads = [threading.Thread(target=collect_jk808_every_second, name='JK808 每秒采样',args=(configuration,start_epoch,start_monotonic,duration,stop_event,output_folder,surface_rows,errors)),
               threading.Thread(target=collect_smart_at_scheduled_times, name='SMART 计划采样',args=(configuration,smart_connection,start_epoch,start_monotonic,duration,settings['smart_interval_s'],stop_event,output_folder,smart_rows,errors))]
    fio_state = None
    try:
        for thread in threads:
            thread.start()
        print('正式测试开始，现开始采样。',flush=True)
        if mode in ('read_1m','write_1m'):
            direction = 'read' if mode=='read_1m' else 'write'
            fio_state = start_remote_fio(linux_connection,configuration,output_folder,direction,settings['fio_duration_s'])
            state['fio'] = fio_state
            save_json(output_folder/'本次运行记录.json',state)
        next_display, next_fio_check = 0, 0
        fio_communication_error_reported = False
        clock_jump_reported = False
        while time.monotonic()<start_monotonic+duration:
            if stop_event.is_set():
                raise InterruptedError('操作员中断，或本地文件无法继续写入')
            elapsed = time.monotonic()-start_monotonic
            clock_change = (time.time()-start_epoch)-elapsed
            if abs(clock_change)>.5 and not clock_jump_reported:
                errors.append(f'测试中接收电脑时间跳变约 {clock_change:+.3f} 秒；采样按单调时钟继续，计划时间和实际接收时间均保留，请核对时间对应。')
                clock_jump_reported = True
            if elapsed>=next_display:
                print(f'测试中：{int(elapsed)}/{duration} 秒。采样异常会在最终结果中列明。',flush=True)
                next_display = elapsed+30
            if fio_state and not fio_state['finished'] and elapsed>=next_fio_check:
                try:
                    exit_code = get_fio_exit_code(linux_connection,fio_state)
                except Exception as error:
                    exit_code = None
                    linux_connection.close()
                    if not fio_communication_error_reported:
                        errors.append('测试中暂时无法查询 fio 状态，采样继续：'+str(error))
                        fio_communication_error_reported = True
                if exit_code is not None and exit_code!=0:
                    raise RuntimeError(f'fio 工作负载失败，退出码 {exit_code}')
                next_fio_check = elapsed+10
            # 两项低功耗测试期间不执行额外 SSH 状态轮询。
            stop_event.wait(min(.2,max(0,start_monotonic+duration-time.monotonic())))
        state['completed'] = True
    except BaseException as error:
        if isinstance(error,(KeyboardInterrupt,InterruptedError)):
            errors.append('测试被中断，已保留采到的数据')
        else:
            errors.append(f'{type(error).__name__}: {error}')
    finally:
        state['ended_at'] = utc_timestamp()
        stop_event.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=20)
                if thread.is_alive():
                    smart_connection.close()
                    thread.join(timeout=5)
                    if thread.is_alive():
                        raise RuntimeError(thread.name+'未退出，原始文件保留；尚未生成最终表格，避免边写边统计')
        if fio_state:
            try:
                # fio 在采样起点之后经 SSH 启动；运行时间相同时不能到点立即杀掉它。
                if state['completed']:
                    print('正式采样结束，正在确认 fio 正常退出；此阶段不追加绘图数据。',flush=True)
                    exit_code = wait_for_fio_completion(linux_connection,fio_state)
                else:
                    exit_code = get_fio_exit_code(linux_connection,fio_state)
                state['fio_exit_code'] = exit_code
                if exit_code is None or exit_code!=0:
                    state['completed'] = False
                    errors.append('fio 未正常完成，保留本次不完整结果')
            except BaseException as error:
                state['completed'] = False
                errors.append('fio 收尾失败：'+str(error))
            finally:
                try:
                    stop_this_fio_process(linux_connection,fio_state)
                except Exception as error:
                    state['completed'] = False
                    errors.append('fio 停止失败：'+str(error))
            try:
                log_folder = retrieve_fio_files(linux_connection,fio_state,output_folder)
                actual_fio_start = float((log_folder/'started_epoch').read_text().strip())
                state['fio_start_epoch'] = actual_fio_start
                state['fio_offset_s'] = actual_fio_start-start_epoch
                if abs(state['fio_offset_s'])>2:
                    errors.append(f"fio 实际启动与正式采样起点相差 {state['fio_offset_s']:.3f} 秒；图按真实时间放置带宽，请核对。")
                candidates = list(log_folder.glob('bandwidth_bw.*.log'))
                if len(candidates)!=1:
                    raise ValueError('未取得唯一的单任务 fio 带宽日志')
                bandwidth_rows, parse_errors = parse_fio_bandwidth_log(candidates[0].read_text(),actual_fio_start,settings['fio_duration_s'],0 if mode=='read_1m' else 1)
                # 带宽按实际 fio 起点放置，不把启动延迟悄悄移到测试第 0 秒。
                for row in bandwidth_rows:
                    row['FioSecond'] = row['Second']
                    row['Second'] += state['fio_offset_s']
                errors.extend(parse_errors)
                save_csv_table(output_folder/'带宽原始换算数据.csv',['Time','ActualTime','Second','FioSecond','BW(MiB/s)','Raw_KiB/s','RawLine','Error'],bandwidth_rows)
            except Exception as error:
                errors.append('取回/读取 fio 原始日志失败：'+str(error)+'；远端目录：'+fio_state['remote_folder'])
        save_json(output_folder/'本次运行记录.json',state)
    generate_all_test_results(configuration,state,output_folder,surface_rows,smart_rows,bandwidth_rows)
    return state


# ---------- 正式测试阶段的数据采集 ----------

def append_json_record(stream, record):
    stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False)+'\n')
    stream.flush()


def collect_jk808_every_second(configuration, start_epoch, start_monotonic, duration_seconds, stop_event, output_folder, rows, fatal_errors):
    """第 0 秒开始。每秒有独立记录；断线、探头无效、晚到都记缺失，下一秒继续。"""
    settings = configuration['thermometer']
    current_second = 0
    thermometer = None
    try:
        with (output_folder/'JK808原始报文.jsonl').open('w', encoding='utf-8') as raw_stream, \
             (output_folder/'JK808逐秒原始数据.csv').open('w', encoding='utf-8-sig', newline='') as csv_stream:
            columns = ['Time','ActualTime','Second','Error']+METRICS[:3]
            columns += [f'CH{channel:03d}'+suffix for channel in range(1,9) for suffix in ('','_Raw','_Error')]
            writer = csv.DictWriter(csv_stream, fieldnames=columns, extrasaction='ignore')
            writer.writeheader()

            def save_usb_message(direction, data):
                append_json_record(raw_stream, {'Second':current_second, 'ActualTime':utc_timestamp(),
                                               'direction':direction, 'hex':data.hex(' ')})

            thermometer = JK808Thermometer(settings, save_usb_message)
            for current_second in range(duration_seconds):
                if stop_event.wait(max(0, start_monotonic+current_second-time.monotonic())):
                    break
                row = {'Time':utc_timestamp(start_epoch+current_second), 'ActualTime':'',
                       'Second':current_second, 'Error':''}
                values, statuses, raw_values = {}, {}, {}
                try:
                    if time.monotonic() >= start_monotonic+current_second+1:
                        raise TimeoutError('本秒未能按时执行采样')
                    values, statuses, raw_values = thermometer.read()
                    row['ActualTime'] = utc_timestamp()
                    if time.monotonic() > start_monotonic+current_second+1:
                        # 原始 USB 报文还在，晚到数据不冒充本秒准时取得的数据。
                        raise TimeoutError('响应晚于本秒结束，原报文已保留')
                except Exception as error:
                    row['Error'] = f'{type(error).__name__}: {error}'
                    values = {}
                    try:
                        thermometer.close()
                    except Exception:
                        pass
                for channel in range(1,9):
                    name = f'CH{channel:03d}'
                    row[name] = values.get(channel)
                    row[name+'_Raw'] = raw_values.get(channel)
                    row[name+'_Error'] = row['Error'] or ('' if statuses.get(channel)=='ok' else statuses.get(channel,'no_data'))
                for metric, channel in settings['channel_map'].items():
                    row[metric] = row.get(f'CH{channel:03d}')
                    row[metric+'_Error'] = row.get(f'CH{channel:03d}_Error')
                rows.append(row)
                writer.writerow(row)
                csv_stream.flush()
    except Exception as error:
        fatal_errors.append('JK808 原始记录无法继续保存：'+str(error))
        stop_event.set()
    finally:
        if thermometer is not None:
            try:
                thermometer.close()
            except Exception:
                pass


def convert_nvme_smart_temperatures(raw_data, settings):
    values, errors = {}, {}
    for metric, field in [('Composite','temperature'),('CPU','temperature_sensor_1'),('NAND','temperature_sensor_2')]:
        raw = raw_data.get(field)
        if isinstance(raw,bool) or not isinstance(raw,(int,float)) or not math.isfinite(raw):
            values[metric] = None
            errors[metric+'_Error'] = 'SMART 字段缺失或不是有限数值：'+field
            continue
        value = raw-273.15 if settings['smart_temperature_unit']=='K' else raw
        if not -100 <= value <= 200:
            values[metric] = None
            errors[metric+'_Error'] = 'SMART 温度超出有效范围，请核对 K/C 单位'
        else:
            values[metric] = math.ceil(value) if settings['smart_round_up'] else value
    return values, errors


def collect_smart_at_scheduled_times(configuration, linux_connection, start_epoch, start_monotonic, duration_seconds, interval_seconds, stop_event, output_folder, rows, fatal_errors):
    """仅在 0、60、120… 或 0、1500… 取样；计划点失败留下记录，不额外密集轮询。"""
    try:
        with (output_folder/'SMART完整原始响应.jsonl').open('w', encoding='utf-8') as raw_stream, \
             (output_folder/'SMART计划采样数据.csv').open('w', encoding='utf-8-sig', newline='') as csv_stream:
            columns = ['Time','ActualTime','Second','Error','Composite','CPU','NAND','Composite_Error','CPU_Error','NAND_Error']
            writer = csv.DictWriter(csv_stream, fieldnames=columns, extrasaction='ignore')
            writer.writeheader()
            for second in range(0,duration_seconds,interval_seconds):
                if stop_event.wait(max(0, start_monotonic+second-time.monotonic())):
                    break
                row = {'Time':utc_timestamp(start_epoch+second), 'ActualTime':'', 'Second':second, 'Error':''}
                raw_text = ''
                try:
                    if time.monotonic() >= start_monotonic+second+1:
                        raise TimeoutError('未在计划采样秒内开始 SMART 请求')
                    device = quote_shell_argument(configuration['ssh']['device'])
                    _, raw_text, _ = linux_connection.execute(f'nvme smart-log {device} -o json', timeout=1)
                    row['ActualTime'] = utc_timestamp()
                    if time.monotonic() >= start_monotonic+second+1:
                        raise TimeoutError('SMART 响应晚于计划采样秒，原始响应保留但不计作按时采到')
                    values, errors = convert_nvme_smart_temperatures(json.loads(raw_text), configuration['ssh'])
                    row.update(values)
                    row.update(errors)
                except Exception as error:
                    row['Error'] = f'{type(error).__name__}: {error}'
                    linux_connection.close()
                append_json_record(raw_stream, {'Second':second, 'ActualTime':row['ActualTime'],
                                               'Error':row['Error'], 'raw_response':raw_text})
                rows.append(row)
                writer.writerow(row)
                csv_stream.flush()
    except Exception as error:
        fatal_errors.append('SMART 原始记录无法继续保存：'+str(error))
        stop_event.set()
    finally:
        linux_connection.close()


def parse_fio_bandwidth_log(text, start_epoch, duration_seconds, expected_direction):
    """保留 fio 原始 KiB/s，同时转换为 MiB/s。每条日志代表刚结束的一秒区间。"""
    records, errors = [], []
    for line_number, line in enumerate(text.splitlines(),1):
        if not line.strip():
            continue
        try:
            fields = [field.strip() for field in line.split(',')]
            epoch = int(fields[0])/1000
            bandwidth_kib = float(fields[1])
            direction = int(fields[2])
            if direction != expected_direction:
                raise ValueError('日志的读写方向与本项测试不同')
            if not math.isfinite(bandwidth_kib) or bandwidth_kib < 0:
                raise ValueError('带宽不是有效非负数值')
            second = round(epoch-start_epoch)-1
            if not 0 <= second < duration_seconds:
                raise ValueError('日志时间不属于计划的读写区间')
            records.append({'Time':utc_timestamp(start_epoch+second), 'ActualTime':utc_timestamp(epoch),
                            'Second':second, 'BW(MiB/s)':bandwidth_kib/1024, 'Raw_KiB/s':bandwidth_kib,
                            'RawLine':line, 'Error':''})
        except (ValueError,IndexError) as error:
            errors.append(f'fio 日志第 {line_number} 行：{error}；原文：{line}')
    return records, errors


def walk_block_devices(node):
    yield node
    for child in node.get('children',[]):
        yield from walk_block_devices(child)


def verify_test_disk_is_unmounted(linux_connection, device):
    if not re.fullmatch(r'/dev/nvme\d+n\d+',device):
        raise ValueError('被测盘必须是明确的 NVMe 设备路径')
    tree = json.loads(linux_connection.execute('lsblk -J -p -o NAME,TYPE,MOUNTPOINT '+quote_shell_argument(device))[1])
    roots = tree.get('blockdevices',[])
    if len(roots)!=1 or roots[0].get('name')!=device:
        raise ValueError('无法确定被测盘的系统挂载状态')
    for node in walk_block_devices(roots[0]):
        name = node.get('name','')
        if node.get('mountpoint') or node.get('type') not in ('disk','part'):
            raise ValueError('被测盘或子设备正在使用：'+name)
        holder_path = '/sys/class/block/'+Path(name).name+'/holders'
        if linux_connection.execute('ls -A '+quote_shell_argument(holder_path))[1].strip():
            raise ValueError('被测盘正在被上层卷使用：'+name)


def read_disk_identity(linux_connection, device):
    if not re.fullmatch(r'/dev/nvme\d+n\d+',device):
        raise ValueError('无效 NVMe 路径')
    quoted = quote_shell_argument(device)
    controller = json.loads(linux_connection.execute(f'nvme id-ctrl {quoted} -o json')[1])
    controller_name = re.fullmatch(r'/dev/(nvme\d+)n\d+',device)[1]
    location = linux_connection.execute(f'readlink -f /sys/class/nvme/{controller_name}/device')[1]
    addresses = re.findall(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]',location)
    if not addresses:
        raise ValueError('未查到 PCI 地址')
    size = int(linux_connection.execute(f'blockdev --getsize64 {quoted}')[1])
    capacity = next((label for label,nominal in [('512G',512e9),('1T',1e12),('2T',2e12)] if nominal*.85<=size<=nominal*1.12),None)
    if capacity is None:
        raise ValueError('此容量不在当前公司模板的 512G / 1T / 2T 中')
    return {'device':device, 'pci':addresses[-1].lower(), 'serial':str(controller.get('sn','')).strip(),
            'model':str(controller.get('mn','')).strip(), 'firmware':str(controller.get('fr','')).strip(),
            'capacity':capacity, 'capacity_bytes':size}


def trim_selected_nvme_disk(configuration, linux_connection, output_folder):
    """只有读、写测试函数调用这里。执行前再次核对盘、序列号和显式写盘许可。"""
    device = configuration['ssh']['device']
    if not configuration['ssh']['allow_destructive']:
        raise ValueError('本项包含 TRIM，会清除专用测试盘数据；请在 config.json 核对 allow_destructive')
    verify_test_disk_is_unmounted(linux_connection,device)
    identity = read_disk_identity(linux_connection,device)
    if identity['serial'] != configuration['company_report']['expected_serial']:
        raise ValueError('TRIM 前序列号不符，未执行')
    namespace = json.loads(linux_connection.execute('nvme id-ns '+quote_shell_argument(device)+' -o json')[1])
    count = namespace.get('nsze')
    if type(count) is not int or count<=0:
        raise ValueError('未查到有效命名空间大小，未执行 TRIM')
    command = f'nvme dsm {quote_shell_argument(device)} -s 0 -b {count} -d'
    print(f'开始 TRIM：{device}，SN={identity["serial"]}。',flush=True)
    started = utc_timestamp()
    code, stdout, stderr = linux_connection.execute(command,timeout=120)
    save_json(output_folder/'TRIM执行记录.json', {'command':command,'started_at':started,'ended_at':utc_timestamp(),
                                             'exit_code':code,'stdout':stdout,'stderr':stderr})
    print(f'TRIM 完成：命令返回成功（退出码 {code}）。',flush=True)
    print('TRIM 记录：'+str(output_folder/'TRIM执行记录.json'),flush=True)


def wait_before_starting_test(seconds, stop_event):
    """等待期不读取 JK808 或 SMART。Ctrl+C 可取消，还没开始正式采样。"""
    deadline = time.monotonic()+seconds
    next_display = 0
    while time.monotonic()<deadline:
        if stop_event.is_set():
            raise InterruptedError('测试人员取消等待')
        if time.monotonic()>=next_display:
            print(f'准备等待：还剩 {math.ceil(deadline-time.monotonic())} 秒。',flush=True)
            next_display = time.monotonic()+30
        stop_event.wait(min(.5,max(0,deadline-time.monotonic())))


# ---------- 缺秒检查：只记录问题，不因一次采集失败终止测试 ----------

def find_missing_measurements(expected_slots, measurements, metric_names, source_name, start_epoch, phase='测试'):
    """按计划采样点逐个核对，包括开头、末尾和连续丢失；低频 SMART 只检查应采点。"""
    by_slot = {}
    for measurement in measurements:
        by_slot.setdefault(measurement['Second'], []).append(measurement)
    missing = []
    for second in expected_slots:
        candidates = by_slot.get(second, [])
        for metric in metric_names:
            values = [item.get(metric) for item in candidates]
            valid = [value for value in values if isinstance(value, (int, float))
                     and not isinstance(value, bool) and math.isfinite(value)]
            if len(candidates) > 1:
                reason = '同一计划采样点存在多条记录，全部原始记录保留，需核对'
            elif not valid:
                reason = (candidates[0].get(metric+'_Error') or candidates[0].get('Error') or '未收到有效值') if candidates else '该计划采样点没有记录'
            else:
                continue
            missing.append({'计划时间': utc_timestamp(start_epoch+second), '相对测试秒': second,
                            '阶段': phase, '来源': source_name, '缺失通道或指标': metric, '原因': reason})
    return missing


def build_full_temperature_data(surface_rows, smart_rows, bandwidth_rows, duration_seconds, start_epoch):
    """原模板八列在最前。每个采样点保留一次；低频 SMART 的非采样秒保持空白。"""
    # 用采样计划编号对齐，而不是把一条 SMART 值复制到随后 60/1500 秒。
    rows_by_second = {}
    for source, rows in [('JK808', surface_rows), ('SMART', smart_rows), ('带宽', bandwidth_rows)]:
        for row in rows:
            rows_by_second.setdefault(row['Second'], {}).setdefault(source, []).append(row)
    result = []
    seconds = sorted(set(range(duration_seconds)) | set(rows_by_second))
    for second in seconds:
        sources = rows_by_second.get(second, {})
        # 异常重复值另起一行保存，不平均、不覆盖第一条。
        repeat_count = max([1]+[len(items) for items in sources.values()])
        for repeat in range(repeat_count):
            row = dict.fromkeys(DATA_COLUMNS)
            row.update(Time=utc_timestamp(start_epoch+second), Second=second,
                       Phase='测试' if 0 <= second < duration_seconds else '准备或收尾')
            for source, items in sources.items():
                if repeat >= len(items):
                    continue
                item = items[repeat]
                for key in METRICS:
                    if key in item:
                        row[key] = item[key]
                row[source+'_实际接收时间'] = item.get('ActualTime', '')
                row[source+'_异常'] = item.get('Error', '')
                if source == 'JK808':
                    for key, value in item.items():
                        if key.startswith('CH'):
                            row[key] = value
            result.append(row)
    return result


def calculate_test_statistics(data_rows, left_second, right_second):
    """使用半开区间 [起点, 终点)，缺值不计入平均，真实的 0 计入；保留未舍入值。"""
    statistics = {}
    selected = [row for row in data_rows if left_second <= row['Second'] < right_second]
    for metric in METRICS:
        values = [row[metric] for row in selected if isinstance(row.get(metric), (int, float))
                  and not isinstance(row.get(metric), bool) and math.isfinite(row[metric])]
        statistics[metric] = {'count': len(values), 'max': max(values) if values else None,
                              'mean': sum(values)/len(values) if values else None,
                              'min': min(values) if values else None}
    return statistics, selected


def exclude_initial_smart_outliers(mode, smart_rows, interval_seconds, threshold_c=8, scope='round'):
    """仅读写：前两次 Composite 与后续 Composite 均值相差 >=8℃，统计和绘图排除。

    原始 rows 不改。缺失点不改编号，不用后来的点补成第一点。
    L0/L1.2 直接保留全部有效值，不做此剔除。
    """
    if mode not in ('read_1m','write_1m'):
        return [], []
    valid_reference = [row['Composite'] for row in smart_rows if row['Second']>=2*interval_seconds
                       and isinstance(row.get('Composite'),(int,float)) and math.isfinite(row['Composite'])]
    if not valid_reference:
        return [], ['读写 SMART 第三个及之后没有有效 Composite，不能判断前两点；已保留']
    reference_mean = sum(valid_reference)/len(valid_reference)
    rejected = []
    for row in smart_rows:
        if row['Second'] not in (0,interval_seconds):
            continue
        composite = row.get('Composite')
        if not isinstance(composite,(int,float)) or not math.isfinite(composite):
            continue
        difference = abs(composite-reference_mean)
        if difference >= threshold_c:
            rejected.append({'计划时间':row['Time'],'相对测试秒':row['Second'],'Composite':composite,
                             '后续Composite均值':reference_mean,'差值绝对值':difference,
                             '剔除阈值℃':threshold_c,'剔除范围':'Composite/CPU/NAND' if scope=='round' else 'Composite',
                             '说明':'不参与统计和绘图；原始值保留在完整基础数据中'})
    return rejected, []


def remove_rejected_smart_values(data_rows, rejected_points, scope):
    """统计和绘图共用的副本：剔除值置空，保留时间位置，不修改原始数据。"""
    rows = copy.deepcopy(data_rows)
    rejected_seconds = {item['相对测试秒'] for item in rejected_points}
    metrics = METRICS[3:6] if scope=='round' else ['Composite']
    for row in rows:
        if row['Second'] in rejected_seconds:
            for metric in metrics:
                row[metric] = None
    return rows


def find_chart_start_second(smart_rows, rejected_points):
    """剔除起始点后，用最后一个剔除点之后的首个有效 Composite 作共同新零点。"""
    if not rejected_points:
        return 0
    last_rejected = max(item['相对测试秒'] for item in rejected_points)
    candidates = [row['Second'] for row in smart_rows if row['Second']>last_rejected
                  and isinstance(row.get('Composite'),(int,float)) and math.isfinite(row['Composite'])]
    return min(candidates) if candidates else 0


def find_chart_end_second(smart_rows, duration, interval, chart_start_second):
    """所有曲线用同一个截止点：正式计划中最后一个有效 Composite 采样点。"""
    candidates = [row['Second'] for row in smart_rows
                  if chart_start_second<=row['Second']<duration and row['Second']%interval==0
                  and isinstance(row.get('Composite'),(int,float))
                  and not isinstance(row['Composite'],bool) and math.isfinite(row['Composite'])]
    return max(candidates) if candidates else None


def generate_all_test_results(configuration, state, folder, surface_rows, smart_rows, bandwidth_rows):
    import pandas as pd
    result_folder,folder = prepare_result_folders(folder)
    state['configuration'] = configuration_without_password(configuration)
    mode = state['mode']
    start_epoch = state['start_epoch']
    duration = state['duration_s']
    interval = state['smart_interval_s']
    raw_data = build_full_temperature_data(surface_rows,smart_rows,bandwidth_rows,duration,start_epoch)
    missing = find_missing_measurements(range(duration),surface_rows,METRICS[:3],'JK808',start_epoch)
    missing += find_missing_measurements(range(0,duration,interval),smart_rows,METRICS[3:6],'SMART',start_epoch)
    if mode in ('read_1m','write_1m'):
        fio_duration = configuration['tests'][mode]['fio_duration_s']
        missing += find_missing_measurements([second+state.get('fio_offset_s',0) for second in range(fio_duration)],bandwidth_rows,METRICS[6:],'带宽',start_epoch)
    missing.sort(key=lambda item:(item['计划时间'],item['来源'],item['缺失通道或指标']))
    rejected, filter_notes = exclude_initial_smart_outliers(mode,smart_rows,interval,8,configuration['report']['initial_smart_scope'])
    statistics_input = remove_rejected_smart_values(raw_data,rejected,configuration['report']['initial_smart_scope'])
    left_second = max(0,duration-600) if configuration['company_report']['window']=='last_600s' else 0
    statistics, selected = calculate_test_statistics(statistics_input,left_second,duration)
    notes = [f'统计区间：正式测试第 {left_second} 秒至第 {duration} 秒（不含终点）。',
             '剔除的 SMART 值不参与统计和绘图；完整基础数据保留原始值，缺失值不补零。',
             '读写前两次按 Composite 与后续均值相差达到 8℃ 判定；L0/L1.2 不做此剔除。']
    notes += filter_notes+state['errors']
    if rejected:
        notes.append(f'{len(rejected)} 个起始 SMART 采样点已从统计和图中排除，详见剔除记录')
    if not state['completed']:
        notes.append('测试未正常完成，不能作为完整成功测试')
    if state.get('simulated'):
        notes.insert(0,'模拟演练数据，不是实测结果，不可上交')
    save_csv_table(folder/'完整基础数据.csv',DATA_COLUMNS+['Second','Phase','JK808_实际接收时间','SMART_实际接收时间','带宽_实际接收时间'],raw_data)
    save_csv_table(folder/'参与统计的数据.csv',DATA_COLUMNS+['Second'],selected)
    rejection_columns = ['计划时间','相对测试秒','Composite','后续Composite均值','差值绝对值','剔除阈值℃','剔除范围','说明']
    save_csv_table(folder/'SMART起始点剔除记录.csv',rejection_columns,rejected)
    write_data_loss_notice(folder,missing,state['errors']+filter_notes)
    save_base_data_workbook(folder/'基础数据.xlsx',raw_data,selected,missing,statistics)
    data_parts = read_xlsx_parts(folder/'基础数据.xlsx')
    add_or_replace_data_sheet(data_parts,'剔除记录',rejection_columns,rejected)
    save_xlsx_parts(data_parts,folder/'基础数据.xlsx')
    # 仅显示范围前移。所有曲线共用新零点，不能只平移 SMART 造成与表面温度错位。
    chart_start_second = find_chart_start_second(smart_rows,rejected)
    chart_start_epoch = start_epoch+chart_start_second
    smart_plot_data = remove_rejected_smart_values(smart_rows,rejected,configuration['report']['initial_smart_scope'])
    chart_end_second = find_chart_end_second(smart_plot_data,duration,interval,chart_start_second)
    notes.append(f'绘图第 0 秒 = 正式测试第 {chart_start_second} 秒，北京时间 {beijing_timestamp(chart_start_epoch)}。统计区间不因此改变。')
    if chart_end_second is None:
        notes.append('没有有效 SMART 截止点，本图不绘制曲线；原始数据、统计结果和缺失记录仍保留。')
    else:
        notes.append(f'所有曲线统一截到最后有效 SMART 点：正式测试第 {chart_end_second} 秒（图中第 {chart_end_second-chart_start_second} 秒）。截止后的数据保留用于复算，统计仍按正式测试最后 10 分钟。')
    notes.append('内部缺失处断线，缺少末端数据的曲线提前结束，不插值补到共同截止点。')
    def within_chart(second):
        return chart_end_second is not None and chart_start_second<=second<=chart_end_second
    surface_by_second = {row['Second']:row for row in surface_rows}
    surface_frame = pd.DataFrame([{'Time':utc_timestamp(start_epoch+second),
        **{metric:surface_by_second.get(second,{}).get(metric) for metric in METRICS[:3]}} for second in range(duration) if within_chart(second)])
    bandwidth_by_second = {row['Second']:row for row in bandwidth_rows}
    bandwidth_frame = pd.DataFrame([{'Time':utc_timestamp(start_epoch+second),
        'BW(MiB/s)':bandwidth_by_second.get(second,{}).get('BW(MiB/s)')}
        for second in [slot+state.get('fio_offset_s',0) for slot in range(configuration['tests'][mode].get('fio_duration_s',0))] if within_chart(second)])
    smart_plot_rows = []
    by_second = {row['Second']:row for row in smart_plot_data}
    for second in range(0,duration,interval):
        if not within_chart(second):
            continue
        smart_plot_rows.append({'Time':utc_timestamp(start_epoch+second),**{metric:by_second.get(second,{}).get(metric) for metric in METRICS[3:6]}})
    smart_frame = pd.DataFrame(smart_plot_rows)
    chart = result_folder/'【最终结果+双轴图表】_双Y轴图表.png'
    figure = create_company_temperature_figure([(surface_frame,METRICS[:3],False),(smart_frame,METRICS[3:6],True),(bandwidth_frame,METRICS[6:],False)],
                                               TEST_NAMES[mode]+('（模拟）' if state.get('simulated') else ''),start=utc_timestamp(chart_start_epoch),mode=mode)
    figure.savefig(chart,dpi=300,bbox_inches='tight')
    import matplotlib.pyplot as plt
    plt.close(figure)
    write_temperature_html(result_folder/'测试报告.html',TEST_NAMES[mode],statistics,chart,missing,notes)
    save_json(folder/'统计结果.json',{'statistics':statistics,'left_second':left_second,'right_second':duration,
                                 'rejected_smart_points':rejected,'missing_count':len(missing),'notes':notes,
                                 'chart_start_original_second':chart_start_second,'chart_start_beijing':beijing_timestamp(chart_start_epoch),
                                 'chart_end_original_second':chart_end_second,
                                 'chart_end_beijing':beijing_timestamp(start_epoch+chart_end_second) if chart_end_second is not None else None})
    organize_manual_recalculation_files(configuration,state,folder,raw_data,smart_rows,statistics,chart_start_second,chart_end_second)
    company = configuration['company_report']
    if not state.get('simulated'):
        target = result_folder.parent.parent/'温度模版.xlsx'
        metadata = dict(state.get('identity',{}),test_version=company['test_version'],
                        sampling_label=f'多路测温仪温度曲线\n（表面温度每1秒采样，SMART每{interval}秒采样）')
        caption = '全程统计' if left_second==0 else '最后10min'
        if missing or not state['completed'] or state['errors']:
            caption = '10min内（有异常）'
        # 模板显示位数沿用公司整数格式；基础数据、均值和复算公式保留精度。
        display_statistics = {metric:{key:(round(value,company['decimal_places']) if key!='count' and value is not None else value)
                                      for key,value in values.items()} for metric,values in statistics.items()}
        staging = folder/('_模板待写入_'+uuid.uuid4().hex+'.xlsx')
        try:
            if target.exists():
                shutil.copy2(target,staging)
            else:
                create_empty_company_template(PROGRAM_FOLDER/'温度模版_原始.xlsx',staging)
            fill_company_test_result(staging,staging,company['capacity'],mode,display_statistics,metadata,chart,caption)
            parts = read_xlsx_parts(staging)
            prefix = company['capacity']+'_'+{'read_1m':'读','write_1m':'写','l0_ps4':'L0','l12_ps4':'L12'}[mode]
            add_or_replace_data_sheet(parts,prefix+'_基础数据',DATA_COLUMNS+['Second','Phase'],raw_data)
            add_or_replace_data_sheet(parts,prefix+'_统计数据',DATA_COLUMNS+['Second'],selected)
            add_or_replace_data_sheet(parts,prefix+'_缺失明细',['计划时间','相对测试秒','阶段','来源','缺失通道或指标','原因'],missing)
            add_or_replace_data_sheet(parts,prefix+'_剔除记录',rejection_columns,rejected)
            # 所有数据和图写完，最后一次性替换正式模板；失败时旧表保持完整。
            save_xlsx_parts(parts,target)
        finally:
            staging.unlink(missing_ok=True)
        print('最终公司模板：'+str(target),flush=True)
        # 留存本次出表时的模板副本，之后同批次复测不会改变这个副本。
        shutil.copy2(target,folder/'本次出表时的温度模版.xlsx')
    print(f'采样检查：{len(missing)} 条缺失/重复指标记录。详见“手工复测/先看数据完整性.txt”。',flush=True)
    print('全部基础数据：'+str(folder),flush=True)
    print('人工复算：双击 '+str(folder/'人工复算代码'/'开始复算.cmd'),flush=True)


def save_csv_table(path, columns, rows):
    with Path(path).open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def rebuild_saved_test_results(folder):
    """Excel 占用等写表失败后，只用本次已保存记录重新出表，不重新跑盘。"""
    folder = Path(folder).resolve()
    source = folder if (folder/'本次运行记录.json').exists() else folder/MANUAL_DATA_FOLDER_NAME
    state = json.loads((source/'本次运行记录.json').read_text('utf-8'))
    configuration = state['configuration']
    _,folder = prepare_result_folders(folder)
    state['configuration'] = configuration_without_password(configuration)
    save_json(folder/'本次运行记录.json',state)
    def read_measurements(filename):
        path = folder/filename
        if not path.exists():
            return []
        rows = []
        with path.open(encoding='utf-8-sig',newline='') as stream:
            for record in csv.DictReader(stream):
                for key,value in record.items():
                    if key in METRICS+['Second','FioSecond','Raw_KiB/s'] or re.fullmatch(r'CH\d{3}(?:_Raw)?',key):
                        record[key] = None if not value else float(value)
                if record.get('Second') is None:
                    raise ValueError('原始 CSV 缺少 Second，无法按原采样计划复算')
                for metric,channel in configuration['thermometer']['channel_map'].items():
                    if f'CH{channel:03d}_Error' in record:
                        record[metric+'_Error'] = record[f'CH{channel:03d}_Error']
                rows.append(record)
        return rows
    generate_all_test_results(configuration,state,folder,read_measurements('JK808逐秒原始数据.csv'),
                             read_measurements('SMART计划采样数据.csv'),read_measurements('带宽原始换算数据.csv'))


def excel_column_name(number):
    result = ''
    while number:
        number, remainder = divmod(number-1, 26)
        result = chr(65+remainder)+result
    return result


def prepare_data_sheet_styles(workbook_parts):
    """只追加明细表需要的样式，不改变公司原模板的任何既有样式编号。"""
    if 'xl/styles.xml' not in workbook_parts:
        workbook_parts['xl/styles.xml'] = (f'<styleSheet xmlns="{S}"><numFmts count="0"></numFmts>'
            '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>').encode()
        relationships = ET.fromstring(workbook_parts['xl/_rels/workbook.xml.rels'])
        ET.SubElement(relationships,f'{{{P}}}Relationship',Id='rDataStyles',Type=R+'/styles',Target='styles.xml')
        workbook_parts['xl/_rels/workbook.xml.rels'] = ET.tostring(relationships,encoding='utf-8')
        types = ET.fromstring(workbook_parts['[Content_Types].xml'])
        ET.SubElement(types,'{http://schemas.openxmlformats.org/package/2006/content-types}Override',
                      PartName='/xl/styles.xml',ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml')
        workbook_parts['[Content_Types].xml'] = ET.tostring(types,encoding='utf-8')
    text = workbook_parts['xl/styles.xml'].decode('utf-8')
    root = ET.fromstring(text)
    formats = root.find(f'{{{S}}}numFmts')
    date_format = 'yyyy-mm-dd hh:mm:ss.000'
    existing = next((int(item.get('numFmtId')) for item in formats if item.get('formatCode')==date_format),None)
    xfs = root.find(f'{{{S}}}cellXfs')
    if existing is not None:
        date_style = next((i for i,item in enumerate(xfs) if int(item.get('numFmtId','0'))==existing),None)
        if date_style is not None:
            return date_style, date_style+1
    number = max([163]+[int(item.get('numFmtId')) for item in formats])+1
    def append_style_element(xml, tag, element):
        pattern = r'(<'+tag+r'\b[^>]*\bcount=")(\d+)("[^>]*>)(.*?)(</'+tag+'>)'
        def change(match):
            return match[1]+str(int(match[2])+1)+match[3]+match[4]+element+match[5]
        changed, count = re.subn(pattern,change,xml,count=1,flags=re.S)
        if count!=1:
            raise ValueError('模板样式结构不符：'+tag)
        return changed
    text = append_style_element(text,'numFmts',f'<numFmt numFmtId="{number}" formatCode="{date_format}"/>')
    font_id = len(root.find(f'{{{S}}}fonts'))
    text = append_style_element(text,'fonts','<font><b/><sz val="11"/><color rgb="FF1F4E78"/><name val="Calibri"/></font>')
    date_style = len(xfs)
    text = append_style_element(text,'cellXfs',f'<xf numFmtId="{number}" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>')
    text = append_style_element(text,'cellXfs',f'<xf numFmtId="0" fontId="{font_id}" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf>')
    workbook_parts['xl/styles.xml'] = text.encode('utf-8')
    return date_style,date_style+1


def build_table_sheet_xml(columns, rows, formula_rows=(), date_style=0, header_style=0):
    """为自动测试程序生成带类型的基础数据表；公司原模板样式另行原样保留。"""
    xml_rows = []
    for number, record in enumerate([dict(zip(columns, columns))]+rows, 1):
        cells = []
        for index, name in enumerate(columns, 1):
            value = record.get(name)
            address = excel_column_name(index)+str(number)
            if value is None or value == '':
                continue
            is_time = number>1 and (name in ('Time','ActualTime','计划时间') or name.endswith('实际接收时间'))
            if is_time:
                stamp = datetime.fromisoformat(value).timestamp()
                excel_value = 25569+(stamp+8*3600)/86400  # Excel 显示北京时间；CSV 保留 UTC。
                cells.append(f'<c r="{address}" s="{date_style}"><v>{excel_value!r}</v></c>')
            elif isinstance(value, bool):
                cells.append(f'<c r="{address}" t="b"><v>{int(value)}</v></c>')
            elif isinstance(value, (int, float)):
                if math.isfinite(value):
                    cells.append(f'<c r="{address}"><v>{value!r}</v></c>')
            else:
                # 字符串只作为文字写入，路径/项目名不能变成 Excel 公式。
                style = f' s="{header_style}"' if number==1 else ''
                cells.append(f'<c r="{address}"{style} t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>')
        height = ' ht="32" customHeight="1"' if number==1 else ''
        xml_rows.append(f'<row r="{number}"{height}>'+''.join(cells)+'</row>')
    for row_number, cells in formula_rows:
        xml_rows.append(f'<row r="{row_number}">'+''.join(cells)+'</row>')
    last_row = max(len(rows)+1, max((number for number, _ in formula_rows), default=1))
    width = '<cols>'+''.join(f'<col min="{i}" max="{i}" width="{70 if columns[i-1] in ("原因","说明") else 30 if i==1 or "时间" in columns[i-1] else 23}" customWidth="1"/>' for i in range(1, len(columns)+1))+'</cols>'
    return (f'<worksheet xmlns="{S}"><dimension ref="A1:{excel_column_name(len(columns))}{last_row}"/>'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            +width+'<sheetData>'+''.join(xml_rows)+f'</sheetData><autoFilter ref="A1:{excel_column_name(len(columns))}{len(rows)+1}"/></worksheet>').encode('utf-8')


def add_or_replace_data_sheet(workbook_parts, sheet_name, columns, rows, formula_rows=()):
    """在最终模板里追加基础数据和缺失记录；不重建原有三张温度表。"""
    date_style,header_style = prepare_data_sheet_styles(workbook_parts)
    workbook_text = workbook_parts['xl/workbook.xml'].decode('utf-8')
    workbook = ET.fromstring(workbook_text)
    sheets = workbook.find(f'{{{S}}}sheets')
    relationships = ET.fromstring(workbook_parts['xl/_rels/workbook.xml.rels'])
    existing = next((sheet for sheet in sheets if sheet.get('name') == sheet_name), None)
    if existing is None:
        number = max(int(sheet.get('sheetId')) for sheet in sheets)+1
        identifier = 'rAutoData'+str(number)
        relative_path = f'worksheets/auto_data_{number}.xml'
        ET.SubElement(sheets, f'{{{S}}}sheet', name=sheet_name, sheetId=str(number), **{f'{{{R}}}id':identifier})
        # 保留 WPS / mc:Ignorable 命名空间和扩展对象的原始声明。
        workbook_text = workbook_text.replace('</sheets>',f'<sheet name="{escape(sheet_name)}" sheetId="{number}" r:id="{identifier}"/></sheets>')
        ET.SubElement(relationships, f'{{{P}}}Relationship', Id=identifier, Type=R+'/worksheet', Target=relative_path)
        types = ET.fromstring(workbook_parts['[Content_Types].xml'])
        ET.SubElement(types, '{http://schemas.openxmlformats.org/package/2006/content-types}Override',
                      PartName='/xl/'+relative_path, ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml')
        workbook_parts['[Content_Types].xml'] = ET.tostring(types, encoding='utf-8', xml_declaration=True)
    else:
        identifier = existing.get(f'{{{R}}}id')
        relative_path = next(item.get('Target') for item in relationships if item.get('Id') == identifier)
        relative_path = relative_path.removeprefix('/xl/')
    workbook_parts['xl/'+relative_path] = build_table_sheet_xml(columns, rows, formula_rows,date_style,header_style)
    workbook_parts['xl/workbook.xml'] = workbook_text.encode('utf-8')
    workbook_parts['xl/_rels/workbook.xml.rels'] = ET.tostring(relationships, encoding='utf-8', xml_declaration=True)


def save_base_data_workbook(path, all_rows, selected_rows, missing_rows, statistics):
    # 新建明细簿只含用户需要的原始数据、统计输入、可复算公式及缺失清单。
    relationships = f'<Relationships xmlns="{P}"><Relationship Id="rId1" Type="{R}/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    types = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>'
    parts = {'[Content_Types].xml':types.encode(), '_rels/.rels':relationships.encode(),
             'xl/workbook.xml':f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="临时表" sheetId="1" r:id="temporary"/></sheets></workbook>'.encode(),
             'xl/_rels/workbook.xml.rels':f'<Relationships xmlns="{P}"/>'.encode()}
    base_columns = DATA_COLUMNS+['Second','Phase','JK808_实际接收时间','SMART_实际接收时间','带宽_实际接收时间','JK808_异常','SMART_异常','带宽_异常']
    add_or_replace_data_sheet(parts, '完整基础数据', base_columns, all_rows)
    add_or_replace_data_sheet(parts, '参与统计的数据', DATA_COLUMNS+['Second'], selected_rows)
    columns = ['指标','有效点数','最大值','平均值','最小值']
    formula_rows = []
    for number, metric in enumerate(METRICS, 2):
        letter = excel_column_name(METRICS.index(metric)+2)
        reference = f"'参与统计的数据'!{letter}2:{letter}{max(2,len(selected_rows)+1)}"
        cells = [f'<c r="A{number}" t="inlineStr"><is><t>{escape(metric)}</t></is></c>']
        for column, function, key in [('B','COUNT','count'),('C','MAX','max'),('D','AVERAGE','mean'),('E','MIN','min')]:
            formula = f'{function}({reference})' if function=='COUNT' else f'IF(COUNT({reference})=0,"",{function}({reference}))'
            value = statistics[metric][key]
            cached = f'<v>{value}</v>' if value is not None else '<v></v>'
            cell_type = ' t="str"' if value is None else ''
            cells.append(f'<c r="{column}{number}"{cell_type}><f>{escape(formula)}</f>{cached}</c>')
        formula_rows.append((number,cells))
    add_or_replace_data_sheet(parts, '人工复算', columns, [], formula_rows)
    missing_columns = ['计划时间','相对测试秒','阶段','来源','缺失通道或指标','原因']
    add_or_replace_data_sheet(parts, '缺失明细', missing_columns, missing_rows)
    text = parts['xl/workbook.xml'].decode('utf-8').replace('<sheet name="临时表" sheetId="1" r:id="temporary"/>','')
    parts['xl/workbook.xml'] = text.replace('</workbook>','<calcPr calcId="191029" fullCalcOnLoad="1"/></workbook>').encode('utf-8')
    save_xlsx_parts(parts, path)


def write_data_loss_notice(folder, missing_rows, other_errors=()):
    columns = ['计划时间','相对测试秒','阶段','来源','缺失通道或指标','原因']
    save_csv_table(folder/'缺失数据时间点.csv', columns, missing_rows)
    lines = ['本次数据检查', f'缺失或重复的通道/指标记录数：{len(missing_rows)}',
             '同一秒多个通道缺失会分别列出。SMART 非计划采样秒不会被当成丢失。',
             '有数据缺失时，统计只使用实际有效值，空白没有补零或用前值填充。', '']
    lines.extend(other_errors)
    for item in missing_rows:
        local_time = datetime.fromisoformat(item['计划时间']).astimezone(BEIJING_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        lines.append(f"{local_time} | 第 {item['相对测试秒']} 秒 | {item['来源']} | {item['缺失通道或指标']} | {item['原因']}")
    (folder/'先看数据完整性.txt').write_text('\n'.join(lines), 'utf-8-sig')


def write_temperature_html(path, title, statistics, chart_path, missing_rows, notes):
    rows = []
    for metric in METRICS:
        values = statistics[metric]
        numbers = ['空缺' if values[key] is None else f'{values[key]:.2f}' for key in ('max','mean','min')]
        rows.append('<tr><td>'+escape(metric)+'</td>'+''.join('<td>'+number+'</td>' for number in numbers)+'</tr>')
    display_missing = [dict(item,计划时间=datetime.fromisoformat(item['计划时间']).astimezone(BEIJING_TIMEZONE).isoformat(timespec='milliseconds')) for item in missing_rows]
    loss_table = ''.join('<tr>'+''.join('<td>'+escape(str(item.get(key,'')))+'</td>' for key in
                         ['计划时间','相对测试秒','来源','缺失通道或指标','原因'])+'</tr>' for item in display_missing)
    image = base64.b64encode(Path(chart_path).read_bytes()).decode()
    document = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>'+escape(title)+'</title><style>body{font:16px/1.7 "Microsoft YaHei",sans-serif;background:#f5f7fa;margin:24px}main{max-width:1400px;margin:auto;background:white;padding:30px}h1{color:#2c3e50}table{border-collapse:collapse;width:100%}th{background:#367fa9;color:white}th,td{padding:10px;border-bottom:1px solid #ddd;text-align:center}.notice{background:#fff2cc;padding:14px}img{width:100%}pre{white-space:pre-wrap}</style><main><h1>'+escape(title)+'</h1>'
    document += '<p class="notice">'+escape('\n'.join(notes))+f'；缺失/异常记录：{len(missing_rows)} 条。完整基础数据和参与统计的数据见 <a href="手工复测/基础数据.xlsx">手工复测/基础数据.xlsx</a>。</p>'
    document += '<table><tr><th>指标项</th><th>最大值</th><th>平均值</th><th>最小值</th></tr>'+''.join(rows)+'</table><img src="data:image/png;base64,'+image+'">'
    document += '<h2>缺失数据的具体时间点</h2><table><tr><th>计划时间（北京时间 UTC+8）</th><th>相对秒</th><th>来源</th><th>通道/指标</th><th>原因</th></tr>'+loss_table+'</table></main></html>'
    Path(path).write_text(document, 'utf-8')


# ---------- 打包本次复算材料；计算代码本身不依赖测试程序 ----------

def organize_manual_recalculation_files(configuration, state, folder, raw_data, smart_rows, statistics, chart_start_second, chart_end_second=None):
    data_folder = folder
    code_folder = folder/'人工复算代码'
    data_folder.mkdir(exist_ok=True)
    code_folder.mkdir(exist_ok=True)
    metadata = {'format_version':1,'mode':state['mode'],'duration_s':state['duration_s'],
                'smart_interval_s':state['smart_interval_s'],'start_epoch':state['start_epoch'],
                'start_beijing':beijing_timestamp(state['start_epoch']),
                'statistics_window_s':600,'outlier_threshold_c':8,
                'initial_smart_scope':configuration['report']['initial_smart_scope'],
                'decimal_places':configuration['company_report']['decimal_places'],
                'chart_start_original_second':chart_start_second,
                'chart_end_original_second':chart_end_second,
                'identity':state.get('identity',{}),'sample':configuration['company_report'],
                'completed':state['completed'],'errors':state['errors'],
                '说明':'本文件是这次测试的固定复算条件，不是运行测试的配置文件。Second 为正式测试相对秒，未因图归零而改动。'}
    save_json(data_folder/'本次复算条件.json',metadata)
    # 主出表流程已写入完整数据（含实际接收时间），不以精简列覆盖它。
    if not (data_folder/'完整基础数据.csv').exists():
        save_csv_table(data_folder/'完整基础数据.csv',DATA_COLUMNS+['Second','Phase'],raw_data)
    save_csv_table(data_folder/'SMART原始计划点.csv',['Time','Second','Composite','CPU','NAND','ActualTime','Error'],smart_rows)
    # 给人工检查增加北京时间列，不改动原始 Time/Second 或任何测量值。
    beijing_rows = [dict(row,北京时间=beijing_timestamp(state['start_epoch']+row['Second'])) for row in raw_data]
    save_csv_table(data_folder/'基础数据_北京时间.csv',['北京时间']+DATA_COLUMNS+['Second','Phase'],beijing_rows)
    save_json(data_folder/'程序统计结果_仅供对照.json',statistics)
    # 原始 CSV、USB 日志、TRIM 记录和校时记录从创建时就写入本目录，无需另存重复副本。
    for filename in ('复算温度.py','开始复算.cmd','复算说明.txt'):
        shutil.copy2(PROGRAM_FOLDER/'人工复算代码'/filename,code_folder/filename)
    essential = ['本次复算条件.json','完整基础数据.csv','SMART原始计划点.csv']
    manifest = {name:hashlib.sha256((data_folder/name).read_bytes()).hexdigest() for name in essential}
    save_json(data_folder/'原始输入校验.json',manifest)



# ---------- 操作员入口：A 首次部署；B 核对 JSON 后选择测试 ----------

def ask_text(label, current=''):
    value = input(f'{label} [{current}]：').strip()
    return value or current


@contextmanager
def supply_linux_password(configuration, remember=False):
    validate_ssh_file_paths(configuration['ssh'])
    name = configuration['ssh']['password_env']
    if not re.fullmatch(r'JK500_[A-Z0-9_]+',name):
        raise ValueError('password_env 必须使用 JK500_ 开头的环境变量名')
    previous = os.environ.get(name)
    try:
        password = configuration['ssh'].get('password','')
        if not isinstance(password,str):
            raise ValueError('ssh.password 必须是字符串；密码使用英文双引号包住')
        if remember and not configuration['ssh']['key_filename']:
            entered = getpass.getpass('Linux 登录密码（保存在 config.json；回车保留已记住的密码）：')
            password = entered or password or previous or ''
            configuration['ssh']['password'] = password
        elif not password:
            password = previous or ''
            if not password and not configuration['ssh']['key_filename']:
                password = getpass.getpass('Linux 登录密码（尚未记住；以后可在 A 保存密码）：')
        if password:
            os.environ[name] = password
        yield
    finally:
        if previous is None:
            os.environ.pop(name,None)
        else:
            os.environ[name] = previous


def install_local_python_dependencies():
    """A 内完成安装，使用随包离线依赖。没有安装公司测试流程的自定义库。"""
    if sys.version_info[:2] != (3,12) or ctypes.sizeof(ctypes.c_void_p)!=8:
        raise RuntimeError('请先安装 Python 3.12 x64，再运行开始测试.cmd')
    python = PROGRAM_FOLDER/'.venv/Scripts/python.exe'
    if not python.exists():
        subprocess.run([sys.executable,'-m','venv',str(PROGRAM_FOLDER/'.venv')],check=True)
    subprocess.run([str(python),'-m','pip','install','--no-index','--find-links',str(PROGRAM_FOLDER/'wheelhouse'),
                    '--require-hashes','-r',str(PROGRAM_FOLDER/'requirements.lock')],check=True)
    return python


def import_old_receiver_configuration(configuration):
    old_folder = input('已有旧版时，粘贴旧程序目录以导入 IP 和设备；全新部署直接回车：').strip().strip('"')
    if not old_folder:
        return
    old_path = Path(old_folder)/'config.json'
    if old_path.resolve()==CONFIGURATION_FILE.resolve():
        return
    old = json.loads(old_path.read_text('utf-8-sig'))
    for section in ('ssh','thermometer','company_report'):
        for key in configuration[section]:
            if key in old.get(section,{}):
                configuration[section][key] = old[section][key]
    configuration['thermometer']['timeout_s'] = .75
    configuration['company_report']['window'] = 'last_600s'
    if isinstance(old.get('clock_sync'),dict):
        configuration['clock_sync'] = old['clock_sync']
    print('已导入连接和设备信息；四项测试顺序使用本次确认的新流程。')


def detect_and_fill_nvme_information(configuration, linux_connection):
    devices = json.loads(linux_connection.execute('nvme list -o json')[1]).get('Devices')
    if not isinstance(devices,list):
        raise ValueError('未识别 nvme list JSON 格式，未修改盘配置')
    candidates = []
    for device in devices:
        path = device.get('DevicePath','')
        if not re.fullmatch(r'/dev/nvme\d+n\d+',path):
            continue
        try:
            verify_test_disk_is_unmounted(linux_connection,path)
        except ValueError:
            continue
        candidates.append(read_disk_identity(linux_connection,path))
    if not candidates:
        raise ValueError('没有可选的未挂载 NVMe 数据盘，系统盘不会自动选中')
    for index, item in enumerate(candidates,1):
        print(f"{index}. {item['device']}  {item['model']}  {item['capacity']}  SN={item['serial']}")
    if len(candidates)==1:
        selected = candidates[0]
    else:
        answer = input('本次测试哪一块盘？输入编号：').strip()
        if not answer.isdigit() or not 1<=int(answer)<=len(candidates):
            raise ValueError('没有选定被测盘，原配置保留')
        selected = candidates[int(answer)-1]
    previous_serial = configuration['company_report']['expected_serial']
    if previous_serial != selected['serial']:
        configuration['ssh']['allow_destructive'] = False
    configuration['ssh'].update(device=selected['device'],expected_pci=selected['pci'])
    configuration['company_report'].update(expected_serial=selected['serial'],capacity=selected['capacity'])
    if not configuration['company_report']['test_version'] or configuration['company_report']['test_version']==configuration.get('detected_disk',{}).get('firmware'):
        configuration['company_report']['test_version'] = selected['firmware']
    configuration['detected_disk'] = selected
    # 此单位核对只在 A 部署时做，正式测试的等待期不读 SMART。
    quoted = quote_shell_argument(selected['device'])
    raw = json.loads(linux_connection.execute(f'nvme smart-log {quoted} -o json')[1]).get('temperature')
    text = linux_connection.execute(f'nvme smart-log {quoted}')[1]
    match = re.search(r'^\s*temperature\s*:\s*(-?\d+(?:\.\d+)?)\s*(?:°\s*)?C\b',text,re.M|re.I)
    if isinstance(raw,(int,float)) and not isinstance(raw,bool) and math.isfinite(raw) and match:
        units = [unit for unit,value in [('K',raw-273.15),('C',raw)] if abs(value-float(match[1]))<=3]
        if len(units)==1:
            configuration['ssh']['smart_temperature_unit'] = units[0]
    print(f"已自动填写：{selected['device']} / {selected['pci']} / {selected['serial']} / {selected['capacity']}")
    print('实际固件：'+selected['firmware']+'；接下来确认这块盘的项目和批次。')


def identify_and_verify_jk808(configuration):
    import hid
    settings = configuration['thermometer']
    existing_paths = {item['path'].hex() for item in hid.enumerate(0x0483,0x5757)}
    if settings.get('hid_path_hex') not in existing_paths:
        print('只拔下 JK808 的 REMOTE 数据线，不动其他 USB 设备。')
        before = existing_paths
        input('拔下后按回车：')
        absent = {item['path'].hex() for item in hid.enumerate(0x0483,0x5757)}
        input('插回原来的 USB 口，等识别后按回车：')
        returned = {item['path'].hex() for item in hid.enumerate(0x0483,0x5757)}
        candidates = sorted((before-absent)&returned)
        if len(candidates)!=1:
            raise ValueError('没有识别到唯一的 JK808，请核对 USB 数据线后重试 A')
        settings['hid_path_hex'] = candidates[0]
        settings['protocol_verified'] = False
    thermometer = JK808Thermometer(settings,lambda direction,data:None)
    try:
        values, statuses, _ = thermometer.read()
        if any(statuses.get(channel)!='ok' for channel in settings['active_channels']):
            raise ValueError('启用的探针没有全部返回有效温度，请核对接线和通道')
        print('部署检查温度：'+'  '.join(f'{name}={values[channel]}℃' for name,channel in settings['channel_map'].items()))
        if input('与 JK808 屏幕上的对应通道一致吗？一致输入 Y：').strip().upper()!='Y':
            raise ValueError('温度对应关系尚未确认，未启用测试')
        settings['protocol_verified'] = True
    finally:
        thermometer.close()


def first_deployment():
    configuration = load_configuration()
    if not configuration.get('deployment_complete'):
        import_old_receiver_configuration(configuration)
    previous_sample = dict(configuration['company_report'])
    previous_host = configuration['ssh']['host']
    previous_port = configuration['ssh']['port']
    previous_username = configuration['ssh']['username']
    configuration['ssh']['host'] = ask_text('Linux IP',previous_host)
    configuration['ssh']['username'] = ask_text('Linux 登录账号',configuration['ssh']['username'])
    configuration['ssh']['port'] = int(ask_text('SSH 端口',str(configuration['ssh']['port'])))
    if previous_host!=configuration['ssh']['host'] or previous_port!=configuration['ssh']['port']:
        configuration['ssh']['host_key'] = None
        configuration['ssh']['allow_destructive'] = False
    if (previous_host,previous_port,previous_username)!=(configuration['ssh']['host'],configuration['ssh']['port'],configuration['ssh']['username']):
        configuration['ssh']['password'] = ''
    linux_connection = LinuxConnection(configuration['ssh'])
    with lock_receiver_during_test(), supply_linux_password(configuration,remember=True):
        try:
            linux_connection.connect(allow_new_host_key=True)
            detect_and_fill_nvme_information(configuration,linux_connection)
            confirm_disk_project_and_batch(configuration,previous_sample)
            linux_connection.execute('command -v nvme')
            fio_version = linux_connection.execute('fio --version')[1].strip()
            if not re.match(r'fio-\d+\.',fio_version):
                raise ValueError('Linux 上的 fio 不是所需的 Flexible I/O Tester')
            linux_connection.execute('command -v setsid')
            identify_and_verify_jk808(configuration)
            configuration['deployment_complete'] = True
            save_configuration(configuration)
            print('部署完成，登录信息和盘的项目批次归属已保存。B 可自由选 1 或 2。')
        finally:
            linux_connection.close()


def validate_report_folder_name(value, key):
    """项目和批次作为一层文件夹名；保留测试人员填写的点号、空格和括号。"""
    label = {'project':'项目名','batch':'批次名'}[key]
    field = f'company_report.{key}（{label}）'
    if not isinstance(value,str) or not value.strip():
        raise ValueError(f'{field} 不能为空，请在 config.json 填写并按 Ctrl+S 保存')
    if len(value)>60:
        raise ValueError(f'{field} 最多 60 个字符')
    if value in ('.','..') or value.endswith(('.', ' ')):
        raise ValueError(f'{field} 不能是 . 或 ..，结尾不能是点号或空格；9.15 可以使用')
    if re.search(r'[<>:"/\\|?*\x00-\x1f]',value):
        raise ValueError(f'{field} 含有 Windows 文件夹名不允许的字符；完整路径请填 output_dir')
    base = value.split('.',1)[0].rstrip(' ').upper()
    if base in ('CON','PRN','AUX','NUL','CONIN$','CONOUT$') or re.fullmatch(r'(COM|LPT)[1-9¹²³]',base):
        raise ValueError(f'{field} 是 Windows 保留的设备名称，请换一个名称')


def get_report_batch_folder(configuration):
    """同一项目、批次的所有容量共用此目录及一本温度模板。"""
    company = configuration['company_report']
    root = Path(company.get('output_dir') or 'deliveries')
    if not root.is_absolute():
        root = PROGRAM_FOLDER/root
    return root/company['project']/company['batch']


def disk_assignment_fields(configuration):
    """绑定人工确认的归属与自动识别的盘身份；不包含密码或测试参数。"""
    company = configuration['company_report']
    return {'project':company['project'], 'batch':company['batch'],
            'capacity':company['capacity'], 'serial':company['expected_serial'],
            'version':company['test_version'],
            'firmware':configuration.get('detected_disk',{}).get('firmware',''),
            'output_dir':os.path.normcase(os.path.normpath(company.get('output_dir') or 'deliveries'))}


def validate_confirmed_disk_assignment(configuration):
    saved = configuration.get('sample_assignment')
    expected = disk_assignment_fields(configuration)
    if (not isinstance(saved,dict) or not saved.get('confirmed_beijing')
            or not expected['firmware'] or any(saved.get(key)!=value for key,value in expected.items())):
        raise ValueError('请先选 A 确认这块盘的项目和批次归属。修改项目、批次、盘身份、报告版本或保存目录后，需要重新在 A 确认')


def check_report_batch_disk_conflict(configuration, batch_folder):
    """只检查已有容量归属，不创建目录、不登记新盘、不改历史表格。"""
    path = batch_folder/'汇总对象记录.json'
    if not path.exists() and (batch_folder/'温度模版.xlsx').exists():
        raise ValueError('这个目录已有温度模版.xlsx，但缺少汇总对象记录，无法核对归属。请选择另一个批次，原表不会覆盖')
    try:
        saved = json.loads(path.read_text('utf-8-sig')) if path.exists() else {}
        if not isinstance(saved,dict) or any(
                capacity not in ('512G','1T','2T') or not isinstance(item,dict)
                or any(not isinstance(item.get(key),str) or not item[key] for key in ('serial','version','firmware'))
                for capacity,item in saved.items()):
            raise ValueError('记录结构不正确')
    except (ValueError,UnicodeError) as error:
        raise ValueError('汇总对象记录损坏，不能核对已有盘身份。请选择另一个批次或检查原记录，不会自动清空记录') from error
    company = configuration['company_report']
    identity = {'serial':company['expected_serial'], 'version':company['test_version'],
                'firmware':configuration.get('_identity',configuration.get('detected_disk',{})).get('firmware','')}
    if not all(isinstance(value,str) and value for value in identity.values()):
        raise ValueError('盘序列号、报告版本或固件缺失，请重新在 A 查询盘信息')
    previous = saved.get(company['capacity'])
    if previous and previous != identity:
        raise ValueError(f"{company['project']} / {company['batch']} 的 {company['capacity']} 已有盘 "
                         f"SN={previous['serial']}，报告版本={previous['version']}，固件={previous['firmware']}；"
                         '与本次不一致。请选择另一个批次，原结果不会覆盖')
    return saved,identity


def select_report_project_and_batch(configuration):
    """列出已有汇总批次，也允许输入新项目/批次；此时仍不保存配置。"""
    root = Path(configuration['company_report'].get('output_dir') or 'deliveries')
    if not root.is_absolute():
        root = PROGRAM_FOLDER/root
    existing = sorted((project.name,batch.name)
                      for project in root.iterdir() if project.is_dir()
                      for batch in project.iterdir() if batch.is_dir()
                      and ((batch/'汇总对象记录.json').is_file() or (batch/'温度模版.xlsx').is_file())) if root.is_dir() else []
    print('选择这块盘实际所属的项目和批次：')
    for index,(project,batch) in enumerate(existing,1):
        print(f'{index}. {project} / {batch}')
    print('N. 填写项目和批次（新建或尚未有结果的批次）\n0. 取消，保留原配置')
    while True:
        answer = input('输入编号或 N：').strip().upper()
        if answer=='0':
            raise InterruptedError('已取消盘归属确认，原配置保留；新盘尚未启用')
        if answer.isdigit() and 1<=int(answer)<=len(existing):
            return existing[int(answer)-1]
        if answer=='N':
            project = input('项目名（所有容量共用，例如 2380-PCBA；0 取消）：').strip()
            if project=='0':
                raise InterruptedError('已取消盘归属确认，原配置保留；新盘尚未启用')
            batch = input('批次名（同一批次的不同容量使用相同名称；0 取消）：').strip()
            if batch=='0':
                raise InterruptedError('已取消盘归属确认，原配置保留；新盘尚未启用')
            try:
                validate_report_folder_name(project,'project')
                validate_report_folder_name(batch,'batch')
                return project,batch
            except ValueError as error:
                print(str(error))
        else:
            print('请输入已有批次编号、N 或 0；回车不代表确认。')


def confirm_disk_project_and_batch(configuration, previous_sample):
    """A 查盘后必须明确确认；所有选择仅在内存中，部署成功才统一保存。"""
    company = dict(configuration['company_report'])
    candidate = dict(configuration,company_report=company)
    disk = configuration['detected_disk']
    print(f"本次盘：{disk['device']} / {disk['model']} / {disk['capacity']} / SN={disk['serial']} / 固件={disk['firmware']}")
    if previous_sample.get('expected_serial'):
        print(f"原配置盘：{previous_sample.get('capacity','')} / SN={previous_sample['expected_serial']}")
    if company['capacity'] not in ('512G','1T','2T'):
        raise ValueError('本次容量不对应公司模板的 512G / 1T / 2T，未保存配置')
    try:
        for key in ('project','batch'):
            validate_report_folder_name(company.get(key,''),key)
    except ValueError:
        company['project'],company['batch'] = select_report_project_and_batch(candidate)
    while True:
        print(f"待确认归属：项目 {company['project']} / 批次 {company['batch']} / 容量页 {company['capacity']}")
        print('汇总表：'+str(get_report_batch_folder(candidate)/'温度模版.xlsx'))
        print('不同容量可以共用此表；项目和批次由测试人员核对，程序不能从盘序列号判断。')
        answer = input('这块盘属于上述项目和批次吗？Y 确认 / N 另选或新建 / 0 取消：').strip().upper()
        if answer=='0':
            raise InterruptedError('已取消盘归属确认，原配置保留；新盘尚未启用')
        if answer=='Y':
            try:
                check_report_batch_disk_conflict(candidate,get_report_batch_folder(candidate))
            except ValueError as error:
                print(str(error))
                company['project'],company['batch'] = select_report_project_and_batch(candidate)
                continue
            configuration['company_report'] = company
            configuration['sample_assignment'] = dict(disk_assignment_fields(configuration),
                                                       confirmed_beijing=datetime.now(BEIJING_TIMEZONE).isoformat(timespec='seconds'))
            print('归属已确认；剩余部署检查全部通过后保存到 config.json。')
            return
        if answer=='N':
            company['project'],company['batch'] = select_report_project_and_batch(candidate)
        else:
            print('请输入 Y、N 或 0；回车不代表确认。')


def validate_test_configuration(configuration, mode):
    company = configuration['company_report']
    if not configuration['ssh']['host'] or not 1<=configuration['ssh']['port']<=65535:
        raise ValueError('请先在 A 填写 Linux IP 和 SSH 端口')
    if configuration['ssh']['smart_temperature_unit'] not in ('K','C'):
        raise ValueError('SMART 单位必须是 K 或 C，可在 A 自动核对')
    if type(configuration['ssh']['allow_destructive']) is not bool:
        raise ValueError('allow_destructive 必须是 JSON 的 true 或 false，不能加引号')
    if not 0 < configuration['thermometer']['timeout_s'] <= .8:
        raise ValueError('JK808 timeout_s 应为 0 到 0.8 秒之间，给每秒采样留出时间')
    for key in ('project','batch'):
        validate_report_folder_name(company.get(key,''),key)
    validate_ssh_file_paths(configuration['ssh'])
    if not isinstance(company.get('output_dir',''),str):
        raise ValueError('company_report.output_dir 应为目录文本；填 "" 使用程序目录下的 deliveries')
    if not company['expected_serial'] or not company['test_version']:
        raise ValueError('序列号和版本不能为空。盘信息可在 A 自动填写')
    if company['capacity'] not in ('512G','1T','2T'):
        raise ValueError('容量须与公司模板的 512G / 1T / 2T 对应')
    validate_confirmed_disk_assignment(configuration)
    if company['window']!='last_600s':
        raise ValueError('当前确认的统计范围是最后 10 分钟，window 应为 last_600s')
    if configuration['report']['initial_smart_scope'] not in ('round','composite'):
        raise ValueError('initial_smart_scope 只能为 round 或 composite')
    if not configuration['thermometer']['protocol_verified']:
        raise ValueError('请先选 A 核对温度仪')
    validate_jk808_settings(configuration['thermometer'])
    mapping = configuration['thermometer']['channel_map']
    if set(mapping)!=set(METRICS[:3]) or len(set(mapping.values()))!=3:
        raise ValueError('三个表面温度指标必须对应三个不同的实际探针通道')
    if any(type(channel) is not int or channel not in configuration['thermometer']['active_channels'] for channel in mapping.values()):
        raise ValueError('channel_map 必须使用已启用通道')
    # BIOS 由测试人员实际设置。旧配置的 bios_profile 仅作记录，不作为启动条件。
    settings = configuration['tests'][mode]
    if type(settings['duration_s']) is not int or not 600<=settings['duration_s']<=86400:
        raise ValueError('正式采样时长须为 600..86400 秒整数，保证最后 10 分钟统计范围')
    expected_interval = 1500 if mode=='l12_ps4' else 60
    if settings['smart_interval_s']!=expected_interval:
        raise ValueError(f'本项保留的 SMART 周期应为 {expected_interval} 秒')
    if mode in ('read_1m','write_1m'):
        if not configuration['ssh']['allow_destructive']:
            raise ValueError('读写两项会 TRIM，请先在 JSON 核对专用测试盘并设置 allow_destructive=true')
        if type(settings['fio_duration_s']) is not int or not 600<=settings['fio_duration_s']<=settings['duration_s']:
            raise ValueError('fio 持续时间必须至少 600 秒，且不能超过正式采样时间')
    validate_company_template(read_xlsx_parts(PROGRAM_FOLDER/'温度模版_原始.xlsx'))
    for filename in ('复算温度.py','开始复算.cmd','复算说明.txt'):
        if not (PROGRAM_FOLDER/'人工复算代码'/filename).is_file():
            raise ValueError('新版文件不完整：请把新包的整个“人工复算代码”文件夹复制到程序目录，再开始测试')


def check_linux_identity_before_test(configuration, linux_connection, mode):
    verify_test_disk_is_unmounted(linux_connection,configuration['ssh']['device'])
    identity = read_disk_identity(linux_connection,configuration['ssh']['device'])
    if identity['serial']!=configuration['company_report']['expected_serial'] or identity['pci']!=configuration['ssh']['expected_pci'].lower():
        raise ValueError('实际序列号或 PCI 与 JSON 不符，请到 A 重新查盘')
    if identity['capacity']!=configuration['company_report']['capacity']:
        raise ValueError('实际容量与 JSON 不符')
    if configuration.get('detected_disk',{}).get('firmware') not in (None,'',identity['firmware']):
        raise ValueError('固件已变化，请到 A 更新盘信息并核对报告版本、批次')
    code, _, _ = linux_connection.execute('pgrep -x fio',check=False)
    if code!=1:
        raise RuntimeError('Linux 上已有 fio，或无法确认进程状态，未开始新测试')
    for key,command in [('platform','cat /sys/class/dmi/id/board_name'),
                        ('cpu',"awk -F: '/^model name/{print $2;exit}' /proc/cpuinfo"),('os','cat /etc/os-release')]:
        identity[key] = linux_connection.execute(command)[1].strip()
    os_name = re.search(r'^PRETTY_NAME="?(.*?)"?$',identity['os'],re.M)
    if os_name:
        identity['os'] = os_name[1]
    # 这里只读取身份和主机信息，不提前读 SMART，不把准备阶段作为采样。
    return identity


def check_batch_disk_identity(configuration, batch_folder):
    """同一批次同一容量不能混入不同序列号/固件；历史测次目录始终保留。"""
    saved,identity = check_report_batch_disk_conflict(configuration,batch_folder)
    saved[configuration['company_report']['capacity']] = identity
    save_json(batch_folder/'汇总对象记录.json',saved)


def create_test_result_folder(configuration, mode):
    """每次任务自动建独立目录；相同测试重复执行也不覆盖上一次原始结果。"""
    company = configuration['company_report']
    batch_folder = get_report_batch_folder(configuration)
    sample_folder = batch_folder/company['capacity']
    sample_folder.mkdir(parents=True,exist_ok=True)
    name = datetime.now(BEIJING_TIMEZONE).strftime('%Y%m%d_%H%M%S_')+TEST_NAMES[mode]+'_'+uuid.uuid4().hex[:8]
    folder = sample_folder/name
    folder.mkdir(exist_ok=False)
    print('已自动创建本次结果目录：'+str(folder),flush=True)
    print('本批次最终表格：'+str(batch_folder/'温度模版.xlsx'),flush=True)
    return folder


def execute_selected_test(configuration, mode, receiver_locked=False):
    validate_test_configuration(configuration,mode)
    linux_connection = LinuxConnection(configuration['ssh'])
    smart_settings = dict(configuration['ssh'],command_timeout_s=1)
    smart_connection = LinuxConnection(smart_settings)
    stop_event = threading.Event()
    with (nullcontext() if receiver_locked else lock_receiver_during_test()):
        # 在登录、校时之前就创建目录，准备阶段出错也有本次记录可查。
        output_folder = create_test_result_folder(configuration,mode)
        data_folder = output_folder/MANUAL_DATA_FOLDER_NAME
        data_folder.mkdir()
        try:
            save_json(data_folder/'测试配置快照.json',configuration_without_password(configuration))
            with supply_linux_password(configuration):
                clock_record = synchronize_receiver_clock(configuration, data_folder)
                if clock_record:
                    # 初始目录保留失败记录；校时成功后，以校准后的北京时间重新命名。
                    new_name = datetime.now(BEIJING_TIMEZONE).strftime('%Y%m%d_%H%M%S_')+TEST_NAMES[mode]+'_'+output_folder.name.rsplit('_',1)[-1]
                    corrected_folder = output_folder.with_name(new_name)
                    if corrected_folder != output_folder:
                        output_folder.rename(corrected_folder)
                        output_folder = corrected_folder
                        data_folder = output_folder/MANUAL_DATA_FOLDER_NAME
                        print('校时后的本次结果目录：'+str(output_folder),flush=True)
                linux_connection.connect()
                configuration['_identity'] = check_linux_identity_before_test(configuration,linux_connection,mode)
                check_batch_disk_identity(configuration,output_folder.parent.parent)
                # 建好 SMART 独立 SSH 会话，等待期间不发送采样请求。
                smart_connection.connect()
                save_json(data_folder/'测试盘与主机信息.json',configuration['_identity'])
                functions = {'read_1m':run_1m_read_test,'write_1m':run_1m_write_test,
                             'l0_ps4':run_l0_ps4_test,'l12_ps4':run_l12_ps4_test}
                result = functions[mode](configuration,linux_connection,smart_connection,data_folder,stop_event)
                result.update(result_folder=str(output_folder.resolve()),results_saved=True)
                save_json(data_folder/'本次运行记录.json',result)
                print('本次结果目录：'+str(output_folder))
                return result
        except BaseException as error:
            if output_folder.exists():
                (data_folder/'操作未完成.txt').write_text(f'{type(error).__name__}: {error}\n已采集的文件保留。','utf-8-sig')
            raise
        finally:
            stop_event.set()
            smart_connection.close()
            linux_connection.close()


def confirm_l12_configuration():
    """L1.2 的两个入口使用同样的配置核对；其他单项不弹出配置编辑器。"""
    print('打开 config.json，核对 IP、密码、盘序列号、项目、批次和保存目录。')
    print('如修改项目、批次、报告版本或保存目录，请先返回 A 确认归属，再开始测试。')
    print('L1.2 不执行 TRIM；读写测试才需要 allow_destructive。BIOS 由你手动设置。')
    print('本次使用的配置：'+str(CONFIGURATION_FILE))
    if os.name=='nt':
        subprocess.Popen(['notepad.exe',str(CONFIGURATION_FILE)])
    return input('保存 JSON 后，确认开始新一轮 L1.2，输入 Y；其他输入返回：').strip().upper()=='Y'


def run_single_test(configuration, mode):
    """独立跑一次所选项目；沿用采集和出表，不修改三项连跑进度。"""
    if mode not in TEST_NAMES:
        raise ValueError('不支持的单项测试')
    print('单项测试：'+TEST_NAMES[mode]+'；完成后返回，不自动执行其他项目。',flush=True)
    print('BIOS 由测试人员按本项要求设置。原有连跑进度保留。',flush=True)
    result = execute_selected_test(configuration,mode)
    if not result.get('completed') or not result.get('results_saved'):
        raise RuntimeError(TEST_NAMES[mode]+' 未完整结束或结果未保存，请检查本次记录。')
    print(TEST_NAMES[mode]+' 单项测试已完成，结果已保存并更新本批次温度模板。',flush=True)
    if result.get('errors'):
        print('本次有采样或数据异常提示，请核对报告中的完整性记录。',flush=True)
    if mode=='l12_ps4':
        print('如需改测其他 BIOS 条件，请手动关闭 Linux 主机、修改 BIOS 后再开机。',flush=True)
    return result


def choose_and_run_single_test():
    options = {'1':'l12_ps4','2':'l0_ps4','3':'write_1m','4':'read_1m'}
    while True:
        print('\n单项测试（只执行所选的一项）')
        print('1. L1.2 / PS4\n2. L0 / PS4\n3. 1M 写测试\n4. 1M 读测试\n0. 返回测试选择')
        choice = input('请选择单项测试：').strip()
        if choice=='0':
            return False
        mode = options.get(choice)
        if mode is None:
            print('输入 1、2、3、4 或 0。')
            continue
        if mode=='l12_ps4' and not confirm_l12_configuration():
            return False
        run_single_test(load_configuration(),mode)
        return True


def choose_and_run_test():
    while True:
        print('1. L1.2（核对配置，完成后提示人工关机改 BIOS）')
        print('2. L0 → 写 → 读（可直接选择；中断后可续测）')
        print('3. 单项测试（L1.2、L0、写、读任选一项）\n0. 返回')
        print('各入口可自由选择。BIOS 由测试人员按所选项目设置。')
        choice = input('请选择本次操作：').strip()
        if choice=='0':
            return
        if choice=='1':
            if confirm_l12_configuration():
                start_l12_then_pause(load_configuration())
            return
        if choice=='2':
            continue_l0_write_read(load_configuration())
            return
        if choice=='3':
            if choose_and_run_single_test():
                return
        else:
            print('输入 1、2、3 或 0。')


def main():
    if '--rebuild' in sys.argv:
        position = sys.argv.index('--rebuild')
        if position+1>=len(sys.argv):
            raise ValueError('--rebuild 后面需要本次结果目录')
        with lock_receiver_during_test():
            rebuild_saved_test_results(Path(sys.argv[position+1]))
        return
    if '--deploy' in sys.argv:
        first_deployment()
        return
    if '--test' in sys.argv:
        choose_and_run_test()
        return
    while True:
        print('\nJK808 + Linux 温度测试 v2.2.4\nA. 首次部署 / 换盘后自动更新盘信息\nB. 执行测试\n0. 退出')
        choice = input('请选择 A 或 B：').strip().upper()
        try:
            if choice=='0':
                return
            if choice=='A':
                python = install_local_python_dependencies()
                subprocess.run([str(python),str(Path(__file__).resolve()),'--deploy'],check=True)
            elif choice=='B':
                python = PROGRAM_FOLDER/'.venv/Scripts/python.exe'
                if not python.exists():
                    print('请先选 A 完成首次部署。')
                    continue
                if Path(sys.executable).resolve()==python.resolve():
                    # 同一进程执行，Ctrl+C 由采集函数处理并完成保存、停止本次 fio。
                    choose_and_run_test()
                else:
                    # 首次 A 安装后，切换到本目录环境，不留下会提前终止采集的父进程。
                    os.execv(str(python),[str(python),str(Path(__file__).resolve()),'--test'])
            else:
                print('输入 A、B 或 0。')
        except (Exception,KeyboardInterrupt) as error:
            print('本次操作未完成：'+str(error))
        input('按回车返回主菜单：')


# ---------- 两段操作：L1.2 → 人工关机改 BIOS → L0、写、读 ----------

def configuration_without_password(configuration):
    """密码只留在实际 config.json；结果、运行记录、复算材料不携带登录密码。"""
    snapshot = copy.deepcopy({key:value for key,value in configuration.items() if not key.startswith('_')})
    snapshot.setdefault('ssh',{})['password'] = ''
    return snapshot


def workflow_configuration_key(configuration):
    """续测核对同一主机、同一盘、同一批次和采样规则；不检查 BIOS 标记。"""
    ssh = configuration['ssh']
    identity = {key:ssh.get(key) for key in ('host','port','username','device','expected_pci','smart_temperature_unit','smart_round_up')}
    content = {'ssh':identity,'sample':configuration['company_report'],
               'tests':configuration['tests'],'thermometer':configuration['thermometer'],
               'report':configuration['report'],'detected_disk':configuration.get('detected_disk',{})}
    return hashlib.sha256(json.dumps(content,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def save_workflow_progress(progress):
    """这是自动生成的运行进度，不是第二份配置。原始测次目录始终保留。"""
    directory = PROGRAM_FOLDER/'运行记录'
    directory.mkdir(exist_ok=True)
    progress['updated_beijing'] = beijing_timestamp()
    save_json(directory/('流程_'+progress['id']+'.json'),progress)
    save_json(directory/'当前流程.json',progress)


def load_workflow_progress():
    path = PROGRAM_FOLDER/'运行记录'/'当前流程.json'
    if not path.exists():
        return None
    progress = json.loads(path.read_text('utf-8'))
    if progress.get('format_version')!=1 or not re.fullmatch(r'[0-9a-f]{32}',progress.get('id','')):
        raise ValueError('续测进度格式无效，请保留运行记录并核查，不会自动重跑写盘。')
    return progress


def remember_completed_test(progress, mode, result):
    if not result.get('completed') or not result.get('results_saved'):
        raise RuntimeError(TEST_NAMES[mode]+' 未完整结束或结果未保存，已保留数据，后续项目暂未启动。')
    progress['completed'][mode] = {'result_folder':result['result_folder'],
                                   'finished_epoch':time.time(),'finished_beijing':beijing_timestamp(),
                                   'sampling_notices':list(result.get('errors',[]))}
    progress['current_test'] = None
    save_workflow_progress(progress)
    # 每次结果里也保留截至本项的进度，方便复核本轮执行顺序。
    target = Path(result['result_folder'])/MANUAL_DATA_FOLDER_NAME
    target.mkdir(parents=True,exist_ok=True)
    save_json(target/'本轮流程进度.json',progress)


def start_l12_then_pause(configuration):
    """只运行 L1.2。报告保存完毕后提示人工关机，不发送 shutdown/reboot 命令。"""
    validate_test_configuration(configuration,'l12_ps4')
    progress = {'format_version':1,'id':uuid.uuid4().hex,'phase':'running_l12',
                'configuration_key':workflow_configuration_key(configuration),
                'sample':copy.deepcopy(configuration['company_report']),
                'completed':{},'current_test':'l12_ps4','last_error':''}
    with lock_receiver_during_test():
        save_workflow_progress(progress)
        try:
            result = execute_selected_test(configuration,'l12_ps4',receiver_locked=True)
            remember_completed_test(progress,'l12_ps4',result)
            progress['phase'] = 'waiting_for_manual_bios'
            save_workflow_progress(progress)
        except BaseException as error:
            progress.update(phase='l12_incomplete',last_error=f'{type(error).__name__}: {error}')
            save_workflow_progress(progress)
            raise
    print('\nL1.2 已完成，结果已保存。',flush=True)
    print('下一步：手动关闭 Linux 测试主机 → 修改 BIOS 为后面三项的设置 → 开机。',flush=True)
    print('Windows 接收电脑保持运行；也可以关闭程序，稍后重新打开，选 B → 2 继续。',flush=True)
    print('后续顺序：L0 → 等 5 分钟 → 写 → 等 5 分钟 → 读。',flush=True)


def wait_between_completed_tests(progress, previous_mode, stop_event):
    """两项之间补足 5 分钟；各项内部原有 TRIM/预写后的 5 分钟保持不变。"""
    finish = progress['completed'][previous_mode]['finished_epoch']
    elapsed = max(0,time.time()-finish)
    remaining = max(0,math.ceil(300-elapsed))
    if remaining:
        print(f'项目间隔：上一项已完成，等待 {remaining} 秒后开始下一项准备。',flush=True)
        wait_before_starting_test(remaining,stop_event)


def continue_l0_write_read(configuration):
    """可直接执行三项；相同配置下中断后续测，整组完成后再次选择则新开一组。"""
    modes = ['l0_ps4','write_1m','read_1m']
    for mode in modes:
        validate_test_configuration(configuration,mode)
    stop_event = threading.Event()
    with lock_receiver_during_test():
        progress = load_workflow_progress()
        configuration_key = workflow_configuration_key(configuration)
        if (progress is None or progress['configuration_key']!=configuration_key
                or progress['phase'] not in ('waiting_for_manual_bios','running_sequence','sequence_paused')):
            progress = {'format_version':1,'id':uuid.uuid4().hex,'phase':'running_sequence',
                        'configuration_key':configuration_key,
                        'sample':copy.deepcopy(configuration['company_report']),
                        'completed':{},'current_test':None,'last_error':''}
            save_workflow_progress(progress)
            print('开始新一组 L0、写、读；原有各项结果和流程记录保留。',flush=True)
        # L1.2 是否执行、是否成功均不限制选 2；仅核对本组需要续测的已完成项目。
        for mode,item in progress['completed'].items():
            if mode not in modes:
                continue
            saved = Path(item['result_folder'])/MANUAL_DATA_FOLDER_NAME/'本次运行记录.json'
            if not saved.is_file():
                raise ValueError('已完成项目的结果目录被移动或缺少运行记录。请恢复原结果目录后再继续：'+str(saved))
        # 已完成项必须形成顺序前缀，损坏或手工修改的进度不会跳过中间项目。
        flags = [mode in progress['completed'] for mode in modes]
        if flags != sorted(flags,reverse=True):
            raise ValueError('续测进度顺序不一致，请检查运行记录，未开始测试。')
        print('按本轮已保存配置继续：'+configuration['company_report']['project']+' / '+configuration['company_report']['batch'],flush=True)
        print('BIOS 由你实际设置；此处不打开 JSON，也不按 BIOS 标记拦截。',flush=True)
        try:
            for index,mode in enumerate(modes):
                if mode in progress['completed']:
                    continue
                progress.update(phase='running_sequence',current_test=None,last_error='')
                save_workflow_progress(progress)
                if index:
                    wait_between_completed_tests(progress,modes[index-1],stop_event)
                progress['current_test'] = mode
                save_workflow_progress(progress)
                print('\n开始 '+TEST_NAMES[mode],flush=True)
                result = execute_selected_test(configuration,mode,receiver_locked=True)
                remember_completed_test(progress,mode,result)
            progress.update(phase='complete',current_test=None)
            save_workflow_progress(progress)
        except BaseException as error:
            progress.update(phase='sequence_paused',last_error=f'{type(error).__name__}: {error}')
            save_workflow_progress(progress)
            raise
    print('\nL0、写、读全部完成，各项结果已分别保存，最终温度模板已汇总。',flush=True)


def prepare_result_folders(folder):
    """每次结果外面只放 PNG/HTML，其余资料集中到“手工复测”。兼容旧结果重算。"""
    folder = Path(folder)
    result_folder = folder.parent if folder.name==MANUAL_DATA_FOLDER_NAME else folder
    data_folder = result_folder/MANUAL_DATA_FOLDER_NAME
    data_folder.mkdir(parents=True,exist_ok=True)
    # 只整理当前测次的直接子项；先检查所有目标，不覆盖或删除同名历史数据。
    moves = []
    root = result_folder.resolve()
    for item in result_folder.iterdir():
        if item==data_folder or (item.is_file() and item.suffix.lower() in ('.png','.html')):
            continue
        target = data_folder/item.name
        if not item.resolve().is_relative_to(root) or not target.resolve().is_relative_to(root):
            raise ValueError('结果目录包含指向外部的路径，未移动：'+item.name)
        if target.exists():
            raise ValueError('手工复测目录已有同名资料，保留两份请先核对：'+item.name)
        moves.append((item,target))
    for source,target in moves:
        source.rename(target)
    return result_folder,data_folder


# ---------- Linux SSH 与测试前校时 ----------
def validate_ssh_file_paths(settings):
    """密码登录时这两项留空；只有使用实际密钥文件时才填写文件路径。"""
    paths, errors = {}, []
    for key in ('key_filename','known_hosts'):
        value = settings.get(key,'')
        paths[key] = None
        if value == '':
            continue
        try:
            if not isinstance(value,str):
                raise ValueError('file path must be text')
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = PROGRAM_FOLDER/path
            exists = path.is_file()
        except (OSError,ValueError,RuntimeError):
            exists = False
        if not exists:
            # 错填内容可能是密码，提示中不回显原值。
            advice = ('私钥文件路径，不是密码；用密码登录请设为 ""' if key=='key_filename'
                      else '主机记录文件路径，不是 IP；通常请设为 ""，自动生成的 host_key 保持原样')
            errors.append(f'ssh.{key} 应填写{advice}')
        else:
            paths[key] = str(path)
    if errors:
        raise ValueError('\n'.join(errors))
    return paths


class LinuxConnection:
    """只处理 SSH 连接和命令。测试顺序直接写在文件开头的四个函数中。"""
    def __init__(self, settings):
        self.settings = settings
        self.client = None

    def connect(self, allow_new_host_key=False):
        files = validate_ssh_file_paths(self.settings)
        import paramiko
        self.close()
        client = paramiko.SSHClient()
        host = self.settings['host']
        port = self.settings['port']
        host_label = host if port == 22 else f'[{host}]:{port}'
        pinned = self.settings.get('host_key')
        if pinned:
            if pinned['host'] != host_label:
                raise ValueError('Linux IP 或端口已变，请在 A 重新识别主机')
            key = paramiko.PKey.from_type_string(pinned['type'], base64.b64decode(pinned['base64']))
            client.get_host_keys().add(host_label, pinned['type'], key)
        else:
            client.load_system_host_keys()
            if files['known_hosts']:
                client.load_host_keys(files['known_hosts'])
        settings = self.settings

        class ConfirmNewLinuxHost(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, ssh_client, hostname, key):
                fingerprint = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
                print(f'首次连接 {hostname}，SSH 主机指纹 SHA256:{fingerprint}')
                print('请与该 Linux 主机或已有 MobaXterm 会话记录核对。')
                if input('确认是本台测试主机，输入 Y：').strip().upper() != 'Y':
                    raise ValueError('尚未确认 Linux 主机身份')
                ssh_client.get_host_keys().add(hostname, key.get_name(), key)

        client.set_missing_host_key_policy(ConfirmNewLinuxHost() if allow_new_host_key and not pinned else paramiko.RejectPolicy())
        try:
            client.connect(hostname=host, port=port, username=settings['username'],
                           password=os.environ.get(settings['password_env']) or None,
                           key_filename=files['key_filename'],
                           timeout=settings['command_timeout_s'], auth_timeout=settings['command_timeout_s'],
                           banner_timeout=settings['command_timeout_s'])
            client.get_transport().set_keepalive(30)
            if allow_new_host_key:
                key = client.get_transport().get_remote_server_key()
                settings['host_key'] = {'host':host_label, 'type':key.get_name(), 'base64':key.get_base64()}
            self.client = client
        except BaseException:
            client.close()
            raise

    def execute(self, command, timeout=None, check=True):
        if self.client is None:
            self.connect()
        limit = timeout if timeout is not None else self.settings['command_timeout_s']
        deadline = time.monotonic()+limit
        _, stdout_stream, _ = self.client.exec_command(command, timeout=limit)
        channel = stdout_stream.channel
        stdout, stderr = bytearray(), bytearray()
        try:
            while True:
                while channel.recv_ready():
                    stdout.extend(channel.recv(65536))
                while channel.recv_stderr_ready():
                    stderr.extend(channel.recv_stderr(65536))
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('SSH 命令响应超时')
                time.sleep(.005)
            code = channel.recv_exit_status()
        finally:
            channel.close()
        text = stdout.decode('utf-8', errors='replace')
        error = stderr.decode('utf-8', errors='replace')
        if check and code != 0:
            raise RuntimeError(f'Linux 命令退出码 {code}：{error[:1000]}')
        return code, text, error

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None

_thread_lock = threading.Lock()
MUTEX = 'Global\\JK808_ReceiverClockAndAcquisition_v1'

def measure_linux_clock_difference(remote, samples=5, max_rtt_s=.5):
    """SSH 会话应先连接好。选最快往返样本，以半个 RTT 估计传输不确定度。"""
    accepted = []
    for _ in range(samples):
        wall = time.time()
        begin = time.monotonic()
        _, text, _ = remote.execute('date +%s.%N')
        finish = time.monotonic()
        after = time.time()
        rtt = finish-begin
        if abs((after-wall)-rtt) > .1:
            raise ValueError('测量期间电脑时间发生跳变，请等待校时结束后重试')
        try:
            stamp = float(text.strip())
        except ValueError:
            raise ValueError('Linux 没有返回有效的秒数时间，无法自动校时') from None
        if not math.isfinite(stamp) or not 946684800 <= stamp <= 4102444800:
            raise ValueError('Linux 返回的时间无效或不在 2000—2100 年范围内')
        if 0 <= rtt <= max_rtt_s:
            accepted.append(dict(offset_s=stamp-(wall+after)/2, rtt_s=rtt,
                                 remote_epoch_s=stamp, received_monotonic_s=finish))
    if len(accepted) < 3:
        raise ValueError('SSH 校时往返过慢（需至少三次在 0.5 秒内），请检查接收电脑到 Linux 的网络后重试')
    if max(s['offset_s'] for s in accepted)-min(s['offset_s'] for s in accepted) > .5:
        raise ValueError('Linux 时间或网络延迟不稳定，暂未校时，请稍后重试')
    best = dict(min(accepted, key=lambda s:s['rtt_s']))
    best['accepted_samples'] = len(accepted)
    return best


def utc_system_time_fields(epoch):
    if not math.isfinite(epoch) or not 946684800 <= epoch <= 4102444800:
        raise ValueError('拒绝设置无效的 UTC 时间')
    dt = datetime.fromtimestamp(epoch, timezone.utc)
    return (dt.year, dt.month, (dt.weekday()+1)%7, dt.day,
            dt.hour, dt.minute, dt.second, dt.microsecond//1000)


def set_windows_system_time(epoch):
    if os.name != 'nt':
        raise RuntimeError('自动修改接收电脑时间仅支持 Windows')
    class SYSTEMTIME(ctypes.Structure):
        _fields_ = [(name, wintypes.WORD) for name in
                    ('year','month','weekday','day','hour','minute','second','milliseconds')]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.SetSystemTime.argtypes = [ctypes.POINTER(SYSTEMTIME)]
    kernel.SetSystemTime.restype = wintypes.BOOL
    value = SYSTEMTIME(*utc_system_time_fields(epoch))
    # SetSystemTime 接收 UTC，并自行启用/还原调用者已有的系统时间权限。
    if not kernel.SetSystemTime(ctypes.byref(value)):
        code = ctypes.get_last_error()
        if code in (5, 1314):
            raise PermissionError('Windows 不允许修改时间。请关闭程序，右键“开始测试.cmd” → 以管理员身份运行，再选原菜单。')
        raise OSError(code, 'Windows 校时失败：'+ctypes.FormatError(code))


@contextmanager
def lock_receiver_during_test():
    """互斥所有已安装本补丁的采集/校时进程，覆盖同机多个 RDP 会话。"""
    if not _thread_lock.acquire(blocking=False):
        raise RuntimeError('接收电脑已有采集或校时任务，请等该任务结束')
    handle, acquired, kernel = None, False, None
    try:
        if os.name == 'nt':
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            kernel.CreateMutexW.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
            kernel.ReleaseMutex.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = kernel.CreateMutexW(None, False, MUTEX)
            if not handle:
                raise RuntimeError('不能取得接收电脑运行锁，请用同一 Windows 账号以管理员身份运行')
            state = kernel.WaitForSingleObject(handle, 0)
            acquired = state in (0, 0x80)  # 已退出进程留下的 mutex 由系统回收。
            if not acquired:
                raise RuntimeError('接收电脑已有采集或校时任务（可能在另一个远程桌面会话），请先等它结束')
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        if handle:
            kernel.CloseHandle(handle)
        _thread_lock.release()


# 沿用现场已验证的 v2.1.0 NTP 校时流程。

def beijing_timestamp(epoch=None):
    return datetime.fromtimestamp(time.time() if epoch is None else epoch, BEIJING_TIMEZONE).isoformat(timespec='milliseconds')


def decode_ntp_reply(packet, request_token, round_trip_s, received_monotonic):
    """SNTP 回复检查与换算。使用单调时钟测往返，不依赖电脑原来的错误日期。

    参考 RFC 5905。接收时刻估计 = 服务器发送时间 + 网络往返的一半。
    服务器处理耗时从往返中扣除；记录误差估计，不声称零误差。
    """
    if len(packet)<48:
        raise ValueError('NTP 回复不足 48 字节')
    leap, version, mode = packet[0]>>6, (packet[0]>>3)&7, packet[0]&7
    if leap==3 or version not in (3,4) or mode!=4 or not 1<=packet[1]<=15:
        raise ValueError('NTP 服务器未同步、限流，或回复类型无效')
    if packet[24:32]!=request_token:
        raise ValueError('NTP 回复不属于本次请求')
    def unix_seconds(raw):
        seconds, fraction = struct.unpack('!II',raw)
        if seconds==0 and fraction==0:
            raise ValueError('NTP 时间戳为零')
        epoch = seconds-2208988800+fraction/(2**32)
        if epoch<946684800:  # NTP 秒字段在 2036 年回绕，覆盖 2000—2100 年。
            epoch += 2**32
        utc_system_time_fields(epoch)
        return epoch
    received = unix_seconds(packet[32:40])
    transmitted = unix_seconds(packet[40:48])
    processing = transmitted-received
    if not 0<=round_trip_s<=1 or processing<0 or processing>round_trip_s+.01:
        raise ValueError('NTP 回复耗时或时间顺序异常')
    root_delay = struct.unpack('!i',packet[4:8])[0]/65536
    dispersion = struct.unpack('!I',packet[8:12])[0]/65536
    uncertainty = max(0,round_trip_s-processing)/2+max(0,root_delay)/2+dispersion
    if uncertainty>.5:
        raise ValueError('NTP 误差估计超过 0.5 秒')
    return {'epoch_s':transmitted+max(0,round_trip_s-processing)/2,
            'received_monotonic_s':received_monotonic,'rtt_s':round_trip_s,
            'uncertainty_s':uncertainty,'stratum':packet[1]}


def request_ntp_time(server):
    """Windows 接收电脑发起 UDP 123 请求。联网主机名可在唯一 config.json 中替换。"""
    token = os.urandom(8)
    request = bytes([0x23])+bytes(39)+token
    # 连接 UDP socket 会限制回复来源；随机 origin token 防止旧回复混入。
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as client:
        client.settimeout(2)
        client.connect((server,123))
        begin = time.monotonic()
        client.send(request)
        packet = client.recv(512)
        end = time.monotonic()
    result = decode_ntp_reply(packet,token,end-begin,end)
    result['server'] = server
    return result


def standard_time_now(reference):
    """标准时间用 monotonic 推进；修改 Windows 墙钟不会使这个基准跳变。"""
    return reference['epoch_s']+time.monotonic()-reference['received_monotonic_s']


def obtain_internet_standard_time(configuration):
    servers = configuration.get('clock_sync',{}).get('ntp_servers',['ntp.aliyun.com','ntp1.aliyun.com'])
    if not isinstance(servers,list) or not 1<=len(servers)<=4 or any(
        not isinstance(server,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}',server) for server in servers):
        raise ValueError('clock_sync.ntp_servers 应为 1 到 4 个 NTP 域名或 IPv4 地址')
    errors = []
    for server in servers:
        samples = []
        for attempt in range(3):
            try:
                samples.append(request_ntp_time(server))
            except (OSError,ValueError) as error:
                errors.append(server+': '+str(error))
            if attempt<2:
                time.sleep(1)
        if len(samples)>=2:
            estimates = [standard_time_now(sample) for sample in samples]
            if max(estimates)-min(estimates)>.25:
                errors.append(server+': 多次回复相差超过 0.25 秒')
                continue
            best = dict(min(samples,key=lambda sample:sample['uncertainty_s']))
            best['accepted_samples'] = len(samples)
            best['source_failures'] = errors
            return best
    raise RuntimeError('没有取得可靠的联网标准时间，未开始测试。请让接收电脑放行 NTP UDP 123，'
                       '或在 config.json 的 clock_sync.ntp_servers 填公司 NTP 地址。'+ '；'.join(errors))


def require_windows_clock_permission():
    if os.name!='nt' or not ctypes.windll.shell32.IsUserAnAdmin():
        raise PermissionError('请右键“开始测试.cmd” → 以管理员身份运行，程序需要校准两台电脑时间')


def set_beijing_timezones(linux_connection):
    linux_connection.execute('timedatectl set-timezone Asia/Shanghai')
    result = subprocess.run(['tzutil.exe','/s','China Standard Time'],capture_output=True,timeout=15)
    if result.returncode:
        raise RuntimeError('Windows 北京时区设置失败，请检查管理员权限或公司时区策略')
    zone = linux_connection.execute('date +%z')[1].strip()
    if zone!='+0800':
        raise RuntimeError('Linux 时区未变为北京时间 +0800')


def synchronize_receiver_clock(configuration, output_folder=None):
    """每次测试准备之前执行。Linux 不用联网，也不用安装 NTP 客户端。

    1. 接收电脑读取公网 NTP，以单调时钟保存标准时间基准。
    2. SSH 设置 Linux 的 UTC 秒数；两台电脑显示时区设置为 UTC+8。
    3. Windows 对准同一标准，再对 Linux 微调；核对两机时差与标准时间。
    全过程不读取 SMART、不运行 fio、不刷 BIOS、不改变 NTP 服务配置。
    """
    # 兼容旧文件，但测试前的联网校时不允许因旧开关 false 而静默跳过。
    ssh = configuration['ssh']
    tolerance = ssh.get('clock_tolerance_s',2)
    if isinstance(tolerance,bool) or not isinstance(tolerance,(int,float)) or not math.isfinite(tolerance) or tolerance<=0:
        raise ValueError('clock_tolerance_s 必须是正数')
    folder = Path(output_folder) if output_folder else Path(configuration.get('_config_dir',PROGRAM_FOLDER))/'clock_sync'
    folder.mkdir(parents=True,exist_ok=True)
    target = folder/'北京时间校准记录.json'
    record = {'outcome':'running','host':ssh['host'],'receiver_before_beijing':beijing_timestamp(),
              'changed_linux':False,'changed_windows':False,'method':'接收电脑公网 NTP → SSH Linux → Windows → 两机核对'}
    save_json(target,record)
    remote = LinuxConnection(ssh)
    try:
        require_windows_clock_permission()
        print('校时 1/3：接收电脑联网获取标准时间……',flush=True)
        reference = obtain_internet_standard_time(configuration)
        record['ntp_reference'] = reference
        record['standard_beijing'] = beijing_timestamp(standard_time_now(reference))
        save_json(target,record)
        remote.connect()
        if remote.execute('id -u')[1].strip()!='0':
            raise PermissionError('通过 SSH 设置 Linux 时间需要 root 账号；请在 config.json 填有权限的账号')
        set_beijing_timezones(remote)
        print('校时 2/3：通过 SSH 校准 Linux，北京时间 '+beijing_timestamp(standard_time_now(reference)),flush=True)
        # date 可能原本错误，先用无关时间的命令估计 SSH 传输耗时。
        delays = []
        for _ in range(3):
            begin = time.monotonic()
            remote.execute('printf ok')
            delays.append(time.monotonic()-begin)
        if min(delays)>.5:
            raise RuntimeError('SSH 校时往返超过 0.5 秒，请检查内网连接')
        epoch = standard_time_now(reference)+min(delays)/2
        utc_system_time_fields(epoch)
        script = 'import time; time.clock_settime(time.CLOCK_REALTIME, '+f'{epoch:.6f}'+'); print(time.time())'
        remote.execute('python3 -c '+quote_shell_argument(script))
        record['changed_linux'] = True
        save_json(target,record)
        print('校时 3/3：校准 Windows，并核对它与 Linux 的时间差……',flush=True)
        set_windows_system_time(standard_time_now(reference))
        record['changed_windows'] = True
        before = measure_linux_clock_difference(remote)
        record['before_fine_alignment'] = before
        if abs(before['offset_s'])+before['rtt_s']/2>.5:
            raise RuntimeError('Linux 写入后与标准时间偏差过大，未开始测试，请检查 SSH 延迟或系统校时策略')
        epoch = before['remote_epoch_s']+before['rtt_s']/2+time.monotonic()-before['received_monotonic_s']
        set_windows_system_time(epoch)
        after = measure_linux_clock_difference(remote)
        reference_error = time.time()-standard_time_now(reference)
        record.update(after=after,receiver_vs_standard_s=reference_error)
        if abs(after['offset_s'])+after['rtt_s']/2>min(.5,tolerance) or abs(reference_error)>.5:
            raise RuntimeError('校时后检查未通过，可能有其他校时策略干预，未开始测试')
        record['outcome'] = 'ok'
        print(f"北京时间校准完成：{beijing_timestamp()}；两机估计时差 {after['offset_s']:+.3f} 秒。",flush=True)
        return record
    except BaseException as error:
        record.update(outcome='failed',error=f'{type(error).__name__}: {error}')
        raise
    finally:
        remote.close()
        record['receiver_after_beijing'] = beijing_timestamp()
        save_json(target,record)
        print('校时记录：'+str(target),flush=True)


# ---------- JK808 已验证的 USB V13 协议 ----------
class ProtocolError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def calculate_crc16(data):
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def convert_temperature_words(words, cfg):
    values, statuses = {}, {}
    for channel, raw in words.items():
        value = raw / 10.0
        if cfg["unit"] == "F":
            value = (value - 32) / 1.8
        elif cfg["unit"] == "K":
            value -= 273.15
        if channel not in cfg["active_channels"]:
            status = "disabled"
        elif raw in cfg["invalid_raw_values"]:
            status = "invalid_code"
        elif not cfg["valid_min_c"] <= value <= cfg["valid_max_c"]:
            status = "out_of_range"
        else:
            status = "ok"
        values[channel] = round(value, 4) if status == "ok" else None
        statuses[channel] = status
    return values, statuses

V9_IDS = (0x0483, 0x5757)

def jk808_response_version(cfg):
    # Preserve V9 semantics for existing 1.3.0 configurations. New templates use V13.
    version = cfg.get('hid_data_format', 'v9')
    if version not in ('v9', 'v13'):
        raise ValueError('hid_data_format 必须是 v9 或 v13；新包按所提供厂家软件使用 v13。')
    return version


def jk808_invalid_temperature_word(cfg):
    return 32767 if jk808_response_version(cfg) == 'v13' else -1


def convert_jk808_temperature_words(words, cfg):
    conversion = dict(cfg)
    conversion['invalid_raw_values'] = list(set(cfg['invalid_raw_values']) | {jk808_invalid_temperature_word(cfg)})
    return convert_temperature_words(words, conversion)


def validate_jk808_settings(cfg, require_path=True):
    jk808_response_version(cfg)
    if cfg['channel_count'] != 8:
        raise ValueError('JK808 专用适配器要求 8 通道。')
    path = cfg.get('hid_path_hex', '')
    if require_path and not path:
        raise ValueError('请先选首次部署 A 识别 JK808 USB 设备，不需要填写 COM 口。')
    if path:
        try:
            if not bytes.fromhex(path):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('hid_path_hex 不是有效设备路径，请重新执行首次部署 A。') from None


def build_jk808_read_request():
    body = bytes.fromhex('01 03 00 00')
    return b'\0' + (body + calculate_crc16(body).to_bytes(2, 'big')).ljust(64, b'\0')


class JK808ResponseDecoder:
    def __init__(self):
        self.buffer = bytearray()
        self.words = {}

    def feed(self, payload):
        self.buffer.extend(payload)
        while self.buffer:
            if self.buffer[0] == 0:
                # Padding only between complete frames, never inside a frame.
                del self.buffer[0]
                continue
            if len(self.buffer) < 6:
                break
            if self.buffer[:2] != b'\x01\x03':
                raise ProtocolError('header_error', '不是 USB V9/V13 温度响应，请保留原始报文核对固件。')
            start = int.from_bytes(self.buffer[2:4], 'big')
            count = int.from_bytes(self.buffer[4:6], 'big')
            if not 1 <= count <= 8 or start + count > 8:
                raise ProtocolError('channel_error', f'错误的零起始通道范围：{start}, {count}')
            length = 8 + count * 2
            if len(self.buffer) < length:
                break
            frame = bytes(self.buffer[:length])
            if calculate_crc16(frame[:-2]) != int.from_bytes(frame[-2:], 'big'):
                raise ProtocolError('crc_error', 'USB V9/V13 响应 CRC 校验失败')
            for offset in range(count):
                channel = start + offset + 1
                if channel in self.words:
                    raise ProtocolError('duplicate_channel', f'同轮重复通道 {channel}')
                self.words[channel] = int.from_bytes(frame[6+2*offset:8+2*offset], 'big', signed=True)
            del self.buffer[:length]
        return self.words


class JK808Thermometer:
    def __init__(self, cfg, trace):
        self.cfg, self.trace, self.device = cfg, trace, None

    def open(self):
        import hid
        validate_jk808_settings(self.cfg)
        path = bytes.fromhex(self.cfg['hid_path_hex'])
        matches = [x for x in hid.enumerate(*V9_IDS) if x['path'] == path]
        if len(matches) != 1:
            raise ProtocolError('device_missing', '指定 JK808 USB 设备未找到；检查数据线，换 USB 口后重做首次部署 A。')
        dev = hid.device()
        try:
            dev.open_path(path)
        except Exception:
            dev.close()
            raise
        self.device = dev

    def close(self):
        if self.device is not None:
            self.device.close()
            self.device = None

    def read(self):
        if self.device is None:
            self.open()
        # Bounded drain: a device streaming unsolicited data is not this profile.
        for _ in range(16):
            old = bytes(self.device.read(65, 1))
            if not old:
                break
            self.trace('discard_before_request', old)
        else:
            raise ProtocolError('unexpected_stream', '设备持续主动发送数据，与当前协议不符。')
        request = build_jk808_read_request()
        self.trace('tx_hid', request)
        if self.device.write(request) != len(request):
            raise ProtocolError('write_error', 'USB 读取请求未完整发送')
        decoder = JK808ResponseDecoder()
        deadline = time.monotonic() + self.cfg['timeout_s']
        while time.monotonic() < deadline:
            ms = max(1, min(50, int((deadline-time.monotonic())*1000)))
            raw = bytes(self.device.read(65, ms))
            if not raw:
                continue
            self.trace('rx_hid', raw)
            if len(raw) == 65:
                if raw[0] != 0:
                    raise ProtocolError('report_id_error', '不是 Report ID 0 的 HID 响应')
                raw = raw[1:]
            words = decoder.feed(raw)
            if len(words) == 8 and not decoder.buffer:
                values, statuses = convert_jk808_temperature_words(words, self.cfg)
                return values, statuses, words
        raise ProtocolError('timeout', f'USB V9/V13 超时，收到 {len(decoder.words)}/8 路；可能为另一固件。')


# ---------- 保留公司模板格式的定点写入 ----------
S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
X = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P = 'http://schemas.openxmlformats.org/package/2006/relationships'
BLOCKS = {'l12_ps4': (49, 53), 'l0_ps4': (58, 62),
          'write_1m': (67, 71), 'read_1m': (76, 80)}
CAPACITIES = ('512G', '1T', '2T')


def read_xlsx_parts(path):
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist() if not n.endswith('/')}


def save_xlsx_parts(data, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # OPC 的内容类型清单使用默认命名空间，兼容 Excel/.NET 的严格读取器。
    data = dict(data)
    content_types = data['[Content_Types].xml'].decode('utf-8')
    prefix = re.search(r'xmlns:(\w+)="http://schemas.openxmlformats.org/package/2006/content-types"', content_types)
    if prefix:
        name = prefix[1]
        content_types = content_types.replace('xmlns:'+name+'=', 'xmlns=')
        content_types = content_types.replace('<'+name+':', '<').replace('</'+name+':', '</')
        data['[Content_Types].xml'] = content_types.encode('utf-8')
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED) as z:
            for name, value in data.items():
                z.writestr(name, value)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def replace_template_cells(xml, values):
    """仅替换已存在的目标单元格内容；保留原样式编号和其他 XML 字节。"""
    text = xml.decode('utf-8')
    for address, value in values.items():
        pattern = r'<c\b(?=[^>]*\br="' + re.escape(address) + r'")[^>]*?(?:/>|>.*?</c>)'
        found = re.search(pattern, text, re.S)
        if not found:
            raise ValueError(f'模板缺少预定单元格 {address}，请使用随包模板')
        attrs = re.match(r'<c\b([^>]*?)/?>', found[0])[1].rstrip('/')
        attrs = re.sub(r'\s+t="[^"]*"', '', attrs)
        if value is None:
            cell = f'<c{attrs}/>'
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value):
                raise ValueError('不能把非有限数值写入模板')
            cell = f'<c{attrs}><v>{value}</v></c>'
        else:
            cell = f'<c{attrs} t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'
        text = text[:found.start()] + cell + text[found.end():]
    return text.encode('utf-8')


def validate_company_template(data):
    wb = ET.fromstring(data['xl/workbook.xml'])
    names = [e.get('name') for e in wb.findall(f'{{{S}}}sheets/{{{S}}}sheet')]
    if names[:3] != [c + '-温度' for c in CAPACITIES]:
        raise ValueError('模板工作表须依次为 512G-温度、1T-温度、2T-温度')
    for i in range(1, 4):
        sheet = ET.fromstring(data[f'xl/worksheets/sheet{i}.xml'])
        merges = {e.get('ref') for e in sheet.findall(f'.//{{{S}}}mergeCell')}
        if not {'A51:B57', 'A60:B66', 'A69:B75', 'A78:B84'} <= merges:
            raise ValueError('模板 Linux 区域布局不同，不能按此坐标填表')


def create_empty_company_template(source, destination):
    data = read_xlsx_parts(source)
    validate_company_template(data)
    # 新批次必须清空历史 Windows 和 Linux 结果、平台信息及所有旧图。
    addresses = [f'B{r}' for r in (1, 2, 3, 4, 5, 44, 45, 46, 47, 48)]
    addresses += [f'{c}{r+o}' for r in (11, 20, 29, 38, 53, 62, 71, 80)
                  for o in (0, 2, 4) for c in 'CDE']
    for i, capacity in enumerate(CAPACITIES, 1):
        values = dict.fromkeys(addresses)
        values.update({'B4': capacity, 'B47': capacity, 'C43': None})
        for row in (8, 17, 26, 35, 50, 59, 68, 77):
            values[f'C{row}'] = '未测试'
        name = f'xl/worksheets/sheet{i}.xml'
        data[name] = replace_template_cells(data[name], values)
        data[f'xl/drawings/drawing{i}.xml'] = f'<xdr:wsDr xmlns:xdr="{X}"/>'.encode()
        data[f'xl/drawings/_rels/drawing{i}.xml.rels'] = f'<Relationships xmlns="{P}"/>'.encode()
    for name in list(data):
        if name.startswith('xl/media/'):
            del data[name]
    save_xlsx_parts(data, destination)


def build_excel_picture_anchor(row, rid, object_id):
    """铺满模板 A:B 的七行合并区域，并随单元格移动、缩放；row 为零起始。"""
    def marker(tag, col, r):
        return f'<xdr:{tag}><xdr:col>{col}</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>{r}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:{tag}>'
    # 右下角锚在 C 列、下一行的左上角，恰好贴合 A:B 区域边界，不越过邻格。
    # twoCell 对应 Excel 的“随单元格改变位置和大小”；宽高独立适配单元格。
    return (f'<xdr:twoCellAnchor editAs="twoCell" xmlns:xdr="{X}" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="{R}">'
            + marker('from', 0, row) + marker('to', 2, row+7)
            + f'<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="{object_id}" name="Linux 自动温度曲线"/><xdr:cNvPicPr><a:picLocks noChangeAspect="0"/></xdr:cNvPicPr></xdr:nvPicPr>'
            + f'<xdr:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></xdr:blipFill>'
            + '<xdr:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr></xdr:pic><xdr:clientData/></xdr:twoCellAnchor>')


def replace_company_chart(data, index, mode, png):
    row, _ = BLOCKS[mode]
    key = f'xl/drawings/drawing{index}.xml'
    xml = data[key].decode('utf-8')
    # 删除本项目区域的旧图片；其他 Windows / Linux 对象保持原字节。
    pattern = r'<(?:\w+:)?(?:twoCellAnchor|oneCellAnchor)\b[^>]*>.*?</(?:\w+:)?(?:twoCellAnchor|oneCellAnchor)>'
    def remove(match):
        fragment = match[0]
        start = re.search(r'<(?:\w+:)?from>.*?<(?:(?:\w+):)?row>(\d+)</', fragment, re.S)
        return '' if start and row <= int(start[1]) < row+9 else fragment
    xml = re.sub(pattern, remove, xml, flags=re.S)
    media = f'auto_linux_{index}_{mode}.png'
    rid = 'rAuto_' + mode
    rel_key = f'xl/drawings/_rels/drawing{index}.xml.rels'
    rels = ET.fromstring(data[rel_key])
    for rel in list(rels):
        if rel.get('Id') == rid:
            rels.remove(rel)
    ET.SubElement(rels, f'{{{P}}}Relationship', Id=rid, Type=R+'/image', Target='../media/'+media)
    data[rel_key] = ET.tostring(rels, encoding='utf-8', xml_declaration=True)
    # validate_company_template 已核对四个合并区域；直接用单元格边界定位，
    # 不再复制旧图的固定宽高、偏移和 oneCell 设置。模板行高、列宽不需要修改。
    pic = build_excel_picture_anchor(row+1, rid, 1000+row)
    if re.search(r'<(?:\w+:)?wsDr\b[^>]*/>', xml):
        xml = re.sub(r'(<(?:\w+:)?wsDr\b[^>]*?)/>', r'\1>'+pic+'</xdr:wsDr>', xml)
    else:
        xml = re.sub(r'</(?:\w+:)?wsDr>', lambda m: pic+m[0], xml)
    data[key] = xml.encode('utf-8')
    data['xl/media/'+media] = Path(png).read_bytes()


def fill_company_test_result(source, destination, capacity, mode, statistics, metadata, png, caption):
    data = read_xlsx_parts(source)
    validate_company_template(data)
    index = CAPACITIES.index(capacity)+1
    _, first = BLOCKS[mode]
    values = {f'B{r}': metadata.get(k) or '未提供' for r, k in
              ((44, 'platform'), (45, 'cpu'), (46, 'os'), (48, 'test_version'))}
    values['B47'] = capacity
    values['A44'] = '测试平台： '
    if caption == '最后10min':
        caption = '测试结果（10min内）'
    values[f'C{first-3}'] = caption
    values[f'A{first-3}'] = metadata.get('sampling_label',
        '多路测温仪温度曲线\n(摄氏度，Controller/Nand表面 1s采集1次，标准smart '
        + ('测试10min取一次)' if mode == 'l12_ps4' else '1min采集一次)'))
    for offset, metric in ((0, 'Composite'), (2, 'CPU_Surface'), (4, 'NAND_Surface')):
        values.update({f'{col}{first+offset}': statistics.get(metric, {}).get(key)
                       for col, key in zip('CDE', ('max', 'mean', 'min'))})
    key = f'xl/worksheets/sheet{index}.xml'
    data[key] = replace_template_cells(data[key], values)
    replace_company_chart(data, index, mode, png)
    save_xlsx_parts(data, destination)




# ---------- 沿用公司原图颜色与坐标 ----------
STYLES = {
    'CPU_Surface': ('#D83A56', 'CPU表面', 0),
    'NAND_Surface': ('#FF7F50', 'NAND表面', 0),
    'AMB': ('#457B9D', '环境AMB', 0),
    'Composite': ('#7209B7', '复合温度', 8),
    'CPU': ('#9C6615', 'CPU', -8),
    'NAND': ('#2D9596', 'NAND', -16),
    'BW(MiB/s)': ('#69a3ff', '带宽 BW(MiB/s)', 0),
}


def create_company_temperature_figure(frames, title, start=None, mode=None):
    import pandas as pd
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MultipleLocator

    plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    prepared = []
    for frame, columns, sparse in frames:
        if frame.empty:
            continue
        stamps = pd.to_datetime(frame['Time'], utc=True, format='mixed')
        for metric in columns:
            if metric not in frame:
                continue
            values = pd.to_numeric(frame[metric], errors='raise')
            valid = values.notna() & stamps.notna()
            if valid.any():
                if not all(math.isfinite(v) for v in values[valid]):
                    raise ValueError(f'{metric} 有无穷值')
                series = pd.DataFrame({'Time': stamps, 'Value': values}).sort_values('Time')
                # 每条曲线只画首末有效点之间，内部空值保留断线；不外推末尾。
                valid_positions = series['Value'].notna().to_numpy().nonzero()[0]
                series = series.iloc[valid_positions[0]:valid_positions[-1]+1].reset_index(drop=True)
                prepared.append((metric, series, sparse))
    origin = pd.to_datetime(start, utc=True) if start is not None else min(
        (s.Time.min() for _, s, _ in prepared), default=pd.Timestamp('1970-01-01', tz='UTC'))
    fig, ax = plt.subplots(figsize=(10, 6))
    temperatures, bandwidth = [], []
    bandwidth_data = []
    for metric, data, sparse in prepared:
        color, label, offset = STYLES[metric]
        seconds = (data.Time - origin).dt.total_seconds()
        values = data.Value
        is_bw = metric == 'BW(MiB/s)'
        if is_bw:
            bandwidth.extend(values.dropna().tolist())
            bandwidth_data.append((seconds,values))
            continue
        ax.plot(seconds, values, color=color, label=label,
                  linewidth=1.5 if sparse else 2,
                  linestyle='--' if sparse else '-', alpha=.8 if sparse else 1)
        temperatures.extend(values.dropna().tolist())
        if sparse:
            # 只标实际采样点。虚线连接这些点，不往明细表补造 SMART 数据。
            ax.scatter(seconds, values, color=color, s=60, zorder=5)
            for i in range(len(data)):
                if pd.isna(values.iloc[i]):
                    continue
                ax.text(seconds.iloc[i], values.iloc[i] + offset, f'{values.iloc[i]:.0f}',
                        fontsize=10, color=color, ha='center', va='center')
    # 公司低功耗原图不显示空的带宽轴；读写测试仍保留该轴，缺数据不会假画成零。
    right = ax.twinx() if bandwidth_data or mode in ('read_1m', 'write_1m') else None
    for seconds,values in bandwidth_data:
        right.plot(seconds,values,color=STYLES['BW(MiB/s)'][0],linewidth=1,label=STYLES['BW(MiB/s)'][1])
    # 默认与公司原图相同；超范围的真实值必须可见，不能被固定坐标裁掉。
    low = min(0, math.floor(min(temperatures, default=0)/10)*10)
    high = max(110, math.ceil((max(temperatures, default=0)+10)/10)*10)
    bw_high = max(10000, math.ceil(max(bandwidth, default=0)/1000)*1000)
    ax.set_ylim(low, high)
    ax.yaxis.set_major_locator(MultipleLocator(10))
    ax.set_ylabel('温度 (℃)', fontsize=14)
    if right is not None:
        right.set_ylim(0, bw_high)
        right.yaxis.set_major_locator(MultipleLocator(1000 if bw_high <= 15000 else 2000))
        right.set_ylabel('带宽 (MiB/s)', fontsize=14)
    if mode != 'l0_ps4':
        ax.set_xlabel('时间(秒)', fontsize=14, labelpad=10)
    ax.grid(alpha=.2)
    (right if right is not None else ax).set_title(title, fontsize=16, pad=20)
    lines, labels = ax.get_legend_handles_labels()
    more, more_labels = right.get_legend_handles_labels() if right is not None else ([], [])
    if lines or more:
        ax.legend(lines+more, labels+more_labels, loc='upper center', bbox_to_anchor=(.5, -.12),
                  ncol=7, fontsize=11)
    fig.tight_layout()
    return fig


def save_company_temperature_chart(frames, destination, title, subtitle='', start=None, mode=None):
    """时间范围记录在统计 JSON/HTML 中；图内沿用公司原有单行标题。"""
    import matplotlib.pyplot as plt
    fig = create_company_temperature_figure(frames, title, start=start, mode=mode)
    try:
        fig.savefig(destination, dpi=300, bbox_inches='tight')
    finally:
        plt.close(fig)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('操作未完成：'+str(error))
        sys.exit(1)
