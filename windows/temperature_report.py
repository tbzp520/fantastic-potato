"""独立复算：原始CSV → 按时间对齐 → 连续选点 → 统计 → 绘图。

可直接复制本文件和数据到另一目录运行，不依赖测试主程序或硬件。
"""
import bisect
import csv
import json
import math
import statistics
from pathlib import Path
from datetime import datetime, timezone, timedelta
from html import escape

BEIJING = timezone(timedelta(hours=8))
TEMPERATURES = ('Composite', 'CPU', 'NAND', 'CPU_Surface', 'NAND_Surface', 'AMB')
SURFACE = ('CPU_Surface', 'NAND_Surface', 'AMB')
LABELS = {'idle': 'Windows_待机', 'local_video': 'Windows_本地视频',
          'online_video': 'Windows_在线视频', 'bit': 'Windows_BIT'}


def write_csv(path, rows, columns):
    with Path(path).open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def column_index(header, selector):
    if isinstance(selector, str):
        matches = [i for i, name in enumerate(header) if name == selector]
        if len(matches) != 1:
            raise ValueError(f'列 {selector!r} 有 {len(matches)} 项；重名时需指定name/occurrence')
        return matches[0]
    if not isinstance(selector, dict) or set(selector) != {'name', 'occurrence'}:
        raise ValueError('温度列须为精确列名，或{name, occurrence}')
    matches = [i for i, name in enumerate(header) if name == selector['name']]
    n = selector['occurrence']
    if type(n) is not int or not 1 <= n <= len(matches):
        raise ValueError(f'未找到列：{selector}')
    return matches[n-1]


def read_hwinfo(path, settings):
    """保留原始行号，不把空值变0，不把表头尾注当成温度。"""
    records, errors = [], []
    with Path(path).open(encoding=settings['encoding'], newline='') as stream:
        reader = csv.reader(stream, delimiter=settings['delimiter'], strict=True)
        header = next(reader)
        if settings.get('expected_header') and header!=settings['expected_header']:
            raise ValueError('HWiNFO表头与已核对日志不同，请重新核对系统盘列映射，不能沿用同名列序号')
        di = column_index(header, settings['date_column'])
        ti = column_index(header, settings['time_column'])
        columns = {k: column_index(header, v) for k, v in settings['columns'].items() if v is not None}
        if 'Composite' not in columns or len(set(columns.values())) != len(columns):
            raise ValueError('必须映射Composite，且不同温度不得映射同一列')
        previous = -math.inf
        expected_group = settings.get('expected_sensor_group')
        found_group = False
        for line, values in enumerate(reader, 2):
            if not values or values == header:
                continue
            # HWiNFO停止记录后附带盘分组行，它不是缺失温度或损坏数据。
            if (len(values) == len(header) and not values[di].strip() and not values[ti].strip()
                    and all(values[index].strip().startswith('S.M.A.R.T.:') for index in columns.values())):
                groups = {values[index].strip() for index in columns.values()}
                if len(groups) != 1 or (expected_group and groups != {expected_group}):
                    raise ValueError('SMART所属盘与已确认配置不一致，请换盘后重新导入CSV核对：'+str(sorted(groups)))
                found_group = True
                continue
            try:
                if len(values) != len(header):
                    raise ValueError('字段数与表头不一致')
                stamp = datetime.strptime(values[di].strip()+' '+values[ti].strip(),
                                          settings['datetime_format']).replace(tzinfo=BEIJING).timestamp()
                if stamp <= previous:
                    raise ValueError('时间重复或倒退')
                previous = stamp
                row = {'epoch': stamp, 'source_row': line}
                for name, index in columns.items():
                    try:
                        number = float(values[index].strip().replace(settings['decimal_mark'], '.'))
                        row[name] = number if math.isfinite(number) and -100 <= number <= 200 else None
                    except ValueError:
                        row[name] = None
                records.append(row)
            except (ValueError, IndexError) as exc:
                errors.append({'source': 'HWiNFO', 'source_row': line, 'reason': str(exc)})
        if expected_group and not found_group:
            raise ValueError('CSV缺少已确认的SMART盘分组，请停止HWiNFO日志后提供完整文件核对所属盘')
    return records, errors


