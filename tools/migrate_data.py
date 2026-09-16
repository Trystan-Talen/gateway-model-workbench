"""Copy legacy runtime data into data/ without deleting or altering evidence files."""
import argparse
import hashlib
import json
import shutil
import socket
import sqlite3
from pathlib import Path


def digest(path):
    result=hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):
            result.update(chunk)
    return result.hexdigest()


def migrate(source, destination):
    source=Path(source).resolve();destination=Path(destination).resolve()
    if destination==source:
        raise ValueError('目标目录不能与来源相同')
    marker=destination/'.migrated-from-root'
    if marker.exists():
        raise ValueError('此目录已迁移；拒绝用旧数据覆盖正在使用的数据')
    destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    names={'tasks.json','accounts.json','task_index.json','test_templates.json','events.jsonl','audit.db','task_index.sqlite3','sessions.sqlite3'}
    files=[p for p in source.iterdir() if p.is_file() and (p.name in names or (p.name.startswith(('run_','quality_run_')) and p.suffix in {'.json','.jsonl'}))]
    for folder in ('results','fingerprint_data'):
        if (source/folder).is_dir():
            files.extend(p for p in (source/folder).rglob('*') if p.is_file() and not p.is_symlink())
    copied=[]
    for original in files:
        if original.is_symlink():
            raise ValueError('运行数据存在符号链接，请先确认数据归属')
        relative=original.relative_to(source);target=destination/relative
        target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if target.exists():
            if digest(target)!=digest(original):
                raise ValueError(f'目标已存在不同内容，未覆盖：{relative}')
        elif original.suffix in {'.db','.sqlite3'}:
            src=sqlite3.connect(f'file:{original}?mode=ro',uri=True);dst=sqlite3.connect(target)
            try:
                src.backup(dst)
            finally:
                dst.close();src.close()
            target.chmod(0o600)
        else:
            shutil.copy2(original,target);target.chmod(0o600)
            if digest(original)!=digest(target):
                raise OSError(f'复制校验失败：{relative}')
        copied.append({'file':str(relative),'bytes':target.stat().st_size,'sha256':digest(target)})
    # Marker is written last: startup switches only after every file is verified.
    marker.write_text(json.dumps({'source':str(source),'files':copied},ensure_ascii=False,indent=2))
    marker.chmod(0o600)
    return {'files':len(copied),'bytes':sum(x['bytes'] for x in copied),'destination':str(destination),'originals_preserved':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--destination',type=Path)
    parser.add_argument('--port',type=int,default=8666)
    args=parser.parse_args()
    try:
        connection=socket.create_connection(('127.0.0.1',args.port),timeout=.5)
    except OSError:
        connection=None
    if connection:
        connection.close();parser.error('请先停止项目服务，再执行数据迁移')
    print(json.dumps(migrate(args.source,args.destination or args.source/'data'),ensure_ascii=False,indent=2))
