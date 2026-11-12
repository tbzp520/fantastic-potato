"""Verify public source, safe examples and startup hashes without devices or network."""
import argparse
import ast
import hashlib
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'runtime', 'wheelhouse', 'drivers', 'prerequisites', '.venv', '__pycache__', 'node_modules'}
SITE_NAMES = {'config.json', '双机配置.json', 'dual_config.json', 'dual_settings.json', 'known_hosts', 'id_rsa', 'id_ed25519'}
SITE_DIRS = {'部署记录', '运行记录', '启动记录', '配置备份', 'clock_sync', 'browser_profile',
             'deliveries', 'deliveries_dual', '手工复测', '双机工作区', '人工复算结果',
             '原始主机数据', '诊断包', '诊断记录', 'jobs', 'output', 'outputs', 'tmp',
             'diagnostics', 'reports', 'results'}
SECRET_KEYS = {'password', 'passwd', 'secret', 'token', 'access_token', 'api_key', 'private_key'}
BINDING_KEYS = {'machine_id', 'platform_id', 'expected_serial', 'system_disk_model', 'online_adapter_guid',
                'expected_sensor_group', 'hid_path_hex', 'expected_pci', 'host_key', 'key_filename', 'known_hosts'}
EMPTY_STATE_KEYS = {'detected_platform', 'detected_disk', 'sample_assignment', 'system_disk_binding',
                    'logging_verification', 'script_verification', 'native_edge_verification'}
FALSE_KEYS = {'allow_destructive', 'deployment_complete', 'protocol_verified', 'smart_refresh_verified'}


def demand(condition, message):
    if not condition:
        raise ValueError(message)


def validate_ntp(servers, location):
    demand(isinstance(servers, list), location+': NTP must be a list')
    for host in servers:
        demand(isinstance(host, str) and host.strip(), location+': invalid NTP entry')
        value = host.strip().strip('[]')
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            demand(value.lower() != 'localhost' and not value.lower().endswith(('.local', '.internal', '.lan')),
                   location+': NTP must use a public example, not an onsite hostname')
        else:
            demand(address.is_global, location+': NTP must not contain private/local addresses')


def validate_example(configuration, label):
    demand(isinstance(configuration, dict), label+': configuration must be an object')

    def walk(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                location = path+'.'+key
                if key in SECRET_KEYS or key in BINDING_KEYS or key in EMPTY_STATE_KEYS:
                    demand(not item, location+': public example must be blank')
                if key in FALSE_KEYS:
                    demand(not item, location+': public example must not authorize a device/test')
                if key == 'ssh':
                    demand(isinstance(item, dict), location+': SSH must be an object')
                    for field in ('host', 'device', 'expected_pci', 'password', 'key_filename', 'known_hosts', 'host_key'):
                        demand(not item.get(field), location+'.'+field+': SSH example must be blank')
                if key == 'ntp_servers':
                    validate_ntp(item, location)
                if key == 'usb' and isinstance(item, dict):
                    for field in ('volume_serial', 'volume_label', 'device_id', 'pnp_device_id', 'serial', 'guid'):
                        demand(not item.get(field), location+'.'+field+': USB binding must be blank')
                walk(item, location)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, path+'['+str(index)+']')

    walk(configuration, label)


