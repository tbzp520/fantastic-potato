"""只读已有主机记录，生成中文查看副本；仅用标准库，不连接硬件或网络。"""
import csv
from datetime import datetime, timedelta, timezone
import hashlib
from html import escape
import json
import math
from pathlib import Path
import re
import shutil

METRICS = ['CPU_Surface', 'NAND_Surface', 'AMB', 'Composite', 'CPU', 'NAND', 'BW(MiB/s)']
LABELS = {'Time':'计划时间UTC', 'ActualTime':'实际接收时间UTC', 'Second':'正式测试相对秒',
          'Error':'采集错误', 'CPU_Surface':'主控表面℃', 'NAND_Surface':'NAND表面℃',
          'AMB':'环境℃', 'Composite':'SMART综合℃', 'CPU':'主控传感器℃',
          'NAND':'NAND传感器℃', 'BW(MiB/s)':'带宽MiB每秒',
          'Phase':'测试阶段', 'JK808_实际接收时间':'JK808实际接收时间UTC',
          'SMART_实际接收时间':'SMART实际接收时间UTC', '带宽_实际接收时间':'带宽实际接收时间UTC'}
ORIGINAL_FILES = ('SMART完整原始响应.jsonl', 'SMART计划采样数据.csv', 'SMART原始计划点.csv',
                  'JK808原始报文.jsonl', 'JK808逐秒原始数据.csv')


def chinese_name(key):
    if key in LABELS:
        return LABELS[key]
    if key.endswith('_Error'):
        return chinese_name(key[:-6])+'错误'
    match = re.fullmatch(r'CH(\d{3})(_Raw)?', key)
    if match:
        return 'JK808通道'+match[1]+('原始字值' if match[2] else '温度℃')
    return key  # 未知原始字段保留名字，不能静默丢掉。