def align_and_select(smart, surface, start_epoch, end_epoch, interval, count,
                     time_tolerance=2.0, surface_tolerance=.75, required_smart=('Composite',)):
    """连续指连续采样时刻，不是把缺失行删掉之后的连续下标。"""
    if end_epoch <= start_epoch or interval <= 0 or count < 2:
        raise ValueError('测试时间、采样间隔或点数无效')
    smart = [dict(row) for row in smart if start_epoch <= row['epoch'] < end_epoch]
    surface = sorted((row for row in surface if start_epoch <= row['epoch'] < end_epoch), key=lambda r: r['epoch'])
    times = [r['epoch'] for r in surface]
    errors, aligned = [], []
    if not smart:
        return [], [], [{'source': 'HWiNFO', 'epoch': start_epoch, 'reason': '正式区间没有采样'}]
    anchor = smart[0]['epoch']
    if anchor - start_epoch > interval + time_tolerance:
        errors.append({'source': 'HWiNFO', 'epoch': start_epoch, 'reason': '首个采样到达过晚'})
    previous = None
    used_surface = set()
    for record in smart:
        stamp = record['epoch']
        if previous is not None:
            gap = stamp - previous
            if gap > interval + time_tolerance:
                missing = previous + interval
                while missing < stamp - time_tolerance:
                    errors.append({'source': 'HWiNFO', 'epoch': missing, 'reason': '预期采样缺失'})
                    missing += interval
        previous = stamp
        pos = bisect.bisect_left(times, stamp)
        candidates = [j for j in (pos-1, pos) if 0 <= j < len(times)]
        nearest = min(candidates, key=lambda j: abs(times[j]-stamp)) if candidates else None
        row = dict(record)
        row['time_beijing'] = datetime.fromtimestamp(stamp, BEIJING).isoformat(timespec='milliseconds')
        row['surface_row'] = None
        if nearest is not None and abs(times[nearest]-stamp) <= surface_tolerance and nearest not in used_surface:
            row.update({name: surface[nearest].get(name) for name in SURFACE})
            row['surface_row'] = surface[nearest].get('source_row', nearest+1)
            row['align_error_s'] = times[nearest]-stamp
            used_surface.add(nearest)
        row['valid'] = all(finite(row.get(name)) for name in (*required_smart, *SURFACE))
        if not row['valid']:
            errors.append({'source': '对齐', 'epoch': stamp, 'reason': '必需温度缺失或JK808未在容差内对齐'})
        aligned.append(row)
    # 末端没有SMART时，按其实际采样相位列出遗漏的预期时刻。
    expected = smart[-1]['epoch'] + interval
    while expected < end_epoch - time_tolerance:
        errors.append({'source': 'HWiNFO', 'epoch': expected, 'reason': '末端预期采样缺失'})
        expected += interval
    best, best_score = [], None
    for left in range(len(aligned)-count+1):
        window = aligned[left:left+count]
        if not all(r['valid'] for r in window):
            continue
        if any(abs((b['epoch']-a['epoch'])-interval) > time_tolerance for a, b in zip(window, window[1:])):
            continue
        values = [r['Composite'] for r in window]
        # 同极差时采用最早的一段，规则写入输出，避免不可复现的挑点。
        score = (max(values)-min(values), window[0]['epoch'])
        if best_score is None or score < best_score:
            best, best_score = window, score
    selected = [dict(r, plot_second=r['epoch']-best[0]['epoch']) for r in best]
    if not selected:
        errors.append({'source': '选点', 'reason': f'没有符合要求的连续{count}点，不生成合格统计'})
    return aligned, selected, errors