def literal_assignment(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('Missing source constant: '+name)


def verify_startup(root):
    folder = root/'windows'
    manifest = json.loads((folder/'发布信息.json').read_text('utf-8-sig'))
    launcher = ast.parse((folder/'启动校验.py').read_text('utf-8-sig'))
    expected = literal_assignment(launcher, 'EXPECTED_VERSION')
    required = set(literal_assignment(launcher, 'REQUIRED_FILES'))
    for node in launcher.body:
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute) and isinstance(node.value.func.value, ast.Name)
                and node.value.func.value.id == 'REQUIRED_FILES'):
            call = node.value
            if call.func.attr == 'add':
                required.add(ast.literal_eval(call.args[0]))
            elif call.func.attr == 'update':
                required.update(ast.literal_eval(call.args[0]))
    main = ast.parse((folder/'windows_temperature_test.py').read_text('utf-8-sig'))
    demand(manifest.get('version') == expected == literal_assignment(main, 'VERSION'), 'Windows version constants/manifest differ')
    files = manifest.get('files')
    demand(isinstance(files, dict) and required.issubset(files), 'Windows startup file list is incomplete')
    for name, expected_hash in files.items():
        demand(isinstance(name, str) and '/' not in name and '\\' not in name and ':' not in name
               and name not in ('', '.', '..'), 'Invalid startup manifest filename')
        demand(isinstance(expected_hash, str) and re.fullmatch('[0-9a-f]{64}', expected_hash), 'Invalid startup SHA256')
        path = folder/name
        demand(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash,
               'Wrong startup hash: windows/'+name)
    return len(files)


def verify_file_list(folder):
    manifest = folder/'FILES.sha256'
    demand(manifest.is_file(), 'Missing source FILES.sha256: '+folder.name)
    seen = set()
    for line in manifest.read_text('utf-8-sig').splitlines():
        digest, separator, name = line.partition('  ')
        relative = PurePosixPath(name)
        demand(separator and re.fullmatch('[0-9a-f]{64}', digest) and name and not relative.is_absolute()
               and '..' not in relative.parts and '\\' not in name and ':' not in name and name not in seen,
               'Invalid source FILES.sha256 entry in '+folder.name)
        seen.add(name)
        path = folder/name
        demand(path.is_file() and path.resolve().is_relative_to(folder.resolve())
               and hashlib.sha256(path.read_bytes()).hexdigest() == digest,
               'Wrong source hash: '+folder.name+'/'+name)
    return len(seen)


def verify_repository(root, check_files=False):
    root = Path(root).resolve()
    syntax_count, example_count, source_hashes = 0, 0, 0
    for platform in ('windows', 'linux'):
        folder = root/platform
        demand((folder/'config.example.json').is_file(), 'Missing public example: '+platform)
        for path in sorted(folder.rglob('*')):
            relative = path.relative_to(folder)
            if any(part in SKIP for part in relative.parts) or not path.is_file():
                continue
            demand(relative.parts[0] not in SITE_DIRS, 'Onsite data directory in public source: '+platform+'/'+relative.parts[0])
            demand(not path.is_symlink() and path.resolve().is_relative_to(folder.resolve()), 'Source link is not allowed: '+platform+'/'+relative.as_posix())
            demand(path.name not in SITE_NAMES and path.suffix.lower() not in {'.pem', '.key'}
                   and not path.name.startswith('.env'), 'Onsite configuration/key in public source: '+platform+'/'+relative.as_posix())
            if path.suffix == '.py':
                ast.parse(path.read_text('utf-8-sig'), filename=str(path))
                syntax_count += 1
            if path.name.endswith('.example.json'):
                validate_example(json.loads(path.read_text('utf-8-sig')), platform+'/'+relative.as_posix())
                example_count += 1
            # Inspect NTP defaults in production files; local UI callback addresses and test fixtures are unrelated.
            if 'tests' not in relative.parts and path.suffix in {'.py', '.ps1', '.md', '.html', '.txt'}:
                for line in path.read_text('utf-8-sig').splitlines():
                    if 'ntp' not in line.lower():
                        continue
                    for candidate in re.findall(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])', line):
                        try:
                            address = ipaddress.ip_address(candidate)
                        except ValueError:
                            continue
                        demand(address.is_global, 'Private NTP default in '+platform+'/'+relative.as_posix())
        if check_files:
            source_hashes += verify_file_list(folder)
    startup_hashes = verify_startup(root)
    return {'python_files': syntax_count, 'startup_hashes': startup_hashes,
            'safe_examples': example_count, 'source_hashes': source_hashes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--check-files', action='store_true', help='Also verify generated source FILES.sha256 lists')
    args = parser.parse_args()
    result = verify_repository(args.root, args.check_files)
    print('PASS: '+json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
