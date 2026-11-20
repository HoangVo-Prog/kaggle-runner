"""Offline integration tests for durable objective planning and fake execution."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import base64
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import re
from threading import Barrier
import tempfile
import unittest
from unittest.mock import patch

from kaggle_runner.agent_ledger import AgentLedger
from kaggle_runner.agent_workflow import aggregate, load_spec, validate_result
from kaggle_runner.config import RunnerError, Settings
from kaggle_runner.jobs import JobStore
from kaggle_runner.packaging import slugify
from kaggle_runner.packaging import make_bootstrap
from kaggle_runner.notebooks import stage_notebook
from kaggle_runner import work_checkpoint as work


class WorkCheckpointTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.settings = Settings(self.root / "state", None, None)
        self.source = self.project / "train.py"
        self.source.write_text("import os\nprint(os.environ.get('KGR_SEED'))\n", encoding="utf-8")
        self.template = {
            "schema_version": "1.0", "experiment_id": "matrix", "attempt_id": "placeholder",
            "project": "offline", "objective": "seed matrix test",
            "source": {"kind": "python", "root": str(self.project), "entrypoint": "train.py"},
            "parameters": {"learning_rate": 0.01}, "seed": 1001,
            "protocol": {"id": "fixed-v1", "dataset_split": "validation"},
            "datasets": ["alice/dataset-a"], "models": ["alice/model-b/tf2/vision/1"],
            "resources": {"accelerator": "t4x2", "max_concurrent": 2},
            "authorization": {"allowed_profiles": ["alice"], "requires_submit_approval": False},
            "expected_outputs": ["result.json"],
            "primary_metric": {"name": "accuracy", "unit": "fraction", "higher_is_better": True},
        }
        self.obj = work.create(self.settings, title="Notebook evaluation", description="offline",
                               acceptance=["All valid attempts use the pinned protocol"],
                               authorized_profiles=["alice"], objective_id="obj-notebook-eval")
        self.plan_path = self.root / "plan.json"

    def write_plan(self, *, seeds=None, params=None, budget=10.0, per_run=0.5, dataset=None):
        template = json.loads(json.dumps(self.template))
        if params is not None:
            template["parameters"] = params
        if dataset is not None:
            template["datasets"] = [dataset]
        self.plan_path.write_text(json.dumps({"template": template,
            "seeds": seeds or list(range(1001, 1011)), "total_gpu_hours": budget,
            "planned_gpu_hours_per_attempt": per_run, "max_concurrent": 2,
            "approval_required": True}, indent=2), encoding="utf-8")

    def make_plan(self):
        self.write_plan()
        return work.plan(self.settings, self.obj["objective_id"], self.plan_path, expected_revision=1)

    def test_ten_seed_plan_is_stable_and_survives_new_session(self):
        first = self.make_plan()
        self.assertEqual(first["objective"]["state"], "awaiting_approval")
        attempts = first["attempts"]
        self.assertEqual(len(attempts), 10)
        self.assertEqual([a["attempt_id"] for a in attempts], [f"run-{i:02d}-seed-{1000+i}" for i in range(1, 11)])
        paths_before = [Path(a["spec_path"]).read_bytes() for a in work.WorkLedger(self.settings.state / "jobs.sqlite3").links(self.obj["objective_id"])]
        second = work.plan(self.settings, self.obj["objective_id"], self.plan_path, expected_revision=2)
        paths_after = [Path(a["spec_path"]).read_bytes() for a in work.WorkLedger(self.settings.state / "jobs.sqlite3").links(self.obj["objective_id"])]
        self.assertEqual(paths_before, paths_after)
        self.assertEqual(len(second["attempts"]), 10)
        self.assertTrue(all(a["spec_available"] for a in second["attempts"]))
        self.assertEqual(second["progress"]["planned_gpu_hours"], 5.0)

    def test_budget_and_profile_allowlist_are_enforced(self):
        self.write_plan(budget=1.0, per_run=0.5)
        with self.assertRaisesRegex(RunnerError, "exceed"):
            work.plan(self.settings, self.obj["objective_id"], self.plan_path, expected_revision=1)
        self.write_plan()
        plan_doc = json.loads(self.plan_path.read_text())
        plan_doc["template"]["authorization"]["allowed_profiles"] = ["alice", "unapproved"]
        self.plan_path.write_text(json.dumps(plan_doc))
        with self.assertRaisesRegex(RunnerError, "allowlist"):
            work.plan(self.settings, self.obj["objective_id"], self.plan_path, expected_revision=1)

    def test_objective_plan_can_contain_multiple_experiments(self):
        first = json.loads(json.dumps(self.template))
        second = json.loads(json.dumps(self.template))
        first["experiment_id"] = "protocol-a"
        second["experiment_id"] = "protocol-b"
        second["parameters"]["learning_rate"] = 0.02
        self.plan_path.write_text(json.dumps({"experiments": [
            {"template": first, "seeds": [11, 12]},
            {"template": second, "seeds": [21, 22]},
        ], "total_gpu_hours": 2.0, "planned_gpu_hours_per_attempt": 0.5,
            "max_concurrent": 2}), encoding="utf-8")
        planned = work.plan(self.settings, self.obj["objective_id"], self.plan_path, expected_revision=1)
        self.assertEqual(planned["progress"]["planned_attempts"], 4)
        self.assertEqual({a["experiment_id"] for a in planned["attempts"]}, {"protocol-a", "protocol-b"})
        self.assertEqual({a["attempt_id"] for a in planned["attempts"]}, {
            "run-01-seed-11", "run-02-seed-12", "run-01-seed-21", "run-02-seed-22"})
        self.assertEqual(planned["objective"]["metadata"]["plan"]["experiment_ids"],
                         ["protocol-a", "protocol-b"])

    def test_wait_resumes_each_experiment_in_a_mixed_batch(self):
        templates = []
        for experiment_id in ("protocol-a", "protocol-b"):
            template = json.loads(json.dumps(self.template))
            template["experiment_id"] = experiment_id
            templates.append({"template": template, "seeds": [1]})
        self.plan_path.write_text(json.dumps({"experiments": templates,
            "total_gpu_hours": 1.0, "planned_gpu_hours_per_attempt": 0.5,
            "max_concurrent": 2}), encoding="utf-8")
        planned = work.plan(self.settings, self.obj["objective_id"], self.plan_path, expected_revision=1)
        work.approve(self.settings, self.obj["objective_id"], expected_revision=planned["objective"]["revision"])
        submitted = []

        def fake_submit(spec_path, **kwargs):
            spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
            job_id = hashlib.sha256(spec["experiment_id"].encode()).hexdigest()[:32]
            ledger = AgentLedger(self.settings.state / "jobs.sqlite3")
            record, _ = ledger.reserve(experiment_id=spec["experiment_id"], attempt_id=spec["attempt_id"],
                fingerprint=spec["experiment_id"], job_id=job_id, spec=spec,
                account_profile="alice", account_owner="alice", kernel_ref=f"alice/{job_id}", limit=2)
            for state in ("staging", "submitting", "submitted"):
                ledger.transition(job_id, state)
            JobStore(self.settings.state).add({"job_id": job_id, "kernel_ref": record["kernel_ref"],
                "source": str(self.project), "source_revision": None, "dataset_refs": "[]",
                "status": "submitted", "profile": None, "account_profile": "alice",
                "account_owner": "alice", "staging_dir": str(self.settings.state / "jobs" / job_id),
                "output_dir": None, "last_message": None})
            submitted.append(spec["experiment_id"])
            return {"ok": True, "job_id": job_id, "state": "submitted"}

        with patch("kaggle_runner.agent_workflow.submit", side_effect=fake_submit), \
             patch("kaggle_runner.agent_workflow.resume", return_value={"ok": True, "attempts": []}) as resume_mock:
            work.execute(self.settings, self.obj["objective_id"], account="alice", approve=True, wait=True)
        self.assertCountEqual(submitted, ["protocol-a", "protocol-b"])
        resumed_ids = [call.args[1] for call in resume_mock.call_args_list]
        self.assertCountEqual(resumed_ids, ["protocol-a", "protocol-b"])

    def test_objective_report_includes_authoritative_aggregation_evidence(self):
        self.make_plan()
        result = work.report(self.settings, self.obj["objective_id"])
        self.assertIn("Experiment aggregates", result)
        self.assertIn("No submitted experiment attempts", result)

    def test_plan_rejects_missing_acceptance_criteria(self):
        empty = work.create(self.settings, title="No criteria", objective_id="obj-no-criteria")
        self.write_plan(seeds=[1])
        with self.assertRaisesRegex(RunnerError, "acceptance"):
            work.plan(self.settings, empty["objective_id"], self.plan_path, expected_revision=1)

    def test_competing_checkpoint_updates_use_optimistic_revision(self):
        planned = self.make_plan()
        approved = work.approve(self.settings, self.obj["objective_id"], expected_revision=2)
        def checkpoint():
            try:
                return work.checkpoint(self.settings, self.obj["objective_id"], "source_review",
                    expected_revision=approved["objective"]["revision"], step_status="done",
                    evidence=[{"kind": "source-inspection", "sha256": "a" * 64}])
            except RunnerError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: checkpoint(), range(2)))
        self.assertEqual(sum(isinstance(x, dict) for x in outcomes), 1)
        self.assertTrue(any(isinstance(x, str) and "REVISION_CONFLICT" in x for x in outcomes))

    def test_changed_source_after_plan_is_rejected_before_submission(self):
        planned = self.make_plan()
        work.approve(self.settings, self.obj["objective_id"], expected_revision=2)
        self.source.write_text("print('changed')\n", encoding="utf-8")
        stale = work.show(self.settings, self.obj["objective_id"])
        self.assertEqual(stale["attempts"][0]["plan_status"], "stale")
        self.assertTrue(all(step["status"] == "stale" for step in stale["steps"]))
        with self.assertRaisesRegex(RunnerError, "source|SOURCE_REVISION_MISMATCH"):
            work.execute(self.settings, self.obj["objective_id"], account="alice", approve=True)

    def test_changed_immutable_attempt_snapshot_is_stale_and_never_submitted(self):
        self.make_plan()
        work.approve(self.settings, self.obj["objective_id"], expected_revision=2)
        links = work.WorkLedger(self.settings.state / "jobs.sqlite3").links(self.obj["objective_id"])
        spec_path = Path(links[0]["spec_path"])
        spec_path.write_text(spec_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(RunnerError, "[Ii]mmutable planned spec"):
            work.execute(self.settings, self.obj["objective_id"], account="alice", approve=True)

    def test_quota_exhaustion_stops_after_completed_first_batch(self):
        self.write_plan(seeds=[1001, 1002, 1003, 1004], budget=2.0)
        planned = work.plan(self.settings, self.obj["objective_id"], self.plan_path, expected_revision=1)
        work.approve(self.settings, self.obj["objective_id"], expected_revision=planned["objective"]["revision"])
        def submit(spec_file, **kwargs):
            spec = json.loads(Path(spec_file).read_text(encoding="utf-8"))
            if spec["seed"] >= 1003:
                raise RunnerError("GPU quota exhausted during fresh preflight")
            job_id = hashlib.sha256(spec["attempt_id"].encode()).hexdigest()[:32]
            ledger = AgentLedger(self.settings.state / "jobs.sqlite3")
            record, _ = ledger.reserve(experiment_id=spec["experiment_id"], attempt_id=spec["attempt_id"],
                fingerprint="fake", job_id=job_id, spec=spec, account_profile="alice",
                account_owner="alice", kernel_ref=f"alice/{spec['attempt_id']}", limit=2)
            for state in ("staging", "submitting", "submitted", "remote_complete", "succeeded"):
                ledger.transition(job_id, state, note="fake provider and validator", scientific_status="succeeded" if state == "succeeded" else None)
            JobStore(self.settings.state).add({"job_id": job_id, "kernel_ref": record["kernel_ref"], "source": str(self.project),
                "source_revision": None, "dataset_refs": "[]", "status": "complete", "profile": None,
                "account_profile": "alice", "account_owner": "alice", "staging_dir": str(self.settings.state / "jobs" / job_id),
                "output_dir": None, "last_message": None})
            return {"ok": True, "job_id": job_id, "state": "submitted"}
        with patch("kaggle_runner.agent_workflow.submit", side_effect=submit), \
             patch("kaggle_runner.agent_workflow.resume", return_value={"ok": True, "attempts": []}):
            result = work.execute(self.settings, self.obj["objective_id"], account="alice", wait=True)
        self.assertTrue(result["execution"]["stopped_after_blocker"])
        self.assertEqual(result["progress"]["scientifically_validated_attempts"], 2)
        self.assertEqual(result["objective"]["state"], "blocked")

    def test_execute_ten_attempts_with_fake_provider_and_no_duplicate_push(self):
        planned = self.make_plan()
        work.approve(self.settings, self.obj["objective_id"], expected_revision=2)
        pushed = []
        def choose(settings, store, spec, env_file, requested):
            self.assertEqual(requested, "alice")
            self.assertEqual(spec["authorization"]["allowed_profiles"], ["alice"])
            return "alice", None, "alice", "alice", object()
        def fake_create(settings, cli, store, **kwargs):
            job_id = kwargs["job_id"]
            pushed.append(job_id)
            kwargs["on_submission_start"]()
            ref = f"alice/kgr-{slugify(self.project.name,24)}-{job_id[:10]}".lower()
            store.add({"job_id": job_id, "kernel_ref": ref, "source": str(self.root),
                "source_revision": None, "dataset_refs": "[]", "status": "submitted", "profile": None,
                "account_profile": "alice", "account_owner": "alice", "staging_dir": str(self.settings.state / "jobs" / job_id / "kernel"),
                "output_dir": None, "last_message": None})
            return {"job_id": job_id, "kernel_ref": ref}
        original_submit = __import__("kaggle_runner.agent_workflow", fromlist=["submit"]).submit
        def fake_submit(*args, **kwargs):
            response = original_submit(*args, **kwargs)
            ledger = AgentLedger(self.settings.state / "jobs.sqlite3")
            ledger.transition(response["job_id"], "remote_complete", note="fake provider completed")
            ledger.transition(response["job_id"], "succeeded", note="fake scientific validator accepted result",
                              scientific_status="succeeded", validation={"ok": True, "result_sha256": "b" * 64,
                                                                          "runtime_seconds": 0.1})
            JobStore(self.settings.state).update(response["job_id"], status="complete")
            return response
        with patch("kaggle_runner.agent_workflow._choose_account", choose), \
             patch("kaggle_runner.agent_workflow.create_job", fake_create), \
             patch("kaggle_runner.agent_workflow.submit", side_effect=fake_submit), \
             patch("kaggle_runner.agent_workflow.resume", return_value={"ok": True, "attempts": []}):
            result = work.execute(self.settings, self.obj["objective_id"], account="alice", approve=True, wait=True)
            resumed = work.execute(self.settings, self.obj["objective_id"], account="alice", approve=True)
        self.assertEqual(len(pushed), 10, json.dumps({"execution": result.get("execution"), "attempts": result.get("attempts")[:2]}, indent=2))
        self.assertEqual(result["progress"]["linked_jobs"], 10)
        self.assertEqual(result["progress"]["scientifically_validated_attempts"], 10)
        self.assertGreater(result["progress"]["observed_gpu_hours"], 0)
        self.assertEqual(len(resumed["attempts"]), 10)
        current = result
        for step_id in ("source_review", "execute_attempts", "validate_results", "aggregate_report"):
            current = work.checkpoint(self.settings, self.obj["objective_id"], step_id,
                expected_revision=current["objective"]["revision"], step_status="done",
                evidence=[{"kind": "offline-test", "attempt_count": 10}])
        completed = work.complete(self.settings, self.obj["objective_id"],
            expected_revision=current["objective"]["revision"], acceptance_review=[{
                "criterion": self.obj["acceptance_criteria"][0], "evidence": [{"kind": "validated-results", "count": 10}]}])
        self.assertEqual(completed["objective"]["state"], "completed")
        report = work.report(self.settings, self.obj["objective_id"])
        latest = work.status(self.settings, self.obj["objective_id"])
        saved_report = latest["objective"]["metadata"]["last_successful_report"]
        self.assertTrue(Path(saved_report["path"]).is_file())
        self.assertIn(saved_report["sha256"], report)

    def test_two_workers_racing_same_objective_reserve_one_attempt(self):
        self.write_plan(seeds=[1001], budget=1.0)
        planned = work.plan(self.settings, self.obj["objective_id"], self.plan_path, expected_revision=1)
        approved = work.approve(self.settings, self.obj["objective_id"], expected_revision=planned["objective"]["revision"])
        work.WorkLedger(self.settings.state / "jobs.sqlite3", create=False).change(
            self.obj["objective_id"], expected_revision=approved["objective"]["revision"], state="running")
        barrier = Barrier(2)
        pushed = []
        def fake_submit(spec_path, **kwargs):
            spec, _ = load_spec(spec_path)
            barrier.wait(timeout=5)
            ledger = AgentLedger(self.settings.state / "jobs.sqlite3")
            old = ledger.get(spec["experiment_id"], spec["attempt_id"])
            if old:
                return {"ok": True, "existing": True, "job_id": old["job_id"], "state": old["state"]}
            job_id = hashlib.sha256(spec["attempt_id"].encode()).hexdigest()[:32]
            record, created = ledger.reserve(experiment_id=spec["experiment_id"], attempt_id=spec["attempt_id"],
                fingerprint="same", job_id=job_id, spec=spec, account_profile="alice",
                account_owner="alice", kernel_ref=f"alice/{job_id}", limit=2)
            if created:
                pushed.append(job_id)
                for state in ("staging", "submitting", "submitted", "remote_complete", "succeeded"):
                    ledger.transition(job_id, state, scientific_status="succeeded" if state == "succeeded" else None)
            return {"ok": True, "job_id": record["job_id"], "state": record["state"]}
        with patch("kaggle_runner.agent_workflow.submit", side_effect=fake_submit):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(work.execute, self.settings, self.obj["objective_id"], account="alice") for _ in range(2)]
                results = [future.result() for future in futures]
        self.assertEqual(len(pushed), 1)
        self.assertTrue(all(len(result["attempts"]) == 1 for result in results))

    def test_resume_flags_unknown_remote_for_operator_reconciliation(self):
        self.make_plan()
        link = work.WorkLedger(self.settings.state / "jobs.sqlite3").links(self.obj["objective_id"])[0]
        spec = json.loads(Path(link["spec_path"]).read_text())
        ledger = AgentLedger(self.settings.state / "jobs.sqlite3")
        job_id = "unknown-job"
        ledger.reserve(experiment_id=spec["experiment_id"], attempt_id=spec["attempt_id"], fingerprint="f",
            job_id=job_id, spec=spec, account_profile="alice", account_owner="alice", kernel_ref="alice/kgr-unknown")
        ledger.transition(job_id, "staging")
        ledger.transition(job_id, "submitting")
        ledger.transition(job_id, "unknown_remote", note="push outcome uncertain")
        work.WorkLedger(self.settings.state / "jobs.sqlite3").link_job(self.obj["objective_id"], spec["experiment_id"], spec["attempt_id"], job_id)
        result = work.resume(self.settings, self.obj["objective_id"])
        self.assertIn("reconcile", result["next_action"].casefold())
        self.assertIn("Ambiguous", result["current_blocker"])

    def test_pause_is_revision_checked_and_idempotent(self):
        planned = self.make_plan()
        approved = work.approve(self.settings, self.obj["objective_id"], expected_revision=planned["objective"]["revision"])
        paused = work.pause(self.settings, self.obj["objective_id"],
            expected_revision=approved["objective"]["revision"], reason="Operator reviewing scope")
        revision = paused["objective"]["revision"]
        repeated = work.pause(self.settings, self.obj["objective_id"], expected_revision=revision,
                              reason="Operator reviewing scope")
        self.assertEqual(repeated["objective"]["state"], "paused")
        self.assertEqual(repeated["objective"]["revision"], revision)

    def test_evidence_redaction_and_timeline(self):
        planned = self.make_plan()
        approved = work.approve(self.settings, self.obj["objective_id"], expected_revision=2)
        result = work.checkpoint(self.settings, self.obj["objective_id"], "source_review",
            expected_revision=approved["objective"]["revision"], step_status="done",
            evidence=[{"kind": "verification", "api_token": "should-not-persist", "sha256": "c" * 64}])
        rendered = json.dumps(result)
        self.assertNotIn("should-not-persist", rendered)
        self.assertIn("[redacted]", rendered)
        self.assertGreaterEqual(len(result["timeline"]), 3)

    def test_cli_json_contract(self):
        from kaggle_runner.cli import main
        with patch("kaggle_runner.cli.build_settings", return_value=self.settings):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["work", "list", "--active", "--json"]), 0)

    def test_corrupt_database_is_preserved_and_actionable(self):
        state = self.root / "corrupt-state"
        state.mkdir()
        db = state / "jobs.sqlite3"
        original = b"not a sqlite database"
        db.write_bytes(original)
        with self.assertRaisesRegex(RunnerError, "was not reset"):
            work.WorkLedger(db, create=True)
        self.assertEqual(db.read_bytes(), original)

    def test_old_job_database_lists_no_objectives_without_migration_writes(self):
        state = self.root / "old-state"
        JobStore(state)
        db = state / "jobs.sqlite3"
        result = work.list_work(Settings(state, None, None), active=True)
        self.assertEqual(result["objectives"], [])
        import sqlite3
        con = sqlite3.connect(db)
        try:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        self.assertNotIn("work_objectives", tables)

    def test_aggregate_rejects_changed_invariant_parameters_or_dataset(self):
        for variant in ("parameters", "datasets"):
            with self.subTest(variant=variant):
                temp = tempfile.TemporaryDirectory()
                self.addCleanup(temp.cleanup)
                root = Path(temp.name)
                project = root / "project"
                project.mkdir()
                (project / "train.py").write_text("print('ok')\n", encoding="utf-8")
                settings = Settings(root / "state", None, None)
                jobs = JobStore(settings.state)
                ledger = AgentLedger(settings.state / "jobs.sqlite3")
                for index, seed in enumerate((31, 32)):
                    spec = json.loads(json.dumps(self.template))
                    spec["source"]["root"] = str(project)
                    spec["seed"] = seed
                    spec["attempt_id"] = f"seed-{seed}"
                    if index and variant == "parameters":
                        spec["parameters"]["learning_rate"] = 0.02
                    if index and variant == "datasets":
                        spec["datasets"] = ["alice/dataset-b"]
                    path = root / f"spec-{index}.json"
                    path.write_text(json.dumps(spec), encoding="utf-8")
                    normalized, _ = load_spec(path)
                    job_id = hashlib.sha256(f"{variant}-{seed}".encode()).hexdigest()[:32]
                    record, _ = ledger.reserve(experiment_id="matrix", attempt_id=spec["attempt_id"],
                        fingerprint=f"{variant}-{seed}", job_id=job_id, spec=normalized,
                        account_profile="alice", account_owner="alice", kernel_ref=f"alice/{job_id}", limit=1)
                    for state in ("staging", "submitting", "submitted", "remote_complete", "succeeded"):
                        ledger.transition(job_id, state, scientific_status="succeeded" if state == "succeeded" else None)
                    output = settings.state / "outputs" / job_id
                    output.mkdir(parents=True)
                    result = {"schema_version": "1.0", "experiment_id": "matrix", "attempt_id": spec["attempt_id"],
                        "status": "succeeded", "seed": seed,
                        "dataset": {"ref": normalized["datasets"][0]["ref"], "split": "validation"},
                        "protocol_id": "fixed-v1", "source_sha256": normalized["source"]["sha256"],
                        "metrics": {"accuracy": {"value": 0.8, "unit": "fraction", "higher_is_better": True}},
                        "samples_evaluated": 10, "runtime_seconds": 1.0, "artifacts": []}
                    raw_result = json.dumps(result)
                    (output / "result.json").write_text(raw_result, encoding="utf-8")
                    (output / "kgr_download_manifest.json").write_text(json.dumps({"job_id": job_id,
                        "files": [{"name": "result.json", "sha256": hashlib.sha256(raw_result.encode()).hexdigest()}]}), encoding="utf-8")
                    jobs.add({"job_id": job_id, "kernel_ref": record["kernel_ref"], "source": str(project),
                        "source_revision": None, "dataset_refs": "[]", "status": "complete", "profile": None,
                        "account_profile": "alice", "account_owner": "alice", "staging_dir": str(settings.state / "jobs" / job_id),
                        "output_dir": str(output), "last_message": None})
                for attempt in ledger.list("matrix"):
                    verdict = validate_result(settings, attempt["job_id"], save=False)
                    self.assertTrue(verdict["ok"], json.dumps(verdict, indent=2))
                with self.assertRaisesRegex(RunnerError, "INCOMPATIBLE_RESULTS"):
                    aggregate(settings, "matrix")

    def test_seed_is_injected_for_script_and_notebook_runtimes(self):
        script = make_bootstrap(b"print('ok')", entry="train.py", arguments=[], datasets=[],
            install_requirements=False, experiment_id="seeded", attempt_id="run-1",
            parameters={"x": 1}, seed=2031)
        self.assertIn('os.environ["KGR_SEED"] = str(_OPTIONS.get("seed", ""))', script)
        encoded = re.search(r'_OPTIONS = json.loads\(base64.b64decode\("([^"]+)"\)\)', script).group(1)
        self.assertEqual(json.loads(base64.b64decode(encoded))["seed"], 2031)
        notebook = self.project / "train.ipynb"
        notebook.write_text(json.dumps({"nbformat": 4, "nbformat_minor": 5,
            "metadata": {"kernelspec": {"language": "python"}}, "cells": [
                {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [],
                 "source": ["print('ok')\n"], "id": "cell-1"}]}), encoding="utf-8")
        output = self.root / "staged.ipynb"
        stage_notebook(notebook, output, [], [], root=self.project, entry="train.ipynb",
            experiment_id="seeded", attempt_id="run-1", parameters={}, seed=2031)
        staged = json.loads(output.read_text(encoding="utf-8"))
        self.assertIn("os.environ['KGR_SEED'] = '2031'", staged["cells"][0]["source"])

    def test_completed_objective_needs_all_criteria_and_validated_attempts(self):
        self.make_plan()
        approved = work.approve(self.settings, self.obj["objective_id"], expected_revision=2)
        ledger = work.WorkLedger(self.settings.state / "jobs.sqlite3", create=False)
        running = ledger.change(self.obj["objective_id"], expected_revision=approved["objective"]["revision"], state="running")
        with self.assertRaisesRegex(RunnerError, "All required checklist"):
            work.complete(self.settings, self.obj["objective_id"], expected_revision=running["revision"],
                acceptance_review=[{"criterion": self.obj["acceptance_criteria"][0], "evidence": [{"path": "review.md"}]}])


if __name__ == "__main__":
    unittest.main()
