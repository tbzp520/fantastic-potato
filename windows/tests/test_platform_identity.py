import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from platform_identity import (REPORT_PLATFORM_KEYS, PLATFORM_QUERY, build_platform_snapshot,
                               sync_platform_report, refresh_platform, check_platform_identity)


def raw(**changes):
    value = dict(manufacturer='LENOVO', model='21TK0000CD', family='ThinkBook 14 G7+ IAH',
                 product_uuid='11111111-1111-4111-8111-111111111111', cpu=['Intel Core Ultra 5 225H'],
                 os_caption='Microsoft Windows 11 Pro', os_version='10.0.26200', os_build='26200',
                 os_arch='64-bit', bios='ABCN01WW')
    value.update(changes)
    return value


class PlatformIdentityTests(unittest.TestCase):
    def report(self, **changes):
        return sync_platform_report({}, build_platform_snapshot(raw(**changes)))

    def test_model_family_and_actual_cpu_are_preserved(self):
        info = build_platform_snapshot(raw(cpu='Different Actual CPU'))
        self.assertIn('ThinkBook 14 G7+ IAH', info['platform'])
        self.assertIn('21TK0000CD', info['platform'])
        self.assertEqual(info['cpu'], 'Different Actual CPU')
        self.assertEqual(len(info['machine_id']), 64)
        self.assertNotIn('11111111', json.dumps(info))

    def test_two_identical_laptops_share_platform_but_not_machine(self):
        first = self.report()
        second = self.report(product_uuid='22222222-2222-4222-8222-222222222222')
        self.assertEqual(first['platform_id'], second['platform_id'])
        self.assertNotEqual(first['machine_id'], second['machine_id'])
        check_platform_identity(first, second)
        with self.assertRaisesRegex(ValueError, '物理电脑'):
            check_platform_identity(first, second, physical=True)

    def test_disk_serial_has_no_effect_on_platform_or_machine(self):
        first = build_platform_snapshot(raw(disk_serial='old SSD'))
        self.assertEqual(first, build_platform_snapshot(raw(disk_serial='new SSD')))

    def test_cpu_bios_os_or_model_change_requires_new_platform(self):
        first = self.report()
        for change in (dict(cpu='Other CPU'), dict(bios='ABCN02WW'), dict(os_build='26201'),
                       dict(model='21SJ0000CD'), dict(os_arch='ARM64')):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, '新批次'):
                check_platform_identity(first, self.report(**change))

    def test_product_version_and_name_can_fill_missing_family_and_model(self):
        info = build_platform_snapshot(raw(family='Default string', model='',
                                          product_name='21TK0000CD', product_version='ThinkBook 14 G7+ IAH'))
        self.assertIn('ThinkBook 14 G7+ IAH (21TK0000CD)', info['platform'])

    def test_unknown_optional_family_does_not_pollute_report(self):
        info = build_platform_snapshot(raw(family='To be filled by O.E.M.'))
        self.assertEqual(info['platform'], 'LENOVO 21TK0000CD')

    def test_invalid_uuid_uses_system_serial_not_ssd_serial(self):
        for invalid in ('', 'not-a-uuid', '00000000-0000-0000-0000-000000000000',
                        'FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF'):
            with self.subTest(invalid=invalid):
                self.assertEqual(build_platform_snapshot(raw(product_uuid=invalid, bios_serial='HOST001'))['machine_id'],
                                 build_platform_snapshot(raw(product_uuid='', product_serial='HOST001'))['machine_id'])

    def test_invalid_all_physical_ids_fails(self):
        with self.assertRaisesRegex(ValueError, '物理身份'):
            build_platform_snapshot(raw(product_uuid='', bios_serial='Default string', product_serial='00000'))

    def test_oem_placeholder_punctuation_never_hides_real_machine_serial(self):
        for placeholder in ('To Be Filled By O.E.M', 'TO BE FILLED BY O.E.M.',
                            ' To Be  Filled By OEM ', 'System Serial Number', '123456789'):
            with self.subTest(placeholder=placeholder):
                first = self.report(product_uuid='', bios_serial=placeholder, product_serial='REAL-HOST-A')
                second = self.report(product_uuid='', bios_serial=placeholder, product_serial='REAL-HOST-B')
                self.assertNotEqual(first['machine_id'], second['machine_id'])
                with self.assertRaisesRegex(ValueError, '物理电脑'):
                    check_platform_identity(first, second, physical=True)

    def test_required_fields_cannot_silently_use_old_config(self):
        for key in ('manufacturer', 'model', 'cpu', 'os_caption', 'os_version', 'os_build', 'os_arch', 'bios'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, '不完整'):
                build_platform_snapshot(raw(**{key:''}))

    def test_processor_order_and_space_do_not_change_identity(self):
        one = build_platform_snapshot(raw(cpu=[' CPU B ', 'CPU  A', 'CPU B']))
        two = build_platform_snapshot(raw(cpu=['CPU A', 'CPU B']))
        self.assertEqual(one['platform_id'], two['platform_id'])

    def test_refresh_preserves_user_fields_and_only_queries_fixed_readonly_script(self):
        cfg = dict(report=dict(project='2260', batch='11-12', test_version='人工版本', expected_serial='SSD001',
                               platform='OLD', machine_id='stale'), extra='keep')
        runner = Mock(return_value='\ufeff' + json.dumps(raw()))
        info = refresh_platform(cfg, runner)
        runner.assert_called_once_with(PLATFORM_QUERY, timeout=30)
        self.assertEqual(cfg['detected_platform'], info)
        for key, value in dict(project='2260', batch='11-12', test_version='人工版本', expected_serial='SSD001').items():
            self.assertEqual(cfg['report'][key], value)
        self.assertEqual(cfg['extra'], 'keep')

    def test_query_failure_clears_auto_fields_without_discarding_project_or_batch(self):
        for failure in (RuntimeError('CIM offline'), None):
            cfg = {'report':dict(self.report(), project='2260', batch='11-12'), 'detected_platform':raw()}
            runner = Mock(side_effect=failure) if failure else Mock(return_value='{broken JSON')
            with self.subTest(failure=failure), self.assertRaisesRegex(ValueError, '读取测试电脑平台失败'):
                refresh_platform(cfg, runner)
            self.assertNotIn('detected_platform', cfg)
            self.assertEqual(cfg['report'], dict(project='2260', batch='11-12'))

    def test_sync_preserves_user_report_and_rejects_forged_label_before_mutation(self):
        report = dict(project='2380', batch='same', test_version='manual')
        info = build_platform_snapshot(raw()); info['cpu'] = 'old hard-coded CPU'
        before = copy.deepcopy(report)
        with self.assertRaisesRegex(ValueError, '不一致'):
            sync_platform_report(report, info)
        self.assertEqual(report, before)

    def test_sync_rejects_missing_wrong_or_boolean_schema(self):
        for schema in (None, 0, True, '1', 2):
            info = build_platform_snapshot(raw()); info['schema'] = schema
            with self.subTest(schema=schema), self.assertRaisesRegex(ValueError, '快照'):
                sync_platform_report({}, info)

    def test_manual_legacy_labels_are_not_new_identity(self):
        check_platform_identity(dict(platform='Lenovo 21SJ', cpu='Old CPU'), dict(platform='Different old label'))

    def test_new_and_legacy_cannot_mix(self):
        report = self.report()
        for before, after in (({},report),(report,{})):
            with self.assertRaisesRegex(ValueError, '新旧测试平台身份不能混用'):
                check_platform_identity(before, after)

    def test_half_identity_and_invalid_ids_do_not_become_legacy(self):
        for key, bad in (('machine_id',''),('platform_id',''),('machine_id','bogus'),('platform_id','bogus')):
            report = self.report(); report[key] = bad
            with self.subTest(key=key,bad=bad), self.assertRaisesRegex(ValueError, '身份记录'):
                check_platform_identity(report, report)

    def test_only_report_platform_fields_are_synced(self):
        info = build_platform_snapshot(raw())
        report = sync_platform_report({}, info)
        self.assertEqual(set(report), set(REPORT_PLATFORM_KEYS))


if __name__ == '__main__':
    unittest.main()
