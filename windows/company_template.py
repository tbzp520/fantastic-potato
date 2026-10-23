"""运行时定点写入公司原模板；图片适应预留单元格，保留其余版式。"""
from pathlib import Path
import zipfile, re, uuid, math
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET
PROGRAM_FOLDER=Path(__file__).resolve().parent
S='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
X='http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P='http://schemas.openxmlformats.org/package/2006/relationships'
CAPACITIES=('512G','1T','2T')
BLOCKS={'idle':(7,11),'local_video':(16,20),'online_video':(25,29),'bit':(34,38)}

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

def format_temperature_cells(data, sheet_key, addresses):
    """只给结果单元格添加整数显示格式；完整数值、字体、填色和边框保持原样。"""
    styles=data['xl/styles.xml'].decode('utf-8')
    block=re.search(r'<cellXfs\b([^>]*)>(.*?)</cellXfs>',styles,re.S)
    if not block:raise ValueError('模板缺少单元格样式表')
    formats=re.findall(r'<xf\b[^>]*?(?:/>|>.*?</xf>)',block[2],re.S)
    sheet=data[sheet_key].decode('utf-8')
    for address in addresses:
        pattern=r'<c\b(?=[^>]*\br="'+re.escape(address)+r'")[^>]*>'
        cell=re.search(pattern,sheet)
        if not cell:raise ValueError('缺少统计单元格：'+address)
        saved=re.search(r'\bs="(\d+)"',cell[0])
        original=formats[int(saved[1]) if saved else 0]
        # 内置numFmtId=1是0位小数显示，不把完整统计值舍入后存储。
        new=re.sub(r'\s+(?:numFmtId|applyNumberFormat)="[^"]*"','',original)
        new=new.replace('<xf','<xf numFmtId="1" applyNumberFormat="1"',1)
        if new not in formats:formats.append(new)
        style_id=formats.index(new)
        tag=re.sub(r'\s+s="[^"]*"','',cell[0]).replace('<c',f'<c s="{style_id}"',1)
        sheet=sheet[:cell.start()]+tag+sheet[cell.end():]
    attrs=re.sub(r'\s+count="\d+"','',block[1])
    replacement=f'<cellXfs{attrs} count="{len(formats)}">'+''.join(formats)+'</cellXfs>'
    data['xl/styles.xml']=(styles[:block.start()]+replacement+styles[block.end():]).encode('utf-8')
    data[sheet_key]=sheet.encode('utf-8')

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
    """图片填入 A:B 的七行合并区域，随单元格改变位置和大小。行列为零起始。"""
    def marker(tag, col, r):
        return f'<xdr:{tag}><xdr:col>{col}</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>{r}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:{tag}>'
    return (f'<xdr:twoCellAnchor editAs="twoCell" xmlns:xdr="{X}" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="{R}">'
            + marker('from', 0, row) + marker('to', 2, row+7)
            + f'<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="{object_id}" name="Windows 自动温度曲线"/><xdr:cNvPicPr><a:picLocks noChangeAspect="0"/></xdr:cNvPicPr></xdr:nvPicPr>'
            + f'<xdr:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></xdr:blipFill>'
            + '<xdr:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr></xdr:pic><xdr:clientData/></xdr:twoCellAnchor>')

