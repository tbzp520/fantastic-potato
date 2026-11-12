"""同项目、同批次的 Windows 统一结果。不连接设备，不重新执行负载。

每个测次的原始文件仍独立保存。容量页按最新测次开始时间逐项汇总，
补取旧测次不覆盖较新的同项。同一容量不允许混入另一序列号的盘。
"""
from datetime import datetime, timezone, timedelta
from html import escape
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import quote

from company_template import (CAPACITIES, BLOCKS, create_empty_company_template,
    fill_windows_result, read_xlsx_parts, replace_template_cells, replace_company_chart,
    save_xlsx_parts)
from temperature_report import LABELS, artifact_stem
from capacity_template import DISPLAY_CAPACITIES, ensure_capacity_sheets, capacity_sheet_index

BOOK = '温度汇总.xlsx'
PAGE = '批次测试结果.html'
RECORD = '手工复测/批次汇总记录.json'
ORDER = ('online_video', 'idle', 'local_video', 'bit')
BJ = timezone(timedelta(hours=8))
PLATFORM_FIELDS = ('platform', 'cpu', 'os', 'bios', 'machine_id', 'platform_id')


def read_json(path):
    return json.loads(Path(path).read_text('utf-8-sig'))


def check_batch_platform(before, after):
    """批次只比较测试平台，同平台的不同物理主机可汇总不同容量。"""
    if not any(report.get(key) for report in (before, after) for key in ('platform_id', 'machine_id')):
        return  # 两侧均为旧测次，保留原来的离线汇总规则。
    from platform_identity import check_platform_identity
    try:
        check_platform_identity(before, after, physical=False)
    except ValueError as exc:
        raise ValueError('批次测试平台身份不能混合：'+str(exc)+
                         '；请使用新批次，旧数据可离线重建旧批次汇总；原表不覆盖。') from exc


def batch_platform_record(folder):
    path = folder/RECORD
    if not path.is_file():
        return None
    record = read_json(path)
    identity = record.get('platform_identity', {})
    if not isinstance(identity, dict):
        raise ValueError('批次平台身份登记损坏；原表不覆盖')
    return identity


def disk_identity(report):
    capacity = report.get('capacity')
    identity = {key: str(report.get(key) or '').strip()
                for key in ('system_disk_model', 'expected_serial')}
    if capacity not in CAPACITIES or not all(identity.values()):
        raise ValueError('批次汇总需要当前盘的容量、型号和序列号，不能沿用空身份')
    return capacity, identity


def same_disk(capacity, before, after):
    if before != after:
        raise ValueError(f"本项目本批次的{capacity}已登记{before['system_disk_model']}，"
                         f"SN={before['expected_serial']}；当前SN={after['expected_serial']}。"
                         '请使用另一个批次，原盘数据不会覆盖。')


def registered_samples(folder, project, batch):
    path = folder/RECORD
    if not path.exists():
        if (folder/BOOK).exists():
            raise ValueError('批次目录已有温度汇总.xlsx但缺少批次汇总记录，无法核对原表归属；原表不覆盖')
        return {}
    data = read_json(path)
    if (not isinstance(data, dict) or data.get('format_version') != 1 or data.get('project') != project
            or data.get('batch') != batch or not isinstance(data.get('samples'), dict)):
        raise ValueError('批次汇总记录损坏或项目批次不一致；原表不覆盖')
    samples = {}
    for capacity, item in data['samples'].items():
        cap, identity = disk_identity(dict(item, capacity=capacity))
        samples[cap] = identity
    return samples


def discover_batch_names(folder):
    """用户明确选旧批次目录时，读取其归属，不套用当前另一项目的配置。"""
    folder = Path(folder).resolve()
    if (folder/RECORD).is_file():
        record = read_json(folder/RECORD)
        return record['project'], record['batch']
    names = set()
    for child in folder.iterdir():
        path = child/'手工复测/本次任务.json'
        if child.name == '手工复测' or not path.is_file():
            continue
        task = read_json(path)
        cfg = task.get('config', {})
        if task.get('kind', 'formal') != 'formal' or cfg.get('_simulated') or cfg.get('_commissioning'):
            continue
        report = cfg.get('report', {})
        names.add((report.get('project'), report.get('batch')))
    if len(names) != 1 or not all(isinstance(v, str) and v.strip() for v in next(iter(names), ())):
        raise ValueError('所选目录没有唯一的正式项目/批次，请选择包含各测次的那一级批次目录')
    return next(iter(names))


