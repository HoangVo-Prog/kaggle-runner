"""Offline end-to-end regression tests for credentials, preflight and notebooks."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from kaggle_runner.accounts import load_account, read_env, write_account
from kaggle_runner.api import KaggleCLI, Result
from kaggle_runner.cli import main
from kaggle_runner.config import RunnerError, Settings
from kaggle_runner.jobs import JobStore
from kaggle_runner.packaging import validate_model_ref, validate_ref
from kaggle_runner.preflight import active_report, check_preflight, parse_gpu_quota


class MockAPI:
    def __init__(self, settings):
        self.settings = settings
        self.calls = []
        self.running = []
        self.remaining = 9.5

    def call(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("kernels", "list"):
            rows = [{"ref": ref} for ref in self.running]
            return Result(list(args), 0, json.dumps(rows), "")
        if args[:2] == ("kernels", "status"):
            return Result(list(args), 0, "KernelWorkerStatus.RUNNING", "")
        if args[:1] == ("quota",):
            return Result(list(args), 0, json.dumps({"GPU": {"remaining_hours": self.remaining}}), "")
        if args[:2] == ("kernels", "push"):
            staged = Path(args[args.index("-p") + 1])
            slug = json.loads((staged / "kernel-metadata.json").read_text())["id"].split("/")[1]
            return Result(list(args), 0, f"Kernel version 1 successfully pushed at https://www.kaggle.com/code/authenticated/{slug}", "")
        if args[:2] == ("kernels", "output"):
            dest = Path(args[args.index("-p") + 1])
            (dest / "result.json").write_text('{"pass":true}')
            return Result(list(args), 0, "downloaded", "")
        if args[:2] == ("kernels", "files"):
            return Result(list(args), 0, '[{"name":"result.json"}]', "")
        return Result(list(args), 0, "ok", "")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root / "state", None, None)
        self.api = MockAPI(self.settings)
        self.store = JobStore(self.settings.state)
        self.env = self.root / ".env"
        write_account(str(self.env), "actualuser", "person@example.org", "not-a-real-token-for-testing")

    def test_reject_symlinked_env(self):
        link = self.root / "linked.env"
        try:
            link.symlink_to(self.env)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks not available")
        with self.assertRaises(RunnerError):
            load_account(str(link))
        with self.assertRaises(RunnerError):
            write_account(str(link), "actualuser", "person@example.org", "another-token")

    def test_auth_init_reads_token_from_stdin_without_echoing(self):
        new = self.root / "new.env"
        out = StringIO()
        with patch("sys.stdin", StringIO("fake-token-from-stdin\n")), redirect_stdout(out):
            self.assertEqual(main(["--env-file", str(new), "auth", "init", "--owner", "abc",
                                   "--email", "person@example.org", "--token-stdin"]), 0)
        self.assertNotIn("fake-token-from-stdin", out.getvalue())
        self.assertEqual(load_account(str(new)).token, "fake-token-from-stdin")

    def test_private_env_and_redaction(self):
        self.assertEqual(load_account(str(self.env)).owner, "actualuser")
        self.assertEqual(load_account(str(self.env)).email, "person@example.org")
        self.assertEqual(load_account(str(self.env)).token, "not-a-real-token-for-testing")
        if os.name != "nt":
            self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        out = StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--env-file", str(self.env), "auth", "show"]), 0)
        self.assertNotIn("not-a-real-token", out.getvalue())
        self.assertIn('"token_configured": true', out.getvalue())
        self.assertRaises(RunnerError, write_account, str(self.env), "bad name", "person@example.org", "x")

    def test_token_is_only_passed_to_subprocess_not_command(self):
        from unittest import mock
        with mock.patch("kaggle_runner.api.subprocess.run", return_value=mock.Mock(returncode=1, stdout="secret-test", stderr="no")) as cmd:
            client = KaggleCLI(self.settings, token="secret-test")
            with self.assertRaises(RunnerError) as e:
                client.call("quota")
            self.assertNotIn("secret-test", str(e.exception))
            self.assertEqual(cmd.call_args.kwargs["env"]["KAGGLE_API_TOKEN"], "secret-test")
            self.assertNotIn("secret-test", cmd.call_args.args[0])

    def test_quota_parsing(self):
        self.assertEqual(parse_gpu_quota('{"gpu_quota":{"time_used":"1800s","total_time_allowed":"108000s"}}')["remaining_hours"], 29.5)
        self.assertEqual(parse_gpu_quota('[{"accelerator":"GPU", "remaining":"8h 30m"}]')["remaining_hours"], 8.5)
        with self.assertRaises(RunnerError):
            parse_gpu_quota('{"other":"unknown"}')

    def test_two_running_jobs_block_and_low_quota_blocks(self):
        self.api.running = ["person/run1", "person/run2"]
        result = check_preflight(self.api, gpu="t4x2", required_gpu_hours=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["active"]["running_count"], 2)
        self.api.running = []
        self.api.remaining = 0.5
        result = check_preflight(self.api, gpu="t4x2", required_gpu_hours=1)
        self.assertFalse(result["ok"])
        result = check_preflight(self.api, gpu="cpu")
        self.assertTrue(result["ok"])
        self.assertIsNone(result["gpu_quota"])

    def test_missing_historical_session_does_not_block_gpu_check(self):
        """Regression: Kaggle may list a notebook whose live session is gone."""
        baseline = self.api.call

        def stale(*args, **kwargs):
            if args[:2] == ("kernels", "list"):
                rows = [
                    {"ref": "hoanggvo/notebookd1d58717f6"},
                    {"ref": "hoanggvo/current-job"},
                ]
                return Result(list(args), 0, json.dumps(rows), "")
            if args[:3] == ("kernels", "status", "hoanggvo/notebookd1d58717f6"):
                return Result(list(args), 1, "", "s.KernelsApiService/GetKernelSessionStatus\nThe resource was not found.")
            return baseline(*args, **kwargs)

        with patch.object(self.api, "call", stale):
            result = check_preflight(self.api, gpu="t4x2", required_gpu_hours=2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["active"]["running_count"], 1)
        self.assertEqual(result["active"]["missing_session_count"], 1)
        self.assertEqual(result["active"]["missing_sessions"][0]["kernel_ref"],
                         "hoanggvo/notebookd1d58717f6")

    def test_missing_session_with_active_list_status_fails_closed(self):
        def inconsistent(*args, **kwargs):
            if args[:2] == ("kernels", "list"):
                return Result(list(args), 0, json.dumps([{"ref": "user/run", "status": "RUNNING"}]), "")
            return Result(list(args), 1, "", "The resource was not found")
        with self.assertRaisesRegex(RunnerError, "refusing to guess"):
            active_report(type("API", (), {"call": staticmethod(inconsistent)})())

    def test_status_permission_and_transient_failures_are_not_ignored(self):
        for message in ("Permission 'kernels.get' was denied",
                        "HTTP 403: The resource was not found",
                        "HTTP 429 rate limited", "HTTP 500 server error"):
            def broken(*args, **kwargs):
                if args[:2] == ("kernels", "list"):
                    return Result(list(args), 0, json.dumps([{"ref": "user/old"}]), "")
                return Result(list(args), 1, "", message)
            with self.subTest(message=message), self.assertRaisesRegex(RunnerError, "Could not verify"):
                active_report(type("API", (), {"call": staticmethod(broken)})())

    def test_stale_session_does_not_hide_two_other_active_jobs(self):
        def mixed(*args, **kwargs):
            if args[:2] == ("kernels", "list"):
                return Result(list(args), 0, json.dumps([{"ref": "u/deleted"}, {"ref": "u/a"}, {"ref": "u/b"}]), "")
            if args[:3] == ("kernels", "status", "u/deleted"):
                return Result(list(args), 1, "", "The resource was not found")
            return Result(list(args), 0, "KernelWorkerStatus.RUNNING", "")
        report = active_report(type("API", (), {"call": staticmethod(mixed)})())
        self.assertFalse(report["has_slot"])
        self.assertEqual(report["running_count"], 2)
        self.assertEqual(report["missing_session_count"], 1)

    def test_incomplete_listing_fail_closed(self):
        original = self.api.call
        def broken(*args, **kwargs):
            if args[:2] == ("kernels", "list"):
                return Result(list(args), 1, "", "permission denied")
            return original(*args, **kwargs)
        with self.assertRaises(RunnerError):
            active_report(type("X", (), {"call": broken})())

    def test_public_url_forms(self):
        self.assertEqual(validate_ref("https://www.kaggle.com/datasets/uciml/iris"), "uciml/iris")
        self.assertEqual(validate_model_ref("https://www.kaggle.com/models/google/mobilenet-v2/tensorFlow2/130-224-classification/1"),
                         "google/mobilenet-v2/tensorFlow2/130-224-classification/1")
        with self.assertRaises(RunnerError):
            validate_ref("https://evil.example/datasets/u/x")

    def test_native_notebook_dry_run(self):
        notebook = self.root / "experiment.ipynb"
        notebook.write_text(json.dumps({"nbformat": 4, "nbformat_minor": 5, "metadata": {},
                                        "cells": [{"cell_type": "code", "id": "original", "metadata": {},
                                                   "execution_count": None, "outputs": [], "source": "print('hello')"}]}))
        out = StringIO()
        argv = ["--env-file", str(self.env), "--state-dir", str(self.settings.state), "run",
                str(notebook), "--gpu", "t4x2", "--dataset", "uciml/iris",
                "--model", "google/mobilenet-v2/tensorFlow2/130-224-classification/1", "--dry-run"]
        with redirect_stdout(out):
            self.assertEqual(main(argv), 0)
        job = json.loads(out.getvalue())
        metadata = json.loads((Path(job["staging_dir"]) / "kernel-metadata.json").read_text())
        self.assertEqual(metadata["kernel_type"], "notebook")
        self.assertEqual(metadata["code_file"], "run.ipynb")
        staged = json.loads((Path(job["staging_dir"]) / "run.ipynb").read_text())
        self.assertIn("KGR_MODEL_PATHS", staged["cells"][0]["source"])
        self.assertEqual(staged["cells"][1]["id"], "original")
        self.assertNotIn("not-a-real-token", json.dumps(staged))
        self.assertEqual(job["kernel_type"], "notebook")

    def test_native_notebook_live_submit_metadata(self):
        nb = self.root / "test.ipynb"
        nb.write_text(json.dumps({"nbformat":4,"nbformat_minor":5,"metadata":{},"cells":[]}))
        args = ["--env-file", str(self.env), "--state-dir", str(self.settings.state),
                "run", str(nb), "--gpu", "cpu"]
        with patch.object(KaggleCLI, "call", self.api.call), redirect_stdout(StringIO()) as output:
            self.assertEqual(main(args), 0)
        result = json.loads(output.getvalue())
        staged = Path(result["staging_dir"])
        meta = json.loads((staged / "kernel-metadata.json").read_text())
        self.assertEqual(meta["kernel_type"], "notebook")
        self.assertTrue((staged / "run.ipynb").exists())
        self.assertTrue(any(c[:2] == ("kernels", "push") for c in self.api.calls))

    def test_real_cli_preflight_before_upload_and_report_download(self):
        src = self.root / "train.py"
        src.write_text("print('submitted')")
        self.api.running = ["test/run-a", "test/run-b"]
        args = ["--env-file", str(self.env), "--state-dir", str(self.settings.state),
                "run", str(src), "--gpu", "t4x2", "--required-gpu-hours", "1.5"]
        with patch.object(KaggleCLI, "call", self.api.call), redirect_stdout(StringIO()):
            self.assertEqual(main(args), 2)
        self.assertFalse(any(c[:2] == ("kernels", "push") for c in self.api.calls))
        self.api.running = []
        with patch.object(KaggleCLI, "call", self.api.call), redirect_stdout(StringIO()) as stdout:
            self.assertEqual(main(args), 0)
            j = json.loads(stdout.getvalue())
            self.assertTrue(j["kernel_ref"].startswith("authenticated/"))
            self.assertEqual(main(["--env-file", str(self.env), "--state-dir", str(self.settings.state),
                                   "report", "--refresh"]), 0)
        job_id = j["job_id"]
        with patch.object(KaggleCLI, "call", self.api.call), redirect_stdout(StringIO()) as stdout:
            self.assertEqual(main(["--env-file", str(self.env), "--state-dir", str(self.settings.state),
                                   "fetch", job_id, "--file-pattern", ".*json$", "--output", str(self.root / "download")]), 0)
        self.assertTrue((self.root / "download" / "result.json").exists())
        self.assertIn("--file-pattern", str(self.api.calls))


if __name__ == "__main__":
    unittest.main()
