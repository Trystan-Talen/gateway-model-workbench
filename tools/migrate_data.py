"""Copy legacy data into data/, or archive unused originals after migration."""
import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


LEGACY_NAMES = frozenset({
    'tasks.json', 'accounts.json', 'task_index.json', 'test_templates.json',
    'connections.json', 'events.jsonl', 'audit.db', 'task_index.sqlite3',
    'sessions.sqlite3', 'server.log',
})
LEGACY_DIRECTORIES = ('results', 'fingerprint_data')
DATABASE_SUFFIXES = ('.db', '.sqlite3')


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
    names={'tasks.json','accounts.json','task_index.json','test_templates.json','connections.json','events.jsonl','audit.db','task_index.sqlite3','sessions.sqlite3'}
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


def _legacy_files(source):
    """Inventory old runtime files, including each database's retained sidecars."""
    files = []
    for path in source.iterdir():
        sidecar = any(path.name == name + suffix for name in LEGACY_NAMES
                      if name.endswith(DATABASE_SUFFIXES) for suffix in ('-wal', '-shm', '-journal'))
        candidate = (path.name in LEGACY_NAMES or sidecar
                     or path.name.startswith('events.jsonl.')
                     or (path.name.startswith(('run_', 'quality_run_')) and path.suffix in {'.json', '.jsonl'}))
        if candidate:
            if path.is_symlink():
                raise ValueError('旧运行数据存在符号链接，未归档或删除任何原文件')
            if path.is_file():
                files.append(path)
    for name in LEGACY_DIRECTORIES:
        directory = source / name
        if directory.is_symlink():
            raise ValueError('旧运行数据目录存在符号链接，未归档或删除任何原文件')
        if directory.is_dir():
            for path in directory.rglob('*'):
                if path.is_symlink():
                    raise ValueError('旧运行数据存在符号链接，未归档或删除任何原文件')
                if path.is_file():
                    files.append(path)
    return sorted(files)