def check_surface_gaps(rows, start, end):
    """按采样线程的秒槽检查；不把跨整秒边界的毫秒抖动当成缺点。"""
    if not rows:
        return [{'source': 'JK808', 'epoch': start, 'reason': '没有任何JK808记录'}]
    errors = []
    records = sorted(rows, key=lambda r: r['epoch'])
    has_slots = all(type(r.get('slot')) is int for r in records)
    # 老数据没有slot时仍能复算：以实际首条记录为相位，按最近秒匹配。
    anchor = statistics.median(r['epoch']-r['slot'] for r in records) if has_slots else records[0]['epoch']
    occupied = {}
    for r in records:
        slot = r['slot'] if has_slots else round(r['epoch']-anchor)
        planned = anchor+slot
        if not start-1e-6 <= planned < end-1e-6:
            continue
        if slot in occupied:
            errors.append({'source':'JK808', 'epoch':r['epoch'], 'reason':'重复秒级记录'})
        occupied[slot] = r
        if abs(r['epoch']-planned)>.75:
            errors.append({'source':'JK808', 'epoch':r['epoch'], 'reason':'秒级记录时刻偏移超过0.75秒'})
        if r.get('error') or any(not finite(r.get(k)) for k in SURFACE):
            errors.append({'source':'JK808', 'epoch':r['epoch'], 'reason':'秒级记录温度无效'})
    # 仅检查实际采样相位内应有的点，末端不足一秒且尚未到采样时刻时不虚报。
    for slot in range(math.ceil(start-anchor-1e-6), math.ceil(end-anchor-1e-6)):
        if slot not in occupied:
            errors.append({'source':'JK808', 'epoch':anchor+slot, 'reason':'缺少秒级记录'})
    return errors


def calculate_statistics(selected):
    statistics = {}
    for name in TEMPERATURES:
        values = [r[name] for r in selected if finite(r.get(name))]
        # 缺列不强行用剩余点数计算一份看起来完整的结果。
        if selected and len(values) == len(selected):
            statistics[name] = {'max':max(values), 'mean':sum(values)/len(values),
                                'min':min(values), 'count':len(values)}
    return statistics


