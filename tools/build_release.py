"""Build public ZIPs from verified source and explicitly supplied dependencies."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

# The portable Python runtime uses an isolated ._pth and does not add script-dir.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_source import verify_repository

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', '__pycache__', '.venv', 'node_modules', '部署记录', '运行记录',
        '启动记录', '配置备份', 'clock_sync', 'browser_profile', 'deliveries',
        'deliveries_dual', '手工复测', '双机工作区', '人工复算结果', '原始主机数据',
        '诊断包', '诊断记录', 'jobs', 'output', 'outputs', 'tmp',
        'diagnostics', 'reports', 'results', 'runtime', 'wheelhouse', 'drivers', 'prerequisites'}
SOURCE_SUFFIXES = {'.py', '.cmd', '.ps1', '.txt', '.lock', '.md', '.html', '.xlsx', '.bitcfg'}
PRIVATE_NAMES = {'config.json', '双机配置.json', 'dual_config.json', 'dual_settings.json',
                 'known_hosts', 'id_rsa', 'id_ed25519', 'files.sha256', 'thumbs.db', '.ds_store'}
PRIVATE_SUFFIXES = {'.pyc', '.log', '.tmp', '.partial', '.pem', '.key', '.zip', '.bak'}


def excluded(relative):
    parts = [part.casefold() for part in relative.parts]
    return (any(part in SKIP or part.startswith(('.venv_', '配置备份', '部署记录', '运行记录',
                                              '启动记录', 'browser_profile', 'deliveries')) for part in parts)
            or relative.name.casefold() in PRIVATE_NAMES
            or (relative.name.casefold().startswith(('.env', 'config.backup', '双机配置.'))
                and not relative.name.endswith('.example.json'))
            or relative.suffix.casefold() in PRIVATE_SUFFIXES)


def collect_source(folder):
    """Include code/docs/templates, never arbitrary JSON or site data."""
    files = {}
    for path in sorted(folder.rglob('*')):
        relative = path.relative_to(folder)
        if not path.is_file() or path.is_symlink() or excluded(relative):
            continue
        if not path.resolve().is_relative_to(folder.resolve()):
            raise ValueError('Source link escapes the selected folder')
        if path.suffix.lower() not in SOURCE_SUFFIXES and not (
                path.name == '发布信息.json' or path.name.endswith('.example.json')):
            continue
        files[relative.as_posix()] = path.read_bytes()
    return files


def collect_dependencies(folder):
    """Only the explicitly selected dependency directory is traversed."""
    files = {}
    for path in sorted(folder.rglob('*')):
        relative = path.relative_to(folder)
        if path.is_file() and not path.is_symlink() and not excluded(relative):
            if not path.resolve().is_relative_to(folder.resolve()):
                raise ValueError('Dependency link escapes the selected folder')
            files[relative.as_posix()] = path.read_bytes()
    return files


def version_number(value):
    match = re.fullmatch(r'(\d+\.\d+\.\d+)(?:\s+[^\r\n]+)?', value)
    if not match:
        raise ValueError('Invalid release version')
    return match[1]


def versions(root, windows_override=None, linux_override=None):
    release = json.loads((root/'windows/发布信息.json').read_text('utf-8-sig'))
    windows = version_number(release['version'])
    launcher = (root/'linux/start_linux.ps1').read_text('utf-8-sig')
    found = set(re.findall(r"\bversion\s*=\s*['\"](\d+\.\d+\.\d+)['\"]", launcher))
    if len(found) != 1:
        raise ValueError('Linux launcher must declare one unambiguous release version')
    linux = found.pop()
    for detected, requested in ((windows, windows_override), (linux, linux_override)):
        if requested is not None and (version_number(requested) != requested or requested != detected):
            raise ValueError('Explicit version does not match verified source')
    return windows, linux


def write_zip(files, path, prefix):
    files = dict(files)
    files['FILES.sha256'] = ''.join(hashlib.sha256(value).hexdigest()+'  '+name+'\n'
                                    for name, value in sorted(files.items())).encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.partial')
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, value in sorted(files.items()):
                if not name or name.startswith(('/', '\\')) or '..' in Path(name).parts:
                    raise ValueError('Invalid archive entry')
                archive.writestr(prefix+name, value)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError('ZIP CRC verification failed')
            for name, value in files.items():
                if archive.read(prefix+name) != value:
                    raise ValueError('ZIP byte verification failed: '+name)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix('.zip.sha256').write_text(checksum+'  '+path.name+'\n', 'utf-8')
    return {'name': path.name, 'bytes': path.stat().st_size, 'files': len(files), 'sha256': checksum}


def build(root, windows_dependencies, linux_wheelhouse, output,
          windows_version=None, linux_version=None):
    root, output = Path(root).resolve(), Path(output).resolve()
    verify_repository(root)
    windows, linux = versions(root, windows_version, linux_version)
    if output.is_relative_to(root):
        raise ValueError('Build output must be outside the source checkout')
    win = collect_source(root/'windows')
    for name in ('runtime', 'drivers', 'prerequisites'):
        folder = Path(windows_dependencies)/name
        if not folder.is_dir():
            raise ValueError('Missing dependency folder: '+str(folder))
        dependencies = collect_dependencies(folder)
        if not dependencies:
            raise ValueError('Empty dependency folder: '+str(folder))
        win.update({name+'/'+key: value for key, value in dependencies.items()})
    win['LICENSE'] = (root/'LICENSE').read_bytes()
    wheelhouse = Path(linux_wheelhouse)
    wheels = {path.name: path.read_bytes() for path in sorted(wheelhouse.glob('*.whl'))
              if path.is_file() and not path.is_symlink()}
    if not wheels:
        raise ValueError('No offline wheels found')
    lin = collect_source(root/'linux')
    lin.update({'wheelhouse/'+name: value for name, value in wheels.items()})
    lin['config.json'] = lin['config.example.json']
    lin['LICENSE'] = (root/'LICENSE').read_bytes()
    update = {name: value for name, value in lin.items()
              if not name.startswith('wheelhouse/') and name not in {'config.json', '温度模版_原始.xlsx'}
              and not name.endswith('.example.json')}
    info = [write_zip(win, output/f'JK808_Windows_v{windows}_full.zip', f'JK808_Windows_v{windows}/'),
            write_zip(lin, output/f'JK808_Linux_v{linux}_full.zip', 'JK808_Linux_v2/'),
            write_zip(update, output/f'JK808_Linux_v{linux}_update.zip', '')]
    (output/'release-manifest.json').write_text(json.dumps(info, ensure_ascii=False, indent=2)+'\n', 'utf-8')
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--windows-dependencies', type=Path, required=True,
                        help='Folder containing runtime, drivers and prerequisites')
    parser.add_argument('--linux-wheelhouse', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--windows-version', help='Optional assertion against the Windows source version')
    parser.add_argument('--linux-version', help='Optional assertion against the Linux launcher version')
    args = parser.parse_args()
    result = build(ROOT, args.windows_dependencies, args.linux_wheelhouse, args.output,
                   args.windows_version, args.linux_version)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