def task_started(task, folder):
    epoch = task.get('created_epoch')
    if isinstance(epoch, (int, float)) and not isinstance(epoch, bool) and math.isfinite(epoch):
        return epoch
    # 旧版文件夹在NTP校时前命名，优先用校时后的正式起点，避免旧电脑时间偏差倒置顺序。
    starts = []
    for mode in task.get('modes', []):
        path = folder/'手工复测/主机数据'/mode/'conditions.json'
        if path.is_file():
            stamp = read_json(path).get('start_epoch')
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and math.isfinite(stamp):
                starts.append(stamp)
    if starts:
        return min(starts)
    match = re.search(r'(?:^|_)(\d{8}_\d{6})(?:_|$)', folder.name)
    if match:
        return datetime.strptime(match[1], '%Y%m%d_%H%M%S').replace(tzinfo=BJ).timestamp()
    return (folder/'手工复测/本次任务.json').stat().st_mtime


def load_tasks(folder, project, batch):
    tasks = []
    seen = set()
    if not folder.exists():
        return tasks
    for child in sorted(folder.iterdir()):
        source = child/'手工复测/本次任务.json'
        if not child.is_dir() or child.name == '手工复测' or not source.is_file():
            continue
        if not child.resolve().is_relative_to(folder.resolve()):
            raise ValueError('批次内的测次路径指向目录以外：'+str(child))
        task = read_json(source)
        cfg = task.get('config', {})
        if task.get('kind', 'formal') != 'formal' or cfg.get('_simulated') or cfg.get('_commissioning'):
            continue
        report = cfg.get('report', {})
        if report.get('project') != project or report.get('batch') != batch:
            raise ValueError('测次项目/批次与目录不一致，不能混合汇总：'+str(child))
        job = task.get('job_id')
        modes = task.get('modes')
        if (not isinstance(job, str) or not re.fullmatch('[a-f0-9]{32}', job)
                or job in seen or not isinstance(modes, (list, tuple)) or not modes
                or any(mode not in ORDER for mode in modes) or len(set(modes)) != len(modes)):
            raise ValueError('测次编号重复或测试项记录无效：'+str(child))
        seen.add(job)
        capacity, identity = disk_identity(report)
        snapshot = child/'手工复测/主机数据/config_snapshot.json'
        if snapshot.is_file():
            actual = read_json(snapshot).get('report', {})
            for key in ('capacity', 'system_disk_model', 'expected_serial'):
                if actual.get(key) and actual[key] != report.get(key):
                    raise ValueError('测次与主机记录的盘身份不一致：'+str(child)+' / '+key)
            check_batch_platform(report, actual)
        tasks.append(dict(folder=child, task=task, report=report, capacity=capacity,
                          identity=identity, rank=(task_started(task, child), job)))
    return tasks


def check_batch_target(folder, report):
    """正式测试前只读核对，容量未知时由主机识盘后调用。"""
    folder = Path(folder).resolve()
    project, batch = report.get('project'), report.get('batch')
    if not isinstance(project, str) or not project.strip() or not isinstance(batch, str) or not batch.strip():
        raise ValueError('请先填写项目和批次')
    samples = registered_samples(folder, project, batch)
    tasks = load_tasks(folder, project, batch)
    platform = batch_platform_record(folder)
    for item in tasks:
        if platform is None:
            platform = item['report']
        else:
            check_batch_platform(platform, item['report'])
        capacity, identity = item['capacity'], item['identity']
        if capacity in samples:
            same_disk(capacity, samples[capacity], identity)
        samples[capacity] = identity
    capacity, identity = disk_identity(report)
    if platform is not None:
        check_batch_platform(platform, report)
    if capacity in samples:
        same_disk(capacity, samples[capacity], identity)
    return tasks


def validate_result(result):
    if not isinstance(result, dict) or result.get('simulated'):
        raise ValueError('模拟数据或无效结果不能纳入正式批次')
    count = result.get('selected_count')
    if type(count) is not int or count < 0 or not isinstance(result.get('status'), str):
        raise ValueError('选点数量或结果状态无效')
    statistics = result.get('statistics', {})
    if not isinstance(statistics, dict) or (count and not {'Composite','CPU_Surface','NAND_Surface','AMB'} <= set(statistics)):
        raise ValueError('结果缺少必需温度统计，请先重新出表')
    for values in statistics.values():
        if not isinstance(values, dict) or values.get('count') != count:
            raise ValueError('统计数量与选中绘图点不一致')
        for key in ('max', 'mean', 'min'):
            value = values.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('统计数值无效，不能写入批次表')
        if not values['min']-1e-9 <= values['mean'] <= values['max']+1e-9:
            raise ValueError('最大、最小、平均数值关系无效，请先重新出表')


