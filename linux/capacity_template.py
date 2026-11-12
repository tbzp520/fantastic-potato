"""容量识别与原模板扩容。只追加空容量页，保留旧页及所有既有对象。

保留原来512G/1T/2T的顺序；128G/256G追加在末尾。带明细页的旧表
可能占用sheet4等文件编号，因此写表必须按工作表名称解析实际编号。
纯标准库，Windows和Linux使用同一份代码。
"""
import math
import posixpath
import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

CAPACITIES = ('512G', '1T', '2T', '128G', '256G')
DISPLAY_CAPACITIES = ('128G', '256G', '512G', '1T', '2T')
LEGACY_CAPACITIES = CAPACITIES[:3]
S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P = 'http://schemas.openxmlformats.org/package/2006/relationships'
X = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'


def capacity_from_bytes(size, lower=.92, upper=1.08):
    if isinstance(size, bool) or not isinstance(size, (int, float)) or not math.isfinite(size) or size <= 0:
        raise ValueError('磁盘字节容量无效')
    nominal = {'128G':128e9, '256G':256e9, '512G':512e9, '1T':1e12, '2T':2e12}
    matches = [name for name, value in nominal.items() if value*lower <= size <= value*upper]
    if len(matches) != 1:
        raise ValueError('磁盘容量不在128G / 256G / 512G / 1T / 2T范围')
    return matches[0]


def sheet_map(parts):
    workbook = ET.fromstring(parts['xl/workbook.xml'])
    relationships = ET.fromstring(parts['xl/_rels/workbook.xml.rels'])
    links = {item.get('Id'):item.get('Target') for item in relationships}
    result = {}
    for sheet in workbook.findall(f'{{{S}}}sheets/{{{S}}}sheet'):
        target = links.get(sheet.get(f'{{{R}}}id'))
        if not target:
            raise ValueError('工作表缺少关系：'+str(sheet.get('name')))
        name = sheet.get('name')
        if name in result:
            raise ValueError('工作表名称重复：'+str(name))
        key = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/'+target)
        if not key.startswith('xl/worksheets/') or key not in parts:
            raise ValueError('工作表路径无效：'+str(name))
        result[name] = key
    return result


def capacity_sheet_index(parts, capacity):
    if capacity not in CAPACITIES:
        raise ValueError('不支持的容量：'+str(capacity))
    key = sheet_map(parts).get(capacity+'-温度', '')
    match = re.fullmatch(r'xl/worksheets/sheet(\d+)\.xml', key)
    if not match:
        raise ValueError('缺少对应容量工作表：'+capacity)
    return int(match[1])


def append_element(xml, parent, child, attributes, text=None):
    """只追加XML片段；保留WPS/Excel扩展、命名空间及既有元素原字节。"""
    source = xml.decode('utf-8')
    closing = re.search(r'</(?P<prefix>\w+:)?'+re.escape(parent)+r'\s*>', source)
    empty = None if closing else re.search(r'<(?P<prefix>\w+:)?'+re.escape(parent)+r'\b[^>]*/>', source)
    marker = closing or empty
    if marker is None:
        raise ValueError('模板XML缺少节点：'+parent)
    prefix = marker.group('prefix') or ''
    attrs = ''.join(' '+key+'='+quoteattr(str(value)) for key,value in attributes.items())
    fragment = '<'+prefix+child+attrs+('/>' if text is None else '>'+escape(text)+'</'+prefix+child+'>')
    if closing:
        return (source[:closing.start()]+fragment+source[closing.start():]).encode('utf-8')
    opened = empty[0][:-2]+'>'
    return (source[:empty.start()]+opened+fragment+'</'+prefix+parent+'>'+source[empty.end():]).encode('utf-8')