def read_csv(path):
    if not path.exists():
        return []
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def _value(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return value


def _columns(rows, first=()):
    result = list(first)
    for row in rows:
        for key in row:
            if key not in result:
                result.append(key)
    return result


def _write_csv(path, rows, first=()):
    columns = _columns(rows, first)
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows({key:_value(value) for key,value in row.items()} for row in rows)
    return columns


def _table(rows, columns):
    head = '<tr>'+''.join('<th>'+escape(key)+'</th>' for key in columns)+'</tr>'
    body = ''.join('<tr>'+''.join('<td>'+escape(str(_value(row.get(key))))+'</td>' for key in columns)+'</tr>' for row in rows)
    return '<div class="scroll"><table><thead>'+head+'</thead><tbody>'+body+'</tbody></table></div>'


def _second(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _beijing(row, metadata):
    stamp = row.get('Time')
    try:
        if stamp:
            return datetime.fromisoformat(stamp).astimezone(timezone(timedelta(hours=8))).isoformat(timespec='milliseconds')
        second = _second(row.get('Second'))
        if second is not None and metadata.get('start_epoch') is not None:
            return datetime.fromtimestamp(metadata['start_epoch']+second, timezone(timedelta(hours=8))).isoformat(timespec='milliseconds')
        if row.get('ActualTime'):
            return datetime.fromisoformat(row['ActualTime']).astimezone(timezone(timedelta(hours=8))).isoformat(timespec='milliseconds')
    except (ValueError, TypeError, OverflowError):
        pass
    return ''


def _raw_temperatures(parsed, metadata):
    """只对已有字段按本次原单位和舍入语义换算；不明单位时保持空白。"""
    unit = metadata.get('SMART原始温度单位')
    result = {}
    if unit not in ('K','C'):
        return result
    for metric,key in [('Composite','temperature'),('CPU','temperature_sensor_1'),('NAND','temperature_sensor_2')]:
        raw = parsed.get(key)
        if isinstance(raw,bool) or not isinstance(raw,(int,float)) or not math.isfinite(raw):
            continue
        value = raw-273.15 if unit=='K' else raw
        if -100<=value<=200:
            result[chinese_name(metric)] = math.ceil(value) if metadata.get('SMART摄氏度向上取整') else value
    return result


def export_raw_host_data(data_folder, smart_rows=(), surface_rows=(), selected_rows=(), statistics=None,
                         metadata=None, output_folder=None):
    """导出全部已存记录。JSONL存在时按文件记录顺序导出，包含晚到/错误/未选记录。"""
    source = Path(data_folder)
    metadata = dict(metadata or {})
    target = Path(output_folder) if output_folder else source/'原始主机数据'
    target.mkdir(parents=True, exist_ok=True)
    originals = target/'原文件_字节保留'
    originals.mkdir(exist_ok=True)
    manifest = {}
    for filename in ORIGINAL_FILES:
        path = source/filename
        if path.exists():
            shutil.copyfile(path, originals/filename)
            manifest[filename] = {'sha256':hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes':path.stat().st_size}
    (target/'原文件校验.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), 'utf-8')
    scheduled = read_csv(source/'SMART计划采样数据.csv') or list(smart_rows)
    scheduled_by_second = {}
    for row in scheduled:
        scheduled_by_second.setdefault(_second(row.get('Second')), []).append(row)
    raw_path = source/'SMART完整原始响应.jsonl'
    smart, fields, matched = [], [], set()
    temperature_mismatches = 0
    if raw_path.exists():
        for line_number, line in enumerate(raw_path.read_text('utf-8-sig').splitlines(), 1):
            if not line.strip():
                continue
            item = {'原始文件行号':line_number, '记录来源':'SMART完整原始响应.jsonl'}
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError('记录不是对象')
                for key,value in record.items():
                    item[chinese_name(key) if key != 'raw_response' else 'SMART原始响应全文'] = value
                raw_response = record.get('raw_response', '')
                parsed = {}
                try:
                    parsed = json.loads(raw_response)
                    if not isinstance(parsed, dict):
                        raise ValueError('SMART响应不是对象')
                    for key,value in parsed.items():
                        label = 'SMART原始字段：'+key
                        item[label] = value
                        if key not in fields:
                            fields.append(key)
                except (ValueError, TypeError):
                    parsed = {}
                    item['原始响应解析说明'] = '空响应或非JSON，保留响应全文；不补造温度'
                second = _second(record.get('Second'))
                candidates = scheduled_by_second.get(second, [])
                if len(candidates) == 1:
                    matched.add(second)
                    for key,value in candidates[0].items():
                        item['采样表：'+chinese_name(key)] = value
                    for key in ('Time','Second','Composite','CPU','NAND'):
                        item[chinese_name(key)] = candidates[0].get(key)
                    item['北京时间'] = _beijing(candidates[0],metadata)
                    item['温度列来源'] = '原计划采样表（空白表示本计划点未有效采到）'
                    # 把报告已有解析值与原始响应分开，不能将计划表的修改误称为原始上报。
                    from_response = _raw_temperatures(parsed,metadata)
                    differences = []
                    for metric in ('Composite','CPU','NAND'):
                        label = chinese_name(metric)
                        if label not in from_response:
                            continue
                        actual = from_response[label]
                        item['原始响应换算：'+label] = actual
                        scheduled_value = _second(candidates[0].get(metric))
                        if scheduled_value is not None and math.isfinite(scheduled_value) and abs(scheduled_value-actual)>1e-8:
                            differences.append(label+'：计划表'+str(scheduled_value)+'，原始响应换算'+str(actual))
                    if differences:
                        item['数据核对提示'] = '计划解析值与原始响应换算不一致；'+'；'.join(differences)
                        temperature_mismatches += 1
                else:
                    item.update(_raw_temperatures(parsed,metadata))
                    item['北京时间'] = _beijing(record,metadata)
                    item['温度列来源'] = '已有原始响应换算；未找到唯一计划行，不参与统计'
            except (ValueError, TypeError) as error:
                item.update({'原始记录文本':line, '原始响应解析说明':str(error)})
            smart.append(item)
    for row in scheduled:
        if not raw_path.exists() or _second(row.get('Second')) not in matched:
            smart.append({'记录来源':'已有SMART计划采样表（未找到对应原始响应）','北京时间':_beijing(row,metadata),
                          **{chinese_name(key):value for key,value in row.items()}})
    surface = read_csv(source/'JK808逐秒原始数据.csv') or list(surface_rows)
    jk808 = [{'北京时间':_beijing(row,metadata),**{chinese_name(key):value for key,value in row.items()}} for row in surface]
    selected = [{'北京时间':_beijing(row,metadata),**{chinese_name(key):value for key,value in row.items()}} for row in selected_rows]
    summaries = [{'指标':chinese_name(metric), '有效点数':values.get('count'), '最大值':values.get('max'),
                  '最小值':values.get('min'), '平均值':values.get('mean')}
                 for metric,values in (statistics or {}).items()]
    smart_first = ('北京时间','正式测试相对秒','SMART综合℃','主控传感器℃','NAND传感器℃','采集错误','记录来源','温度列来源')
    jk_first = ('北京时间','正式测试相对秒','主控表面℃','NAND表面℃','环境℃','采集错误')
    smart_columns = _write_csv(target/'SMART全部原始记录_中文.csv', smart, smart_first)
    jk_columns = _write_csv(target/'JK808每秒原始记录_中文.csv', jk808, jk_first)
    selected_columns = _write_csv(target/'对齐选点_中文.csv', selected, ('正式测试相对秒',))
    summary_columns = _write_csv(target/'统计汇总_中文.csv', summaries, ('指标','有效点数','最大值','最小值','平均值'))
    meta = dict(metadata or {}, SMART导出记录数=len(smart), JK808逐秒记录数=len(jk808), 对齐选点行数=len(selected),
                SMART计划解析与原始响应换算不一致条数=temperature_mismatches,
                已有SMART原始字段=fields, 已有JK808字段=list(surface[0]) if surface else [], 原文件=list(manifest))
    (target/'已有原始数据说明.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), 'utf-8')
    links = ''.join('<li><a href="原文件_字节保留/'+escape(name, quote=True)+'">'+escape(name)+'</a>（'+str(value['bytes'])+'字节）</li>' for name,value in manifest.items())
    document = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><link rel="icon" href="data:,"><title>原始主机数据、对齐选点与统计</title>
<style>body{font:15px/1.6 "Microsoft YaHei",sans-serif;margin:24px;color:#222}nav{position:sticky;top:0;background:white;padding:12px;border-bottom:1px solid #ccc}nav a{margin-right:24px}.scroll{overflow:auto;max-height:560px}table{border-collapse:collapse;min-width:100%}th,td{border:1px solid #ddd;padding:6px;white-space:pre-wrap;max-width:600px;overflow-wrap:anywhere}th{position:sticky;top:0;background:#edf2f7}details{margin:18px 0}pre{white-space:pre-wrap}h2{scroll-margin-top:64px}</style>
<h1>原始主机数据、对齐选点与统计</h1><nav><a href="#smart">全部原始SMART</a><a href="#jk">JK808每秒</a><a href="#selected">对齐选点</a><a href="#stats">最大/最小/平均</a></nav>
<p>这是已有记录的查看副本，原始文件按字节复制并附SHA256校验。空白表示原记录没有该值，不补零、不插值；原始SMART字段沿用nvme输出单位，摄氏度换算和有效性以采样表列为准。原始响应包括未选入报告、晚到和错误记录；对齐选点另列，不能用原始全部记录替代原报告统计。</p>'''
    document += '<p>下表三路摄氏温度优先显示已有计划采样表的解析值，原始响应全文及原始字段在展开区域保留。只有明确原单位时才另列原始响应换算值。</p>'
    if temperature_mismatches:
        document += '<p style="background:#fff2cc;padding:12px"><strong>数据核对提示：</strong>'+str(temperature_mismatches)+'条记录的计划解析值与原始响应换算不一致，请展开全部字段核对。原统计和原始文件没有因此修改。</p>'
    document += '<details><summary>已有原始数据字段、记录数量与原文件</summary><pre>'+escape(json.dumps(meta,ensure_ascii=False,indent=2))+'</pre><ul>'+links+'</ul></details>'
    if metadata.get('模拟数据'):
        document = document.replace('<nav>', '<p style="background:#fff2cc;padding:12px">模拟演练数据，不是实测结果，不可上交。</p><nav>',1)
    for anchor,title,rows,columns,filename in [
        ('smart','全部原始SMART记录',smart,smart_columns,'SMART全部原始记录_中文.csv'),
        ('jk','JK808每秒原始记录',jk808,jk_columns,'JK808每秒原始记录_中文.csv'),
        ('selected','对齐后实际参与统计的数据',selected,selected_columns,'对齐选点_中文.csv'),
        ('stats','原选点规则的最大、最小、平均',summaries,summary_columns,'统计汇总_中文.csv')]:
        visible = list(smart_first[:7]) if anchor=='smart' else list(jk_first) if anchor=='jk' else columns
        document += '<h2 id="'+anchor+'">'+title+'（'+str(len(rows))+'行）</h2><p><a href="'+filename+'">打开全部字段中文CSV</a></p>'+_table(rows,visible)
        if visible != columns:
            document += '<details><summary>展开原始全文、全部字段和采样明细</summary>'+_table(rows,columns)+'</details>'
    document += '</html>'
    (target/'原始数据与选点统计.html').write_text(document, 'utf-8')
    return meta
