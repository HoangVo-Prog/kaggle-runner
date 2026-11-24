"""Isolated regressions: no live API operations, credentials or real job state."""
import contextlib
import io
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kaggle_runner.config import build_settings, RunnerError
from kaggle_runner.local_preflight import probe_directory, ledger_readiness


class LocalHardeningTests(unittest.TestCase):
    def test_explicit_state_precedence_across_environments(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            config = base / 'config.toml'; config.write_text('[runner]\n', encoding='utf-8')
            selected = base / 'state'; selected.mkdir()
            for suffix in ('one', 'two'):
                with patch.dict(os.environ, {'HOME':str(base/suffix), 'LOCALAPPDATA':str(base/suffix), 'XDG_STATE_HOME':str(base/suffix), 'KGR_STATE_DIR':str(base/suffix)}, clear=False):
                    settings = build_settings(state_dir=str(selected), profile=None, config=str(config))
                    self.assertEqual(settings.state, selected.resolve())
            from kaggle_runner.local_preflight import diagnose
            wrong = build_settings(state_dir=str(base/'other'), profile=None, config=str(config))
            self.assertEqual(diagnose(wrong, expected_state_dir=str(selected))['blockers'], ['STATE_PATH_DRIFT'])
            self.assertFalse((base/'one').exists()); self.assertFalse((base/'two').exists()); self.assertFalse((base/'other').exists())

    def test_exact_directory_probe_and_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertTrue(probe_directory(root)['ok'])
            self.assertEqual(list(root.iterdir()), [])
            self.assertFalse(probe_directory(root/'absent')['ok'])
            self.assertFalse((root/'absent').exists())
            with patch('pathlib.Path.mkdir', side_effect=PermissionError('fixture')):
                self.assertFalse(probe_directory(root)['ok'])
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(ledger_readiness(root)['status'], 'missing')
            self.assertEqual(list(root.iterdir()), [])

    def test_existing_ledger_readonly_and_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); db=root/'jobs.sqlite3'
            from contextlib import closing
            with closing(sqlite3.connect(db)) as con:
                with con:
                    con.execute('CREATE TABLE work_objectives (objective_id TEXT)')
                    con.execute('INSERT INTO work_objectives VALUES (?)', ('existing-objective',))
            before=db.read_bytes()
            verdict=ledger_readiness(root)
            self.assertEqual(verdict['objectives'], ['existing-objective'])
            self.assertEqual(db.read_bytes(), before)
            with patch('kaggle_runner.local_preflight.sqlite3.connect', side_effect=sqlite3.OperationalError('fixture')):
                self.assertEqual(ledger_readiness(root)['status'], 'failed')
            self.assertEqual(db.read_bytes(), before)

    def test_explicit_work_report_and_draft_handoff(self):
        from kaggle_runner.cli import main
        with tempfile.TemporaryDirectory() as d:
            base = Path(d); state = base/'state'; state.mkdir()
            config = base/'config.toml'; config.write_text('[runner]\n', encoding='utf-8')
            handoff = base/'handoff.json'; handoff.write_text(json.dumps({'blockers':[{'code':'MISSING_BUDGET','category':'authorization','summary':'Waiting for operator','evidence':['fixture']}], 'missing_inputs':['total_gpu_hours'], 'facts':['Local source inspected'], 'completed_steps':['B01'], 'artifacts':[], 'next_action':'collect_operator_inputs'}), encoding='utf-8')
            def call(*args):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    code = main(['--config',str(config),'--state-dir',str(state),*args,'--json'])
                self.assertEqual(code, 0, out.getvalue()); return json.loads(out.getvalue())
            with patch.dict(os.environ, {'LOCALAPPDATA':str(base/'redirect'),'XDG_STATE_HOME':str(base/'redirect'),'KGR_STATE_DIR':str(base/'redirect')}):
                call('work','create','--title','fixture','--objective-id','fixture-obj','--acceptance-criterion','fixture')
                result=call('work','handoff','fixture-obj','--file',str(handoff),'--expected-revision','1')
                self.assertEqual(result['objective']['state'],'draft')
                self.assertEqual(call('work','resume','fixture-obj')['next_action'],'collect_operator_inputs')
                self.assertEqual(call('work','list','--active')['state_directory'],str(state.resolve()))
                call('work','report','fixture-obj','--format','md')
                obj=call('work','show','fixture-obj')['objective']
                report=Path(obj['metadata']['last_successful_report']['path'])
                self.assertTrue(report.is_relative_to(state.resolve())); self.assertTrue(report.is_file())
                self.assertFalse((base/'redirect').exists())

    def test_package_inspection_never_claims_execution_readiness(self):
        from kaggle_runner.source_contract import inspect_source
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); script=root/'train.py'; script.write_text('print("fixture")',encoding='utf-8')
            result=inspect_source(script)
            self.assertTrue(result['ok']); self.assertTrue(result['safe_to_submit'])
            self.assertEqual(result['readiness']['execution_authorization'],'not_checked')
            nb=root/'sample.ipynb'; nb.write_text(json.dumps({'nbformat':4,'nbformat_minor':5,'metadata':{},'cells':[{'cell_type':'code','metadata':{},'source':'!git clone https://example.invalid/repo\n!pip install something\npath="/kaggle/input/data"','outputs':[],'execution_count':None}]}),encoding='utf-8')
            result=inspect_source(nb)
            self.assertIn('external_source',result['readiness']['static_indicators'])
            self.assertIn('runtime_install',result['readiness']['static_indicators'])
            self.assertIn('dataset_path',result['readiness']['static_indicators'])
            self.assertEqual(result['readiness']['experiment'],'not_verified')

if __name__ == '__main__': unittest.main()
