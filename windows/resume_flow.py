"""续测记录与数据来源。每次尝试独立保存，已完成项目的原始文件不覆盖。"""
import json
import re
from pathlib import Path
import shutil

MODES = ('online_video', 'idle', 'local_video', 'bit')
IDENTITY = ('project', 'batch', 'capacity', 'system_disk_model', 'expected_serial')


def read_json(path):
    return json.loads(Path(path).read_text('utf-8-sig'))


def task_record(folder):
    task = read_json(Path(folder)/'手工复测/本次任务.json')
    modes = task.get('modes', [])
    if (not isinstance(task.get('job_id'), str) or not re.fullmatch('[a-f0-9]{32}', task['job_id']) or
            task.get('kind', 'formal') != 'formal' or not modes or
            len(set(modes)) != len(modes) or any(m not in MODES for m in modes)):
        raise ValueError('只能继续正式测试任务，不能将联调短测接入正式结果')
    return task


def check_identity(original, current, *, disk_only=False):
    keys = IDENTITY[2:] if disk_only else IDENTITY
    for key in keys:
        expected = str(original.get(key) or '').strip()
        actual = str(current.get(key) or '').strip()
        if not expected or expected != actual:
            raise ValueError('续测身份不一致：'+key+'（原任务 '+expected+'，当前 '+actual+
                             '）。未启动测试；换盘或更换项目批次请选B新建任务。')


def attempts(root):
    root = Path(root).resolve()
    original = task_record(root)
    journal = root/'手工复测/续测记录.json'
    records = read_json(journal).get('attempts', []) if journal.exists() else []
    paths = [root]
    for relative in records:
        path = (root/relative).resolve()
        if not path.is_relative_to((root/'手工复测/续测记录').resolve()) or path in paths:
            raise ValueError('续测记录路径重复或不在本任务目录内')
        task = task_record(path)
        check_identity(original['config']['report'], task['config']['report'])
        if (original['config']['station'] != task['config']['station'] or
                not set(task['modes']).issubset(original['modes'])):
            raise ValueError('续测工位或测试项目与原任务不符')
        paths.append(path)
    return paths


def add_attempt(root, folder, save_json):
    root, folder = Path(root).resolve(), Path(folder).resolve()
    paths = attempts(root)
    if not folder.is_relative_to((root/'手工复测/续测记录').resolve()):
        raise ValueError('续测目录无效')
    check_identity(task_record(root)['config']['report'], task_record(folder)['config']['report'])
    if folder not in paths:
        paths.append(folder)
    save_json(root/'手工复测/续测记录.json',
              {'schema': 1, 'attempts': [p.relative_to(root).as_posix() for p in paths[1:]]})


def transfer_complete(folder):
    """清单只有所有分块和SHA校验完成后才落盘；旧版本任务也支持。"""
    manual = Path(folder)/'手工复测'
    receipt = manual/'接收进度.json'
    if receipt.exists() and read_json(receipt).get('phase') == 'not_started':
        return True
    host = manual/'主机数据'
    return (host/'文件校验.json').is_file() and not any(host.rglob('*.partial'))


def stage_completed(result):
    # Excel占用等出表错误不需要重跑负载；原始数据完整才保留本项。
    return (result.get('workload_ok') is True and result.get('selected_count', 0) > 0
            and result['selected_count'] == result.get('required_count')
            and not result.get('data_issue_count', 0) and not result.get('simulated'))


def plan(root):
    """重新检查实际原始数据，不单凭以前的绿色结果或人工编辑的汇总决定跳过。"""
    from temperature_report import build_stage_report
    root = Path(root).resolve()
    original = task_record(root)
    sources, completed = {}, set()
    paths = attempts(root)
    for folder in paths:
        task = task_record(folder)
        manual = folder/'手工复测'
        host = manual/'主机数据'
        if (host/'文件校验.json').exists():
            actual = read_json(host/'task.json')
            if actual.get('job_id') != task['job_id'] or actual.get('station') != task['config']['station']:
                raise ValueError('回传数据任务编号或工位与续测记录不一致')
            snapshot = host/'config_snapshot.json'
            if snapshot.exists():
                check_identity(task['config']['report'], read_json(snapshot)['report'])
        for mode in task['modes']:
            stage = manual/'主机数据'/mode
            if mode not in completed:
                sources[mode] = folder
            if not transfer_complete(folder):
                continue  # 不从未回传完整的文件里宣称该项已完成。
            if not (stage/'conditions.json').is_file():
                continue
            try:
                conditions = read_json(stage/'conditions.json')
                setting = task['config']['tests'][mode]
                if (conditions.get('mode') != mode or
                    conditions.get('interval_s') != setting['interval_s'] or
                    conditions.get('points') != setting['points'] or
                    abs(conditions['end_epoch']-conditions['start_epoch']-setting['duration_s']) > 0.01):
                    continue
                shutil.copy2(manual/'jk808.jsonl', stage/'jk808.jsonl')
                result, _ = build_stage_report(stage)
                if stage_completed(result):
                    sources[mode] = folder
                    completed.add(mode)
            except (OSError, ValueError, KeyError, TypeError):
                continue
    # 已完成项不重跑；旧任务剩余项目也按当前测试顺序继续。
    return original, paths, sources, [m for m in MODES if m in original['modes'] and m not in completed]
