"""沿用已收到的C盘.bitcfg，接入官方脚本并实际运行2分钟验证。

配置、启动、停止、结果归档由程序完成；不要求操作员重新填写路径。
本次是功能验证，不替代正式15分钟BIT；不切换网卡、不修改系统时间。
"""
import copy
from datetime import datetime
import json
from pathlib import Path
import time
import traceback
import uuid

import windows_temperature_test as app
from 补采软件窗口 import make_archive


def confirmed_bit_configuration(cfg):
    """仅更新BIT配置项，HWiNFO、1T盘、COM口和网卡等设置原样保留。"""
    updated = copy.deepcopy(cfg)
    bit = updated['burnintest']
    supplied = app.ROOT/'工具配置'/'已确认_C盘.bitcfg'
    if not supplied.is_file() or app.sha256(supplied) != app.CONFIRMED_BIT_CONFIG_SHA256:
        raise RuntimeError('更新包中的已确认BIT配置缺失或内容改变，请重新完整解压更新包')
    existing = Path(bit.get('configuration') or '未填写.bitcfg')
    if existing.is_file() and app.sha256(existing) != app.CONFIRMED_BIT_CONFIG_SHA256:
        raise RuntimeError('当前BIT配置已另行修改，保留原配置未覆盖；本包对应10月9日提供的LastUsed.bitcfg')
    executable = Path(bit.get('executable',''))
    if not executable.is_file():
        executable = Path('C:/Program Files/BurnInTest/bit.exe')
    if not executable.is_file():
        raise RuntimeError('未找到已安装的C:/Program Files/BurnInTest/bit.exe')
    bit.update(executable=str(executable.resolve()), configuration=str(supplied.resolve()),
               configuration_sha256=app.CONFIRMED_BIT_CONFIG_SHA256,
               system_disk_file_test_verified=True, adapter='bit9_script')
    bit.pop('script_verification', None)
    return updated


def check_bit_hwinfo_csv(path, cfg, result):
    rows, errors = app.read_hwinfo(path, cfg['hwinfo'])
    if errors:
        raise RuntimeError('BIT期间HWiNFO CSV有无法解析的记录：'+str(errors[:3]))
    rows = [row for row in rows if result['start_epoch'] <= row['epoch'] <= result['end_epoch']]
    if len(rows) < 3:
        raise RuntimeError('BIT实际运行期间不足3条HWiNFO温度记录')
    if any(row.get(name) is None for row in rows for name in ('Composite','CPU','NAND')):
        raise RuntimeError('BIT实际运行期间存在缺失的温度值')
    gaps = [b['epoch']-a['epoch'] for a,b in zip(rows, rows[1:])]
    if any(abs(gap-30)>2 for gap in gaps):
        raise RuntimeError('BIT期间HWiNFO没有按30秒连续记录：'+str(gaps))
    return {'rows_during_bit':len(rows), 'actual_gaps_s':gaps, 'csv_sha256':app.sha256(path),
            'sensor_group':cfg['hwinfo']['expected_sensor_group']}