def create_selected_points_figure(selected, title):
    """沿用 Linux 图的配色、尺寸及 SMART 标注；只使用已选中的连续点。"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MultipleLocator

    plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    if not selected:
        raise ValueError('没有选中点，不能绘图')
    # offset 是文字与采样点的纵向距离，不改变真实曲线数值。
    styles = [('CPU_Surface','#D83A56','CPU表面',0),('NAND_Surface','#FF7F50','NAND表面',0),
              ('AMB','#457B9D','环境AMB',0),('Composite','#7209B7','复合温度',8),
              ('CPU','#9C6615','CPU',-8),('NAND','#2D9596','NAND',-16)]
    seconds = [r['plot_second'] for r in selected]
    fig, ax = plt.subplots(figsize=(10, 6))
    extents = [0, 110]
    label_heights = [[] for _ in selected]
    for name, color, label, offset in styles:
        values = [r[name] if finite(r.get(name)) else math.nan for r in selected]
        if not any(finite(v) for v in values):
            continue
        smart = name in ('Composite','CPU','NAND')
        ax.plot(seconds, values, color=color, label=label,
                linewidth=1.5 if smart else 2, linestyle='--' if smart else '-', alpha=.8 if smart else 1)
        extents.extend(v for v in values if finite(v))
        if smart:
            ax.scatter(seconds, values, color=color, s=60, zorder=5)
            for index, (second, value) in enumerate(zip(seconds, values)):
                if finite(value):
                    # 温度相近时，固定偏移可能碰到另一条曲线，稍移文字而不移动采样点。
                    occupied = [selected[index][key] for key in TEMPERATURES if finite(selected[index].get(key))]
                    occupied += label_heights[index]
                    height = value+offset
                    direction = 1 if offset > 0 else -1
                    while any(abs(height-other) < 3 for other in occupied):
                        height += direction*3
                    label_heights[index].append(height)
                    ax.text(second, height, f'{value:.0f}', fontsize=10, color=color,
                            ha='center', va='center', zorder=8,
                            bbox=dict(facecolor='white', edgecolor='none', pad=.2, alpha=.85))
                    extents.extend((height-3, height+3))
    ax.set_ylim(math.floor(min(extents)/10)*10, math.ceil(max(extents)/10)*10)
    ax.yaxis.set_major_locator(MultipleLocator(10))
    # 留白只让首末圆点和数字完整可见，曲线仍到最后一个真实选中点为止。
    ax.margins(x=.035)
    ax.set_xlabel('时间(秒)', fontsize=14, labelpad=10)
    ax.set_ylabel('温度 (℃)', fontsize=14)
    ax.set_title(title, fontsize=16, pad=20)
    ax.grid(alpha=.2)
    ax.legend(loc='upper center', bbox_to_anchor=(.5,-.12), ncol=6, fontsize=11)
    fig.tight_layout()
    return fig


def draw_selected_points(selected, path, title):
    import matplotlib.pyplot as plt
    fig = create_selected_points_figure(selected, title)
    try:
        fig.savefig(path, dpi=300, bbox_inches='tight')
    finally:
        plt.close(fig)


def build_stage_report(folder):
    """folder中只需conditions.json、hwinfo.csv、jk808.jsonl即可独立复算。"""
    folder = Path(folder)
    conditions = json.loads((folder/'conditions.json').read_text(encoding='utf-8'))
    smart, errors = read_hwinfo(folder/'hwinfo.csv', conditions['hwinfo'])
    surface = [json.loads(line) for line in (folder/'jk808.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    aligned, selected, missing = align_and_select(smart, surface, conditions['start_epoch'],
        conditions['end_epoch'], conditions['interval_s'], conditions['points'],
        conditions.get('time_tolerance_s',2), conditions.get('surface_tolerance_s',.75),
        conditions['hwinfo'].get('required_metrics',['Composite']))
    errors += missing + check_surface_gaps(surface, conditions['start_epoch'], conditions['end_epoch'])
    notices = []
    events_path=folder/'events.jsonl'
    if events_path.exists():
        for line in events_path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                event=json.loads(line)
                if event.get('notice'):
                    notices.append({'source':'在线视频提示','epoch':event['epoch'],'reason':event['notice']})
                if event.get('issue'):
                    errors.append({'source':'负载运行','epoch':event['epoch'],'reason':event['issue']})
    workload_status=conditions.get('workload_status','未记录负载状态')
    workload_ok=workload_status=='完成计时' or conditions.get('simulated',False)
    if not workload_ok:
        errors.append({'source':'负载运行','reason':workload_status})
    for e in errors+notices:
        if 'epoch' in e:
            e['time_beijing'] = datetime.fromtimestamp(e['epoch'],BEIJING).isoformat(timespec='milliseconds')
    columns = ['source_row','epoch','time_beijing','surface_row','align_error_s','plot_second','valid',*TEMPERATURES]
    write_csv(folder/'对齐基础数据.csv', aligned, columns)
    write_csv(folder/'选中绘图及统计点.csv', selected, columns)
    write_csv(folder/'缺失及异常明细.csv', errors, ['source','source_row','epoch','time_beijing','reason'])
    write_csv(folder/'播放提示明细.csv', notices, ['source','epoch','time_beijing','reason'])
    statistics = calculate_statistics(selected)
    result = {'selected_count':len(selected), 'required_count':conditions['points'],
              'statistics':statistics, 'data_issue_count':len(errors), 'playback_notice_count':len(notices),
              'status':'有效选点（有全程异常）' if selected and errors else ('有效选点' if selected else '数据不足'),
              'selection':'Composite极差最小的连续窗口；同分取最早窗口',
              'workload_status':workload_status,'workload_ok':workload_ok,
              'simulated':conditions.get('simulated',False)}
    if not workload_ok:
        result['status']='负载异常，统计仅供复核' if selected else '负载异常且数据不足'
    (folder/'statistics.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result, selected


def artifact_stem(label, capacity='', simulated=False):
    if capacity and capacity not in ('512G', '1T', '2T'):
        raise ValueError('报告容量无效，不能用上一张盘的容量命名')
    return label+('_'+capacity if capacity else '')+('_模拟数据' if simulated else '')


def clear_generated_stage_report(folder, output, mode, simulated=False, capacity=''):
    """只清除本项可重新计算的产物；原始CSV、JK808、条件和事件一律保留。"""
    folder, output = Path(folder), Path(output)
    title = artifact_stem(LABELS[mode], capacity, simulated)
    for name in ('对齐基础数据.csv','选中绘图及统计点.csv','缺失及异常明细.csv','播放提示明细.csv','statistics.json'):
        (folder/name).unlink(missing_ok=True)
    for suffix in ('.png','.html'):
        (output/(title+suffix)).unlink(missing_ok=True)
    return title


def write_stage_failure(folder, output, mode, error, simulated=False, capacity=''):
    title = clear_generated_stage_report(folder,output,mode,simulated,capacity)
    result = {'status':'报告生成失败','error':str(error),'selected_count':0,
              'statistics':{},'data_issue_count':1,'workload_ok':False,'simulated':simulated}
    Path(folder).mkdir(parents=True,exist_ok=True)
    Path(output).mkdir(parents=True,exist_ok=True)
    (Path(folder)/'statistics.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
    (Path(output)/(title+'.html')).write_text('<!doctype html><meta charset="utf-8"><h1>'+escape(title)+
        '</h1><p>报告生成失败，本项旧图和统计已作废。</p><pre>'+escape(str(error))+'</pre>',encoding='utf-8')
    return result


def export_stage(folder, output, mode=None, simulated=False, capacity=''):
    """外层流程提供mode，原始文件丢失/损坏时也能准确清除本项旧图。"""
    folder, output = Path(folder), Path(output)
    if mode is None:
        mode = folder.name if folder.name in LABELS else json.loads((folder/'conditions.json').read_text('utf-8'))['mode']
    try:
        conditions = json.loads((folder/'conditions.json').read_text('utf-8'))
        simulated = conditions.get('simulated',simulated)
        if conditions['mode']!=mode:
            raise ValueError('数据目录与测试项目不一致，不能混用统计')
        clear_generated_stage_report(folder,output,mode,simulated,capacity)
        return export_valid_stage(folder,output,capacity)
    except Exception as exc:
        write_stage_failure(folder,output,mode,exc,simulated,capacity)
        raise


def export_valid_stage(folder, output, capacity=''):
    folder, output = Path(folder), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    conditions = json.loads((folder/'conditions.json').read_text(encoding='utf-8'))
    result, selected = build_stage_report(folder)
    title = artifact_stem(LABELS[conditions['mode']], capacity, result['simulated'])
    image_path = output/(title+'.png')
    if selected:
        draw_selected_points(selected,image_path,title)
    else:
        image_path.unlink(missing_ok=True)
    body = '<!doctype html><meta charset="utf-8"><title>'+escape(title)+'</title>'
    body += '<style>body{font:16px sans-serif;margin:32px}img{max-width:100%}td,th{padding:8px;border:1px solid #bbb}table{border-collapse:collapse}</style>'
    body += '<h1>'+escape(title)+'</h1><p>'+escape(result['status'])+f'；所选{len(selected)}/{conditions["points"]}点；异常{result["data_issue_count"]}项。</p>'
    if selected:
        body += '<img src="'+escape(image_path.name)+'"><table><tr><th>测量项</th><th>最大</th><th>平均</th><th>最小</th></tr>'
        for name, value in result['statistics'].items():
            body += '<tr><td>'+name+'</td>'+''.join(f'<td>{value[k]:.3f}</td>' for k in ('max','mean','min'))+'</tr>'
        body += '</table>'
    body += '<p>按所选连续点统计，原始数据、选点表和缺失时间明细在手工复测文件夹。</p>'
    if result.get('playback_notice_count'):
        body += '<p>播放缓冲/观测提示：'+str(result['playback_notice_count'])+'次；计时和采样未暂停，未据此判失败。具体北京时间见手工复测中的播放提示明细.csv。</p>'
    if 'workload_status' in conditions:
        body += '<p>负载状态：'+escape(conditions['workload_status'])+'</p>'
    (output/(title+'.html')).write_text(body,encoding='utf-8')
    return result, image_path if selected else None


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='复算指定项目数据目录；输出到该目录的复算结果子目录')
    parser.add_argument('folder')
    args = parser.parse_args()
    result, _ = export_stage(args.folder, Path(args.folder)/'复算结果')
    print(json.dumps(result, ensure_ascii=False,indent=2))