def stage_complete(result):
    return (result.get('workload_ok') is True and result.get('selected_count', 0) > 0
            and result['selected_count'] == result.get('required_count')
            and not result.get('data_issue_count') and not result.get('simulated'))


def clear_windows_cells(workbook):
    """重建时清理Windows结果；保留人工版本号、版式以及其他区域。"""
    parts = read_xlsx_parts(workbook)
    ensure_capacity_sheets(parts, replace_template_cells)
    for capacity in CAPACITIES:
        index=capacity_sheet_index(parts,capacity)
        values = {}
        for mode, (_, row) in BLOCKS.items():
            values.update({f'{col}{row+offset}': None for offset in (0, 2, 4) for col in 'CDE'})
            values[f'C{row-3}'] = '未测试'
            replace_company_chart(parts, index, mode, None)
        name = f'xl/worksheets/sheet{index}.xml'
        parts[name] = replace_template_cells(parts[name], values)
    save_xlsx_parts(parts, workbook)


def stage_artifact(folder, capacity, mode, suffix):
    current = folder/(artifact_stem(LABELS[mode], capacity)+suffix)
    legacy = folder/(LABELS[mode]+suffix)
    return current if current.exists() or not legacy.exists() else legacy


def build_page(folder, project, batch, chosen, samples):
    link = lambda p: escape(quote(p.relative_to(folder).as_posix(), safe='/'), quote=True)
    body = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>批次测试结果</title>'
            '<style>body{font:16px/1.7 "Microsoft YaHei",sans-serif;max-width:1200px;margin:30px auto;'
            'padding:0 20px;color:#24384a}table{width:100%;border-collapse:collapse}td,th{border:1px solid #ccd6df;'
            'padding:10px;text-align:left}th{background:#eaf0f5}.ok{color:#18733a}.warn{color:#ad5200}</style>'
            '<h1>批次测试结果</h1><p>项目：'+escape(project)+'　批次：'+escape(batch)+'</p>'
            '<p><a href="'+quote(BOOK)+'">打开统一Excel（128G / 256G / 512G / 1T / 2T）</a></p>'
            '<p>仅纳入已出表测次，新测次回传出表后更新；每项采用最近测次，补取旧测次不覆盖更新测次。'
            '空白容量表示尚无结果，未执行或有异常的项目不会标为完成。</p>'
            '<p>更新于：'+datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S')+'</p>')
    warnings = {}
    for item in chosen.values():
        source = item['folder']
        if source in warnings:
            continue
        result_path = source/'手工复测/任务结果.json'
        if result_path.is_file():
            result = read_json(result_path)
            issues = [str(issue) for issue in result.get('issues', [])
                      if not str(issue).startswith('本次数据已保留，批次汇总未更新：')]
            if result.get('completed') is not True and not issues:
                issues = ['来源测次尚未确认完整结束，请核对该测次报告']
        else:
            issues = ['来源测次缺少最终任务检查记录，请核对任务结束及网卡恢复状态']
        warnings[source] = issues
    if any(warnings.values()):
        body += '<p class="warn">部分来源测次仍有待核对事项，不能仅凭温度表认定整批完成，详见页末。</p>'
    for capacity in DISPLAY_CAPACITIES:
        body += '<h2>'+capacity+'</h2>'
        identity = samples.get(capacity)
        if identity:
            body += '<p>型号：'+escape(identity['system_disk_model'])+'　SN：'+escape(identity['expected_serial'])+'</p>'
        body += '<table><tr><th>测试项</th><th>结果</th><th>绘图点数</th><th>来源测次</th></tr>'
        for mode in ORDER:
            item = chosen.get((capacity, mode))
            if item is None:
                body += '<tr><td>'+escape(LABELS[mode])+'</td><td>未测试 / 尚未出表</td><td></td><td></td></tr>'
                continue
            result = item['result']
            state = ('完成：' if stage_complete(result) else '待处理：')+result['status']
            stage_page = stage_artifact(item['folder'], capacity, mode, '.html')
            source_link = ('<a href="'+link(stage_page)+'">'+escape(item['folder'].name)+'</a>'
                           if stage_page.exists() else escape(item['folder'].name))
            body += ('<tr><td>'+escape(LABELS[mode])+'</td><td class="'+('ok' if stage_complete(result) else 'warn')+'">'
                     +escape(state)+'</td><td>'+str(result['selected_count'])+'</td><td>'+source_link+'</td></tr>')
        body += '</table>'
    if any(warnings.values()):
        body += '<h2>来源测次待核对事项</h2>'
        for source, issues in warnings.items():
            if issues:
                body += '<h3>'+escape(source.name)+'</h3><ul>'+''.join('<li>'+escape(issue)+'</li>' for issue in issues)+'</ul>'
    return body+'<p>各测次图片、报告和手工复测数据保持独立；统计及真实缺失按所引用测次保留。</p></html>'


