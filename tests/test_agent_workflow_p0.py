"""Mocked Kaggle workflow: submit-once, crash/timeout recovery, fetch and scientific validation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kaggle_runner.agent_ledger import AgentLedger
from kaggle_runner.agent_workflow import (aggregate, load_spec, reconcile, report, resume,
                                          submit, validate_result, evidence_bundle)
from kaggle_runner.config import RunnerError, Settings
from kaggle_runner.jobs import JobStore
from kaggle_runner.packaging import slugify


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        self.settings = Settings(self.state, None, None)
        self.source = self.root / "run.py"
        self.source.write_text("print('hello')\n")
        self.spec_file = self.root / "experiment.json"
        self.spec = {
            "schema_version": "1.0", "experiment_id": "offline", "attempt_id": "seed-42",
            "project": "p0", "objective": "Offline workflow check",
            "source": {"kind": "python", "root": ".", "entrypoint": "run.py"},
            "parameters": {}, "seed": 42, "protocol": {"id": "smoke-v1", "dataset_split": "validation"},
            "datasets": [], "models": [], "resources": {"accelerator": "cpu", "max_concurrent": 2},
            "authorization": {"allowed_profiles": ["alice"], "requires_submit_approval": False},
            "expected_outputs": ["result.json"],
            "primary_metric": {"name": "accuracy", "unit": "fraction", "higher_is_better": True}
        }
        self.spec_file.write_text(json.dumps(self.spec))
        self.jobs = JobStore(self.state)
        self.pushed = 0

    def account(self, settings, store, spec, env_file, requested):
        self.assertIn(requested, ("auto", "alice"))
        return "aliceowner", None, "alice", "alice", object()

    def create_job(self, settings, cli, store, **kwargs):
        self.pushed += 1
        kwargs["on_submission_start"]()
        jid = kwargs["job_id"]
        kernel_ref = f"aliceowner/kgr-{slugify(self.root.name,24)}-{jid[:10]}".lower()
        store.add({"job_id": jid, "kernel_ref": kernel_ref, "source": str(self.source),
                   "source_revision": None, "dataset_refs": "[]", "status": "submitted",
                   "profile": None, "account_profile": "alice", "account_owner": "aliceowner",
                   "staging_dir": str(self.state / "jobs" / jid / "kernel"),
                   "output_dir": None, "last_message": None})
        return {"job_id": jid, "kernel_ref": kernel_ref}

    def fake_fetch(self, cli, store, state, job_id, output_path, force=False):
        output = self.state / "outputs" / job_id
        output.mkdir(parents=True, exist_ok=True)
        spec = json.loads(AgentLedger(self.state / "jobs.sqlite3").by_job(job_id)["spec_json"])
        result = {"schema_version": "1.0", "experiment_id": "offline", "attempt_id": "seed-42",
                  "status": "succeeded", "seed": 42, "dataset": {"ref": "example/synthetic", "split": "validation"},
                  "protocol_id": "smoke-v1", "source_sha256": spec["source"]["sha256"],
                  "metrics": {"accuracy": {"value": .8, "unit": "fraction", "higher_is_better": True}},
                  "samples_evaluated": 20, "runtime_seconds": 2.3, "artifacts": []}
        raw = json.dumps(result)
        (output / "result.json").write_text(raw)
        (output / "kgr_download_manifest.json").write_text(json.dumps({
            "job_id": job_id, "files": [{"name": "result.json", "sha256": hashlib.sha256(raw.encode()).hexdigest()}]}))
        store.update(job_id, output_dir=str(output))
        return {"job_id":job_id, "output_dir":str(output)}

    def test_submit_twice_single_push_and_successful_report(self):
        with patch("kaggle_runner.agent_workflow._choose_account", self.account), \
             patch("kaggle_runner.agent_workflow.create_job", self.create_job):
            result = submit(self.spec_file, settings=self.settings)
            second = submit(self.spec_file, settings=self.settings)
        self.assertTrue(result["ok"])
        self.assertTrue(second["existing"])
        self.assertEqual(self.pushed, 1)
        job_id = result["job_id"]
        with patch("kaggle_runner.agent_workflow._remote_status", return_value="complete"):
            outcomes = reconcile(self.settings)
        self.assertEqual(outcomes["attempts"][0]["outcome"], "remote_complete")
        with patch("kaggle_runner.agent_workflow._credentials", return_value=("aliceowner", "token", None, "alice")), \
             patch("kaggle_runner.agent_workflow.fetch_job", self.fake_fetch):
            finished = resume(self.settings, "offline")
        self.assertTrue(finished["ok"])
        self.assertEqual(validate_result(self.settings, job_id)["scientific_status"], "valid")
        agg = aggregate(self.settings, "offline")
        self.assertEqual(agg["valid_runs"], 1)
        self.assertEqual(agg["mean"], .8)
        self.assertIn("scientific result", report(self.settings, "offline"))
        first = evidence_bundle(self.settings, "offline")
        second = evidence_bundle(self.settings, "offline")
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertTrue(Path(first["bundle_path"]).is_file())

    def test_timeout_does_not_resubmit_even_if_remote_accepted(self):
        def timeout(*args, **kwargs):
            self.pushed += 1
            raise RunnerError("Kaggle CLI request timed out")
        with patch("kaggle_runner.agent_workflow._choose_account", self.account), \
             patch("kaggle_runner.agent_workflow.create_job", timeout):
            with self.assertRaises(RunnerError):
                submit(self.spec_file, settings=self.settings)
            repeated = submit(self.spec_file, settings=self.settings)
        self.assertEqual(self.pushed, 1)
        self.assertTrue(repeated["existing"])
        self.assertEqual(repeated["state"], "unknown_remote")
        self.assertEqual(len(AgentLedger(self.state / "jobs.sqlite3").stale_leases()), 0)


    def test_non_object_result_is_rejected_safely(self):
        with patch("kaggle_runner.agent_workflow._choose_account", self.account), \
             patch("kaggle_runner.agent_workflow.create_job", self.create_job):
            item = submit(self.spec_file, settings=self.settings)
        ledger = AgentLedger(self.state / "jobs.sqlite3")
        ledger.transition(item["job_id"], "remote_complete")
        out = self.state / "outputs" / item["job_id"]
        out.mkdir(parents=True)
        (out / "result.json").write_text('["not an object"]')
        self.jobs.update(item["job_id"], output_dir=str(out))
        verdict = validate_result(self.settings, item["job_id"])
        self.assertFalse(verdict["ok"])
        self.assertIn("top-level JSON", " ".join(verdict["errors"]))

    def test_empty_and_malformed_results_are_never_scientific_success(self):
        with patch("kaggle_runner.agent_workflow._choose_account", self.account), \
             patch("kaggle_runner.agent_workflow.create_job", self.create_job):
            item = submit(self.spec_file, settings=self.settings)
        ledger = AgentLedger(self.state / "jobs.sqlite3")
        ledger.transition(item["job_id"], "remote_complete")
        out = self.state / "outputs" / item["job_id"]
        out.mkdir(parents=True)
        self.jobs.update(item["job_id"], output_dir=str(out))
        for payload in ("{}", "[]", "null", "{broken json"):
            (out / "result.json").write_text(payload)
            verdict = validate_result(self.settings, item["job_id"])
            self.assertFalse(verdict["ok"], payload)
            self.assertEqual(ledger.by_job(item["job_id"])["state"], "invalid_result")

    def test_missing_metrics_is_not_scientific_success(self):
        with patch("kaggle_runner.agent_workflow._choose_account", self.account), \
             patch("kaggle_runner.agent_workflow.create_job", self.create_job):
            result = submit(self.spec_file, settings=self.settings)
        ledger = AgentLedger(self.state / "jobs.sqlite3")
        ledger.transition(result["job_id"], "remote_complete")
        verdict = validate_result(self.settings, result["job_id"])
        self.assertFalse(verdict["ok"])
        self.assertEqual(ledger.by_job(result["job_id"])["state"], "invalid_result")

    def test_reconcile_manual_review_is_not_success(self):
        with patch("kaggle_runner.agent_workflow._choose_account", self.account), \
             patch("kaggle_runner.agent_workflow.create_job", self.create_job):
            submit(self.spec_file, settings=self.settings)
        with patch("kaggle_runner.agent_workflow._remote_status", return_value="unrecognized"):
            outcome = reconcile(self.settings)
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["outcome"], "manual_review")
        self.assertEqual(outcome["attempts"][0]["action"], "operator_must_verify_remote_state")

    def test_reconcile_manual_review_uses_failure_exit_code(self):
        from kaggle_runner.cli import main
        result = {"schema_version": "1.0", "ok": False, "outcome": "manual_review", "attempts": []}
        with patch("kaggle_runner.cli.build_settings", return_value=self.settings), \
             patch("kaggle_runner.agent_workflow.reconcile", return_value=result), \
             patch("kaggle_runner.cli._emit"):
            self.assertEqual(main(["--state-dir", str(self.state), "reconcile", "--json"]), 3)

    def test_spec_rejects_unauthorized_or_credentials_in_parameters(self):
        self.spec["parameters"] = {"api_token": "never"}
        self.spec_file.write_text(json.dumps(self.spec))
        with self.assertRaisesRegex(RunnerError, "Credentials"):
            load_spec(self.spec_file)
        self.spec["parameters"] = {}
        self.spec["authorization"]["allowed_profiles"] = []
        self.spec_file.write_text(json.dumps(self.spec))
        with self.assertRaisesRegex(RunnerError, "allowed_profiles"):
            load_spec(self.spec_file)


if __name__ == "__main__":
    unittest.main()
