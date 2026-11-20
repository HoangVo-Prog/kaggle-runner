"""Offline agent-first regressions. No real Kaggle calls or credentials."""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kaggle_runner.agent_ledger import AgentLedger
from kaggle_runner.agent_workflow import load_spec, submit, validate_result, aggregate, report, reconcile, resume
from kaggle_runner.config import RunnerError, Settings
from kaggle_runner.jobs import JobStore
from kaggle_runner.packaging import pack_source
from kaggle_runner.source_contract import inspect_source, stage_agent_notebook


def _race(state: str, name: str, output):
    try:
        ledger = AgentLedger(Path(state) / "jobs.sqlite3")
        _, created = ledger.reserve(experiment_id=name, attempt_id="a", fingerprint="same",
                                     job_id=hashlib.sha256(name.encode()).hexdigest()[:32],
                                     spec={"s": 1}, account_profile="team-one",
                                     account_owner="owner", kernel_ref="owner/slug", limit=1)
        output.put((created, None))
    except RunnerError as exc:
        output.put((False, str(exc).split(":")[0]))


def _legacy_race(state: str, name: str, output):
    try:
        store = JobStore(Path(state))
        store.add({"job_id": name, "kernel_ref": f"owner/{name}", "source": "x",
                   "source_revision": None, "dataset_refs": "[]", "status": "prepared",
                   "profile": None, "account_profile": "team-one", "account_owner": "owner",
                   "staging_dir": "x", "output_dir": None, "last_message": None}, admission_limit=1)
        output.put((True, None))
    except RunnerError as exc:
        output.put((False, str(exc).split(":")[0]))


def _mixed_race(state: str, name: str, kind: str, barrier, output):
    try:
        barrier.wait(timeout=10)
        if kind == "agent":
            AgentLedger(Path(state) / "jobs.sqlite3").reserve(
                experiment_id=name, attempt_id="a", fingerprint="fp", job_id=name,
                spec={"s": 1}, account_profile="team-one", account_owner="owner",
                kernel_ref=f"owner/{name}", limit=1)
        else:
            _legacy_race(state, name, output)
            return
        output.put((True, None))
    except RunnerError as exc:
        output.put((False, str(exc).split(":")[0]))


class SourceContractTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "model.py").write_text("VALUE = 42\n")
        (self.root / "train.py").write_text("from src.model import VALUE\nprint(VALUE)\n")
        (self.root / ".env").write_text("API_TOKEN=SHOULD_NOT_UPLOAD")
        (self.root / "data").mkdir()
        (self.root / "data" / "data.json").write_text('{"sensitive":"true"}')

    def test_deterministic_python_bundle_and_exclusions(self):
        a, files = pack_source(self.root, "train.py", 3 * 1024 * 1024)
        b, _ = pack_source(self.root, "train.py", 3 * 1024 * 1024)
        self.assertEqual(a, b)
        self.assertEqual({f["path"] for f in files}, {"train.py", "src/model.py"})
        info = inspect_source(self.root, entry="train.py")
        self.assertTrue(info["safe_to_submit"])
        self.assertIn(".env", {item["path"] for item in info["excluded"]})
        self.assertIn("data/", {item["path"] for item in info["excluded"]})
        self.assertEqual(info["source_kind"], "python")
        self.assertEqual(info["source_zip_sha256"], hashlib.sha256(a).hexdigest())
        _, subset = pack_source(self.root, "train.py", 3 * 1024 * 1024, include=["*.md"])
        self.assertEqual([f["path"] for f in subset], ["train.py"])
        with self.assertRaises(RunnerError):
            inspect_source(self.root, entry="../escape.py")

    def test_notebook_companion_import_and_no_source_overwrite(self):
        nb = {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": [
            {"cell_type": "code", "id": "user-cell", "metadata": {},
             "source": "from src.model import VALUE\nprint(VALUE)",
             "execution_count": 4, "outputs": [{"output_type": "stream", "name": "stdout", "text": "SECRET"}]}]}
        source = self.root / "experiment.ipynb"
        source.write_text(json.dumps(nb))
        original = source.read_bytes()
        target = self.root / "staging" / "run.ipynb"
        manifest = stage_agent_notebook(source, target, root=self.root, entry="experiment.ipynb",
                                         datasets=[], models=[], parameters={"seed":42},
                                         experiment_id="smoke", attempt_id="a")
        self.assertEqual(original, source.read_bytes())
        staged = json.loads(target.read_text())
        self.assertEqual(len(staged["cells"]), 2)
        self.assertEqual(staged["cells"][1]["source"], nb["cells"][0]["source"])
        self.assertEqual(staged["cells"][1]["outputs"], [])
        self.assertIn("KGR_PROJECT_ROOT", staged["cells"][0]["source"])
        self.assertIn("src/model.py", [x["path"] for x in manifest])
        compile(staged["cells"][0]["source"], "setup_cell", "exec")
        target2 = self.root / "staging" / "run_again.ipynb"
        stage_agent_notebook(source, target2, root=self.root, entry="experiment.ipynb", datasets=[], models=[],
                             parameters={"seed":42}, experiment_id="smoke", attempt_id="a")
        self.assertEqual(target.read_bytes(), target2.read_bytes())


    def test_notebook_secret_metadata_rejected(self):
        path = self.root / "experiment.ipynb"
        path.write_text(json.dumps({"nbformat":4,"metadata":{"api_token":"synthetic"},"cells":[]}))
        with self.assertRaisesRegex(RunnerError, "credential"):
            inspect_source(path)

    def test_bad_notebook_fails_before_stage(self):
        path = self.root / "experiment.ipynb"
        path.write_text(json.dumps({"nbformat": 4, "metadata": {"kernelspec": {"language": "r"}}, "cells": []}))
        with self.assertRaises(RunnerError):
            inspect_source(path)
        path.write_text(json.dumps({"nbformat": 4, "metadata": {}, "cells": [
            {"cell_type":"markdown","id":"x","metadata":{},"source":"one"},
            {"cell_type":"markdown","id":"x","metadata":{},"source":"two"}]}))
        with self.assertRaises(RunnerError):
            inspect_source(path)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.jobs = JobStore(self.state)
        self.ledger = AgentLedger(self.state / "jobs.sqlite3")

    def reserve(self, experiment="e1", attempt="a", profile="p", limit=2):
        return self.ledger.reserve(experiment_id=experiment, attempt_id=attempt,
                                   fingerprint="immutable", job_id=hashlib.md5((experiment + attempt).encode()).hexdigest(),
                                   spec={"experiment": experiment}, account_profile=profile,
                                   account_owner="owner", kernel_ref="owner/slug", limit=limit)

    def test_idempotency_conflict_and_persistent_uncertainty(self):
        row, created = self.reserve()
        self.assertTrue(created)
        _, repeated = self.reserve()
        self.assertFalse(repeated)
        self.assertEqual(len(self.ledger.list()), 1)
        with self.assertRaisesRegex(RunnerError, "CONFLICT"):
            self.ledger.reserve(experiment_id="e1", attempt_id="a", fingerprint="changed",
                                job_id="other", spec={}, account_profile="p", account_owner="owner",
                                kernel_ref="owner/slug")
        self.ledger.transition(row["job_id"], "staging")
        self.ledger.transition(row["job_id"], "submitting")
        self.ledger.transition(row["job_id"], "unknown_remote")
        with self.assertRaisesRegex(RunnerError, "SLOT_EXHAUSTED"):
            self.reserve(experiment="e2", profile="p", limit=1)
        with self.assertRaisesRegex(RunnerError, "Illegal agent state transition"):
            self.ledger.transition(row["job_id"], "reserved")

    def test_default_profile_scoping_is_exact(self):
        self.reserve(experiment="team", profile="alice", limit=1)
        self.reserve(experiment="default", profile=None, limit=1)
        with self.assertRaises(RunnerError):
            self.reserve(experiment="default2", profile=None, limit=1)
        self.reserve(experiment="bob", profile="bob", limit=1)

    def test_cpu_gpu_legacy_jobs_consume_same_two_slots(self):
        for n in range(2):
            self.jobs.add({"job_id": f"legacy-{n}", "kernel_ref": f"owner/legacy-{n}",
                           "source": "x", "source_revision": None, "dataset_refs": "[]",
                           "status": "running", "profile": None, "account_profile": "alice",
                           "staging_dir": "x", "output_dir": None, "last_message": None})
        with self.assertRaisesRegex(RunnerError, "SLOT_EXHAUSTED"):
            self.reserve(profile="alice")
        self.reserve(profile=None)

    def test_true_process_race_for_final_slot(self):
        out = multiprocessing.Queue()
        processes = [multiprocessing.Process(target=_race, args=(str(self.state), f"e{n}", out)) for n in range(2)]
        for p in processes:
            p.start()
        for p in processes:
            p.join(15)
            self.assertEqual(p.exitcode, 0)
        data = [out.get(timeout=5) for _ in processes]
        self.assertEqual(sum(int(x[0]) for x in data), 1)
        self.assertEqual(len(self.ledger.list()), 1)

    def test_legacy_processes_share_atomic_admission(self):
        out = multiprocessing.Queue()
        processes = [multiprocessing.Process(target=_legacy_race, args=(str(self.state), f"legacy{n}", out))
                     for n in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        data = [out.get(timeout=5) for _ in processes]
        self.assertEqual(sum(int(x[0]) for x in data), 1)

    def test_legacy_admission_observes_agent_reservation(self):
        _, created = self.reserve(profile="team-one", limit=1)
        self.assertTrue(created)
        with self.assertRaisesRegex(RunnerError, "SLOT_EXHAUSTED"):
            self.jobs.add({"job_id": "legacy", "kernel_ref": "owner/legacy", "source": "x",
                           "source_revision": None, "dataset_refs": "[]", "status": "prepared",
                           "profile": None, "account_profile": "team-one", "account_owner": "owner",
                           "staging_dir": "x", "output_dir": None, "last_message": None},
                          admission_limit=1)

    def test_agent_and_legacy_processes_compete_for_same_final_slot(self):
        output = multiprocessing.Queue()
        barrier = multiprocessing.Barrier(2)
        processes = [
            multiprocessing.Process(target=_mixed_race, args=(str(self.state), "agent-job", "agent", barrier, output)),
            multiprocessing.Process(target=_mixed_race, args=(str(self.state), "legacy-job", "legacy", barrier, output)),
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        outcomes = [output.get(timeout=5) for _ in processes]
        self.assertEqual(sum(int(item[0]) for item in outcomes), 1)


if __name__ == "__main__":
    unittest.main()
