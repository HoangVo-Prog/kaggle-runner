import contextlib
import io
import json
import sys
import tempfile
import sqlite3
import subprocess
from unittest.mock import patch
import unittest
from pathlib import Path
import audit

class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'runtime_v2'
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

    def test_startup_and_export(self):
        events=audit.read_events(self.root)
        first=events[0]; self.assertEqual(first['kind'],'session_start')
        self.assertEqual(first['payload']['agents_sha256'],audit.sha(self.repo/'AGENTS.md'))
        self.assertTrue(first['payload']['startup_evidence_complete'])
        self.assertEqual(self.call('event','--session','uninitialized','--kind','decision','--summary','no'),2)
        self.assertEqual(self.call('report'),0)
        self.assertEqual([json.loads(x) for x in (self.root/'events.jsonl').read_text(encoding='utf-8').splitlines()],events)

if __name__=='__main__': unittest.main()