def _open_file_pids(files):
    """Check source files, so a service using data/ can continue running."""
    if not files:
        return []
    executable = shutil.which('lsof')
    if executable is None:
        raise ValueError('无法核实旧文件是否仍被进程使用：请安装 lsof（打开文件检查）后重试')
    pids = set()
    for offset in range(0, len(files), 128):
        try:
            result = subprocess.run([executable, '-t', '--', *map(str, files[offset:offset + 128])],
                                    capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            raise ValueError('无法核实旧文件是否仍被进程使用，未删除任何原文件') from None
        lines = result.stdout.splitlines()
        if result.returncode not in (0, 1) or result.stderr.strip() or any(not line.strip().isdigit() for line in lines):
            raise ValueError('无法核实旧文件是否仍被进程使用，未删除任何原文件')
        pids.update(int(line) for line in lines)
    return sorted(pids)


def _require_unused(files):
    pids = _open_file_pids(files)
    if pids:
        raise ValueError('旧运行数据仍被进程打开，请先停止使用旧文件的进程；编号：' + ', '.join(map(str, pids)))


def _file_signature(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _destination_observation(target, destination):
    """Observe active data without treating it as the old snapshot's authority."""
    if target.is_symlink() or not target.resolve().is_relative_to(destination):
        return {'state': 'symbolic_link_not_read'}
    if not target.exists():
        return {'state': 'missing'}
    if not target.is_file():
        return {'state': 'not_a_file'}
    try:
        before = _file_signature(target)
        # Raw hashes of a live SQLite file do not describe its committed WAL state.
        if target.suffix in DATABASE_SUFFIXES or target.name.endswith(('-wal', '-shm', '-journal')):
            return {'state': 'database_not_compared', 'bytes': before[2]}
        checksum = digest(target)
        if _file_signature(target) != before:
            return {'state': 'changed_during_inspection'}
        return {'state': 'observed', 'bytes': before[2], 'sha256': checksum}
    except OSError:
        return {'state': 'unavailable_during_inspection'}


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_archive_manifest(path, manifest):
    descriptor, temporary = tempfile.mkstemp(prefix='.manifest-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(manifest, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _restore_archived_file(original, archived, row):
    """Rollback only a removed path; never overwrite a newly created file."""
    if original.exists():
        if original.is_symlink() or digest(original) != row['sha256']:
            raise OSError('原路径已出现不同文件，不能自动覆盖恢复')
        return
    original.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with original.open('xb') as output, archived.open('rb') as source:
        shutil.copyfileobj(source, output)
        output.flush()
        os.fsync(output.fileno())
    shutil.copystat(archived, original)
    original.chmod(row['mode'])
    if digest(original) != row['sha256']:
        raise OSError('原路径恢复校验失败')
    _sync_directory(original.parent)


def _remove_empty_legacy_directories(source):
    """Remove empty old result directories; never remove unknown files or links."""
    for name in LEGACY_DIRECTORIES:
        root = source / name
        if root.is_symlink() or not root.is_dir():
            continue
        try:
            children = [path for path in root.rglob('*') if path.is_dir() and not path.is_symlink()]
        except OSError:
            continue
        for directory in sorted(children, key=lambda path: len(path.parts), reverse=True) + [root]:
            try:
                directory.rmdir()
            except OSError:
                pass


def archive_legacy(source, destination, backup):
    """Archive unused legacy originals after migration, leaving active data alone.

    Old and active files may legitimately differ. All old bytes, including SQLite
    database/WAL/SHM bundles, are verified in a unique backup before any originals
    are removed. The manifest records their original paths for offline recovery.
    """
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    backup = Path(backup).resolve()
    marker = destination / '.migrated-from-root'
    if source == destination or not source.is_dir() or not destination.is_dir():
        raise ValueError('旧来源与正式数据目录须为两个不同的现有目录')
    if marker.is_symlink() or not marker.is_file():
        raise ValueError('正式数据目录尚无迁移标记，不能归档旧来源')
    if backup.exists():
        raise ValueError('备份目录已存在，请为本次旧副本归档指定唯一的新目录')
    if (backup.is_relative_to(destination) or source.is_relative_to(backup)
            or any(backup.is_relative_to(source / name) for name in LEGACY_DIRECTORIES)):
        raise ValueError('备份目录不能位于正式数据目录、旧结果目录或旧来源的上级目录')

    files = _legacy_files(source)
    _require_unused(files)
    signatures = {path: _file_signature(path) for path in files}
    backup.mkdir(parents=True, mode=0o700)
    manifest_path = backup / 'archive-manifest.json'
    manifest = {
        'version': 1, 'mode': 'legacy_archive', 'state': 'copying',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'source': str(source), 'destination': str(destination), 'backup': str(backup),
        'migration_marker_sha256': digest(marker),
        'recovery': '停止使用原目录的服务；按 original（原路径）/backup（备份路径）映射复制回原路径，已有不同内容绝不覆盖。SQLite（嵌入式数据库）主体与 -wal/-shm/-journal（日志/共享内存/回滚日志）文件须组合恢复；不要将旧快照覆盖到正式数据目录。',
        'destination_note': '正式目录可能继续更新；所列差异仅为归档时观测，不用于覆盖或恢复判定。',
        'files': [],
    }
    removed = []
    try:
        for original in files:
            relative = original.relative_to(source)
            archived = backup / relative
            archived.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            checksum = digest(original)
            shutil.copy2(original, archived)
            archived.chmod(0o600)
            with archived.open('rb') as copied_file:
                os.fsync(copied_file.fileno())
            directory = archived.parent
            while directory.is_relative_to(backup):
                _sync_directory(directory)
                directory = directory.parent
            if (_file_signature(original) != signatures[original] or original.is_symlink()
                    or digest(original) != checksum or digest(archived) != checksum):
                raise OSError('旧文件在归档期间发生变化或复制校验失败，未开始删除原文件')
            observation = _destination_observation(destination / relative, destination)
            if observation['state'] == 'observed':
                observation['state'] = 'same_content' if observation['sha256'] == checksum else 'different_content'
            manifest['files'].append({
                'file': str(relative), 'original': str(original), 'backup': str(archived),
                'bytes': signatures[original][2], 'sha256': checksum,
                'mode': original.stat().st_mode & 0o777,
                'active_destination': observation,
            })
        manifest['state'] = 'verified'
        _write_archive_manifest(manifest_path, manifest)
        if _legacy_files(source) != files:
            raise OSError('旧运行文件清单在归档期间发生变化，未开始删除原文件')
        _require_unused(files)
        for row in manifest['files']:
            original = Path(row['original'])
            if (_file_signature(original) != signatures[original] or original.is_symlink()
                    or digest(original) != row['sha256']):
                raise OSError('旧文件在归档期间发生变化，停止删除并恢复已移除副本')
            original.unlink()
            removed.append(row)
        manifest['state'] = 'archived'
        _write_archive_manifest(manifest_path, manifest)
    except Exception:
        failures = []
        for row in reversed(removed):
            try:
                _restore_archived_file(Path(row['original']), Path(row['backup']), row)
            except OSError:
                failures.append(row['file'])
        manifest['state'] = 'rollback_incomplete' if failures else 'originals_preserved'
        manifest['recovery_required'] = failures
        try:
            _write_archive_manifest(manifest_path, manifest)
        except OSError:
            pass
        if failures:
            raise OSError(f'归档失败，部分原路径无法自动恢复；完整旧副本仍保留于 {backup}，请按恢复清单处理') from None
        raise
    _remove_empty_legacy_directories(source)
    return {
        'files': len(manifest['files']), 'bytes': sum(row['bytes'] for row in manifest['files']),
        'backup': str(backup), 'manifest': str(manifest_path), 'destination': str(destination),
        'originals_archived': True, 'destination_modified': False,
    }


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--destination',type=Path)
    parser.add_argument('--port',type=int,default=8666)
    parser.add_argument('--archive-originals', type=Path, metavar='BACKUP',
                        help='仅归档迁移后遗留的旧根副本到唯一备份目录；正式数据目录不改动')
    args=parser.parse_args()
    if args.archive_originals:
        result = archive_legacy(args.source, args.destination or args.source / 'data', args.archive_originals)
    else:
        try:
            connection=socket.create_connection(('127.0.0.1',args.port),timeout=.5)
        except OSError:
            connection=None
        if connection:
            connection.close();parser.error('请先停止项目服务，再执行数据迁移')
        result = migrate(args.source,args.destination or args.source/'data')
    print(json.dumps(result,ensure_ascii=False,indent=2))
