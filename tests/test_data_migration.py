"""Legacy archival never merges old records into active data; all data is synthetic."""
import contextlib
import io
import json
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools import migrate_data


class LegacyArchiveTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        self.source = self.base / 'project'
        self.source.mkdir()
        self.destination = self.source / 'data'
        self.destination.mkdir()
        self.marker = self.destination / '.migrated-from-root'
        self.marker.write_text('{"source":"synthetic-old-installation","files":[]}')
        self.backup = self.source / 'artifacts' / 'legacy-runtime' / 'unique-archive'
        patcher = patch.object(migrate_data, '_open_file_pids', return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, relative, content=b'synthetic-old-evidence'):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def active_bytes(self):
        return {str(path.relative_to(self.destination)): path.read_bytes()
                for path in self.destination.rglob('*') if path.is_file()}

    def test_archives_different_old_data_preserves_absolute_paths_and_active_data(self):
        paths = [
            self.write('accounts.json', b'[{"username":"synthetic-old-user"}]'),
            self.write('tasks.json', b'{"synthetic_config":"old"}'),
            self.write('server.log', b'synthetic old server output\n'),
            self.write('events.jsonl.1', b'{"event":"synthetic-old-event"}\n'),
            self.write('run_a.jsonl', b'{"response":"synthetic-old-response"}\n'),
            self.write('results/routing-old-baseline.jsonl'),
            self.write('fingerprint_data/runs/synthetic.json'),
            self.write('fingerprint_data/references/synthetic.json'),
        ]
        quality = self.write('quality_run_q.json', json.dumps({
            'id': 'q', 'out_path': str(self.source / 'quality_run_q.json'),
            'resources': [{'name': 'synthetic'}],
        }).encode())
        index = self.write('task_index.json', json.dumps({
            'tasks': [{'id': 'a', 'out_path': str(self.source / 'run_a.jsonl')}],
            'quality_tests': [{'id': 'q', 'out_path': str(quality)}],
            'routing_comparisons': [{'id': 'old', 'out_paths': {'baseline': str(paths[5])}}],
        }).encode())
        paths.extend([quality, index])
        source_bytes = {str(path.relative_to(self.source)): path.read_bytes() for path in paths}
        self.write('route_test_ui.py', b'synthetic-source-code')
        (self.destination / 'accounts.json').write_bytes(b'[{"username":"synthetic-active-user"}]')
        (self.destination / 'run_a.jsonl').write_bytes(b'{"response":"synthetic-current-response"}\n')
        (self.destination / 'run_current.jsonl').write_bytes(b'active-only-synthetic')
        active_before = self.active_bytes()

        result = migrate_data.archive_legacy(self.source, self.destination, self.backup)

        self.assertEqual(result['files'], len(source_bytes))
        self.assertTrue(result['originals_archived'])
        self.assertFalse(result['destination_modified'])
        self.assertEqual(self.active_bytes(), active_before)
        self.assertEqual((self.source / 'route_test_ui.py').read_bytes(), b'synthetic-source-code')
        for relative, content in source_bytes.items():
            self.assertFalse((self.source / relative).exists())
            self.assertEqual((self.backup / relative).read_bytes(), content)
            self.assertEqual((self.backup / relative).stat().st_mode & 0o777, 0o600)
        manifest = json.loads(Path(result['manifest']).read_text())
        self.assertEqual(manifest['state'], 'archived')
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o700)
        rows = {row['file']: row for row in manifest['files']}
        self.assertEqual(rows['accounts.json']['active_destination']['state'], 'different_content')
        self.assertEqual(rows['quality_run_q.json']['active_destination']['state'], 'missing')
        self.assertEqual(rows['run_a.jsonl']['original'], str(self.source / 'run_a.jsonl'))
        self.assertEqual(rows['run_a.jsonl']['backup'], str(self.backup / 'run_a.jsonl'))
        self.assertNotIn('synthetic-old-user', Path(result['manifest']).read_text())
        self.assertFalse((self.source / 'results').exists())
        self.assertFalse((self.source / 'fingerprint_data').exists())

    def test_archives_database_and_all_retained_sidecars_as_one_snapshot(self):
        original = self.source / 'task_index.sqlite3'
        with sqlite3.connect(original) as database:
            database.execute('CREATE TABLE synthetic_records(id TEXT)')
            database.execute('INSERT INTO synthetic_records VALUES (?)', ('old-synthetic-record',))
        # Closed synthetic sidecars exercise byte preservation, not recovery of
        # arbitrary fake WAL contents. A real committed WAL is tested separately.
        paths = [original]
        paths.extend(self.write(original.name + suffix, ('synthetic' + suffix).encode())
                     for suffix in ('-wal', '-shm', '-journal'))
        before = {path.name: path.read_bytes() for path in paths}
        (self.destination / original.name).write_bytes(b'different-live-database-not-read')
        migrate_data.archive_legacy(self.source, self.destination, self.backup)
        for name, content in before.items():
            self.assertFalse((self.source / name).exists())
            self.assertEqual((self.backup / name).read_bytes(), content)
        manifest = json.loads((self.backup / 'archive-manifest.json').read_text())
        database_row = next(row for row in manifest['files'] if row['file'] == original.name)
        self.assertEqual(database_row['active_destination']['state'], 'database_not_compared')

    def test_committed_wal_snapshot_can_be_recovered_from_the_archive(self):
        original = self.source / 'task_index.sqlite3'
        # A synthetic process exits without SQLite's normal last-close checkpoint,
        # leaving committed WAL pages with no process holding these old files.
        script = '''import os, sqlite3, sys
database = sqlite3.connect(sys.argv[1])
database.execute("PRAGMA journal_mode=WAL")
database.execute("PRAGMA wal_autocheckpoint=0")
database.execute("CREATE TABLE synthetic_records(id TEXT)")
database.execute("INSERT INTO synthetic_records VALUES ('synthetic-committed-wal')")
database.commit()
os._exit(0)
'''
        subprocess.run([sys.executable, '-c', script, str(original)], check=True)
        self.assertTrue(Path(str(original) + '-wal').exists())
        before = {path.name: path.read_bytes() for path in self.source.iterdir() if path.is_file()}
        migrate_data.archive_legacy(self.source, self.destination, self.backup)
        for name, content in before.items():
            self.assertEqual((self.backup / name).read_bytes(), content)
        with sqlite3.connect((self.backup / original.name).as_uri() + '?mode=ro', uri=True) as database:
            self.assertEqual(database.execute('SELECT id FROM synthetic_records').fetchall(), [('synthetic-committed-wal',)])

    def test_empty_directory_cleanup_preserves_unknown_files(self):
        root = self.source / 'results'
        (root / 'empty' / 'nested').mkdir(parents=True)
        unknown = root / 'unknown.txt'
        unknown.write_bytes(b'synthetic unknown file')
        migrate_data._remove_empty_legacy_directories(self.source)
        self.assertFalse((root / 'empty').exists())
        self.assertEqual(unknown.read_bytes(), b'synthetic unknown file')

    def test_missing_marker_or_reused_backup_refuses_before_changing_files(self):
        original = self.write('run_a.jsonl')
        self.marker.unlink()
        with self.assertRaisesRegex(ValueError, '迁移标记'):
            migrate_data.archive_legacy(self.source, self.destination, self.backup)
        self.assertTrue(original.exists())
        self.assertFalse(self.backup.exists())
        self.marker.write_text('{}')
        self.backup.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, '唯一'):
            migrate_data.archive_legacy(self.source, self.destination, self.backup)
        self.assertTrue(original.exists())

    def test_backup_inside_active_or_old_result_directory_is_refused(self):
        original = self.write('run_a.jsonl')
        for backup in (self.destination / 'archive', self.source / 'results' / 'archive',
                       self.source / 'fingerprint_data' / 'archive', self.base / 'new-parent' / '..'):
            with self.subTest(backup=backup), self.assertRaises(ValueError):
                migrate_data.archive_legacy(self.source, self.destination, backup)
        self.assertTrue(original.exists())

    def test_open_old_file_refuses_but_does_not_require_stopping_active_data_service(self):
        original = self.write('run_a.jsonl')
        with patch.object(migrate_data, '_open_file_pids', return_value=[731]):
            with self.assertRaisesRegex(ValueError, '731'):
                migrate_data.archive_legacy(self.source, self.destination, self.backup)
        self.assertTrue(original.exists())
        self.assertFalse(self.backup.exists())

    def test_archive_cli_does_not_probe_or_stop_active_data_service(self):
        original = self.write('run_a.jsonl')
        output = io.StringIO()
        command = [str(Path(migrate_data.__file__)), '--source', str(self.source),
                   '--destination', str(self.destination), '--archive-originals', str(self.backup)]
        result = SimpleNamespace(returncode=1, stdout='', stderr='')
        with patch.object(sys, 'argv', command), contextlib.redirect_stdout(output), \
                patch.object(migrate_data.shutil, 'which', return_value='/synthetic/lsof'), \
                patch.object(migrate_data.subprocess, 'run', return_value=result), \
                patch.object(migrate_data.socket, 'create_connection', side_effect=AssertionError('formal service must remain running')):
            runpy.run_path(command[0], run_name='__main__')
        response = json.loads(output.getvalue())
        self.assertEqual(response['files'], 1)
        self.assertFalse(response['destination_modified'])
        self.assertFalse(original.exists())
        self.assertTrue((self.backup / original.name).is_file())

    def test_symlink_files_and_directories_are_not_followed(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'run_other.jsonl').write_bytes(b'outside-synthetic')
        for relative, target in (('run_a.jsonl', outside / 'run_other.jsonl'), ('results', outside)):
            path = self.source / relative
            path.symlink_to(target)
            with self.subTest(relative=relative), self.assertRaisesRegex(ValueError, '符号链接'):
                migrate_data.archive_legacy(self.source, self.destination, self.backup)
            path.unlink()
        self.assertEqual((outside / 'run_other.jsonl').read_bytes(), b'outside-synthetic')

    def test_changed_source_during_copy_preserves_originals_and_does_not_touch_active_data(self):
        original = self.write('run_a.jsonl')
        active_before = self.active_bytes()
        copy = migrate_data.shutil.copy2

        def changed(source, target):
            result = copy(source, target)
            source.write_bytes(b'synthetic-new-content')
            return result

        with patch.object(migrate_data.shutil, 'copy2', side_effect=changed):
            with self.assertRaisesRegex(OSError, '变化'):
                migrate_data.archive_legacy(self.source, self.destination, self.backup)
        self.assertEqual(original.read_bytes(), b'synthetic-new-content')
        self.assertEqual(self.active_bytes(), active_before)

    def test_unlink_failure_restores_every_previously_removed_original(self):
        paths = [self.write('accounts.json'), self.write('run_a.jsonl')]
        before = {path: path.read_bytes() for path in paths}
        real_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == paths[1]:
                raise OSError('synthetic permission error')
            return real_unlink(path, *args, **kwargs)

        with patch.object(Path, 'unlink', unlink):
            with self.assertRaisesRegex(OSError, 'synthetic permission error'):
                migrate_data.archive_legacy(self.source, self.destination, self.backup)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual((self.backup / path.name).read_bytes(), content)
        manifest = json.loads((self.backup / 'archive-manifest.json').read_text())
        self.assertEqual(manifest['state'], 'originals_preserved')
        self.assertEqual(manifest['recovery_required'], [])

    def test_manifest_commit_failure_rolls_back_all_removed_originals(self):
        original = self.write('run_a.jsonl')
        before = original.read_bytes()
        writer = migrate_data._write_archive_manifest

        def fail_final_manifest(path, manifest):
            if manifest['state'] == 'archived':
                raise OSError('synthetic manifest failure')
            return writer(path, manifest)

        with patch.object(migrate_data, '_write_archive_manifest', side_effect=fail_final_manifest):
            with self.assertRaisesRegex(OSError, 'synthetic manifest failure'):
                migrate_data.archive_legacy(self.source, self.destination, self.backup)
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual((self.backup / original.name).read_bytes(), before)


