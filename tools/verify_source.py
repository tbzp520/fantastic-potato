"""Verify syntax and Windows startup hashes without connecting hardware."""
import ast
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
count = 0
for folder in ('windows', 'linux'):
    for path in (root/folder).rglob('*.py'):
        if any(p in {'runtime', '.venv', '__pycache__'} for p in path.parts):
            continue
        ast.parse(path.read_text('utf-8-sig'), filename=str(path))
        count += 1
manifest = json.loads((root/'windows/发布信息.json').read_text('utf-8'))
for name, expected in manifest['files'].items():
    actual = hashlib.sha256((root/'windows'/name).read_bytes()).hexdigest()
    assert actual == expected, f'Wrong file version: {name}'
for folder in ('windows', 'linux'):
    cfg = json.loads((root/folder/'config.example.json').read_text('utf-8'))
    assert not cfg.get('ssh', {}).get('password'), 'Example SSH password must be blank'
print(f'PASS: {count} Python files and {len(manifest["files"])} startup hashes')
