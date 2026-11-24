import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
import audit

class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'runtime'
    def call(self,*args):
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            return audit.main(['--root',str(self.root),*args])
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

if __name__=='__main__': unittest.main()
