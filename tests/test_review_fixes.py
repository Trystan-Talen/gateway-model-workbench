"""Security and migration regressions introduced by the source review."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from session_store import SessionStore
from history_store import HistoryStore, quality_index
import credential_store
import route_test_ui as ui


class SessionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'sessions.sqlite3'
        self.now = 100
        self.store = SessionStore(self.path, lifetime=100, idle_timeout=20, clock=lambda: self.now)

    def test_server_enforces_idle_expiry_and_rejects_replay(self):
        self.store['bearer-secret'] = 'user'
        self.now = 121
        self.assertIsNone(self.store.get('bearer-secret'))
        self.now = 100
        self.assertIsNone(self.store.get('bearer-secret'))

    def test_absolute_expiry_cannot_be_extended_by_activity(self):
        self.store['secret'] = 'user'
        for now in range(110, 200, 10):
            self.now = now
            self.assertEqual('user', self.store.get('secret'))
        self.now = 200
        self.assertIsNone(self.store.get('secret'))

    def test_restart_logout_and_hashed_token_storage(self):
        self.store['bearer-secret'] = 'user'
        restarted = SessionStore(self.path, clock=lambda: self.now)
        self.assertEqual('user', restarted.get('bearer-secret'))
        self.assertNotIn(b'bearer-secret', self.path.read_bytes())
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        restarted.pop('bearer-secret')
        self.assertIsNone(self.store.get('bearer-secret'))

    def test_deleted_account_sessions_cannot_be_reused_for_recreated_username(self):
        self.store['first'] = 'user'
        self.store['second'] = 'user'
        self.store['other'] = 'another'
        self.store.revoke_user('user')
        self.assertIsNone(self.store.get('first'))
        self.assertIsNone(self.store.get('second'))
        self.assertEqual('another', self.store.get('other'))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'index.json'
        self.store = HistoryStore(self.path)

    def test_legacy_migration_keeps_original_and_removes_evidence_duplication(self):
        data = {'quality_tests': [{'id':'q', 'name':'质量测试', 'config':{'case_count':120},
                                 'resources':[{'name':'A','case_results':[{'response':'x'*100000}]}],
                                 'report':{'cases':['x'*100000]}}]}
        raw = json.dumps(data)
        self.path.write_text(raw)
        restored = self.store.read()
        self.assertEqual(self.path.read_text(), raw)
        self.assertNotIn('case_results', restored['quality_tests'][0]['resources'][0])
        self.assertNotIn('report', restored['quality_tests'][0])
        self.assertEqual('q', restored['quality_tests'][0]['id'])
        self.assertLess(len(json.dumps(restored)), 1000)

    def test_deletion_is_transactional_and_does_not_resurrect_legacy_records(self):
        self.path.write_text(json.dumps({'tasks':[{'id':'old'}]}))
        self.store.read()
        self.store.write({})
        self.assertEqual(self.store.read()['tasks'], [])

    def test_unchanged_record_is_not_updated(self):
        self.store.write({'tasks':[{'id':'one','status':'finished'}]})
        db=sqlite3.connect(self.store.path)
        try:
            db.execute('CREATE TABLE update_count(n INTEGER)')
            db.execute('CREATE TRIGGER count_inserts AFTER INSERT ON records BEGIN INSERT INTO update_count VALUES(1); END')
            db.commit()
            self.store.write({'tasks':[{'id':'one','status':'finished'}]})
            self.assertEqual(db.execute('SELECT count(*) FROM update_count').fetchone()[0],0)
            self.store.write({'tasks':[{'id':'one','status':'error'}]})
            self.assertEqual(db.execute('SELECT count(*) FROM update_count').fetchone()[0],1)
        finally:
            db.close()


class CredentialTests(unittest.TestCase):
    def test_os_backend_receives_secret_without_spawning_subprocess(self):
        store=Mock()
        with patch.object(credential_store,'backend',return_value=store), patch('subprocess.run') as run:
            self.assertTrue(credential_store.save('https://example.test/v1','synthetic-secret'))
        store.set_password.assert_called_once_with(credential_store.SERVICE,credential_store.account('https://example.test/v1'),'synthetic-secret')
        run.assert_not_called()

    def test_missing_secure_store_never_claims_saved(self):
        with patch.object(credential_store,'backend',return_value=None):
            self.assertFalse(credential_store.save('https://example.test','synthetic-secret'))
            self.assertEqual('',credential_store.load('https://example.test'))

    def test_history_config_read_does_not_access_system_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            config=Path(folder)/'tasks.json'
            config.write_text(json.dumps({'last_config':{'url':'https://example.test','model':'m'}}))
            with patch.object(ui,'TASKS_FILE',str(config)), patch.object(ui,'_load_api_key') as read_key:
                ui.read_saved_config(include_credential=False)
                read_key.assert_not_called()


class HTTPPolicyTests(unittest.TestCase):
    def handler(self, headers=None):
        handler=ui.Handler.__new__(ui.Handler)
        handler.headers=headers or {}
        handler.path='/tasks'
        handler.command='POST'
        handler._json=Mock()
        return handler

    def test_cross_site_post_is_blocked_before_reading_body(self):
        handler=self.handler({'Sec-Fetch-Site':'cross-site'})
        handler._do_post=Mock()
        handler._note=Mock()
        handler.do_POST()
        handler._do_post.assert_not_called()
        self.assertEqual(handler._json.call_args.args[1],403)

    def test_origin_and_fetch_metadata(self):
        for headers,expected in [({},True),({'Host':'127.0.0.1:8666','Origin':'http://127.0.0.1:8666'},True),({'Host':'127.0.0.1:8666','Origin':'https://evil.test'},False),({'Origin':'null'},False)]:
            self.assertEqual(self.handler(headers)._same_origin(),expected)

    def test_internal_error_never_returns_or_logs_secret_exception_text(self):
        handler=self.handler()
        with patch.object(ui,'EVENTS') as events:
            try:
                raise RuntimeError('/private/account synthetic-secret')
            except RuntimeError as exc:
                handler._internal_error(exc)
        self.assertNotIn('synthetic-secret', str(handler._json.call_args)+str(events.log.call_args))
        self.assertTrue(handler._json.call_args.args[0]['request_id'])
        self.assertIn('frames',events.log.call_args.kwargs)

    def test_strict_script_policy(self):
        policy=self.handler()._security_headers()['Content-Security-Policy']
        script=next(item for item in policy.split(';') if 'script-src' in item)
        self.assertNotIn('unsafe-inline',script)
        self.assertNotIn('unsafe-eval',script)
        self.assertIn('/static/app.js',ui.INDEX_HTML)
        self.assertNotIn('<script>',ui.INDEX_HTML)

    def test_list_summary_omits_heavy_evidence(self):
        snapshot={'id':'one','name':'x','status':'finished','recent':[1]*100,'timeline':[1]*1000,'resources':[{'name':'A','case_results':[1]*1000}]}
        summary=ui.list_snapshot(snapshot)
        self.assertLess(len(json.dumps(summary)),300)
        self.assertNotIn('recent',summary)
        self.assertNotIn('case_results',summary['resources'][0])


class DataMigrationTests(unittest.TestCase):
    def test_copy_preserves_evidence_bytes_and_rejects_reimport(self):
        from tools.migrate_data import migrate
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'project';source.mkdir()
            (source/'run_example.jsonl').write_bytes(b'{"response":"synthetic"}\n')
            (source/'results').mkdir();(source/'results'/'routing-demo.jsonl').write_text('{}\n')
            (source/'accounts.json').write_text('[]')
            connection_bytes = b'[{"id":"synthetic-connection","name":"daily","url":"https://synthetic.invalid/v1","updated_at":1}]'
            (source/'connections.json').write_bytes(connection_bytes)
            destination=source/'data'
            result=migrate(source,destination)
            self.assertEqual(result['files'],4)
            self.assertEqual((source/'run_example.jsonl').read_bytes(),(destination/'run_example.jsonl').read_bytes())
            self.assertTrue((destination/'.migrated-from-root').exists())
            self.assertEqual((destination/'accounts.json').stat().st_mode&0o777,0o600)
            self.assertEqual((destination/'connections.json').read_bytes(),connection_bytes)
            self.assertEqual((source/'connections.json').read_bytes(),connection_bytes)
            self.assertEqual((destination/'connections.json').stat().st_mode&0o777,0o600)
            with self.assertRaises(ValueError):migrate(source,destination)

    def test_conflict_never_overwrites_existing_data_or_activates_destination(self):
        from tools.migrate_data import migrate
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'project';source.mkdir();destination=source/'data';destination.mkdir()
            (source/'accounts.json').write_text('[]');(destination/'accounts.json').write_text('[1]')
            with self.assertRaises(ValueError):migrate(source,destination)
            self.assertEqual((destination/'accounts.json').read_text(),'[1]')
            self.assertFalse((destination/'.migrated-from-root').exists())
