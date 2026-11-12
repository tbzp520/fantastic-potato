"""平台身份测试数据；只用常量构建，禁止查询本机CIM。"""
import copy
from platform_identity import build_platform_snapshot, sync_platform_report


def platform_raw(**changes):
    raw = dict(manufacturer='LENOVO', model='UNIT-LAPTOP-A', family='Unit Test Laptop',
               product_uuid='D7F27D48-24A3-4EB4-8C0D-E0F8F5F74100', cpu=['Unit Test CPU'],
               os_caption='Microsoft Windows 11 Pro', os_version='10.0.26100',
               os_build='26100', os_arch='64-bit', bios='UNIT.001')
    raw.update(changes)
    return raw


def platform_snapshot(**changes):
    return build_platform_snapshot(platform_raw(**changes))


def attach_platform(cfg, **changes):
    snapshot = platform_snapshot(**changes)
    cfg['detected_platform'] = copy.deepcopy(snapshot)
    sync_platform_report(cfg['report'], snapshot)
    return cfg


def fixture_platform_refresh(cfg):
    attach_platform(cfg)
    return cfg['detected_platform']
