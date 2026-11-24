"""CLI routing regression: full checkout required, no real state or Kaggle API."""
from contextlib import redirect_stdout, closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from kaggle_runner.cli import main


class StateDiscoveryCLITests(unittest.TestCase):
    def invoke(self, args):
        out=io.StringIO()
        with redirect_stdout(out):
            code=main([*args,'--json'])
        return code,json.loads(out.getvalue())

    def test_discovery_routes_before_build_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); state=root/'state'; state.mkdir()
            with closing(sqlite3.connect(state/'jobs.sqlite3')) as con:
                con.execute('CREATE TABLE jobs (job_id TEXT)')
                con.execute('CREATE TABLE work_objectives (objective_id TEXT, created_at TEXT)')
                con.execute("INSERT INTO work_objectives VALUES ('fixture-objective','date')"); con.commit()
            with patch('kaggle_runner.cli.build_settings',side_effect=AssertionError('Discovery must not initialize settings')):
                code,result=self.invoke(['--state-dir',str(state),'setup','discover','--objective-id','fixture-objective','--context-file',str(root/'context.json')])
            self.assertEqual(code,0); self.assertEqual(result['selected']['path'],str(state)); self.assertFalse((root/'context.json').exists())

    def test_stateless_real_package_inspection_without_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); script=root/'train.py'; script.write_text('print(1)\n')
            with patch('kaggle_runner.cli.build_settings',side_effect=AssertionError('Package inspect is stateless')):
                code,result=self.invoke(['package','inspect',str(script)])
            self.assertEqual(code,0); self.assertTrue(result['ok'])

    def test_validate_routes_without_build_settings(self):
        with patch('kaggle_runner.cli.build_settings',side_effect=AssertionError('Experiment validate is stateless')), \
             patch('kaggle_runner.commands.agent.agent.validate',return_value={'ok':True}) as validate:
            code,result=self.invoke(['experiment','validate','fixture.json'])
        self.assertEqual(code,0); self.assertTrue(result['ok']); validate.assert_called_once()

if __name__=='__main__': unittest.main()
