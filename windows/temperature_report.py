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


def sync_surface_source(source, destination):
    """原始全程文件为准，原子更新派生stage副本，修复中断留下的半文件。"""
    import filecmp
    import os
    import shutil
    import tempfile
    source, destination = Path(source), Path(destination)
    if not source.is_file() or source.resolve() == destination.resolve():
        return
    if destination.is_file() and filecmp.cmp(source, destination, shallow=False):
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.jk808-copy-', suffix='.tmp', dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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


def validate_sampling_geometry(raw_interval, plot_interval, count, window_s=None):
    if (not finite(raw_interval) or not finite(plot_interval) or raw_interval <= 0 or plot_interval <= 0
            or type(count) is not int or count < 2):
        raise ValueError('采集间隔、绘图间隔或点数无效')
    ratio = plot_interval/raw_interval
    stride = round(ratio)
    if stride < 1 or abs(ratio-stride) > 1e-8:
        raise ValueError('绘图间隔必须是原始采集间隔的整数倍')
    if window_s is not None and (not finite(window_s) or abs(window_s-count*plot_interval) > 1e-6):
        raise ValueError('完整窗口时长必须等于绘图点数×绘图间隔')
    return stride


def align_and_select(smart, surface, start_epoch, end_epoch, interval, count,
                     time_tolerance=2.0, surface_tolerance=.75, required_smart=('Composite',),
                     *, sample_interval=None, window_s=None):
    """连续指连续采样时刻，不是把缺失行删掉之后的连续下标。"""
    if end_epoch <= start_epoch or interval <= 0 or count < 2:
        raise ValueError('测试时间、采样间隔或点数无效')
    raw_interval = interval if sample_interval is None else sample_interval
    stride = validate_sampling_geometry(raw_interval, interval, count, window_s)
    smart = [dict(row) for row in smart if start_epoch <= row['epoch'] < end_epoch]
    surface = sorted((row for row in surface if start_epoch <= row['epoch'] < end_epoch), key=lambda r: r['epoch'])
    times = [r['epoch'] for r in surface]
    errors, aligned = [], []
    if not smart:
        return [], [], [{'source': 'HWiNFO', 'epoch': start_epoch, 'reason': '正式区间没有采样'}]
    anchor = smart[0]['epoch']
    if anchor - start_epoch > raw_interval + time_tolerance:
        errors.append({'source': 'HWiNFO', 'epoch': start_epoch, 'reason': '首个采样到达过晚'})
    previous = None
    used_surface = set()
    for record in smart:
        stamp = record['epoch']
        if previous is not None:
            gap = stamp - previous
            if gap > raw_interval + time_tolerance:
                missing = previous + raw_interval
                while missing < stamp - time_tolerance:
                    errors.append({'source': 'HWiNFO', 'epoch': missing, 'reason': '预期采样缺失'})
                    missing += raw_interval
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
    expected = smart[-1]['epoch'] + raw_interval
    while expected < end_epoch - time_tolerance:
        errors.append({'source': 'HWiNFO', 'epoch': expected, 'reason': '末端预期采样缺失'})
        expected += raw_interval
    best, best_score = [], None
    # 新规则按半开区间[start,start+600)审查全部5秒原始点，包括最后绘图点之后的原始记录。
    # 历史文件没有window_s时，保留原来的连续count点规则。
    raw_count = count*stride if window_s is not None else (count-1)*stride+1
    for left in range(len(aligned)-raw_count+1):
        raw_window = aligned[left:left+raw_count]
        if window_s is not None and raw_window[0]['epoch']+window_s > end_epoch+1e-6:
            continue
        if not all(r['valid'] for r in raw_window):
            continue
        if any(abs((b['epoch']-a['epoch'])-raw_interval) > time_tolerance for a, b in zip(raw_window, raw_window[1:])):
            continue
        if window_s is not None and any(abs(r['epoch']-raw_window[0]['epoch']-i*raw_interval) > time_tolerance
                                        for i,r in enumerate(raw_window)):
            continue  # 防止每次小幅延迟累计成超过十分钟的120点。
        window = raw_window[::stride][:count]
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
    """统一六路实线图；直接绘制真实选中值，不平滑、不插值。"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MultipleLocator

    plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    if not selected:
        raise ValueError('没有选中点，不能绘图')
    styles = [('CPU_Surface','#FF0000','CPU表面'),('NAND_Surface','#4A91FA','NAND表面'),
              ('AMB','#228B22','环境温度'),('Composite','#7209B7','复合温度'),
              ('CPU','#FF0000','CPU'),('NAND','#4A91FA','NAND')]
    seconds = [r['plot_second'] for r in selected]
    fig, ax = plt.subplots(figsize=(10, 6), dpi=300)
    extents = [0, 110]
    for name, color, label in styles:
        values = [r[name] if finite(r.get(name)) else math.nan for r in selected]
        if not any(finite(v) for v in values):
            continue
        smart = name in ('Composite','CPU','NAND')
        ax.plot(seconds, values, color=color, label=label,
                linewidth=1.7 if smart else .9, linestyle='-', marker=None, alpha=1)
        extents.extend(v for v in values if finite(v))
    ax.set_ylim(math.floor(min(extents)/10)*10, math.ceil(max(extents)/10)*10)
    ax.yaxis.set_major_locator(MultipleLocator(10))
    # 保留少量留白，曲线严格止于最后真实选中点。
    ax.margins(x=.035)
    ax.set_xlabel('时间（秒）', fontsize=14, labelpad=10)
    ax.set_ylabel('温度（℃）', fontsize=14)
    ax.set_title(title, fontsize=16, pad=20)
    ax.grid(alpha=.2)
    ax.legend(loc='upper center', bbox_to_anchor=(.5,-.12), ncol=6, fontsize=11)
    fig.tight_layout()
    return fig


def draw_selected_points(selected, path, title):
    import matplotlib.pyplot as plt
    fig = create_selected_points_figure(selected, title)
    try:
        fig.savefig(path, dpi=300)
    finally:
        plt.close(fig)


def write_readable_raw_data(folder, conditions=None, aligned=(), selected=()):
    """只新增查看副本；全量来自原文件，正式段和选中点只是标记。"""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    conditions = conditions or {}
    settings = conditions.get('hwinfo', {})
    # 启动失败可能尚未产生conditions；配置快照仍可提供原列映射。
    if not settings:
        try:
            settings = json.loads((folder.parent/'config_snapshot.json').read_text('utf-8-sig'))['hwinfo']
        except (OSError, ValueError, KeyError, TypeError):
            pass
    start, end = conditions.get('start_epoch'), conditions.get('end_epoch')
    has_period = finite(start) and finite(end) and end > start
    selected_smart = {r.get('source_row') for r in selected}
    selected_surface = {r.get('surface_row') for r in selected}
    aligned_surface = {r.get('surface_row') for r in aligned}
    issues, smart_rows, surface_rows, raw_headers = [], [], [], []

    def when(stamp):
        try:
            return datetime.fromtimestamp(stamp, BEIJING).isoformat(timespec='milliseconds') if finite(stamp) else ''
        except (OSError, OverflowError, ValueError):
            return ''

    def phase(stamp):
        if not has_period or not finite(stamp):
            return '正式时段未知'
        return '测试前' if stamp < start else ('正式段' if stamp < end else '测试后')

    def issue(source, line, reason, raw=''):
        issues.append({'来源':source, '原始行号':line, '问题':reason, '原始内容':raw})

    smart_path = folder/'hwinfo.csv'
    if smart_path.is_file():
        try:
            with smart_path.open(encoding=settings.get('encoding', 'utf-8-sig'), newline='') as stream:
                reader = csv.reader(stream, delimiter=settings.get('delimiter', ','), strict=True)
                header = next(reader)
                raw_headers = [f'原始列{i+1}：{name}' for i, name in enumerate(header)]
                di = column_index(header, settings.get('date_column', 'Date'))
                ti = column_index(header, settings.get('time_column', 'Time'))
                columns = {}
                for key in ('Composite', 'CPU', 'NAND'):
                    try:
                        columns[key] = column_index(header, settings.get('columns', {}).get(key))
                    except (ValueError, TypeError) as exc:
                        issue('HWiNFO', 1, f'{key}列映射缺失：{exc}')
                skipped_headers, skipped_groups = 0, 0
                for number, values in enumerate(reader, 2):
                    if not values:
                        continue
                    if values == header:
                        skipped_headers += 1
                        continue
                    if (len(values) > max(di, ti) and not values[di].strip() and not values[ti].strip()
                            and any(value.strip().startswith('S.M.A.R.T.:') for value in values)):
                        skipped_groups += 1
                        continue
                    try:
                        stamp = datetime.strptime(values[di].strip()+' '+values[ti].strip(),
                            settings.get('datetime_format', '%d.%m.%Y %H:%M:%S.%f')).replace(tzinfo=BEIJING).timestamp()
                    except (ValueError, IndexError) as exc:
                        issue('HWiNFO', number, '时间无法解析，未冒充测温行：'+str(exc), json.dumps(values, ensure_ascii=False))
                        continue
                    row = {'原始行号':number, '北京时间':when(stamp), 'Unix时间戳':stamp,
                           '时段':phase(stamp), '选中绘图统计点':'是' if number in selected_smart else '否'}
                    missing = []
                    for metric, label in (('Composite','Drive Temperature（Composite复合温度）'),
                            ('CPU','Drive Temperature 2（CPU/SSD主控）'), ('NAND','Drive Temperature 3（NAND闪存）')):
                        index = columns.get(metric)
                        value = values[index] if index is not None and index < len(values) else ''
                        row[label] = value
                        try:
                            number_value = float(value.strip().replace(settings.get('decimal_mark', '.'), '.'))
                            valid = math.isfinite(number_value) and -100 <= number_value <= 200
                        except (ValueError, TypeError):
                            valid = False
                        if not valid:
                            missing.append(metric)
                    if len(values) != len(header):
                        missing.append('字段数不符')
                    row['数据状态'] = '缺失或无效：'+','.join(missing) if missing else '原始记录'
                    row.update({name:values[i] if i < len(values) else '' for i, name in enumerate(raw_headers)})
                    if len(values) > len(header):
                        row['额外原始字段'] = json.dumps(values[len(header):], ensure_ascii=False)
                    smart_rows.append(row)
        except (OSError, UnicodeError, ValueError, csv.Error, StopIteration) as exc:
            issue('HWiNFO', '', '无法完整读取原始文件：'+str(exc))
    else:
        issue('HWiNFO', '', '缺少hwinfo.csv，不能补造SMART原始记录')

    surface_path = folder/'jk808.jsonl'
    if surface_path.is_file():
        try:
            surface_lines = surface_path.read_text('utf-8-sig').splitlines()
        except (OSError, UnicodeError) as exc:
            issue('JK808', '', '无法完整读取原始文件：'+str(exc))
            surface_lines = []
        for number, line in enumerate(surface_lines, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError('记录不是对象')
                stamp = raw.get('epoch')
                source_row = raw.get('source_row', number)
                missing = [name for name in SURFACE if not finite(raw.get(name))]
                if not finite(stamp):
                    missing.append('时间')
                row = {'原始行号':number, '采集记录行号':source_row, '北京时间':when(stamp), 'Unix时间戳':stamp,
                       '时段':phase(stamp), '秒槽':raw.get('slot', ''),
                       'CPU表面温度（℃）':raw.get('CPU_Surface'), 'NAND表面温度（℃）':raw.get('NAND_Surface'),
                       '环境AMB温度（℃）':raw.get('AMB'),
                       '对齐基础点':'是' if source_row in aligned_surface else '否',
                       '选中绘图统计点':'是' if source_row in selected_surface else '否',
                       '数据状态':str(raw.get('error') or ('缺失或无效：'+','.join(missing) if missing else '原始记录')),
                       '全部原始字段（JSON）':line}
                if missing or raw.get('error'):
                    issue('JK808', number, row['数据状态'], line)
                surface_rows.append(row)
            except (ValueError, TypeError) as exc:
                issue('JK808', number, '原始行无法解析：'+str(exc), line)
                surface_rows.append({'原始行号':number, '时段':'正式时段未知', '数据状态':'原始行无法解析',
                                     '全部原始字段（JSON）':line})
    else:
        issue('JK808', '', '缺少jk808.jsonl，不能补造秒级记录')
    if not has_period:
        issue('正式时段', '', '没有有效的开始/结束时间，不能判定正式段或补造选中点')
    smart_times = [row['Unix时间戳'] for row in smart_rows]
    bounds = smart_times + ([start, end] if has_period else [])
    lower, upper = (min(bounds), max(bounds)) if bounds else (None, None)
    item_surface = [row for row in surface_rows if finite(row.get('Unix时间戳')) and lower is not None
                    and lower <= row['Unix时间戳'] <= upper]
    for row in surface_rows:
        row['本项原始时间范围'] = '是' if finite(row.get('Unix时间戳')) and lower is not None and lower <= row['Unix时间戳'] <= upper else '否'
    smart_columns = ['原始行号','北京时间','Unix时间戳','时段','选中绘图统计点',
                     'Drive Temperature（Composite复合温度）','Drive Temperature 2（CPU/SSD主控）',
                     'Drive Temperature 3（NAND闪存）','数据状态',*raw_headers,'额外原始字段']
    surface_columns = ['原始行号','采集记录行号','北京时间','Unix时间戳','时段','本项原始时间范围','秒槽',
                       'CPU表面温度（℃）','NAND表面温度（℃）','环境AMB温度（℃）','对齐基础点',
                       '选中绘图统计点','数据状态','全部原始字段（JSON）']
    filenames = ['HWiNFO_SMART全部原始记录_中文.csv','JK808全程原始记录_中文.csv',
                 'JK808本项秒级原始记录_中文.csv','原始数据异常行_中文.csv']
    for name, rows, cols in ((filenames[0], smart_rows, smart_columns),
            (filenames[1], surface_rows, surface_columns), (filenames[2], item_surface, surface_columns),
            (filenames[3], issues, ['来源','原始行号','问题','原始内容'])):
        write_csv(folder/name, rows, cols)
    summary = {'SMART全部原始记录':len(smart_rows), 'SMART正式段记录':sum(r['时段']=='正式段' for r in smart_rows),
               'SMART选中点':sum(r['选中绘图统计点']=='是' for r in smart_rows),
               'JK808全程原始记录':len(surface_rows), 'JK808本项原始记录':len(item_surface),
               'JK808正式段记录':sum(r['时段']=='正式段' for r in surface_rows),
               'JK808选中点':sum(r.get('选中绘图统计点')=='是' for r in surface_rows),
               '本项原始范围开始':when(lower), '本项原始范围结束':when(upper),
               '正式开始':when(start), '正式结束（不含）':when(end),
               '原始文件':{'SMART':'hwinfo.csv', 'JK808':'jk808.jsonl'},
               '说明':'SMART全量含正式段前后原始测温行；JK808全程CSV覆盖源jsonl，本项CSV按正式段及本项SMART首尾扩展。CPU指SSD主控，不是电脑CPU。原始文件不改动，缺失不补造。',
               '问题':issues}
    (folder/'原始数据说明.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), 'utf-8')

    def table(rows, columns):
        body = '<div class="scroll"><table><thead><tr>'+''.join('<th>'+escape(col)+'</th>' for col in columns)+'</tr></thead><tbody>'
        for row in rows:
            body += '<tr data-phase="'+escape(row.get('时段',''))+'" data-selected="'+escape(row.get('选中绘图统计点','否'))+'">'
            body += ''.join('<td>'+escape('' if row.get(col) is None else str(row.get(col,'')))+'</td>' for col in columns)+'</tr>'
        return body+'</tbody></table></div>'

    def controls():
        return '<p><label>显示：<select onchange="filterRows(this)"><option value="all">全部原始记录</option><option value="formal">正式段</option><option value="selected">选中绘图统计点</option></select></label></p>'

    html = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>SMART与JK808原始数据</title>'
    html += '<style>body{font:16px/1.6 "Microsoft YaHei",sans-serif;margin:24px;color:#193047}table{border-collapse:collapse;white-space:nowrap}td,th{border:1px solid #ccd6de;padding:6px 10px}th{position:sticky;top:0;background:#eaf0f5}.scroll{overflow:auto;max-height:600px}a{margin-right:20px}select{font:inherit}</style>'
    html += '<h1>SMART与JK808原始数据</h1><p>'+escape(summary['说明'])+'</p>'
    html += '<p>正式段：'+escape(when(start) or '未知')+' 至 '+escape(when(end) or '未知')+'（结束时刻不含）。选中点用于图表统计，不代表全部原始记录。</p>'
    html += '<p>'+''.join('<a href="'+escape(name)+'">'+escape(name)+'</a>' for name in filenames)+'</p>'
    html += '<section><h2>HWiNFO SMART：全部 '+str(len(smart_rows))+' 条 / 正式段 '+str(summary['SMART正式段记录'])+' 条 / 选中 '+str(summary['SMART选中点'])+' 点</h2>'
    html += '<p>Drive Temperature → Composite复合温度；Drive Temperature 2 → CPU/SSD主控；Drive Temperature 3 → NAND闪存。下列温度直接显示CSV原值，所有原始列均保留在中文CSV中。末尾重复表头和设备分组不计测温行。</p>'
    html += controls()+table(smart_rows, smart_columns[:9])+'</section>'
    html += '<section><h2>JK808本项秒级记录：'+str(len(item_surface))+' 条 / 正式段 '+str(summary['JK808正式段记录'])+' 条 / 选中 '+str(summary['JK808选中点'])+' 点</h2>'
    html += '<p>本项查看范围：'+escape(when(lower) or '未知')+' 至 '+escape(when(upper) or '未知')+'；按正式时段和该项SMART首尾原始测温时间扩展。下方全程源数据可能同时包含其他测试项。</p>'
    shown = ['原始行号','北京时间','时段','CPU表面温度（℃）','NAND表面温度（℃）','环境AMB温度（℃）','对齐基础点','选中绘图统计点','数据状态']
    html += controls()+table(item_surface, shown)+'</section>'
    html += '<details><summary>展开JK808全程原始记录（源jsonl全部 '+str(len(surface_rows))+' 条，含本项之外记录）</summary><section>'+controls()+table(surface_rows, shown)+'</section></details>'
    html += '<h2>缺失及读取问题</h2>'+(table(issues, ['来源','原始行号','问题']) if issues else '<p>未发现原始读取问题；采样缺失和对齐异常另见缺失及异常明细.csv。</p>')
    html += '<script>function filterRows(select){const scope=select.closest("section");scope.querySelectorAll("tbody tr").forEach(row=>{row.hidden=select.value==="formal"?row.dataset.phase!=="正式段":select.value==="selected"?row.dataset.selected!=="是":false})}</script></html>'
    (folder/'原始数据查看.html').write_text(html, 'utf-8')
    return summary


def build_stage_report(folder):
    """folder中只需conditions.json、hwinfo.csv、jk808.jsonl即可独立复算。"""
    folder = Path(folder)
    conditions = json.loads((folder/'conditions.json').read_text(encoding='utf-8'))
    smart, errors = read_hwinfo(folder/'hwinfo.csv', conditions['hwinfo'])
    surface = []
    for number, line in enumerate((folder/'jk808.jsonl').read_text(encoding='utf-8-sig').splitlines(), 1):
        if line.strip():
            row = json.loads(line)
            row.setdefault('source_row', number)  # 旧日志无source_row时仍追溯源JSONL行号。
            surface.append(row)
    aligned, selected, missing = align_and_select(smart, surface, conditions['start_epoch'],
        conditions['end_epoch'], conditions.get('plot_interval_s',conditions['interval_s']), conditions['points'],
        conditions.get('time_tolerance_s',2), conditions.get('surface_tolerance_s',.75),
        conditions['hwinfo'].get('required_metrics',['Composite']),
        sample_interval=conditions['interval_s'], window_s=conditions.get('window_s'))
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
    try:
        raw_data = write_readable_raw_data(folder, conditions, aligned, selected)
        raw_data_error = None
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # 查看副本失败不改变原始选点和负载状态，避免续测错误地重跑已完成负载。
        raw_data = {}
        raw_data_error = str(exc)
    statistics = calculate_statistics(selected)
    result = {'selected_count':len(selected), 'required_count':conditions['points'],
              'statistics':statistics, 'data_issue_count':len(errors), 'playback_notice_count':len(notices),
              'status':'有效选点（有全程异常）' if selected and errors else ('有效选点' if selected else '数据不足'),
              'selection':'Composite极差最小的连续窗口；同分取最早窗口',
              'sample_interval_s':conditions['interval_s'],
              'plot_interval_s':conditions.get('plot_interval_s',conditions['interval_s']),
              'window_s':conditions.get('window_s'),
              'workload_status':workload_status,'workload_ok':workload_ok,
              'raw_data':{key:value for key,value in raw_data.items() if key != '问题'},
              'simulated':conditions.get('simulated',False)}
    if raw_data_error:
        result['raw_data_error'] = raw_data_error
    if not workload_ok:
        result['status']='负载异常，统计仅供复核' if selected else '负载异常且数据不足'
    (folder/'statistics.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result, selected


def artifact_stem(label, capacity='', simulated=False):
    if capacity and capacity not in ('128G', '256G', '512G', '1T', '2T'):
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


def write_stage_failure(folder, output, mode, error, simulated=False, capacity='', *, status='报告生成失败'):
    title = clear_generated_stage_report(folder,output,mode,simulated,capacity)
    result = {'status':status,'error':str(error),'selected_count':0,
              'statistics':{},'data_issue_count':1,'workload_ok':False,'simulated':simulated}
    Path(folder).mkdir(parents=True,exist_ok=True)
    Path(output).mkdir(parents=True,exist_ok=True)
    try:
        conditions_path = Path(folder)/'conditions.json'
        conditions = json.loads(conditions_path.read_text('utf-8-sig')) if conditions_path.is_file() else {}
        raw_data = write_readable_raw_data(folder, conditions)
        result['raw_data'] = {key:value for key,value in raw_data.items() if key != '问题'}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result['raw_data_error'] = str(exc)
    (Path(folder)/'statistics.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
    explanation = ('报告生成失败，本项旧图和统计已作废。' if status=='报告生成失败'
                   else status+'；本项没有可用于统计的正式测试数据。')
    from os.path import relpath
    raw_link = Path(relpath(Path(folder)/'原始数据查看.html', output)).as_posix()
    (Path(output)/(title+'.html')).write_text('<!doctype html><meta charset="utf-8"><h1>'+escape(title)+
        '</h1><p>'+escape(explanation)+'</p><pre>'+escape(str(error))+'</pre><p><a href="'+
        escape(raw_link)+'">查看SMART与JK808原始数据（含缺失说明）</a></p>',encoding='utf-8')
    return result


def unstarted_stage_reason(folder, mode):
    """只有明确的中断记录能证明未执行；不能把已运行后的数据丢失伪装成未执行。"""
    folder=Path(folder)
    if (folder/'conditions.json').exists():return None
    failure_path=folder/'stage_failure.json'
    if failure_path.is_file():
        try:
            failure=json.loads(failure_path.read_text('utf-8-sig'))
            if failure.get('mode')==mode and failure.get('formal_started') is False:
                return '启动失败',failure.get('error') or '在正式计时前中断，请查看启动失败记录。'
        except (OSError,ValueError,TypeError):pass
    # 兼容已回传的旧版失败任务，不要求补造条件文件或温度采样。
    try:
        task=json.loads((folder.parent/'task.json').read_text('utf-8-sig'))
        status=json.loads((folder.parent/'status.json').read_text('utf-8-sig'))
        modes=task['modes'];active=status.get('mode')
        if (task.get('finished') is not True or status.get('phase')!='未完成'
                or status.get('job_id')!=task.get('job_id') or not isinstance(modes,list)
                or active not in modes or mode not in modes):return None
        if mode==active and 'start_epoch' not in status:
            return '启动失败',status.get('message') or '在正式计时前中断。'
        if modes.index(mode)>modes.index(active):
            return '未执行','前一项目 '+LABELS.get(active,active)+' 中断，本项尚未执行；可继续未完成的测试。'
    except (OSError,ValueError,TypeError,KeyError):pass
    return None


def export_stage(folder, output, mode=None, simulated=False, capacity=''):
    """外层流程提供mode，原始文件丢失/损坏时也能准确清除本项旧图。"""
    folder, output = Path(folder), Path(output)
    if mode is None:
        mode = folder.name if folder.name in LABELS else json.loads((folder/'conditions.json').read_text('utf-8'))['mode']
    try:
        unstarted=unstarted_stage_reason(folder,mode)
        if unstarted:
            state,reason=unstarted
            result=write_stage_failure(folder,output,mode,reason,simulated,capacity,status=state)
            return result,None
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
    from os.path import relpath
    raw_link = Path(relpath(folder/'原始数据查看.html', output)).as_posix()
    raw = result.get('raw_data', {})
    if raw:
        body += '<p><a href="'+escape(raw_link)+'">查看SMART三路与JK808每秒原始记录</a>：SMART全量 '+str(raw['SMART全部原始记录'])+' 条，正式段 '+str(raw['SMART正式段记录'])+' 条，选中 '+str(raw['SMART选中点'])+' 点；全部原始记录与选中点分别列出。</p>'
    elif result.get('raw_data_error'):
        body += '<p>中文原始查看副本未成功生成：'+escape(result['raw_data_error'])+'。原始文件及本项统计仍保留，解决文件写入问题后可重新复算。</p>'
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