def main():
    print('Windows '+app.VERSION+'：使用已确认配置，验证BIT自动运行和保存结果。', flush=True)
    print('本次实际运行C盘文件测试2分钟，同时记录HWiNFO；不算正式15分钟结果。', flush=True)
    print('请保持U盘已插好、HWiNFO Sensors已打开且旧日志已停止；BIT由本程序启动。', flush=True)
    app.require_administrator()
    cfg = app.load_config()
    folder = app.ROOT/'联调资料'/('BIT自动验证_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6])
    folder.mkdir(parents=True, exist_ok=False)
    report = {'version':app.VERSION,'formal_test_started':False,'bit_duration_s':120,
              'passed':False,'configuration_completed':False}
    workload, updated, csv_path = None, None, None
    try:
        updated = confirmed_bit_configuration(cfg)
        version = app.bit_program_version(updated['burnintest']['executable'])
        if version[:3] != [9,0,1003]:
            raise RuntimeError('当前BIT版本不是已适配的9.0 build1003：'+str(version))
        report['bit_version'] = version
        # 复用已经通过的HWiNFO绑定和验证记录，不重新让操作员验证30/60秒设置。
        problems = app.preflight_host(updated, ['idle'])
        if problems:
            raise RuntimeError('现有HWiNFO配置检查：'+'；'.join(problems))
        from pywinauto import Desktop
        if Desktop(backend='win32').windows(class_name='bit', visible_only=False):
            raise RuntimeError('请关闭已打开的BIT主窗口，然后重新运行本入口；不需要再填写或导出配置')
        app.save_setup_config(updated, 'BIT脚本接入',
                              {'configuration_sha256':app.CONFIRMED_BIT_CONFIG_SHA256,
                               'source':'已提供的LastUsed.bitcfg；C盘文件测试，100%负载'})
        report['configuration_completed'] = True
        app.save_json(folder/'验证结果.json', report)
        print('已接入BIT配置；原有HWiNFO、1T盘、串口、网络配置保留。', flush=True)
        app.launch_hwinfo(updated)
        csv_path = app.start_hwinfo_logging(updated, folder, 30)
        app.ui_window(updated['hwinfo']['ui']).minimize()
        workload = app.start_bit(updated, folder, 120)
        print('BIT脚本已发出C盘测试命令，等待自动结束；无需手动点击Start或Save。', flush=True)
        deadline, next_print = time.monotonic()+120+180, 0
        while workload['process'].poll() is None:
            now = time.monotonic()
            if now >= deadline:
                raise RuntimeError('BIT超过验证时限仍未退出')
            if now >= next_print:
                print(f'BIT验证进度：{max(0,int(now-workload["start_mono"]))}/120秒。', flush=True)
                next_print = now+15
            time.sleep(.2)
        report['bit'] = app.collect_bit_result(workload, require_complete=True)
        app.stop_hwinfo_logging(updated)
        report['hwinfo'] = check_bit_hwinfo_csv(csv_path, updated, report['bit'])
        report['passed'] = True
        proof = {'passed':True,'duration_s':120,'configuration_sha256':app.CONFIRMED_BIT_CONFIG_SHA256,
                 'executable_sha256':app.sha256(updated['burnintest']['executable']),
                 'verified_at':datetime.now().isoformat(timespec='seconds'), 'evidence_folder':str(folder),
                 'scope':'2分钟实际文件读写、自动结束和文字报告；HWiNFO并行30秒记录'}
        updated['burnintest']['script_verification'] = proof
        app.save_setup_config(updated, 'BIT自动验证通过', proof)
    except (Exception, KeyboardInterrupt) as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        (folder/'未完成原因.txt').write_text(traceback.format_exc(), 'utf-8-sig')
        print('本次未通过：'+str(exc), flush=True)
    finally:
        cleanup = []
        if workload:
            try:
                app.stop_bit_script(workload)
            except Exception as exc:
                cleanup.append(str(exc))
            try:
                if not report.get('bit'):
                    app.collect_bit_result(workload, require_complete=False)
            except Exception as exc:
                cleanup.append('保存BIT现场：'+str(exc))
        if updated:
            try:
                app.stop_hwinfo_logging(updated)
            except Exception as exc:
                cleanup.append('停止HWiNFO日志：'+str(exc))
        if cleanup:
            report['passed'] = False
            report['cleanup_errors'] = cleanup
            if updated and 'script_verification' in updated['burnintest']:
                updated['burnintest'].pop('script_verification', None)
                app.save_setup_config(updated, 'BIT验证收尾异常', {'errors':cleanup})
        app.save_json(folder/'验证结果.json', report)
        print('BIT配置和自动执行验证通过。' if report['passed'] else '记录已保留，不会把失败标为通过。')
        print('请发送：'+str(make_archive(folder)), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