def drawing_parts(parts, index):
    """按工作表关系找绘图部件。Excel可能重编号图片，或删除空白页的绘图。"""
    sheet_key=f'xl/worksheets/sheet{index}.xml'
    rel_key=f'xl/worksheets/_rels/sheet{index}.xml.rels'
    sheet=ET.fromstring(parts[sheet_key])
    drawing=sheet.find(f'{{{S}}}drawing')
    rels=ET.fromstring(parts.get(rel_key,f'<Relationships xmlns="{P}"/>'.encode()))
    relation=next((item for item in rels if drawing is not None and item.get('Id')==drawing.get(f'{{{R}}}id')),None)
    if drawing is not None:
        if relation is None or relation.get('Type')!=R+'/drawing':
            raise ValueError('工作表已有图片但缺少绘图关系，原表不会覆盖')
        target=relation.get('Target','')
        key=posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/worksheets/'+target)
        if not key.startswith('xl/drawings/') or key not in parts:
            raise ValueError('工作表已有图片但绘图文件缺失，原表不会覆盖')
    else:
        numbers=[int(match[1]) for name in parts if (match:=re.fullmatch(r'xl/drawings/drawing(\d+)\.xml',name))]
        number=max(numbers,default=0)+1
        key=f'xl/drawings/drawing{number}.xml'
        rid='rCapacityDrawing'
        while any(item.get('Id')==rid for item in rels):rid+='_' 
        fragment=f'<drawing xmlns:r="{R}" r:id="{rid}"/>'
        text=parts[sheet_key].decode('utf-8')
        marker=re.search(r'<(?:\w+:)?(?:legacyDrawing|legacyDrawingHF|drawingHF|picture|oleObjects|controls|webPublishItems|tableParts|extLst)\b',text)
        if marker:
            text=text[:marker.start()]+fragment+text[marker.start():]
        else:
            text=re.sub(r'</(?:\w+:)?worksheet>',lambda m:fragment+m[0],text,count=1)
        parts[sheet_key]=text.encode('utf-8')
        parts[rel_key]=append_element(parts.get(rel_key,f'<Relationships xmlns="{P}"/>'.encode()),
            'Relationships','Relationship',{'Id':rid,'Type':R+'/drawing','Target':'../drawings/'+posixpath.basename(key)})
        parts[key]=f'<xdr:wsDr xmlns:xdr="{X}"/>'.encode()
        parts['[Content_Types].xml']=append_element(parts['[Content_Types].xml'],'Types','Override',
            {'PartName':'/'+key,'ContentType':'application/vnd.openxmlformats-officedocument.drawing+xml'})
    drawing_rels=posixpath.dirname(key)+'/_rels/'+posixpath.basename(key)+'.rels'
    if drawing_rels not in parts:
        parts[drawing_rels]=f'<Relationships xmlns="{P}"/>'.encode()
    return key,drawing_rels


def ensure_png_type(parts):
    root=ET.fromstring(parts['[Content_Types].xml'])
    if not any(item.get('Extension','').lower()=='png' for item in root):
        parts['[Content_Types].xml']=append_element(parts['[Content_Types].xml'],'Types','Default',
            {'Extension':'png','ContentType':'image/png'})


