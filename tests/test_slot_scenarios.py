"""CLI contracts for two concurrent Kaggle jobs, regardless of GPU/CPU.

Uses a simulated Kaggle service (NO real token, GPU quota, or remote jobs).
Run with: python -m unittest discover -s tests -p 'test_slot_scenarios.py' -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from kaggle_runner.accounts import write_account
from kaggle_runner.api import KaggleCLI, Result
from kaggle_runner.cli import main
from kaggle_runner.jobs import JobStore


class KaggleSimulator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.statuses: dict[str, str] = {}
        self.pushes = 0
        self.shapes: list[str] = []
        self.initial_status = "RUNNING"

    def call(self, *args: str, **kwargs) -> Result:
        self.calls.append(args)
        if args[:1] == ("quota",):
            rows = [{"resource": "GPU", "used": "0.01h", "remaining": "29.99h", "total": "30h"}]
            return Result(list(args), 0, json.dumps(rows), "")
        if args[:2] == ("kernels", "status"):
            ref = args[2]
            return Result(list(args), 0,
                          f'{ref} has status "KernelWorkerStatus.{self.statuses[ref]}"', "")
        if args[:2] == ("kernels", "push"):
            meta = json.loads((Path(args[args.index("-p") + 1]) / "kernel-metadata.json").read_text())
            # The configured owner is only metadata and can be an old alias.
            # Trust the canonical owner in the Kaggle submit response instead.
            ref = "canonicalowner/" + meta["id"].split("/", 1)[1]
            self.shapes.append(meta.get("machine_shape", "cpu"))
            self.pushes += 1
            self.statuses[ref] = self.initial_status
            return Result(list(args), 0, f"Kernel successfully pushed: https://www.kaggle.com/code/{ref}", "")
        if args[:2] == ("kernels", "list"):
            raise AssertionError("Fast preflight must not list all historical notebooks")
        raise AssertionError(f"Unexpected Kaggle call: {args}")


class MixedCpuGpuSlotTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = self.root / ".env"
        write_account(str(self.env), "mykaggle", "reports@example.test", "fake-token")
        self.state = self.root / "state"
        self.py = self.root / "experiment.py"
        self.py.write_text("print('hello')\n")
        self.kaggle = KaggleSimulator()
        self.base = ["--env-file", str(self.env), "--state-dir", str(self.state)]

    def invoke(self, *args: str):
        out, err = StringIO(), StringIO()
        with patch.object(KaggleCLI, "call", self.kaggle.call), \
                redirect_stdout(out), redirect_stderr(err):
            exit_code = main([*self.base, *args])
        return exit_code, out.getvalue(), err.getvalue()

    def test_one_gpu_one_cpu_third_job_is_blocked_and_completion_frees_slot(self):
        code, stdout, _ = self.invoke("run", str(self.py), "--gpu", "t4x2", "--required-gpu-hours", "2")
        self.assertEqual(code, 0)
        self.assertIn("SUBMITTED", stdout)
        self.assertLess(len(stdout), 650)
        code, stdout, _ = self.invoke("run", str(self.py), "--gpu", "cpu")
        self.assertEqual(code, 0)
        self.assertIn("SUBMITTED", stdout)
        self.assertEqual(self.kaggle.pushes, 2)
        self.assertTrue(all(j["kernel_ref"].startswith("canonicalowner/")
                            for j in JobStore(self.state).list()))
        self.assertEqual(len(self.kaggle.shapes), 2)
        self.assertEqual(self.kaggle.shapes[1], "cpu")
        self.assertNotEqual(self.kaggle.shapes[0], "cpu")

        code, stdout, _ = self.invoke("check", "--gpu", "t4x2", "--required-gpu-hours", "2")
        self.assertEqual(code, 3)
        self.assertIn("BLOCKED", stdout)
        self.assertIn("2/2", stdout)
        self.assertIn("29.99h", stdout)
        self.assertNotIn("missing_sessions", stdout)

        code, stdout, stderr = self.invoke("run", str(self.py), "--gpu", "cpu")
        self.assertEqual(code, 2)
        self.assertEqual(self.kaggle.pushes, 2, "The third job must NEVER be submitted")
        self.assertIn("Concurrent batch jobs 2/2", stderr)
        self.assertEqual(len(JobStore(self.state).list()), 2)

        code, stdout, _ = self.invoke("check", "--gpu", "cpu", "--json")
        self.assertEqual(code, 3)
        doc = json.loads(stdout)
        self.assertFalse(doc["ok"])
        self.assertEqual(doc["active"]["running_count"], 2)
        self.assertIsNone(doc["gpu_quota"], "CPU checks must not need GPU quota")

        first = JobStore(self.state).list()[-1]
        self.kaggle.statuses[first["kernel_ref"]] = "COMPLETE"
        code, stdout, _ = self.invoke("active")
        self.assertEqual(code, 0)
        self.assertIn("1/2", stdout)
        code, stdout, _ = self.invoke("run", str(self.py), "--gpu", "cpu")
        self.assertEqual(code, 0)
        self.assertEqual(self.kaggle.pushes, 3)

    def test_notebook_gpu_and_python_cpu_share_slots(self):
        notebook = self.root / "experiment.ipynb"
        notebook.write_text(json.dumps({"nbformat": 4, "nbformat_minor": 5,
                                        "metadata": {}, "cells": []}))
        self.assertEqual(self.invoke("run", str(notebook), "--gpu", "t4x2")[0], 0)
        self.assertEqual(self.invoke("run", str(self.py), "--gpu", "cpu")[0], 0)
        code, _, stderr = self.invoke("run", str(notebook), "--gpu", "cpu")
        self.assertEqual(code, 2)
        self.assertIn("2/2", stderr)
        self.assertEqual(self.kaggle.pushes, 2)

    def test_newly_pushed_new_script_is_temporarily_reserved(self):
        # Kaggle may lag behind a successful push: never permit 3 rapid submits
        # just because the new worker is not yet visible.
        self.kaggle.initial_status = "NEW_SCRIPT"
        self.assertEqual(self.invoke("run", str(self.py), "--gpu", "t4x2")[0], 0)
        self.assertEqual(self.invoke("run", str(self.py), "--gpu", "cpu")[0], 0)
        code, out, err = self.invoke("run", str(self.py), "--gpu", "cpu")
        self.assertEqual(code, 2)
        self.assertIn("2/2", err)
        self.assertEqual(self.kaggle.pushes, 2)

    def test_status_output_is_small_by_default_json_is_opt_in(self):
        self.invoke("run", str(self.py), "--gpu", "cpu")
        code, human, _ = self.invoke("jobs")
        self.assertEqual(code, 0)
        self.assertIn("Tracked jobs: 1", human)
        self.assertIn("JOB ID", human)
        self.assertNotIn('"kernel_ref"', human)
        code, machine, _ = self.invoke("jobs", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(machine)), 1)
        code, machine, _ = self.invoke("--json", "active")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(machine)["running_count"], 1)
        code, machine, _ = self.invoke("quota", "--json")
        self.assertEqual(code, 0)
        self.assertAlmostEqual(json.loads(machine)["remaining_hours"], 29.99)


if __name__ == "__main__":
    unittest.main()
