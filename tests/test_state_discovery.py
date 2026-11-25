"""Offline fixtures, no credential, real state, or network access."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from kaggle_runner.state_discovery import discover
from kaggle_runner.file_identity import file_identity


class StateDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve(); self.home=self.root/'home'; self.home.mkdir()
        self.context=self.root/'.kgr-bootstrap.json'
        self.config=self.root/'config.toml'; self.config.write_text('[runner]\n',encoding='utf-8')
        env=patch.dict(os.environ,{'HOME':str(self.home),'USERPROFILE':str(self.home),'XDG_STATE_HOME':str(self.root/'xdg'),'LOCALAPPDATA':str(self.root/'redirected'),'APPDATA':str(self.root/'roaming')},clear=True)
        env.start(); self.addCleanup(env.stop)
    def ledger(self,name,objective='target-objective'):
        path=self.root/name; path.mkdir(parents=True)
        with closing(sqlite3.connect(path/'jobs.sqlite3')) as con:
            con.execute('CREATE TABLE jobs (job_id TEXT)')
            con.execute('CREATE TABLE work_objectives (objective_id TEXT PRIMARY KEY, created_at TEXT)')
            con.execute('INSERT INTO work_objectives VALUES (?,?)',(objective,'2026-01-01T00:00:00Z')); con.commit()
        return path
    def probe(self,**kwargs):
        return discover(objective_id='target-objective',config=str(self.config),context_file=str(self.context),**kwargs)
    def test_unique_match_remember_and_reuse_after_environment_drift(self):
        state=self.ledger('existing'); os.environ['KGR_STATE_DIR']=str(state)
        db=state/'jobs.sqlite3'; old=db.read_bytes()
        first=self.probe(remember=True)
        self.assertTrue(first['ok']); self.assertEqual(first['context_status'],'saved'); self.assertEqual(first['selected']['path'],str(state))
        os.environ['KGR_STATE_DIR']=str(self.root/'new-missing')
        second=self.probe(); self.assertTrue(second['ok']); self.assertEqual(second['selected']['path'],str(state))
        self.assertEqual(old,db.read_bytes()); self.assertFalse((self.root/'new-missing').exists())
        self.assertEqual(set(json.loads(self.context.read_text())),{'schema_version','state_directory','identity','objective','python_executable','runner_module'})
    def test_default_discovery_does_not_write(self):
        state=self.ledger('existing'); os.environ['KGR_STATE_DIR']=str(state)
        self.assertTrue(self.probe()['ok']); self.assertFalse(self.context.exists())
    def test_verified_discovery_accepts_full_width_legacy_identity(self):
        state=self.ledger('existing'); os.environ['KGR_STATE_DIR']=str(state)
        identity=file_identity(state/'jobs.sqlite3')
        if identity.get('platform') == 'windows':
            file_id=bytes.fromhex(identity['file_id'])
            legacy={'device':int(identity['volume_serial'],16),
                    'inode':int.from_bytes(file_id[:8],'little')}
        else:
            legacy=identity
        self.context.write_text(json.dumps({
            'schema_version':1,'state_directory':str(state),'identity':legacy,
            'objective':{'id':'target-objective','created_at':'2026-01-01T00:00:00Z'},
            'python_executable':'legacy-python','runner_module':'legacy-module',
        }),encoding='utf-8')
        result=self.probe()
        self.assertTrue(result['ok'])
        self.assertEqual(result['selected']['path'],str(state))
        if os.name == 'nt':
            self.assertNotEqual(result['selected']['identity'],legacy)
    def test_truncated_windows_legacy_device_is_ambiguous(self):
        if os.name != 'nt': self.skipTest('Windows device truncation only')
        state=self.ledger('existing'); os.environ['KGR_STATE_DIR']=str(state)
        identity=file_identity(state/'jobs.sqlite3')
        file_id=bytes.fromhex(identity['file_id'])
        legacy={'device':int(identity['volume_serial'],16)&0xFFFFFFFF,
                'inode':int.from_bytes(file_id[:8],'little')}
        self.context.write_text(json.dumps({
            'schema_version':1,'state_directory':str(state),'identity':legacy,
            'objective':{'id':'target-objective','created_at':'2026-01-01T00:00:00Z'},
            'python_executable':'legacy-python','runner_module':'legacy-module',
        }),encoding='utf-8')
        result=self.probe()
        self.assertEqual(result['status'],'blocked')
        self.assertIn({'source':'bootstrap_context','error_type':'STATE_IDENTITY_DRIFT'},result['problems'])
    def test_ambiguous_matches(self):
        a=self.ledger('one'); b=self.ledger('two'); os.environ['KGR_STATE_DIR']=str(a)
        self.config.write_text('[runner]\nstate_dir='+json.dumps(str(b))+'\n')
        r=self.probe(remember=True); self.assertEqual(r['status'],'ambiguous'); self.assertFalse(self.context.exists())
    def test_absent_objective_and_no_new_state(self):
        a=self.ledger('other','other-objective'); os.environ['KGR_STATE_DIR']=str(a)
        r=self.probe(remember=True); self.assertEqual(r['status'],'not_found'); self.assertFalse(self.context.exists())
        self.assertFalse((self.root/'xdg').exists())
    def test_corrupt_ledger_blocks_selection(self):
        a=self.ledger('existing'); os.environ['KGR_STATE_DIR']=str(a)
        b=self.root/'corrupt'; b.mkdir(); (b/'jobs.sqlite3').write_bytes(b'broken')
        self.config.write_text('[runner]\nstate_dir='+json.dumps(str(b))+'\n')
        self.assertEqual(self.probe()['status'],'blocked')
        self.assertEqual((b/'jobs.sqlite3').read_bytes(),b'broken')
    def test_explicit_missing_does_not_fallback(self):
        os.environ['KGR_STATE_DIR']=str(self.ledger('existing'))
        r=self.probe(state_dir=str(self.root/'missing')); self.assertEqual(r['status'],'not_found')
        self.assertFalse((self.root/'missing').exists())
    def test_replaced_ledger_context_fails_closed(self):
        a=self.ledger('existing'); os.environ['KGR_STATE_DIR']=str(a)
        self.assertTrue(self.probe(remember=True)['ok']); before=self.context.read_bytes()
        (a/'jobs.sqlite3').rename(a/'previous.sqlite3')
        b=self.ledger('replacement'); (b/'jobs.sqlite3').rename(a/'jobs.sqlite3')
        r=self.probe(remember=True); self.assertEqual(r['status'],'blocked'); self.assertEqual(before,self.context.read_bytes())
    def test_home_candidate_with_redirected_defaults(self):
        relative=('home/AppData/Local/kaggle-runner' if os.name=='nt' else 'home/.local/state/kaggle-runner')
        state=self.ledger(relative)
        self.assertEqual(self.probe()['selected']['path'],str(state))
    def test_wal_without_shm_is_not_touched(self):
        a=self.ledger('existing'); os.environ['KGR_STATE_DIR']=str(a)
        (a/'jobs.sqlite3-wal').write_bytes(b'fake wal')
        self.assertEqual(self.probe()['status'],'blocked'); self.assertFalse((a/'jobs.sqlite3-shm').exists())
    def test_permission_denied_is_not_missing(self):
        a=self.ledger('existing'); os.environ['KGR_STATE_DIR']=str(a)
        real=Path.stat
        def denied(path,*args,**kwargs):
            if path==a/'jobs.sqlite3': raise PermissionError('fixture denial')
            return real(path,*args,**kwargs)
        with patch.object(Path,'stat',denied):
            self.assertEqual(self.probe()['status'],'blocked')

if __name__=='__main__': unittest.main()
