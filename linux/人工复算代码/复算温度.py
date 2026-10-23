# -*- coding: utf-8 -*-
"""人工复算：只读上一层“手工复测”里的原始数据，不连接 Linux，不接温度仪，不运行测试。

只用 Python 标准库，无需安装其他库。顺序：读原值 → 判定剔除 → 取最后600秒 → 求统计。
输入输出字段均在下方直接展开，方便测试人员审查、修改。
运行：python 复算温度.py    或    python 复算温度.py "D:/某次结果/手工复测"
"""
import csv
import hashlib
import json
import math
from pathlib import Path
import sys

METRICS = ['CPU_Surface','NAND_Surface','AMB','Composite','CPU','NAND','BW(MiB/s)']


def read_measurements(path):
    """空白保留 None，真实的零仍为 0，不能用零填补缺失值。"""
    with path.open(encoding='utf-8-sig',newline='') as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        for field in ['Second']+METRICS:
            value = row.get(field,'')
            row[field] = float(value) if value else None
            if row[field] is not None and not math.isfinite(row[field]):
                raise ValueError(f'{path.name} 中 {field} 不是有限数值')
        if row['Second'] is None:
            raise ValueError(path.name+' 缺少正式测试相对秒 Second')
    return rows


def find_excluded_smart_seconds(smart_rows, conditions):
    """仅读写前两次 Composite 与第三次及以后均值比较。L0/L1.2 不剔除。"""
    if conditions['mode'] not in ('read_1m','write_1m'):
        return [], None
    interval = conditions['smart_interval_s']
    later_values = [row['Composite'] for row in smart_rows
                    if row['Second']>=2*interval and row['Composite'] is not None]
    if not later_values:
        return [], None  # 无法判断时保留；缺失的第0点不能让第60点改名为第0点。
    later_mean = sum(later_values)/len(later_values)
    excluded = []
    for row in smart_rows:
        if row['Second'] in (0,interval) and row['Composite'] is not None:
            difference = abs(row['Composite']-later_mean)
            if difference>=conditions['outlier_threshold_c']:
                excluded.append({'原始相对秒':row['Second'],'Composite':row['Composite'],
                                 '后续均值':later_mean,'差值绝对值':difference})
    return excluded, later_mean


def calculate_last_ten_minutes(raw_rows, excluded, conditions):
    """每个指标独立计数；平均 = 有效数值之和 / 有效数量。图的新零点不参与计算。"""
    end = conditions['duration_s']
    begin = max(0,end-conditions['statistics_window_s'])
    excluded_seconds = {row['原始相对秒'] for row in excluded}
    excluded_metrics = ['Composite','CPU','NAND'] if conditions['initial_smart_scope']=='round' else ['Composite']
    calculation_rows = []
    for original in raw_rows:
        if not begin<=original['Second']<end:
            continue
        row = dict(original)
        if row['Second'] in excluded_seconds:
            for metric in excluded_metrics:
                row[metric] = None
        calculation_rows.append(row)
    statistics = {}
    for metric in METRICS:
        values = [row[metric] for row in calculation_rows if row[metric] is not None]
        statistics[metric] = {'count':len(values),'max':max(values) if values else None,
                              'mean':sum(values)/len(values) if values else None,
                              'min':min(values) if values else None}
    return statistics, calculation_rows


def write_csv(path, columns, rows):
    with path.open('w',encoding='utf-8-sig',newline='') as stream:
        writer = csv.DictWriter(stream,fieldnames=columns,extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def recalculate(data_folder):
    """计算只读三个输入文件。最后才读取程序统计结果对照，避免把已有结果当复算。"""
    conditions = json.loads((data_folder/'本次复算条件.json').read_text('utf-8'))
    raw_rows = read_measurements(data_folder/'完整基础数据.csv')
    smart_rows = read_measurements(data_folder/'SMART原始计划点.csv')
    excluded, later_mean = find_excluded_smart_seconds(smart_rows,conditions)
    statistics, calculation_rows = calculate_last_ten_minutes(raw_rows,excluded,conditions)
    output = (data_folder if data_folder.name=='手工复测' else data_folder.parent)/'人工复算结果'
    output.mkdir(exist_ok=True)
    write_csv(output/'复算参与计算的数据.csv',['Time','Second']+METRICS,calculation_rows)
    write_csv(output/'复算剔除记录.csv',['原始相对秒','Composite','后续均值','差值绝对值'],excluded)
    summary = []
    for metric,values in statistics.items():
        item = {'指标':metric,'有效点数':values['count']}
        for key,label in [('max','最大值'),('mean','平均值'),('min','最小值')]:
            value = values[key]
            item[label+'原值'] = value
            item[label+'显示值'] = round(value,conditions['decimal_places']) if value is not None else None
        summary.append(item)
    write_csv(output/'复算汇总.csv',['指标','有效点数','最大值原值','平均值原值','最小值原值',
                                 '最大值显示值','平均值显示值','最小值显示值'],summary)
    (output/'复算统计.json').write_text(json.dumps(statistics,ensure_ascii=False,indent=2,allow_nan=False),'utf-8')
    differences = []
    reference_file = data_folder/'程序统计结果_仅供对照.json'
    if reference_file.exists():
        reference = json.loads(reference_file.read_text('utf-8'))
        for metric in METRICS:
            for key,value in statistics[metric].items():
                saved = reference[metric][key]
                same = (value is None and saved is None) or (
                    value is not None and saved is not None and math.isclose(value,saved,rel_tol=1e-12,abs_tol=1e-9))
                if not same:
                    differences.append(f'{metric} {key}：复算={value}，原程序={saved}')
    checks = []
    manifest = data_folder/'原始输入校验.json'
    if manifest.exists():
        for filename,digest in json.loads(manifest.read_text('utf-8')).items():
            same = hashlib.sha256((data_folder/filename).read_bytes()).hexdigest()==digest
            checks.append(filename+'：'+('与出表时一致' if same else '已被修改，请核对'))
    lines = ['人工复算结果（使用原始数据独立计算）',
             f"范围：正式测试第 {max(0,conditions['duration_s']-600)} 秒至第 {conditions['duration_s']} 秒，不含终点。",
             f'后续 Composite 均值：{later_mean}；剔除点：{[row["原始相对秒"] for row in excluded]}',
             '公司温度模版对应：Composite=复合温度；CPU_Surface=CPU表面；NAND_Surface=NAND表面。',
             'CPU、NAND 为 SMART 传感器温度；AMB 为环境温度；BW(MiB/s) 为带宽，不能互换。',
             '先看“复算汇总.csv”；原值不舍入，显示值按本次模板位数舍入，空白不是0。',
             '数据缺失详情见输入目录的缺失数据时间点.csv；复算不会补造缺失值。',
             '与原程序对照：'+('存在差异' if differences else '全部一致' if reference_file.exists() else '没有提供原程序结果'),
             *differences,*checks]
    (output/'复算说明与对照.txt').write_text('\n'.join(lines),'utf-8-sig')
    print('\n'.join(lines))
    print('复算结果保存到：'+str(output))
    return statistics


if __name__=='__main__':
    try:
        parent = Path(__file__).resolve().parent.parent
        default = parent if (parent/'本次复算条件.json').exists() else parent/'复算数据'
        folder = Path(sys.argv[1]).resolve() if len(sys.argv)>1 else default
        recalculate(folder)
    except Exception as error:
        print('复算未完成：'+str(error))
        sys.exit(1)
