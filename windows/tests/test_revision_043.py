"""图表回归：可见温度标注、真实点边界、Excel 图片范围及人工版本保留。"""
import copy
import math
from pathlib import Path
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import temperature_report as report
import company_template as template

ROOT = Path(__file__).resolve().parents[1]


def points(count=20):
    return [dict(plot_second=i*30, Composite=40+i%3, CPU=42+i%3, NAND=34+i%3,
                 CPU_Surface=43.1+i/10, NAND_Surface=33.5+i/10, AMB=25.2) for i in range(count)]


class PlotTests(unittest.TestCase):
    def tearDown(self):
        import matplotlib.pyplot as plt
        plt.close('all')

    def test_all_selected_smart_points_have_numeric_labels_in_linux_colors(self):
        for count in (10, 12, 20):
            rows = points(count)
            original = copy.deepcopy(rows)
            fig = report.create_selected_points_figure(rows, '温度测试')
            ax = fig.axes[0]
            self.assertEqual(len(ax.texts), count*3)
            for metric, color in [('Composite','#7209B7'),('CPU','#9C6615'),('NAND','#2D9596')]:
                labels = [t for t in ax.texts if t.get_color() == color]
                self.assertEqual([t.get_text() for t in labels], [f'{r[metric]:.0f}' for r in rows])
                self.assertEqual([t.get_position()[0] for t in labels], [r['plot_second'] for r in rows])
            self.assertEqual(rows, original, '绘图不能改变原始数据或统计输入')
            for line in ax.lines:
                self.assertEqual(list(line.get_xdata()), [r['plot_second'] for r in rows])
                self.assertEqual(len(line.get_ydata()), count)
            self.assertEqual(len(ax.collections), 3)

    def test_label_boxes_fit_inside_export_and_avoid_each_other_with_twenty_points(self):
        fig = report.create_selected_points_figure(points(), 'Windows_BIT')
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        boxes = [t.get_window_extent(renderer) for t in fig.axes[0].texts]
        for box in boxes:
            self.assertGreaterEqual(box.x0, 0)
            self.assertLessEqual(box.x1, fig.bbox.width)
            self.assertGreaterEqual(box.y0, 0)
            self.assertLessEqual(box.y1, fig.bbox.height)
        for index, box in enumerate(boxes):
            self.assertFalse(any(box.overlaps(other) for other in boxes[index+1:]))

    def test_png_uses_linux_figure_size_and_300dpi(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'plot.png'
            report.draw_selected_points(points(), path, 'Windows_BIT')
            with Image.open(path) as image:
                self.assertGreaterEqual(image.width, 2900)
                self.assertGreaterEqual(image.height, 1700)
                self.assertAlmostEqual(image.info['dpi'][0], 300, delta=.1)
        fig = report.create_selected_points_figure(points(), 'Windows_BIT')
        self.assertEqual(list(fig.get_size_inches()), [10, 6])

    def test_missing_value_stays_gap_without_fake_label(self):
        rows = points(10)
        rows[4]['NAND'] = None
        fig = report.create_selected_points_figure(rows, 'gap')
        ax = fig.axes[0]
        self.assertEqual(len(ax.texts), 29)
        nand = next(line for line in ax.lines if line.get_label() == 'NAND')
        self.assertTrue(math.isnan(nand.get_ydata()[4]))

    def test_idle_labels_are_not_hidden_by_other_temperature_points(self):
        rows = points(10)
        for row in rows:
            row.update(Composite=32, CPU=39, NAND=32, CPU_Surface=33, NAND_Surface=32.5, AMB=16)
        fig = report.create_selected_points_figure(rows, 'Windows_待机')
        for label in fig.axes[0].texts:
            _, height = label.get_position()
            self.assertTrue(all(abs(height-rows[0][key]) >= 3 for key in report.TEMPERATURES))
            self.assertGreater(label.get_zorder(), 5)

    def test_out_of_usual_range_and_labels_are_not_clipped(self):
        rows = points(10)
        for row in rows:
            row.update(Composite=150, CPU=-5, NAND=-5)
        fig = report.create_selected_points_figure(rows, 'range')
        low, high = fig.axes[0].get_ylim()
        self.assertLess(low, -21)
        self.assertGreater(high, 158)


class WorkbookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.book = Path(self.temp.name)/'report.xlsx'
        template.create_empty_company_template(ROOT/'温度模版_原始.xlsx', self.book)
        self.png = Path(self.temp.name)/'plot.png'
        # 此测试只验证 OOXML 锚点和图片字节，真实 PNG 由 PlotTests 验证。
        self.png.write_bytes(b'test image')

    def fill(self, capacity='1T', mode='idle', metadata=None):
        template.fill_windows_result(self.book, capacity, mode, {}, metadata or {}, self.png, 10, 0)

    def test_all_capacity_images_exactly_fit_reserved_cells_and_resize(self):
        for capacity in template.CAPACITIES:
            for mode in template.BLOCKS:
                self.fill(capacity, mode)
        data = template.read_xlsx_parts(self.book)
        for index in (1, 2, 3):
            drawing = ET.fromstring(data[f'xl/drawings/drawing{index}.xml'])
            self.assertEqual(len(drawing), 4)
            for anchor, start in zip(drawing, (8, 17, 26, 35)):
                self.assertEqual(anchor.get('editAs'), 'twoCell')
                for tag, column, row in [('from', 0, start), ('to', 2, start+7)]:
                    marker = anchor.find(f'{{{template.X}}}{tag}')
                    expected = {'col':column, 'row':row, 'colOff':0, 'rowOff':0}
                    self.assertEqual({e.tag.split('}')[-1]:int(e.text) for e in marker}, expected)
                locks = anchor.find('.//{http://schemas.openxmlformats.org/drawingml/2006/main}picLocks')
                self.assertEqual(locks.get('noChangeAspect'), '0')

    def test_replacing_legacy_anchor_does_not_touch_other_sheets_or_linux(self):
        # 用包含原始 Linux 图的模板验证范围隔离，避免只在空模板上验证。
        self.book.write_bytes((ROOT/'温度模版_原始.xlsx').read_bytes())
        before = template.read_xlsx_parts(self.book)
        self.fill()
        self.fill()
        after = template.read_xlsx_parts(self.book)
        for index in (1, 3):
            for kind in ('worksheets/sheet', 'drawings/drawing'):
                key = f'xl/{kind}{index}.xml'
                self.assertEqual(before[key], after[key])
        pattern = rb'<(?:\w+:)?twoCellAnchor\b[^>]*>.*?</(?:\w+:)?twoCellAnchor>'
        for anchor in re.findall(pattern, before['xl/drawings/drawing2.xml'], re.S):
            row = re.search(rb'<(?:\w+:)?row>(\d+)</', anchor)
            if row and int(row[1]) >= 16:
                self.assertIn(anchor, after['xl/drawings/drawing2.xml'])
        drawing = ET.fromstring(after['xl/drawings/drawing2.xml'])
        references = [e.get(f'{{{template.R}}}embed') for e in drawing.iter()]
        self.assertEqual(references.count('rAuto_idle'), 1)

    def test_missing_reserved_area_does_not_write_partial_workbook(self):
        data = template.read_xlsx_parts(self.book)
        data['xl/worksheets/sheet2.xml'] = data['xl/worksheets/sheet2.xml'].replace(b'A9:B15', b'A9:B14')
        template.save_xlsx_parts(data, self.book)
        original = self.book.read_bytes()
        with self.assertRaisesRegex(ValueError, 'A9:B15'):
            self.fill()
        self.assertEqual(self.book.read_bytes(), original)

    def test_manual_test_version_survives_recalculation_without_firmware_inference(self):
        self.fill(metadata={'test_version':'现场填写 F-123'})
        self.fill(mode='bit', metadata={'test_version':'', 'firmware':'DO-NOT-USE'})
        data = template.read_xlsx_parts(self.book)
        sheet = ET.fromstring(data['xl/worksheets/sheet2.xml'])
        cell = sheet.find(f'.//{{{template.S}}}c[@r="B5"]')
        self.assertEqual(''.join(cell.itertext()), '现场填写 F-123')

    def test_empty_version_is_marked_for_manual_entry(self):
        self.fill()
        sheet = ET.fromstring(template.read_xlsx_parts(self.book)['xl/worksheets/sheet2.xml'])
        self.assertEqual(''.join(sheet.find(f'.//{{{template.S}}}c[@r="B5"]').itertext()), '待人工填写')


if __name__ == '__main__':
    unittest.main()
