"""Excel 插图验证：使用临时表格，不连接 Linux 或仪器。"""
from test_program import program, ROOT
from pathlib import Path
import tempfile
import base64
import unittest
import xml.etree.ElementTree as ET


class AdaptivePictureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.parts = program.read_xlsx_parts(ROOT/'温度模版_原始.xlsx')
        # 使用合成的 1x1 PNG，公开模板不携带现场测试图片。
        self.png = self.folder/'chart.png'
        self.png.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jhS8AAAAASUVORK5CYII='))

    def auto_pictures(self, index):
        drawing = ET.fromstring(self.parts[f'xl/drawings/drawing{index}.xml'])
        pictures = {}
        for anchor in drawing:
            blip = anchor.find('.//{http://schemas.openxmlformats.org/drawingml/2006/main}blip')
            if blip is not None:
                rid = blip.get(f'{{{program.R}}}embed', '')
                if rid.startswith('rAuto_'):
                    self.assertNotIn(rid, pictures, '重复出表不应叠放旧图片')
                    pictures[rid] = anchor
        return pictures

    def test_all_twelve_pictures_fit_their_merged_cells(self):
        for index in (1, 2, 3):
            sheet = ET.fromstring(self.parts[f'xl/worksheets/sheet{index}.xml'])
            merges = {item.get('ref') for item in sheet.findall(f'.//{{{program.S}}}mergeCell')}
            for mode, (row, _) in program.BLOCKS.items():
                program.replace_company_chart(self.parts, index, mode, self.png)
                anchor = self.auto_pictures(index)['rAuto_'+mode]
                self.assertEqual(anchor.tag, f'{{{program.X}}}twoCellAnchor')
                self.assertEqual(anchor.get('editAs'), 'twoCell')
                positions = []
                for name in ('from', 'to'):
                    marker = anchor.find(f'{{{program.X}}}{name}')
                    position = {child.tag.split('}')[1]: int(child.text) for child in marker}
                    self.assertEqual(position['colOff'], 0)
                    self.assertEqual(position['rowOff'], 0)
                    positions.append((position['col'], position['row']))
                start, end = positions
                self.assertEqual((start[0], end[0]), (0, 2))
                self.assertIn(f'A{start[1]+1}:B{end[1]}', merges)
                self.assertEqual(start[1], row+1)
                locks = anchor.find('.//{http://schemas.openxmlformats.org/drawingml/2006/main}picLocks')
                self.assertEqual(locks.get('noChangeAspect'), '0')
                # 不保留旧图固定尺寸，避免覆盖按单元格定位的尺寸。
                self.assertIsNone(anchor.find('.//{http://schemas.openxmlformats.org/drawingml/2006/main}xfrm'))

    def test_replacement_preserves_other_pictures_cells_and_layout(self):
        before = dict(self.parts)
        original_drawing = ET.fromstring(self.parts['xl/drawings/drawing1.xml'])
        other_pictures = [ET.tostring(anchor) for anchor in original_drawing
                          if int(anchor.find(f'{{{program.X}}}from/{{{program.X}}}row').text) < 76]
        for _ in range(2):
            program.replace_company_chart(self.parts, 1, 'read_1m', self.png)
        after_drawing = ET.fromstring(self.parts['xl/drawings/drawing1.xml'])
        self.assertEqual([ET.tostring(anchor) for anchor in after_drawing
                          if int(anchor.find(f'{{{program.X}}}from/{{{program.X}}}row').text) < 76], other_pictures)
        self.assertEqual(len(self.auto_pictures(1)), 1)
        changed = {name for name in self.parts if self.parts[name] != before.get(name)}
        self.assertEqual(changed, {'xl/drawings/drawing1.xml', 'xl/drawings/_rels/drawing1.xml.rels',
                                  'xl/media/auto_linux_1_read_1m.png'})

    def test_empty_template_and_reopened_workbook_use_resize_with_cells(self):
        import openpyxl  # 仅作独立读取验证，不用它重写原模板。
        workbook = self.folder/'result.xlsx'
        program.create_empty_company_template(ROOT/'温度模版_原始.xlsx', workbook)
        self.parts = program.read_xlsx_parts(workbook)
        for index in (1, 2, 3):
            for mode in program.BLOCKS:
                program.replace_company_chart(self.parts, index, mode, self.png)
        program.save_xlsx_parts(self.parts, workbook)
        book = openpyxl.load_workbook(workbook)
        self.addCleanup(book.close)
        for capacity in program.CAPACITIES:
            pictures = book[capacity+'-温度']._images
            self.assertEqual(len(pictures), 4)
            for image in pictures:
                self.assertEqual(image.anchor.editAs, 'twoCell')
                self.assertFalse(image.anchor.pic.nvPicPr.cNvPicPr.picLocks.noChangeAspect)
                self.assertEqual(image.anchor.to.row-image.anchor._from.row, 7)
                self.assertEqual((image.anchor._from.col, image.anchor.to.col), (0, 2))


if __name__ == '__main__':
    unittest.main()