class OpenFileInspectionTests(unittest.TestCase):
    def test_file_arguments_are_passed_without_a_shell_and_pids_are_deduplicated(self):
        result = SimpleNamespace(returncode=0, stdout='123\n123\n456\n', stderr='')
        files = [Path('/synthetic path/accounts.json'), Path('/synthetic path/run_中文.jsonl')]
        with patch.object(migrate_data.shutil, 'which', return_value='/synthetic/lsof'), \
                patch.object(migrate_data.subprocess, 'run', return_value=result) as run:
            self.assertEqual(migrate_data._open_file_pids(files), [123, 456])
        self.assertEqual(run.call_args.args[0], ['/synthetic/lsof', '-t', '--', *map(str, files)])
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_unavailable_checker_errors_and_timeouts_fail_closed(self):
        files = [Path('/synthetic/run_a.jsonl')]
        with patch.object(migrate_data.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(ValueError, '无法核实'):
                migrate_data._open_file_pids(files)
        with patch.object(migrate_data.shutil, 'which', return_value='/synthetic/lsof'):
            for result in (SimpleNamespace(returncode=2, stdout='', stderr=''),
                           SimpleNamespace(returncode=1, stdout='', stderr='permission denied'),
                           SimpleNamespace(returncode=0, stdout='bad pid', stderr='')):
                with self.subTest(result=result), patch.object(migrate_data.subprocess, 'run', return_value=result):
                    with self.assertRaisesRegex(ValueError, '无法核实'):
                        migrate_data._open_file_pids(files)
            with patch.object(migrate_data.subprocess, 'run', side_effect=subprocess.TimeoutExpired('lsof', 15)):
                with self.assertRaisesRegex(ValueError, '无法核实'):
                    migrate_data._open_file_pids(files)


class ExistingMigrationContractTests(unittest.TestCase):
    def test_existing_migration_preserves_originals_and_refuses_marker_reentry(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / 'run_a.jsonl').write_bytes(b'synthetic evidence')
            destination = source / 'data'
            result = migrate_data.migrate(source, destination)
            self.assertTrue(result['originals_preserved'])
            self.assertEqual((source / 'run_a.jsonl').read_bytes(), (destination / 'run_a.jsonl').read_bytes())
            with self.assertRaisesRegex(ValueError, '已迁移'):
                migrate_data.migrate(source, destination)


if __name__ == '__main__':
    unittest.main()
