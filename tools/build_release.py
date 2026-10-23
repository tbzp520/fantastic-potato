"""Build full release ZIPs from source plus local, licensed dependency folders."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]
SKIP={'__pycache__','.venv','node_modules','部署记录','运行记录','deliveries','配置备份'}

def collect(folder):
    return {p.relative_to(folder).as_posix():p.read_bytes() for p in sorted(folder.rglob('*'))
            if p.is_file() and not any(x in SKIP for x in p.relative_to(folder).parts)
            and p.suffix not in {'.pyc','.log','.tmp','.partial'} and p.name not in {'config.json','FILES.sha256'}}

def write_zip(files,path,prefix):
    files=dict(files)
    files['FILES.sha256']=''.join(hashlib.sha256(value).hexdigest()+'  '+name+'\n'
                                  for name,value in sorted(files.items())).encode('utf-8')
    path.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name,value in sorted(files.items()):z.writestr(prefix+name,value)
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None
        for name,value in files.items():assert z.read(prefix+name)==value
    checksum=hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix('.zip.sha256').write_text(checksum+'  '+path.name+'\n','utf-8')
    return {'name':path.name,'bytes':path.stat().st_size,'files':len(files),'sha256':checksum}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--windows-dependencies',type=Path,required=True,
                        help='Folder containing runtime, drivers and prerequisites')
    parser.add_argument('--linux-wheelhouse',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    win=collect(ROOT/'windows')
    for name in ('runtime','drivers','prerequisites'):
        folder=args.windows_dependencies/name
        if not folder.is_dir():raise ValueError('Missing dependency folder: '+str(folder))
        win.update({name+'/'+k:v for k,v in collect(folder).items()})
    win['LICENSE']=(ROOT/'LICENSE').read_bytes()
    assert 'config.json' not in win
    release=json.loads(win['发布信息.json'])
    for name,expected in release['files'].items():assert hashlib.sha256(win[name]).hexdigest()==expected,name
    lin=collect(ROOT/'linux')
    if not list(args.linux_wheelhouse.glob('*.whl')):raise ValueError('No offline wheels found')
    lin.update({'wheelhouse/'+k:v for k,v in collect(args.linux_wheelhouse).items()})
    lin['config.json']=lin['config.example.json']
    cfg=json.loads(lin['config.json']);assert cfg['ssh']['password']=='' and not cfg['ssh']['allow_destructive']
    lin['LICENSE']=(ROOT/'LICENSE').read_bytes()
    update={k:v for k,v in lin.items() if k not in {'config.json','config.example.json','温度模版_原始.xlsx'}
            and not k.startswith('wheelhouse/')}
    info=[write_zip(win,args.output/'JK808_Windows_v0.4.12_full.zip','JK808_Windows_v0.4.12/'),
          write_zip(lin,args.output/'JK808_Linux_v2.2.4_full.zip','JK808_Linux_v2/'),
          write_zip(update,args.output/'JK808_Linux_v2.2.4_update.zip','')]
    (args.output/'release-manifest.json').write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n','utf-8')
    print(json.dumps(info,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
