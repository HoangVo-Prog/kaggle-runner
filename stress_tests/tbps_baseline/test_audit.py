import contextlib
import io
import json
import os
import sys
import tempfile
import sqlite3
import shutil
import subprocess
from unittest.mock import patch
import unittest
from pathlib import Path
import audit

class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        # The desktop host may inject a partial indexed Git config. Keep all
        # temporary-repository operations isolated from that inherited state.
        clean_env={k:v for k,v in os.environ.items() if not k.startswith('GIT_CONFIG_')}
        self.git_env_patch=patch.dict(os.environ,clean_env,clear=True)
        self.git_env_patch.start(); self.addCleanup(self.git_env_patch.stop)
        self.root=Path(self.tmp.name)/'runtime_v3'
        self.repo=Path(self.tmp.name)/'repo'; self.repo.mkdir()
        (self.repo/'AGENTS.md').write_text('test instructions',encoding='utf-8')
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        subprocess.run(['git','-C',str(self.repo),'add','AGENTS.md'],check=True)
        subprocess.run(['git','-C',str(self.repo),'-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture'],check=True)
        self.assertEqual(self.call('init','--session','a'),0)
    def call(self,*args):
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            return audit.main(['--root',str(self.root),*args,*(['--runner-root',str(self.repo)] if args[0]=='init' else [])])
    def test_init_resume(self):
        self.assertEqual(self.call('init','--session','a'),0)
        self.assertEqual(self.call('init','--session','a'),0)
        self.assertEqual(self.call('init','--session','b'),0)
        self.assertEqual(len(audit.read_events(self.root)),2)
    def test_evidence_required(self):
        self.assertEqual(self.call('check','--session','a','--step','A01','--status','done'),2)
        self.assertEqual(self.call('check','--session','a','--step','A01','--status','done','--evidence','file hash'),0)
        self.assertEqual(audit.report(self.root)['prepare_done'],1)
    def test_commands_and_missing_executable(self):
        self.assertEqual(self.call('run','--session','a','--step','A02','--',sys.executable,'-c','print(123)'),0)
        self.assertEqual(self.call('run','--session','a','--step','A02','--','definitely-nonexistent-kgr-command'),127)
        self.assertEqual(audit.report(self.root)['failed_commands'],1)
        self.assertEqual(audit.report(self.root)['commands_finished'],2)
    def test_live_blocked(self):
        self.assertEqual(self.call('run','--session','a','--','kgr','work','execute','id'),3)
        self.assertEqual(audit.report(self.root)['commands_finished'],0)
    def test_redaction(self):
        self.assertEqual(self.call('event','--session','a','--kind','observation','--summary','KAGGLE_API_TOKEN=synthetic-test-token'),0)
        self.assertNotIn('synthetic-test-token',json.dumps(audit.read_events(self.root)))
    def test_unfinished_command_visible(self):
        audit.add(self.root,'a','command_start','A02',command_id='lost')
        self.assertEqual(audit.report(self.root)['unfinished_commands'],['lost'])

    def test_connection_lifecycle_rollback_and_unlink(self):
        with self.assertRaises(RuntimeError):
            with audit.connect(self.root) as con:
                con.execute("INSERT INTO events(ts,session,kind,payload) VALUES('x','a','test','{}')")
                raise RuntimeError('rollback')
        with self.assertRaises(sqlite3.ProgrammingError):
            con.execute('SELECT 1')
        self.assertEqual(len(audit.read_events(self.root)),1)
        (self.root/'audit.sqlite3').unlink()
        self.assertFalse((self.root/'audit.sqlite3').exists())

    def test_probe_permission_and_cleanup(self):
        self.assertTrue(audit.probe(self.root)['ok'])
        self.assertEqual(list(self.root.glob('.audit-probe-*')),[])
        with patch('audit.shared_mkdir',side_effect=PermissionError('denied')):
            verdict=audit.probe(self.root)
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['error_type'],'PermissionError')
        self.assertEqual(verdict['operator_access'],'unverified_cross_identity')

    def test_history_protected(self):
        old=Path(self.tmp.name)/'runtime'; old.mkdir(); journal=old/'events.jsonl';journal.write_bytes(b'historical')
        with self.assertRaises(ValueError): audit.report(old)
        self.assertEqual(journal.read_bytes(),b'historical')
        old2=Path(self.tmp.name)/'archive';old2.mkdir();(old2/'audit.sqlite3').write_bytes(b'historical')
        with self.assertRaises(ValueError): audit.report(old2)
        self.assertEqual((old2/'audit.sqlite3').read_bytes(),b'historical')

    def test_state_binding_and_live_blocked(self):
        state=Path(self.tmp.name)/'state'; state.mkdir()
        self.assertEqual(self.call('bind-state','--session','a','--state-dir',str(state),'--evidence','operator fixture'),0)
        self.assertEqual(self.call('bind-state','--session','a','--state-dir',str(state),'--evidence','same'),0)
        self.assertEqual(self.call('bind-state','--session','a','--state-dir',str(state/'wrong'),'--evidence','mismatch'),2)
        self.assertEqual(self.call('run','--session','a','--','kgr','setup','status'),3)
        self.assertEqual(self.call('run','--session','a','--live','--approval-reference','fake','--','kgr','work','execute','x'),3)
        self.assertEqual(self.call('run','--session','a','--','kgr','--state-dir',str(state),'setup','verify','--online'),3)
        self.assertEqual(audit.report(self.root)['commands_finished'],0)

    def _bind_migration_fixture(self):
        state=Path(self.tmp.name)/'migration-state'; state.mkdir()
        db=state/'jobs.sqlite3'; db.write_bytes(b'disposable ledger identity fixture')
        old_python=Path(self.tmp.name)/'old-python.exe'; old_python.write_bytes(b'disposable old interpreter fixture')
        with audit.connect(self.root) as con:
            con.execute('INSERT INTO events(ts,session,objective,kind,step,payload) VALUES(?,?,?,?,?,?)',
                        (audit.now(),'a','fixture-objective','state_bound','A03',json.dumps({
                            'state_dir':str(state.resolve()),'identity':audit.file_identity(db),
                            'objective':{'id':'fixture-objective','created_at':'fixture-time'},
                            'python':str(old_python.resolve()),'evidence':'temporary test fixture'})))
        return state,db,old_python

    def _state_location_fixture(self):
        source = Path(self.tmp.name) / 'state-source'; source.mkdir()
        (source / 'work' / 'fixture-objective' / 'specs').mkdir(parents=True)
        spec = source / 'work' / 'fixture-objective' / 'specs' / 'attempt.json'
        spec.write_text('{"synthetic":true}', encoding='utf-8')
        con = sqlite3.connect(source / 'jobs.sqlite3')
        con.executescript('''
            CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT NOT NULL);
            CREATE TABLE work_objectives(objective_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, revision INTEGER NOT NULL);
            CREATE TABLE work_attempt_links(objective_id TEXT, ordinal INTEGER, experiment_id TEXT, attempt_id TEXT,
                spec_path TEXT, spec_sha256 TEXT, planned_gpu_hours REAL, job_id TEXT);
            INSERT INTO jobs VALUES('job-fixture','succeeded');
            INSERT INTO work_objectives VALUES('fixture-objective','fixture-created-at',7);
        ''')
        import hashlib
        con.execute('INSERT INTO work_attempt_links VALUES(?,?,?,?,?,?,?,?)',
                    ('fixture-objective',0,'exp-fixture','attempt-fixture',str(spec),
                     hashlib.sha256(spec.read_bytes()).hexdigest(),0.5,'job-fixture'))
        con.commit(); con.close()
        destination = Path(self.tmp.name) / 'state-destination'; destination.mkdir()
        source_db = sqlite3.connect(source / 'jobs.sqlite3')
        destination_db = sqlite3.connect(destination / 'jobs.sqlite3')
        source_db.backup(destination_db)
        destination_db.close(); source_db.close()
        shutil.copytree(source / 'work', destination / 'work')
        identity = audit.file_identity(source / 'jobs.sqlite3')
        objective = {'id':'fixture-objective','created_at':'fixture-created-at'}
        with audit.connect(self.root) as journal:
            journal.execute('INSERT INTO events(ts,session,objective,kind,step,payload) VALUES(?,?,?,?,?,?)',
                            (audit.now(),'a','fixture-objective','state_bound','A03',json.dumps({
                                'state_dir':str(source.resolve()),'identity':identity,'objective':objective,
                                'python':str(Path(sys.executable).resolve()),'evidence':'disposable state fixture'})))
        context = Path(self.tmp.name) / '.kgr-bootstrap.json'
        context.write_text(json.dumps({'schema_version':1,'state_directory':str(source.resolve()),
                                       'identity':identity,'objective':objective,
                                       'python_executable':str(Path(sys.executable).resolve()),
                                       'runner_module':'synthetic'}), encoding='utf-8')
        return source, destination, context, identity

    def _transition_state(self, source, destination, context, authorization='operator fixture authorization'):
        return audit.transition_state_location(self.root, 'a', 'fixture-objective', source, destination,
                                               context, authorization, 'disposable migration test')

    def test_state_location_migration_is_append_only_and_preserves_interpreter_history(self):
        source, destination, context, _identity = self._state_location_fixture()
        old_python = Path(self.tmp.name) / 'old-python.exe'; old_python.write_bytes(b'disposable')
        with audit.connect(self.root) as journal:
            journal.execute("UPDATE events SET payload=? WHERE kind='state_bound'", (json.dumps({
                'state_dir':str(source.resolve()),'identity':audit.file_identity(source/'jobs.sqlite3'),
                'objective':{'id':'fixture-objective','created_at':'fixture-created-at'},
                'python':str(old_python.resolve()),'evidence':'disposable state fixture'}),))
        self.assertEqual(self.call('migrate-interpreter','--session','a','--objective','fixture-objective',
                                   '--new-python',sys.executable,'--authorization-evidence','operator fixture',
                                   '--reason','synthetic interpreter history'),0)
        before = audit.read_events(self.root)
        result = self._transition_state(source, destination, context)
        after = audit.read_events(self.root)
        self.assertTrue(result['ok'])
        self.assertEqual(after[:len(before)],before)
        self.assertEqual(after[-1]['kind'],'state_location_migration')
        self.assertEqual(next(e for e in after if e['kind']=='state_bound'),
                         next(e for e in before if e['kind']=='state_bound'))
        self.assertEqual(next(e for e in after if e['kind']=='interpreter_migration'),
                         next(e for e in before if e['kind']=='interpreter_migration'))
        effective = audit.bound_state(self.root)
        self.assertEqual(effective['state_dir'],str(destination.resolve()))
        self.assertEqual(effective['python'],str(Path(sys.executable).resolve()))
        self.assertEqual(json.loads(context.read_text())['state_directory'],str(destination.resolve()))
        con = sqlite3.connect(destination/'jobs.sqlite3')
        self.assertEqual(con.execute("SELECT objective_id,created_at,revision FROM work_objectives").fetchone(),
                         ('fixture-objective','fixture-created-at',7))
        self.assertEqual(con.execute("SELECT experiment_id,attempt_id,spec_path FROM work_attempt_links").fetchone(),
                         ('exp-fixture','attempt-fixture',str(source/'work'/'fixture-objective'/'specs'/'attempt.json')))
        con.close()
        self.assertTrue((destination/'work'/'fixture-objective'/'specs'/'attempt.json').is_file())

    def test_state_location_unauthorized_and_destination_collision_are_rejected(self):
        source, destination, context, _identity = self._state_location_fixture()
        before = audit.read_events(self.root); pointer = context.read_bytes()
        with self.assertRaisesRegex(ValueError,'authorization-evidence'):
            self._transition_state(source,destination,context,authorization=' ')
        self.assertEqual(audit.read_events(self.root),before)
        con = sqlite3.connect(destination/'jobs.sqlite3')
        con.execute("UPDATE jobs SET status='different'")
        con.commit(); con.close()
        with self.assertRaisesRegex(ValueError,'logical ledger or associated artifacts differ'):
            self._transition_state(source,destination,context)
        self.assertEqual(audit.read_events(self.root),before)
        self.assertEqual(context.read_bytes(),pointer)

    def test_state_location_detects_source_identity_drift(self):
        source, destination, context, _identity = self._state_location_fixture()
        (source/'jobs.sqlite3').rename(source/'old.sqlite3')
        (source/'jobs.sqlite3').write_bytes(b'replacement')
        before = audit.read_events(self.root)
        with self.assertRaisesRegex(ValueError,'STATE_IDENTITY_DRIFT'):
            self._transition_state(source,destination,context)
        self.assertEqual(audit.read_events(self.root),before)

    def test_state_location_recovers_interruption_after_audit_append(self):
        source, destination, context, _identity = self._state_location_fixture()
        with patch.object(audit,'_advance_bootstrap_pointer',side_effect=OSError('synthetic interruption')):
            with self.assertRaisesRegex(OSError,'synthetic interruption'):
                self._transition_state(source,destination,context)
        self.assertEqual(audit.bound_state(self.root)['state_dir'],str(destination.resolve()))
        self.assertEqual(json.loads(context.read_text())['state_directory'],str(source.resolve()))
        result = self._transition_state(source,destination,context)
        self.assertTrue(result['recovered'])
        self.assertEqual(json.loads(context.read_text())['state_directory'],str(destination.resolve()))
        self.assertEqual(len([e for e in audit.read_events(self.root) if e['kind']=='state_location_migration']),1)

    def test_state_location_rollback_appends_reverse_transition(self):
        source, destination, context, _identity = self._state_location_fixture()
        self._transition_state(source,destination,context)
        before = audit.read_events(self.root)
        result = self._transition_state(destination,source,context)
        after = audit.read_events(self.root)
        self.assertTrue(result['ok'])
        self.assertEqual(after[:len(before)],before)
        self.assertEqual(audit.bound_state(self.root)['state_dir'],str(source.resolve()))
        self.assertEqual(json.loads(context.read_text())['state_directory'],str(source.resolve()))

    def test_state_location_cli_appends_transition(self):
        source, destination, context, _identity = self._state_location_fixture()
        self.assertEqual(self.call('migrate-state-location','--session','a','--objective','fixture-objective',
                                   '--source-dir',str(source),'--destination-dir',str(destination),
                                   '--context-file',str(context),'--authorization-evidence','operator fixture',
                                   '--reason','CLI dispatch regression'),0)
        self.assertEqual(audit.bound_state(self.root)['state_dir'],str(destination.resolve()))

    def test_authorized_interpreter_migration_is_append_only(self):
        state,db,old_python=self._bind_migration_fixture()
        before=audit.read_events(self.root)
        code=self.call('migrate-interpreter','--session','a','--objective','fixture-objective',
                       '--new-python',sys.executable,
                       '--authorization-evidence','operator explicitly authorized fixture migration',
                       '--reason','temporary audit regression')
        self.assertEqual(code,0)
        after=audit.read_events(self.root)
        self.assertEqual(after[:len(before)],before)
        self.assertEqual(len(after),len(before)+1)
        event=after[-1]
        self.assertEqual(event['kind'],'interpreter_migration')
        self.assertEqual(event['payload']['old_interpreter']['path'],str(old_python.resolve()))
        self.assertEqual(event['payload']['new_interpreter']['path'],str(Path(sys.executable).resolve()))
        self.assertEqual(event['payload']['state_identity'],audit.file_identity(db))
        self.assertEqual(audit.bound_state(self.root)['python'],str(Path(sys.executable).resolve()))
        # Historical state binding remains byte-for-byte represented by its original event.
        original_binding=next(e for e in before if e['kind']=='state_bound')
        self.assertEqual(next(e for e in after if e['kind']=='state_bound'),original_binding)
        from unittest.mock import MagicMock
        with patch('audit.subprocess.run',return_value=MagicMock(returncode=0,stdout='{}',stderr='')) as run:
            self.assertEqual(self.call('kgr','--python',sys.executable,'--','work','show','fixture-objective'),0)
            self.assertIn(str(state.resolve()),run.call_args.args[0])

    def test_unauthorized_interpreter_migration_is_rejected(self):
        _state,_db,old_python=self._bind_migration_fixture()
        before=audit.read_events(self.root)
        with self.assertRaisesRegex(ValueError,'authorization-evidence'):
            audit.migrate_interpreter(self.root,'a','fixture-objective',sys.executable,'',
                                      'missing operator authorization')
        after=audit.read_events(self.root)
        self.assertEqual(after,before)
        self.assertEqual(audit.bound_state(self.root)['python'],str(old_python.resolve()))
        self.assertEqual(self.call('kgr','--python',sys.executable,'--','work','show','fixture-objective'),2)

    def test_interpreter_migration_rejects_replaced_ledger(self):
        state,db,_old_python=self._bind_migration_fixture()
        db.rename(state/'previous.sqlite3')
        db.write_bytes(b'replacement ledger fixture')
        with self.assertRaisesRegex(ValueError,'STATE_IDENTITY_DRIFT'):
            audit.migrate_interpreter(self.root,'a','fixture-objective',sys.executable,
                                      'operator authorized fixture', 'replacement check')
        self.assertFalse(any(e['kind']=='interpreter_migration' for e in audit.read_events(self.root)))

    def test_startup_and_export(self):
        events=audit.read_events(self.root)
        first=events[0]; self.assertEqual(first['kind'],'session_start')
        self.assertEqual(first['payload']['agents_sha256'],audit.sha(self.repo/'AGENTS.md'))
        self.assertTrue(first['payload']['startup_evidence_complete'])
        self.assertEqual(self.call('event','--session','uninitialized','--kind','decision','--summary','no'),2)
        self.assertEqual(self.call('report'),0)
        self.assertEqual([json.loads(x) for x in (self.root/'events.jsonl').read_text(encoding='utf-8').splitlines()],events)

    def test_stateless_allowed_without_state(self):
        from unittest.mock import MagicMock
        for cmd in (['package','inspect','file.py'], ['experiment','validate','spec.json'], ['setup','discover','--objective-id','fixture']):
            with patch('audit.subprocess.run',return_value=MagicMock(returncode=0,stdout='{}',stderr='')) as run:
                self.assertEqual(self.call('kgr','--',*cmd),0)
                run.assert_called_once()
        with patch('audit.subprocess.run') as run:
            self.assertEqual(self.call('kgr','--','work','show','fixture'),3)
            self.assertEqual(self.call('kgr','--','experiment','submit','spec.json'),3)
            run.assert_not_called()

    def test_verified_discovery_binds_and_detects_replacement(self):
        from unittest.mock import MagicMock
        state=Path(self.tmp.name)/'state'; state.mkdir()
        db=state/'jobs.sqlite3'; db.write_bytes(b'fixture')
        current_ident=audit.file_identity(db)
        if current_ident.get('platform') == 'windows':
            file_id=bytes.fromhex(current_ident['file_id'])
            ident={'device':int(current_ident['volume_serial'],16),
                   'inode':int.from_bytes(file_id[:8],'little')}
        else:
            ident=current_ident
        selected={'path':str(state),'status':'match','identity':ident,'objective':{'id':'fixture','created_at':'time'}}
        result={'schema_version':1,'offline':True,'objective_id':'fixture','ok':True,'status':'verified','selected':selected,'candidates':[selected],'problems':[],'python_executable':sys.executable,'authorization':'not_checked','context_status':'saved'}
        with patch('audit.subprocess.run',return_value=MagicMock(returncode=0,stdout=json.dumps(result),stderr='')):
            self.assertEqual(self.call('discover','--objective','fixture'),0)
        self.assertEqual(audit.bound_state(self.root)['identity'],ident)
        with patch('audit.subprocess.run',return_value=MagicMock(returncode=0,stdout='{}',stderr='')) as run:
            self.assertEqual(self.call('kgr','--','work','show','fixture'),0)
            self.assertIn(str(state),run.call_args.args[0])
        db.rename(state/'old.sqlite3'); db.write_bytes(b'replacement')
        with patch('audit.subprocess.run') as run:
            self.assertEqual(self.call('kgr','--','work','show','fixture'),3)
            run.assert_not_called()

    def test_ambiguous_discovery_does_not_bind(self):
        from unittest.mock import MagicMock
        with patch('audit.subprocess.run',return_value=MagicMock(returncode=3,stdout='{"status":"ambiguous","ok":false}',stderr='')):
            self.assertEqual(self.call('discover','--objective','fixture'),3)
        self.assertIsNone(audit.bound_state(self.root))
        self.assertEqual(audit.report(self.root)['state_discovery_attempts'],1)

    def test_v2_history_preserved(self):
        old=Path(self.tmp.name)/'runtime_v2'; old.mkdir(); evidence=old/'events.jsonl'; evidence.write_bytes(b'V2')
        with self.assertRaises(ValueError): audit.report(old)
        with self.assertRaises(ValueError): audit.ensure_v3(old/'child')
        self.assertEqual(evidence.read_bytes(),b'V2')

    def test_session_shorthand_and_operator_request(self):
        self.assertEqual(self.call('event','--kind','operator_request','--summary','missing'),2)
        self.assertEqual(self.call('event','--kind','operator_request','--summary','missing','--request-class','discovery_exhausted'),0)
        self.assertEqual(audit.report(self.root)['operator_requests']['discovery_exhausted'],1)
        self.assertEqual(self.call('init','--session','b'),0)
        self.assertEqual(self.call('event','--kind','decision','--summary','ambiguous session'),2)

    def test_structured_evidence_redacts_nested_credentials(self):
        from unittest.mock import MagicMock
        source={'ok':True,'package':{'execution_authorization':{'opaque':'fixture-auth-value'},
                                   'api_key':'fixture-key-value'},
                'items':[{'password':'fixture-password-value'},'Bearer fixture-bearer-value'],
                'authorization':'not_checked','unicode':'Tiếng Việt'}
        with patch('audit.subprocess.run',return_value=MagicMock(returncode=0,stdout=json.dumps(source),stderr='')):
            self.assertEqual(self.call('kgr','--','package','inspect','fixture.py','--json'),0)
        event=[e for e in audit.read_events(self.root) if e['kind']=='command_end'][-1]
        body=Path(event['payload']['stdout']).read_text(encoding='utf-8')
        decoded=json.loads(body)
        self.assertEqual(decoded['authorization'],'not_checked')
        self.assertEqual(decoded['package']['execution_authorization'],{'redacted':True})
        self.assertEqual(decoded['unicode'],'Tiếng Việt')
        for secret in ('fixture-auth-value','fixture-key-value','fixture-password-value','fixture-bearer-value'):
            self.assertNotIn(secret,body)
        self.assertEqual(event['payload']['output_status'],'valid_json')

    def test_invalid_structured_output_fails_closed(self):
        from unittest.mock import MagicMock
        for source in ('{"authorization":{"opaque":"fixture-hidden"}',
                       '{"ok":true,"ok":false}', '{"ok":NaN}', '{"ok":1e999}'):
            with patch('audit.subprocess.run',return_value=MagicMock(returncode=0,stdout=source,stderr='')):
                self.assertEqual(self.call('discover','--objective','fixture'),2)
            self.assertIsNone(audit.bound_state(self.root))
            event=[e for e in audit.read_events(self.root) if e['kind']=='command_end'][-1]
            self.assertEqual(event['payload']['exit_code'],0)
            self.assertEqual(event['payload']['output_status'],'invalid_json')
            body=Path(event['payload']['stdout']).read_text(encoding='utf-8')
            self.assertNotIn('fixture-hidden',body)
            self.assertIn('CONTENT WITHHELD',body)

    def test_verified_shape_with_multiple_matches_cannot_bind(self):
        from unittest.mock import MagicMock
        state=Path(self.tmp.name)/'state'; state.mkdir()
        db=state/'jobs.sqlite3'; db.write_bytes(b'fixture')
        selected={'path':str(state),'status':'match','identity':audit.file_identity(db),
                  'objective':{'id':'fixture','created_at':'time'}}
        result={'schema_version':1,'offline':True,'objective_id':'fixture','ok':True,'status':'verified',
                'selected':selected,'candidates':[selected,dict(selected,path=str(state/'other'))],
                'problems':[],'python_executable':sys.executable,'authorization':'not_checked'}
        with patch('audit.subprocess.run',return_value=MagicMock(returncode=0,stdout=json.dumps(result),stderr='')):
            self.assertEqual(self.call('discover','--objective','fixture'),2)
        self.assertIsNone(audit.bound_state(self.root))

    def test_real_discovery_subprocess_and_fresh_audit_session(self):
        # A disposable fixture only: no real state, credentials or Kaggle API.
        from contextlib import closing
        state=Path(self.tmp.name).resolve()/'fixture-state'; state.mkdir()
        with closing(sqlite3.connect(state/'jobs.sqlite3')) as con:
            con.execute('CREATE TABLE jobs (job_id TEXT)')
            con.execute('CREATE TABLE work_objectives (objective_id TEXT, created_at TEXT)')
            con.execute("INSERT INTO work_objectives VALUES ('fixture-objective','date')")
            con.commit()
        context=Path(self.tmp.name).resolve()/'bootstrap.json'
        self.assertEqual(self.call('discover','--objective','fixture-objective','--state-dir',str(state),
                                   '--context-file',str(context)),0)
        binding=audit.bound_state(self.root)
        self.assertEqual(binding['state_dir'],str(state))
        self.assertTrue(context.exists())
        # Fresh audit root has no previous binding; Runner can recover the
        # fixture via the persisted pointer and bounded discovery sources.
        other=Path(self.tmp.name)/'runtime_v3-continuation'
        self.root=other
        self.assertEqual(self.call('init','--session','b'),0)
        # Isolate real state candidates: use the fixture's explicit config and
        # home directories; no operator state path on the discovery invocation.
        import os
        config=Path(self.tmp.name).resolve()/'empty.toml'; config.write_text('[runner]\n',encoding='utf-8')
        home=Path(self.tmp.name).resolve()/'home'; home.mkdir()
        overrides={'KGR_CONFIG':str(config),'KGR_STATE_DIR':'','HOME':str(home),
                   'USERPROFILE':str(home),'LOCALAPPDATA':str(home),'XDG_STATE_HOME':str(home),
                   'HOMEDRIVE':'','HOMEPATH':'','USERNAME':'','SystemDrive':''}
        with patch.dict(os.environ,overrides):
            self.assertEqual(self.call('discover','--objective','fixture-objective','--context-file',str(context)),0)
        self.assertEqual(audit.bound_state(self.root)['identity'],binding['identity'])
        event=[e for e in audit.read_events(self.root) if e['kind']=='state_discovery'][-1]
        self.assertEqual(event['payload']['context_status'],'existing')
        self.assertEqual(event['payload']['candidates'][0]['sources'],['bootstrap_context'])

if __name__=='__main__': unittest.main()