def ensure_capacity_sheets(parts, replace_cells):
    """在内存里的XLSX部件补空白128/256页，返回是否升级；不触碰磁盘原表。"""
    mapping = sheet_map(parts)
    if not all(capacity+'-温度' in mapping for capacity in LEGACY_CAPACITIES):
        raise ValueError('原模板缺少512G / 1T / 2T容量页，不能推测其布局')
    source_key = mapping['512G-温度']
    source_index = capacity_sheet_index(parts, '512G')
    drawing_parts(parts,source_index)
    source_rel_key = f'xl/worksheets/_rels/sheet{source_index}.xml.rels'
    changed = False
    for capacity in ('128G', '256G'):
        if capacity+'-温度' in mapping:
            continue
        indices = [int(match[1]) for key in parts
                   if (match := re.fullmatch(r'xl/(?:worksheets/sheet|drawings/drawing)(\d+)\.xml', key))]
        index = max(indices, default=0)+1
        key = f'xl/worksheets/sheet{index}.xml'
        values = {f'B{row}':None for row in (1,2,3,4,5,44,45,46,47,48)}
        values.update({f'{column}{row+offset}':None for row in (11,20,29,38,53,62,71,80)
                       for offset in (0,2,4) for column in 'CDE'})
        values.update({f'C{row}':'未测试' for row in (8,17,26,35,50,59,68,77)})
        values.update(B4=capacity,B47=capacity,C43=None)
        cloned = replace_cells(parts[source_key], values)
        cloned = re.sub(rb'\btabSelected="1"', b'tabSelected="0"', cloned)
        source_name, new_name = '512G-温度', capacity+'-温度'
        text = cloned.decode('utf-8').replace("'"+source_name+"'!", "'"+new_name+"'!")
        text = text.replace(source_name+'!', "'"+new_name+"'!")
        parts[key] = text.encode('utf-8')
        rels = ET.fromstring(parts[source_rel_key])
        drawing_id = None
        for rel in rels:
            if rel.get('Type') == R+'/drawing':
                drawing_id = rel.get('Id')
                rel.set('Target', f'../drawings/drawing{index}.xml')
        if drawing_id is None:
            raise ValueError('原容量页缺少图片关系，不能建立新容量页')
        parts[f'xl/worksheets/_rels/sheet{index}.xml.rels'] = ET.tostring(rels, encoding='utf-8', xml_declaration=True)
        parts[f'xl/drawings/drawing{index}.xml'] = f'<xdr:wsDr xmlns:xdr="{X}"/>'.encode('utf-8')
        parts[f'xl/drawings/_rels/drawing{index}.xml.rels'] = f'<Relationships xmlns="{P}"/>'.encode('utf-8')
        workbook = ET.fromstring(parts['xl/workbook.xml'])
        sheets = workbook.find(f'{{{S}}}sheets')
        sheet_id = max(int(item.get('sheetId')) for item in sheets)+1
        rid = 'rIdCapacity'+capacity
        links = ET.fromstring(parts['xl/_rels/workbook.xml.rels'])
        while any(item.get('Id') == rid for item in links):
            rid += '_'
        old_count = len(sheets)
        parts['xl/workbook.xml'] = append_element(parts['xl/workbook.xml'], 'sheets', 'sheet',
            {'name':new_name, 'sheetId':sheet_id, 'xmlns:r':R, 'r:id':rid})
        # 复制原页的局部打印范围/标题，仅改新页局部ID和自引用；原定义不改。
        definitions = workbook.find(f'{{{S}}}definedNames')
        source_position = next(i for i,sheet in enumerate(sheets) if sheet.get('name') == source_name)
        if definitions is not None:
            for item in definitions:
                if item.get('localSheetId') == str(source_position):
                    attrs = dict(item.attrib, localSheetId=str(old_count))
                    formula = (item.text or '').replace("'"+source_name+"'!", "'"+new_name+"'!")
                    parts['xl/workbook.xml'] = append_element(parts['xl/workbook.xml'], 'definedNames', 'definedName', attrs, formula)
        parts['xl/_rels/workbook.xml.rels'] = append_element(parts['xl/_rels/workbook.xml.rels'],
            'Relationships', 'Relationship', {'Id':rid,'Type':R+'/worksheet','Target':f'worksheets/sheet{index}.xml'})
        for name, content_type in [(f'/xl/worksheets/sheet{index}.xml','application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'),
                                    (f'/xl/drawings/drawing{index}.xml','application/vnd.openxmlformats-officedocument.drawing+xml')]:
            parts['[Content_Types].xml'] = append_element(parts['[Content_Types].xml'], 'Types','Override',{'PartName':name,'ContentType':content_type})
        mapping[new_name] = key
        changed = True
    for capacity in CAPACITIES:
        drawing_parts(parts,capacity_sheet_index(parts,capacity))
    return changed