def replace_company_chart(data, index, mode, png):
    row, _ = BLOCKS[mode]
    if png:
        sheet = ET.fromstring(data[f'xl/worksheets/sheet{index}.xml'])
        reserved = f'A{row+2}:B{row+8}'
        merges = {e.get('ref') for e in sheet.findall(f'.//{{{S}}}mergeCell')}
        if reserved not in merges:
            raise ValueError(f'模板缺少图片预留合并区域 {reserved}，不能自动填图')
    key = f'xl/drawings/drawing{index}.xml'
    xml = data[key].decode('utf-8')
    # 删除本项目区域的旧图片；其他 Windows / Linux 对象保持原字节。
    pattern = r'<(?:\w+:)?(?:twoCellAnchor|oneCellAnchor)\b[^>]*>.*?</(?:\w+:)?(?:twoCellAnchor|oneCellAnchor)>'
    def remove(match):
        fragment = match[0]
        start = re.search(r'<(?:\w+:)?from>.*?<(?:(?:\w+):)?row>(\d+)</', fragment, re.S)
        return '' if start and row <= int(start[1]) < row+9 else fragment
    xml = re.sub(pattern, remove, xml, flags=re.S)
    media = f'auto_windows_{index}_{mode}.png'
    rid = 'rAuto_' + mode
    rel_key = f'xl/drawings/_rels/drawing{index}.xml.rels'
    rels = ET.fromstring(data[rel_key])
    for rel in list(rels):
        if rel.get('Id') == rid:
            rels.remove(rel)
    if png:
        ET.SubElement(rels, f'{{{P}}}Relationship', Id=rid, Type=R+'/image', Target='../media/'+media)
    data[rel_key] = ET.tostring(rels, encoding='utf-8', xml_declaration=True)
    if not png:
        data[key]=xml.encode('utf-8')
        data.pop('xl/media/'+media,None)
        return
    # 不复制旧模板的浮动图片偏移；它会越过合并区域，且不会随单元格缩放。
    pic = build_excel_picture_anchor(row+1, rid, 1000+row)
    if re.search(r'<(?:\w+:)?wsDr\b[^>]*/>', xml):
        xml = re.sub(r'(<(?:\w+:)?wsDr\b[^>]*?)/>', r'\1>'+pic+'</xdr:wsDr>', xml)
    else:
        xml = re.sub(r'</(?:\w+:)?wsDr>', lambda m: pic+m[0], xml)
    data[key] = xml.encode('utf-8')
    data['xl/media/'+media] = Path(png).read_bytes()

def fill_windows_result(destination, capacity, mode, statistics, metadata, png, count, warning, status=''):
    parts=read_xlsx_parts(destination)
    validate_company_template(parts)
    index=CAPACITIES.index(capacity)+1
    _,row=BLOCKS[mode]
    values={f'B{r}':metadata.get(k) or '未提供' for r,k in
            ((1,'platform'),(2,'cpu'),(3,'os'))}
    # 测试版本由测试人员填写；未填时保留 Excel 中已有的人工填写值。
    if str(metadata.get('test_version') or '').strip():
        values['B5'] = metadata['test_version']
    else:
        sheet = ET.fromstring(parts[f'xl/worksheets/sheet{index}.xml'])
        cell = sheet.find(f'.//{{{S}}}c[@r="B5"]')
        text = ''.join(cell.itertext()).strip() if cell is not None else ''
        if not text or text == '未提供':
            values['B5'] = '待人工填写'
    values['B4']=capacity
    values[f'C{row-3}']=f'连续{count}点'+('（详见异常明细）' if warning else '')
    if '负载异常' in status:
        values[f'C{row-3}']=f'负载异常；{count}点仅供复核'
    values[f'A{row-3}']='HWiNFO与JK808对齐，仅绘制并统计所选连续点'
    for offset,metric in ((0,'Composite'),(2,'CPU_Surface'),(4,'NAND_Surface')):
        for col,key in zip('CDE',('max','mean','min')):
            values[f'{col}{row+offset}']=statistics.get(metric,{}).get(key)
    if not count:
        values[f'C{row-3}']='数据不足，未生成合格统计'
        if status=='报告生成失败':
            values[f'C{row-3}']='报告生成失败，旧统计已清除'
    key=f'xl/worksheets/sheet{index}.xml'
    parts[key]=replace_template_cells(parts[key],values)
    format_temperature_cells(parts,key,[f'{c}{row+offset}' for offset in (0,2,4) for c in 'CDE'])
    replace_company_chart(parts,index,mode,png)
    save_xlsx_parts(parts,destination)
