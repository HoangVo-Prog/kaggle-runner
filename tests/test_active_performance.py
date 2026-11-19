"""Regression tests for bounded, visible Kaggle active-run checks."""
from __future__ import annotations

import io
import json
import os
import subprocess
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from unittest.mock import patch

from kaggle_runner.api import KaggleCLI, Result
from kaggle_runner.accounts import write_account
from kaggle_runner.cli import main
from kaggle_runner.config import RunnerError, Settings
from kaggle_runner.preflight import active_report


class FakeManyKernels:
    def __init__(self, count: int, *, terminal_hints: bool = False, delay: float = 0.0):
        self.count = count
        self.terminal_hints = terminal_hints
        self.delay = delay
        self.in_flight = 0
        self.high_water = 0
        self.calls: list[tuple[str, ...]] = []
        self.lock = Lock()

    def call(self, *argv: str, **kwargs) -> Result:
        with self.lock:
            self.calls.append(tuple(argv))
        if argv[:2] == ("kernels", "list"):
            page = int(argv[argv.index("--page") + 1])
            page_size = int(argv[argv.index("--page-size") + 1])
            start = (page - 1) * page_size
            count = max(0, min(page_size, self.count - start))
            return Result(list(argv), 0, json.dumps([
                {"ref": f"u/job{i}", **({"lastRunStatus": "COMPLETE"} if self.terminal_hints else {})}
                for i in range(start, start + count)
            ]), "")
        if argv[:2] == ("kernels", "status"):
            with self.lock:
                self.in_flight += 1
                self.high_water = max(self.high_water, self.in_flight)
            try:
                time.sleep(self.delay)
            finally:
                with self.lock:
                    self.in_flight -= 1
            return Result(list(argv), 0, "KernelWorkerStatus.COMPLETE", "")
        raise AssertionError(f"unexpected Kaggle call: {argv}")


class ActiveSpeedTests(unittest.TestCase):
    def test_parallel_status_lookups_are_bounded(self):
        api = FakeManyKernels(44, delay=0.025)
        messages: list[str] = []
        start = time.monotonic()
        report = active_report(api, workers=4, progress=messages.append)
        elapsed = time.monotonic() - start
        self.assertEqual(report["scanned"], 44)
        self.assertEqual(report["live_checked_count"], 44)
        self.assertTrue(report["has_slot"])
        self.assertGreater(api.high_water, 1)
        self.assertLessEqual(api.high_water, 4)
        self.assertLess(elapsed, 1.8, f"parallel probing is unexpectedly slow ({elapsed:.2f}s)")
        self.assertIn("Inspecting Kaggle", messages[0])
        self.assertTrue(any("Verified" in message for message in messages))

    def test_historical_terminal_statuses_do_not_need_one_request_each(self):
        api = FakeManyKernels(40, terminal_hints=True)
        report = active_report(api, workers=4)
        self.assertEqual(report["live_checked_count"], 20)
        self.assertEqual(report["listed_terminal_count"], 20)
        self.assertEqual(report["running_count"], 0)
        strict = active_report(api, workers=4, strict=True)
        self.assertEqual(strict["live_checked_count"], 40)
        self.assertEqual(strict["listed_terminal_count"], 0)

    def test_short_circuit_when_two_active_jobs_are_confirmed(self):
        class Busy(FakeManyKernels):
            def call(self, *argv, **kwargs):
                if argv[:2] == ("kernels", "status"):
                    return Result(list(argv), 0, "KernelWorkerStatus.RUNNING", "")
                return super().call(*argv, **kwargs)

        api = Busy(100)
        report = active_report(api, workers=4)
        self.assertEqual(report["running_count"], 8)
        self.assertFalse(report["has_slot"])
        self.assertEqual(report["live_checked_count"], 8)
        self.assertEqual(sum(c[:2] == ("kernels", "list") for c in api.calls), 1)

    def test_deadline_failure_never_assumes_a_free_slot(self):
        api = FakeManyKernels(8, delay=0.15)
        with self.assertRaisesRegex(RunnerError, "cannot verify free slots"):
            active_report(api, workers=4, max_seconds=0.02)

    def test_subprocess_timeout_is_a_clear_error(self):
        client = KaggleCLI(Settings(Path("/tmp"), None, None))
        with patch("kaggle_runner.api.subprocess.run", side_effect=subprocess.TimeoutExpired(["python", "-m", "kaggle"], 2)):
            with self.assertRaisesRegex(RunnerError, "timed out after 2s"):
                client.call("kernels", "status", "u/job", timeout_seconds=2)

    def test_progress_goes_to_stderr_json_stays_machine_readable(self):
        with TemporaryDirectory() as directory:
            fake = FakeManyKernels(0)
            env_file = Path(directory) / ".env"
            write_account(str(env_file), "example", "example@example.org", "mock-token-for-tests")
            argv = ["--env-file", str(env_file), "active", "--remote"]
            output, diagnostic = io.StringIO(), io.StringIO()
            with patch.object(KaggleCLI, "call", fake.call), \
                 redirect_stdout(output), redirect_stderr(diagnostic):
                exit_code = main(argv)
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.getvalue())["running_count"], 0)
            self.assertIn("Listing notebook page", diagnostic.getvalue())
            output, diagnostic = io.StringIO(), io.StringIO()
            with patch.object(KaggleCLI, "call", fake.call), \
                 redirect_stdout(output), redirect_stderr(diagnostic):
                exit_code = main([*argv, "--quiet"])
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.getvalue())["running_count"], 0)
            self.assertEqual(diagnostic.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
