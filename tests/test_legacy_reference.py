"""Historic owner mismatch: recovery without hiding real concurrent jobs."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from kaggle_runner.accounts import write_account
from kaggle_runner.api import KaggleCLI, Result
from kaggle_runner.cli import main
from kaggle_runner.jobs import JobStore
from kaggle_runner.preflight import check_preflight, tracked_active_report


class API:
    def __init__(self):
        self.calls = []
        self.statuses = {}

    def call(self, *args, **kwargs):
        self.calls.append(tuple(args))
        if args[:2] == ("kernels", "status"):
            state = self.statuses.get(args[2])
            if state is None:
                return Result(list(args), 1, "", "Permission 'kernels.get' was denied. notebook may be private")
            return Result(list(args), 0, f'{args[2]} has status "KernelWorkerStatus.{state}"', "")
        if args[:1] == ("quota",):
            return Result(list(args), 0, '[{"resource":"GPU","remaining":"29.99h"}]', "")
        raise AssertionError(args)


class LegacyRefTests(unittest.TestCase):
    def setUp(self):
        ctx = patch.dict(os.environ, {key: val for key, val in os.environ.items()
                                      if key not in ("KAGGLE_OWNER", "KAGGLE_API_TOKEN", "EMAIL", "KGR_ENV_FILE")}, clear=True)
        ctx.start()
        self.addCleanup(ctx.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = JobStore(self.root / "state")
        self.api = API()
        self.env = self.root / ".env"
        write_account(str(self.env), "hoanggvo", "test@example.test", "fake-token")
        self.job_id = "8ea6dc88ae834c73a2fbecb270d2e945"
        self.old_ref = "hoang_vo/kgr-gpu-smoke-test-8ea6dc88ae"
        self.correct_ref = "hoanggvo/kgr-gpu-smoke-test-8ea6dc88ae"
        self.job_dir = self.root / "state" / "jobs" / self.job_id
        (self.job_dir / "kernel").mkdir(parents=True)
        self.store.add({"job_id": self.job_id, "kernel_ref": self.old_ref,
                        "source": str(self.root / "script.py"), "source_revision": None,
                        "dataset_refs": "[]", "status": "status_unavailable", "profile": None,
                        "staging_dir": str(self.job_dir / "kernel"), "output_dir": None,
                        "last_message": "Permission 'kernels.get' was denied. The most likely cause is a wrong kernel slug"})
        (self.job_dir / "manifest.json").write_text(json.dumps({"job_id": self.job_id, "kernel_ref": self.old_ref}))

    def invoke(self, *args):
        out, err = StringIO(), StringIO()
        with patch.object(KaggleCLI, "call", self.api.call), \
             redirect_stdout(out), redirect_stderr(err):
            code = main(["--env-file", str(self.env), "--state-dir", str(self.root / "state"), *args])
        return code, out.getvalue(), err.getvalue()

    def test_autocorrects_alias_only_after_successful_status_lookup(self):
        self.api.statuses[self.correct_ref] = "COMPLETE"
        res = tracked_active_report(self.api, self.store, owner="hoanggvo")
        self.assertEqual(res["running_count"], 0)
        self.assertEqual(res["unverified_count"], 0)
        self.assertEqual(self.store.get(self.job_id)["kernel_ref"], self.correct_ref)
        self.assertEqual(self.store.get(self.job_id)["status"], "complete")
        manifest = json.loads((self.job_dir / "manifest.json").read_text())
        self.assertEqual(manifest["kernel_ref"], self.correct_ref)
        self.assertIn(("kernels", "status", self.correct_ref), self.api.calls)
        self.assertFalse(any(c[:2] == ("kernels", "list") for c in self.api.calls))

    def test_unverified_old_record_reports_action_and_blocks_without_crash(self):
        code, out, err = self.invoke("active")
        self.assertEqual(code, 0)
        self.assertIn("Needs attention: 1", out)
        self.assertIn("kgr archive", out)
        self.assertNotIn("benchmark_task_slug", out)
        code, out, err = self.invoke("check", "--gpu", "t4x2", "--required-gpu-hours", "2")
        self.assertEqual(code, 3)
        self.assertIn("BLOCKED", out)
        self.assertIn("need attention", out)
        self.assertIn("29.99", out)
        self.assertFalse(any(c[:2] == ("kernels", "list") for c in self.api.calls))
        self.assertEqual(self.store.get(self.job_id)["kernel_ref"], self.old_ref)
        self.assertEqual(self.store.get(self.job_id)["status"], "status_unavailable")

    def test_manual_archive_requires_confirmation_and_keeps_history(self):
        code, _, err = self.invoke("archive", self.job_id[:10])
        self.assertEqual(code, 2)
        self.assertIn("--confirm-inactive", err)
        code, out, _ = self.invoke("archive", self.job_id[:10], "--confirm-inactive")
        self.assertEqual(code, 0)
        self.assertIn("archived", out)
        self.assertEqual(self.store.get(self.job_id)["status"], "archived")
        code, out, _ = self.invoke("check", "--gpu", "t4x2", "--required-gpu-hours", "2")
        self.assertEqual(code, 0)
        self.assertIn("READY", out)

    def test_manual_repair_preserves_slug_and_requires_exact_canonical_ref(self):
        code, _, err = self.invoke("repair", self.job_id[:10], "--kernel-ref", "hoanggvo/somethingelse")
        self.assertEqual(code, 2)
        self.assertIn("differs", err)
        code, out, _ = self.invoke("repair", self.job_id[:10], "--owner", "hoanggvo")
        self.assertEqual(code, 0)
        self.assertIn("ref_corrected", out)
        self.assertEqual(self.store.get(self.job_id)["kernel_ref"], self.correct_ref)
        self.assertEqual(self.store.get(self.job_id)["status"], "status_unavailable")

    def test_verified_running_recovery_still_counts_slot(self):
        self.api.statuses[self.correct_ref] = "RUNNING"
        res = check_preflight(self.api, gpu="cpu", store=self.store, owner="hoanggvo")
        self.assertEqual(res["active"]["running_count"], 1)
        self.assertEqual(self.store.get(self.job_id)["kernel_ref"], self.correct_ref)

    def test_definite_missing_session_removed_from_preflight_candidates(self):
        def missing(*args, **kwargs):
            if args[:2] == ("kernels", "status"):
                return Result(list(args), 1, "", "The resource was not found")
            return self.api.call(*args, **kwargs)
        with patch.object(self.api, "call", missing):
            res = tracked_active_report(self.api, self.store, owner="hoanggvo")
        self.assertEqual(res["missing_session_count"], 1)
        self.assertEqual(self.store.get(self.job_id)["status"], "missing_session")
        count = len(self.api.calls)
        tracked_active_report(self.api, self.store, owner="hoanggvo")
        self.assertEqual(len(self.api.calls), count)


if __name__ == "__main__":
    unittest.main()