def rebuild_batch(folder, project, batch, template):
    folder = Path(folder).resolve()
    tasks = load_tasks(folder, project, batch)
    if not tasks:
        raise ValueError('批次目录没有正式测次记录；请选择包含各容量测次的项目/批次目录')
    # 核对所有历史容量身份，不能仅凭最后一个文件夹覆盖旧盘。
    check_batch_target(folder, tasks[-1]['report'])
    samples = registered_samples(folder, project, batch)
    chosen = {}
    for item in tasks:
        samples[item['capacity']] = item['identity']
        path = item['folder']/'手工复测/结果汇总.json'
        if not path.is_file():
            continue
        results = read_json(path)
        if not isinstance(results, dict) or any(mode not in item['task']['modes'] for mode in results):
            raise ValueError('结果汇总与测次测试项不一致：'+str(path))
        for mode, result in results.items():
            validate_result(result)
            key = (item['capacity'], mode)
            if key not in chosen or item['rank'] > chosen[key]['rank']:
                chosen[key] = dict(item, result=result)
    if not chosen:
        raise ValueError('本批次尚无已出表结果；请先补取文件或完成报告生成，不需要重跑负载')
    if (folder/RECORD).exists():
        previous = read_json(folder/RECORD)
        for capacity, modes in previous.get('sources', {}).items():
            if any((capacity, mode) not in chosen for mode in modes):
                raise ValueError('已有汇总引用的测次结果缺失，请恢复原测次目录；不会清空原表中的已有容量结果')
    # 先完整生成临时表，Excel占用或数据错误时不会留下半本正式表。
    manual = folder/'手工复测'
    manual.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='_batch_', dir=manual) as temp:
        staging = Path(temp)/BOOK
        if (folder/BOOK).exists():
            shutil.copy2(folder/BOOK, staging)
            clear_windows_cells(staging)
        else:
            create_empty_company_template(template, staging)
        sources = {}
        for (capacity, mode), item in sorted(chosen.items()):
            result = item['result']
            png = stage_artifact(item['folder'], capacity, mode, '.png') if result['selected_count'] else None
            if png is not None and not png.is_file():
                raise ValueError('选中测次缺少曲线图，请选C重新出表：'+str(png))
            fill_windows_result(staging, capacity, mode, result['statistics'], item['report'], png,
                                result['selected_count'], result.get('data_issue_count', 0), result['status'])
            sources.setdefault(capacity, {})[mode] = dict(
                folder=item['folder'].relative_to(folder).as_posix(), job_id=item['task']['job_id'],
                created_epoch=item['rank'][0], status=result['status'],
                selected_count=result['selected_count'], completed=stage_complete(result))
        record = dict(format_version=1, project=project, batch=batch, samples=samples, sources=sources,
                      platform_identity={key:tasks[0]['report'][key] for key in PLATFORM_FIELDS
                                         if key in tasks[0]['report']},
                      updated_beijing=datetime.now(BJ).isoformat(timespec='seconds'))
        (Path(temp)/'record.json').write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False), 'utf-8')
        (Path(temp)/PAGE).write_text(build_page(folder, project, batch, chosen, samples), 'utf-8')
        staging.replace(folder/BOOK)
        (Path(temp)/'record.json').replace(folder/RECORD)
        (Path(temp)/PAGE).replace(folder/PAGE)
    return dict(passed=True, workbook=str(folder/BOOK), page=str(folder/PAGE),
                capacities=[c for c in CAPACITIES if any(k[0] == c for k in chosen)], stages=len(chosen))
