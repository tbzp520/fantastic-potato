"""只读识别测试电脑，区分测试平台和物理主机；不使用接收电脑的信息。"""
from __future__ import annotations

import hashlib
import json
import re
import uuid


REPORT_PLATFORM_KEYS = ('platform', 'cpu', 'os', 'bios', 'machine_id', 'platform_id')
_LABEL_KEYS = ('platform', 'cpu', 'os', 'bios')
_INVALID = {'', 'unknown', 'none', 'null', 'n/a', 'na', 'default string',
            'to be filled by o.e.m.', 'to be filled by oem', 'system serial number',
            'system product name', 'system version', 'not specified', 'not available',
            'not applicable', 'invalid', 'chassis serial number', '123456789',
            '1234567890', '0123456789'}
_INVALID_NORMALIZED = {re.sub(r'[^\w]', '', value.casefold()) for value in _INVALID}

# 固定只读查询；没有拼入项目名、命令行输入或外部脚本。
PLATFORM_QUERY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$cs = Get-CimInstance Win32_ComputerSystem
$product = Get-CimInstance Win32_ComputerSystemProduct
$processor = @(Get-CimInstance Win32_Processor | ForEach-Object { $_.Name })
$firmware = Get-CimInstance Win32_BIOS
$system = Get-CimInstance Win32_OperatingSystem
[ordered]@{
    manufacturer = [string]$cs.Manufacturer
    model = [string]$cs.Model
    family = [string]$cs.SystemFamily
    product_name = [string]$product.Name
    product_version = [string]$product.Version
    product_uuid = [string]$product.UUID
    product_serial = [string]$product.IdentifyingNumber
    bios_serial = [string]$firmware.SerialNumber
    cpu = $processor
    os_caption = [string]$system.Caption
    os_version = [string]$system.Version
    os_build = [string]$system.BuildNumber
    os_arch = [string]$system.OSArchitecture
    bios = [string]$firmware.SMBIOSBIOSVersion
} | ConvertTo-Json -Depth 4 -Compress
"""


def _text(value):
    if not isinstance(value, str):
        return ''
    return ' '.join(value.strip().split())


def _known(value):
    return bool(value and re.sub(r'[^\w]', '', value.casefold()) not in _INVALID_NORMALIZED and
                not re.fullmatch(r'[0fF\- ]+', value))


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _platform_digest(report):
    return _digest({key: _text(report.get(key)) for key in _LABEL_KEYS})


def _machine_id(raw):
    candidate = _text(raw.get('product_uuid'))
    try:
        value = uuid.UUID(candidate)
        if value.int not in (0, (1 << 128) - 1):
            return _digest({'source': 'system_uuid', 'value': str(value)})
    except (ValueError, AttributeError):
        pass
    for key in ('bios_serial', 'product_serial'):
        value = _text(raw.get(key))
        if _known(value):
            return _digest({'source': 'system_serial', 'value': value.casefold(),
                            'manufacturer': _text(raw.get('manufacturer')).casefold()})
    raise ValueError('无法识别测试电脑的物理身份：系统UUID及主机序列号均不可用；请检查BIOS/WMI，不能沿用旧电脑信息。')


def build_platform_snapshot(raw):
    """将固定CIM查询的原始字段整理成可传输快照，也供纯数据测试使用。"""
    if not isinstance(raw, dict):
        raise ValueError('测试电脑平台查询不是JSON对象。')
    manufacturer = _text(raw.get('manufacturer'))
    model = _text(raw.get('model'))
    if not _known(model):
        model = _text(raw.get('product_name'))
    family = _text(raw.get('family'))
    if not _known(family):
        family = _text(raw.get('product_version'))
    if not _known(family):
        family = ''
    processors = raw.get('cpu')
    if isinstance(processors, str):
        processors = [processors]
    if not isinstance(processors, list):
        processors = []
    processors = sorted(set(_text(name) for name in processors if _known(_text(name))))
    cpu = ' / '.join(processors)
    caption = _text(raw.get('os_caption'))
    version = _text(raw.get('os_version'))
    build = _text(raw.get('os_build'))
    architecture = _text(raw.get('os_arch'))
    bios = _text(raw.get('bios'))
    required = {'制造商': manufacturer, '机型': model, 'CPU': cpu, '操作系统': caption,
                '系统版本': version, '系统构建号': build, '系统架构': architecture, 'BIOS': bios}
    missing = [key for key, value in required.items() if not _known(value)]
    if missing:
        raise ValueError('测试电脑平台信息不完整：' + '、'.join(missing) + '；未沿用旧配置。')
    labels = []
    if manufacturer.casefold() not in (family or model).casefold():
        labels.append(manufacturer)
    if family and family.casefold() != model.casefold():
        labels.extend((family, '(' + model + ')'))
    else:
        labels.append(model)
    snapshot = {'schema': 1, 'manufacturer': manufacturer, 'model': model, 'family': family,
                'platform': ' '.join(labels), 'cpu': cpu,
                'os': f'{caption} ({version}; Build {build}; {architecture})', 'bios': bios,
                'machine_id': _machine_id(raw)}
    snapshot['platform_id'] = _platform_digest(snapshot)
    return snapshot


def _identity(report):
    if not isinstance(report, dict):
        raise ValueError('测试平台记录不是对象。')
    machine = _text(report.get('machine_id'))
    platform = _text(report.get('platform_id'))
    if not machine and not platform:
        return None  # 历史手工机型标签不是新版硬件身份。
    if not all(re.fullmatch('[0-9a-fA-F]{64}', value) for value in (machine, platform)):
        raise ValueError('测试平台身份记录不完整或无效；请新建任务，旧结果可用C补取或离线复算。')
    if any(not _known(_text(report.get(key))) for key in _LABEL_KEYS):
        raise ValueError('测试平台机型/CPU/系统/BIOS信息不完整；请新建任务或检查主机自动识别。')
    if platform.lower() != _platform_digest(report):
        raise ValueError('测试平台身份与机型/CPU/系统/BIOS记录不一致；请新建任务或新批次，旧结果可离线复算。')
    return platform.lower(), machine.lower()


def sync_platform_report(report, snapshot):
    """核验主机快照并原位更新报告自动字段，保留项目、批次及人工版本。"""
    if not isinstance(snapshot, dict) or type(snapshot.get('schema')) is not int or snapshot['schema'] != 1:
        raise ValueError('未收到有效的测试电脑平台快照；请确认两端使用相同新版。')
    if _identity(snapshot) is None:
        raise ValueError('测试电脑平台快照缺少真实身份；请重新识别。')
    if not isinstance(report, dict):
        raise ValueError('报告配置不是对象。')
    report.update({key: snapshot[key] for key in REPORT_PLATFORM_KEYS})
    return report


def refresh_platform(cfg, runner):
    """只在测试主机调用。失败即清空旧自动字段，不能携带旧身份继续测试。"""
    cfg.pop('detected_platform', None)
    report = cfg.setdefault('report', {})
    if not isinstance(report, dict):
        raise ValueError('报告配置不是对象。')
    for key in REPORT_PLATFORM_KEYS:
        report.pop(key, None)
    try:
        raw = runner(PLATFORM_QUERY, timeout=30)
        if isinstance(raw, str):
            raw = json.loads(raw.lstrip('\ufeff').strip())
        snapshot = build_platform_snapshot(raw)
        sync_platform_report(report, snapshot)
    except Exception as exc:
        raise ValueError('自动读取测试电脑平台失败：' + str(exc)) from exc
    cfg['detected_platform'] = snapshot
    return snapshot


def check_platform_identity(before_report, after_report, physical=False):
    """同批次须同平台；续测还须同一物理电脑。旧数据可独立离线复算。"""
    before = _identity(before_report)
    after = _identity(after_report)
    if before is None and after is None:
        return
    if before is None or after is None:
        raise ValueError('新旧测试平台身份不能混用：旧记录未保存可核验的主机身份。请新建任务/新批次；旧结果仍可用C补取或离线复算。')
    if before[0] != after[0]:
        raise ValueError('测试平台不同（机型、CPU、系统或BIOS已改变），不能合并同一批次或续测；请新建任务/新批次。')
    if physical and before[1] != after[1]:
        raise ValueError('测试主机不是原物理电脑，不能继续旧任务；请新建任务，旧结果仍可用C补取或离线复算。')
