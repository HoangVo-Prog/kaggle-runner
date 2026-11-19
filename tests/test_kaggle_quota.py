"""Kaggle quota JSON (official CLI resource/used/remaining/total schema)."""
from __future__ import annotations

import json
import unittest

from kaggle_runner.api import Result
from kaggle_runner.preflight import parse_gpu_quota, quota_report, check_preflight


SAMPLE = [
    {"resource": "TPU", "used": "0.00h", "remaining": "20.00h", "total": "20.00h", "refreshAt": "2026-11-26T00:00:00+00:00"},
    {"resource": "GPU", "used": "1.25h", "remaining": "28.75h", "total": "30.00h", "refreshAt": "2026-11-26T00:00:00+00:00"},
]


class FakeQuota:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.running = False

    def call(self, *args, **kwargs):
        self.calls.append(tuple(args))
        if args[:1] == ("quota",):
            return Result(list(args), 0, self.payload, "")
        if args[:2] == ("kernels", "list"):
            return Result(list(args), 0, json.dumps([{"ref": "u/running"}] if self.running else []), "")
        if args[:2] == ("kernels", "status"):
            return Result(list(args), 0, "KernelWorkerStatus.RUNNING", "")
        raise AssertionError(f"Unexpected call: {args}")


class QuotaRegressionTests(unittest.TestCase):
    def test_official_quota_json_resource_field(self):
        actual = parse_gpu_quota(json.dumps(SAMPLE))
        self.assertEqual(actual["remaining_hours"], 28.75)
        self.assertEqual(actual["used_hours"], 1.25)
        self.assertEqual(actual["total_hours"], 30.0)
        self.assertEqual(actual["refresh_at"], "2026-11-26T00:00:00+00:00")

    def test_direct_gpu_resource_object(self):
        self.assertEqual(parse_gpu_quota(json.dumps(SAMPLE[1]))["remaining_hours"], 28.75)

    def test_quota_does_not_scan_notebooks(self):
        client = FakeQuota(json.dumps(SAMPLE))
        self.assertEqual(quota_report(client)["remaining_hours"], 28.75)
        self.assertEqual(client.calls, [("quota", "--format", "json")])

    def test_concurrency_failure_kept_if_quota_unknown(self):
        client = FakeQuota(json.dumps({"unexpected": "schema"}))
        client.running = True
        report = check_preflight(client, gpu="t4x2", required_gpu_hours=2, max_concurrent=1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["active"]["running_count"], 1)
        self.assertTrue(any("Concurrent batch jobs 1/1" in reason for reason in report["reasons"]))
        self.assertTrue(any("GPU quota unavailable" in reason for reason in report["reasons"]))
        self.assertIsNone(report["gpu_quota"])

    def test_unknown_quota_never_allows_submit_even_with_free_slot(self):
        client = FakeQuota(json.dumps({"unexpected": "schema"}))
        report = check_preflight(client, gpu="t4x2", required_gpu_hours=2)
        self.assertFalse(report["ok"])
        self.assertTrue(report["gpu_quota_error"])

    def test_official_quota_with_busy_account_blocks_and_returns_both_sections(self):
        client = FakeQuota(json.dumps(SAMPLE))
        client.running = True
        report = check_preflight(client, gpu="t4x2", required_gpu_hours=2, max_concurrent=1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["gpu_quota"]["remaining_hours"], 28.75)
        self.assertEqual(report["gpu_quota_error"], None)
        self.assertIn("Concurrent batch jobs 1/1", report["reasons"])


if __name__ == "__main__":
    unittest.main()
