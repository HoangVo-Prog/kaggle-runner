"""Regression coverage for official Kaggle worker status variants."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kaggle_runner.api import Result
from kaggle_runner.config import RunnerError
from kaggle_runner.jobs import JobStore
from kaggle_runner.preflight import active_report, check_preflight
from kaggle_runner.runner import parse_kernel_status, refresh_status


class FakeStatusAPI:
    def __init__(self, statuses: dict[str, str], *, list_rows: list[dict] | None = None):
        self.statuses = statuses
        self.rows = list_rows if list_rows is not None else [{"ref": ref} for ref in statuses]

    def call(self, *args, **kwargs):
        if args[:2] == ("kernels", "list"):
            return Result(list(args), 0, json.dumps(self.rows), "")
        if args[:2] == ("kernels", "status"):
            return Result(list(args), 0, self.statuses[args[2]], "")
        if args[:1] == ("quota",):
            return Result(list(args), 0, '{"GPU":{"remaining_hours":10}}', "")
        raise AssertionError(args)


class StatusParserTests(unittest.TestCase):
    def test_official_enum_states(self):
        states = {
            "RUNNING": "running", "QUEUED": "queued", "COMPLETE": "complete",
            "ERROR": "error", "NEW_SCRIPT": "new_script",
            "CANCEL_REQUESTED": "cancel_requested",
            "CANCEL_ACKNOWLEDGED": "cancel_acknowledged",
        }
        for official, expected in states.items():
            with self.subTest(state=official):
                self.assertEqual(parse_kernel_status(
                    f'hoanggvo/ta-en-ablation has status "KernelWorkerStatus.{official}"'
                ), expected)

    def test_backward_compatible_legacy_states(self):
        for value, expected in (
            ("KernelWorkerStatus.PENDING", "pending"),
            ("KernelWorkerStatus.STARTING", "starting"),
            ("KernelWorkerStatus.CANCELLED", "cancelled"),
            ("KernelWorkerStatus.CANCELED", "cancelled"),
            ('Status: running', 'running'),
            ('{"status":"KernelWorkerStatus.COMPLETE"}', 'complete'),
            ('{"workerStatus":{"name":"NEW_SCRIPT"}}', 'new_script'),
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_kernel_status(value), expected)

    def test_does_not_match_slug_or_arbitrary_text(self):
        self.assertEqual(parse_kernel_status("owner/error has status 'KernelWorkerStatus.RUNNING'"), "running")
        self.assertEqual(parse_kernel_status("owner/running-ablation returned unavailable"), "unknown")
        self.assertEqual(parse_kernel_status("status: SOMETHING_UNEXPECTED"), "unknown")
        self.assertEqual(parse_kernel_status(""), "unknown")
        self.assertEqual(parse_kernel_status("No status could be retrieved"), "unknown")

    def test_cancel_acknowledged_is_separate_from_active(self):
        api = FakeStatusAPI({
            "u/a": 'u/a has status "KernelWorkerStatus.CANCEL_REQUESTED"',
            "u/b": 'u/b has status "KernelWorkerStatus.CANCEL_ACKNOWLEDGED"',
        })
        report = active_report(api)
        self.assertEqual(report["running_count"], 1)
        self.assertTrue(report["has_slot"])
        self.assertEqual({x["status"] for x in report["active"]}, {"cancel_requested"})
        self.assertEqual(report["cancelling_count"], 1)
        self.assertEqual(report["cancelling"][0]["status"], "cancel_acknowledged")

    def test_new_script_is_not_running(self):
        api = FakeStatusAPI({
            "hoanggvo/ta-en-ablation": 'hoanggvo/ta-en-ablation has status "KernelWorkerStatus.NEW_SCRIPT"',
            "hoanggvo/in-progress": 'hoanggvo/in-progress has status "KernelWorkerStatus.RUNNING"',
        })
        report = check_preflight(api, gpu="t4x2", required_gpu_hours=2)
        self.assertTrue(report["ok"])
        self.assertEqual(report["active"]["not_started_count"], 1)
        self.assertEqual(report["active"]["running_count"], 1)
        self.assertEqual(report["active"]["not_started"][0]["kernel_ref"], "hoanggvo/ta-en-ablation")

    def test_unknown_status_fails_with_actual_response(self):
        message = 'hoanggvo/ta-en-ablation has status "KernelWorkerStatus.FUTURE_STATE"'
        api = FakeStatusAPI({"hoanggvo/ta-en-ablation": message})
        with self.assertRaises(RunnerError) as ctx:
            active_report(api)
        self.assertIn(message, str(ctx.exception))
        self.assertIn("python -m kaggle kernels status hoanggvo/ta-en-ablation", str(ctx.exception))

    def test_unknown_empty_status_fails_closed(self):
        api = FakeStatusAPI({"u/old": ""})
        with self.assertRaisesRegex(RunnerError, "<empty response>"):
            active_report(api)

    def test_same_parser_for_local_job_refresh(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp))
            store.add({"job_id": "abc", "kernel_ref": "u/ta-en-ablation", "source": "demo.py",
                       "source_revision": None, "dataset_refs": "[]", "status": "submitted",
                       "profile": None, "staging_dir": temp, "output_dir": None,
                       "last_message": None})
            api = FakeStatusAPI({"u/ta-en-ablation": 'u/ta-en-ablation has status "KernelWorkerStatus.NEW_SCRIPT"'})
            self.assertEqual(refresh_status(api, store, "abc")["status"], "new_script")
            self.assertEqual(store.get("abc")["status"], "new_script")


if __name__ == "__main__":
    unittest.main()
