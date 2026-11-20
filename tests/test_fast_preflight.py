"""Regression: Kaggle Active Events may be empty while old notebooks stay cancelled."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from kaggle_runner.api import KaggleCLI, Result
from kaggle_runner.cli import main
from kaggle_runner.config import RunnerError
from kaggle_runner.jobs import JobStore
from kaggle_runner.preflight import active_report, check_preflight, tracked_active_report


class FakeAccount:
    def __init__(self):
        self.calls: list[tuple[str, ...]] = []
        self.statuses: dict[str, str] = {}
        self.quota = 29.99
        self.raise_status = False

    def call(self, *argv: str, **kwargs):
        self.calls.append(tuple(argv))
        if argv[:1] == ("quota",):
            rows = [{"resource": "GPU", "used": "0.01h", "remaining": f"{self.quota}h", "total": "30h"}]
            return Result(list(argv), 0, json.dumps(rows), "")
        if argv[:2] == ("kernels", "list"):
            return Result(list(argv), 0, json.dumps([{"ref": k} for k in self.statuses]), "")
        if argv[:2] == ("kernels", "status"):
            if self.raise_status:
                return Result(list(argv), 1, "", "Permission 'kernels.get' was denied")
            state = self.statuses.get(argv[2], "RUNNING")
            return Result(list(argv), 0, f'{argv[2]} has status "KernelWorkerStatus.{state}"', "")
        if argv[:2] == ("kernels", "push"):
            staging = Path(argv[argv.index("-p") + 1])
            ref = json.loads((staging / "kernel-metadata.json").read_text())["id"]
            return Result(list(argv), 0,
                          "Kernel version 1 successfully pushed. "
                          f"https://www.kaggle.com/code/{ref}", "")
        raise AssertionError(f"unexpected call: {argv}")


class FastPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = JobStore(self.root / "state")
        self.api = FakeAccount()

    def add_job(self, ref: str, job_id: str, status: str = "submitted"):
        self.store.add({"job_id": job_id, "kernel_ref": ref,
                        "source": str(self.root / "train.py"), "source_revision": None,
                        "dataset_refs": "[]", "status": status, "profile": None,
                        "staging_dir": str(self.root), "output_dir": None,
                        "last_message": None})

    def test_default_check_uses_quota_only_when_no_tracked_jobs(self):
        self.api.statuses = {"hoanggvo/ta-en-ablation": "CANCEL_ACKNOWLEDGED",
                             "hoanggvo/icfg-ta-enrichment": "CANCEL_ACKNOWLEDGED"}
        report = check_preflight(self.api, store=self.store, owner="hoanggvo",
                                 gpu="t4x2", required_gpu_hours=2)
        self.assertTrue(report["ok"])
        self.assertFalse(report["slot_verified"])
        self.assertEqual(report["active"]["source"], "tracked_jobs")
        self.assertEqual(report["active"]["running_count"], 0)
        self.assertEqual(self.api.calls, [("quota", "--format", "json")])
        self.assertIn("runner-tracked", report["warnings"][0])

    def test_remote_mode_is_optional_and_acknowledged_cancels_are_separate(self):
        self.api.statuses = {"hoanggvo/ta-en-ablation": "CANCEL_ACKNOWLEDGED",
                             "hoanggvo/icfg-ta-enrichment": "CANCEL_ACKNOWLEDGED"}
        report = check_preflight(self.api, store=self.store, owner="hoanggvo",
                                 remote=True, gpu="t4x2", required_gpu_hours=2)
        self.assertTrue(report["ok"])
        self.assertEqual(report["active"]["running_count"], 0)
        self.assertEqual(report["active"]["cancelling_count"], 2)
        self.assertEqual({r["status"] for r in report["active"]["cancelling"]},
                         {"cancel_acknowledged"})
        self.assertTrue(any("cancel-acknowledged" in msg for msg in report["warnings"]))
        self.assertTrue(any(c[:2] == ("kernels", "list") for c in self.api.calls))

    def test_tracked_acknowledged_cancel_does_not_falsely_block(self):
        self.add_job("hoanggvo/ta-en-ablation", "old1")
        self.add_job("hoanggvo/icfg-ta-enrichment", "old2")
        self.api.statuses = {"hoanggvo/ta-en-ablation": "CANCEL_ACKNOWLEDGED",
                             "hoanggvo/icfg-ta-enrichment": "CANCEL_ACKNOWLEDGED"}
        report = tracked_active_report(self.api, self.store, owner="hoanggvo")
        self.assertEqual(report["running_count"], 0)
        self.assertEqual(report["cancelling_count"], 2)
        self.assertTrue(report["has_slot"])
        self.assertEqual(self.store.get("old1")["status"], "cancel_acknowledged")
        self.assertFalse(any(c[:2] == ("kernels", "list") for c in self.api.calls))

    def test_two_live_tracked_runs_still_block(self):
        self.add_job("hoanggvo/a", "job1")
        self.add_job("hoanggvo/b", "job2")
        self.api.statuses = {"hoanggvo/a": "RUNNING", "hoanggvo/b": "QUEUED"}
        report = check_preflight(self.api, store=self.store, owner="hoanggvo",
                                 gpu="t4x2", required_gpu_hours=2)
        self.assertFalse(report["ok"])
        self.assertEqual(report["active"]["running_count"], 2)
        self.assertTrue(any("Concurrent batch jobs 2/2" in r for r in report["reasons"]))

    def test_other_owner_jobs_still_count_in_shared_local_ledger(self):
        self.add_job("otheruser/a", "foreign")
        report = tracked_active_report(self.api, self.store, owner="hoanggvo")
        self.assertEqual(report["scanned"], 1)
        self.assertEqual(report["running_count"], 1)
        self.assertTrue(any(c[:2] == ("kernels", "status") for c in self.api.calls))

    def test_quota_shortage_still_blocks_and_uncertain_remote_status_fails(self):
        self.api.quota = 0.5
        report = check_preflight(self.api, store=self.store, owner="hoanggvo",
                                 gpu="t4x2", required_gpu_hours=2)
        self.assertFalse(report["ok"])
        self.assertIn("GPU remaining", " ".join(report["reasons"]))
        self.add_job("hoanggvo/new", "new1")
        self.api.raise_status = True
        report = tracked_active_report(self.api, self.store, owner="hoanggvo")
        self.assertEqual(report["unverified_count"], 1)
        self.assertFalse(report["has_slot"])

    def test_cli_active_defaults_to_local_and_remote_opt_in(self):
        from kaggle_runner.accounts import write_account
        env = self.root / ".env"
        write_account(str(env), "hoanggvo", "test@example.com", "fake-secret")
        api = self.api
        api.statuses = {"hoanggvo/ta-en-ablation": "CANCEL_ACKNOWLEDGED"}
        argv = ["--env-file", str(env), "--state-dir", str(self.root / "state")]
        with patch.object(KaggleCLI, "call", api.call), redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()):
            self.assertEqual(main([*argv, "active", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["source"], "tracked_jobs")
        self.assertEqual(api.calls, [])
        with patch.object(KaggleCLI, "call", api.call), redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()):
            self.assertEqual(main([*argv, "active", "--remote", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["cancelling_count"], 1)
        self.assertTrue(any(c[:2] == ("kernels", "list") for c in api.calls))

    def test_cli_run_uses_fast_preflight_and_preserves_quota_gate(self):
        from kaggle_runner.accounts import write_account
        env = self.root / ".env"
        write_account(str(env), "hoanggvo", "test@example.com", "fake-secret")
        src = self.root / "train.py"
        src.write_text('print("test")\n')
        argv = ["--env-file", str(env), "--state-dir", str(self.root / "state"),
                "run", str(src), "--gpu", "t4x2", "--required-gpu-hours", "2", "--json"]
        with patch.object(KaggleCLI, "call", self.api.call), \
             redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()):
            self.assertEqual(main(argv), 0)
        response = json.loads(out.getvalue())
        self.assertIn("job_id", response)
        self.assertTrue(any(c[:2] == ("kernels", "push") for c in self.api.calls))
        self.assertFalse(any(c[:2] == ("kernels", "list") for c in self.api.calls))
        self.api.quota = 0.1
        with patch.object(KaggleCLI, "call", self.api.call), \
             redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(main(argv), 2)


if __name__ == "__main__":
    unittest.main()
