"""读写报告：原始5秒SMART/逐秒表面温度 → 最后600秒每30秒一个真实点。

无插值、无填充；不从别的SMART时刻补缺失点。带宽按真实fio相位找最近点。
同一函数供测试程序和离线复算使用，所有未选原值仍保留在原始数据中。
"""
import math

METRICS = ['CPU_Surface', 'NAND_Surface', 'AMB', 'Composite', 'CPU', 'NAND', 'BW(MiB/s)']
PROFILE = 'smart5_plot30_last600_v1'


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def migrate_rw_sampling(cfg):
    key = 'smart5_plot30_20261108'
    if cfg.get('config_updates', {}).get(key):
        return False
    tests = cfg.get('tests', {})
    if not all(isinstance(tests.get(mode), dict) for mode in ('read_1m', 'write_1m')):
        return False
    for mode in ('read_1m', 'write_1m'):
        tests[mode].update(smart_interval_s=5, report_interval_s=30, report_points=20,
                           sampling_profile=PROFILE)
    cfg.setdefault('config_updates', {})[key] = True
    return True


def selected_report_rows(raw_rows, duration, interval=30, count=20, bandwidth_tolerance=.75):
    if (type(interval) is not int or interval != 30 or type(count) is not int or count != 20
            or not finite(duration) or duration < interval*count):
        raise ValueError('读写报告要求最后600秒，每30秒取1点，共20点')
    begin = duration-interval*count
    by_second = {}
    bandwidth = []
    for row in raw_rows:
        second = row.get('Second')
        if not finite(second):
            continue
        by_second.setdefault(second, []).append(row)
        if finite(row.get('BW(MiB/s)')):
            bandwidth.append(row)
    selected, issues = [], []
    for index in range(count):
        second = begin+index*interval
        originals = by_second.get(second, [])
        row = dict(originals[0]) if len(originals) == 1 else {'Second': second}
        if len(originals) != 1:
            issues.append(f'绘图计划第{second:g}秒缺少唯一原始记录，未从其他秒补温度')
        for metric in METRICS:
            if not finite(row.get(metric)):
                row[metric] = None
        # fio经SSH启动后可能有小数秒相位偏移；只取真实最近带宽，不插值。
        candidates = sorted((abs(item['Second']-second), number, item)
                            for number, item in enumerate(bandwidth)
                            if abs(item['Second']-second) <= bandwidth_tolerance)
        if candidates and (len(candidates) == 1 or abs(candidates[0][0]-candidates[1][0]) > 1e-9):
            source = candidates[0][2]
            row['BW(MiB/s)'] = source['BW(MiB/s)']
            row['带宽原始相对秒'] = source['Second']
            row['带宽_实际接收时间'] = source.get('带宽_实际接收时间', '')
        else:
            row['BW(MiB/s)'] = None
        row['Second'] = second
        for metric in METRICS:
            if row[metric] is None:
                issues.append(f'绘图计划第{second:g}秒 {metric} 缺失；不补零、不移动后续点')
        selected.append(row)
    return selected, issues
