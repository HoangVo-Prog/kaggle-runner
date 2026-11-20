"""Offline integration tests for public Kaggle dataset/model resource mounting."""
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

from kaggle_runner.api import Result
from kaggle_runner.cli import main
from kaggle_runner.config import RunnerError, Settings
from kaggle_runner.jobs import JobStore
from kaggle_runner.packaging import make_bootstrap, pack_source, validate_model_ref
from kaggle_runner.runner import create_job, refresh_status

ROOT = Path(__file__).resolve().parents[1]
DATA = "uciml/iris"
MODEL = "google/mobilenet-v2/tensorFlow2/130-224-classification/1"


class FakeKaggle:
    def __init__(self, settings):
        self.settings = settings
        self.calls = []

    def call(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("kernels", "list"):
            return Result(list(args), 0, "[]", "")
        if args[:1] == ("quota",):
            return Result(list(args), 0, '{"gpu_quota":{"time_used":"3600s","total_time_allowed":"108000s"}}', "")
        if args[:2] == ("kernels", "push"):
            kernel_path = Path(args[args.index("-p") + 1])
            slug = json.loads((kernel_path / "kernel-metadata.json").read_text())["id"].split("/")[1]
            return Result(list(args), 0, f"Kernel version 1 successfully pushed. Please check progress at https://www.kaggle.com/code/canonicaluser/{slug}", "")
        if args[:2] == ("kernels", "status"):
            return Result(list(args), 0, "KernelWorkerStatus.COMPLETE", "")
        if args[:2] == ("kernels", "output"):
            path = Path(args[args.index("-p") + 1])
            (path / "test_result.json").write_text('{"ok":true}', encoding="utf-8")
            return Result(list(args), 0, "downloaded", "")
        if args[:3] == ("models", "variations", "versions"):
            return Result(list(args), 0, "weights.bin", "")
        return Result(list(args), 0, "results", "")


class PublicResourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root / "state", None, None)
        self.store = JobStore(self.settings.state)
        self.api = FakeKaggle(self.settings)

    def test_validate_pinned_model_refs(self):
        self.assertEqual(validate_model_ref(MODEL), MODEL)
        for bad in ("google/mobilenet-v2", "google/mobilenet-v2/tensorFlow2/130-224-classification", "google/mobilenet-v2/tensorFlow2/130-224-classification/latest", "https://www.kaggle.com/models/google/mobilenet-v2", "a/b/c/../1", "a/b/c/d/0"):
            with self.subTest(bad=bad), self.assertRaises(RunnerError):
                validate_model_ref(bad)

    def test_dry_run_metadata_manifest_pins_model_and_dataset(self):
        out = create_job(self.settings, self.api, self.store,
                         source=str(ROOT / "examples" / "public-resources"),
                         entry="inspect_resources.py", owner="hoanggvo", gpu="cpu",
                         dataset_refs=[DATA, DATA], model_refs=[MODEL, MODEL], arguments=[], dry_run=True)
        kernel = Path(out["staging_dir"])
        metadata = json.loads((kernel / "kernel-metadata.json").read_text())
        self.assertEqual(metadata["dataset_sources"], [DATA])
        self.assertEqual(metadata["model_sources"], [MODEL])
        self.assertEqual(out["model_refs"], [MODEL])
        self.assertEqual(out["dataset_refs"], [DATA])
        self.assertEqual(self.api.calls, [])
        self.assertIn('KGR_MODEL_PATHS', (kernel / "run.py").read_text())

    def test_bootstrap_resolves_case_insensitive_model_framework_and_iris(self):
        fake = self.root / "kaggle"
        working = fake / "working"
        working.mkdir(parents=True)
        iris = fake / "input" / "iris"
        iris.mkdir(parents=True)
        (iris / "Iris.csv").write_text("Id,Species\n1,Iris-setosa\n2,Iris-versicolor\n", encoding="utf-8")
        model_dir = fake / "input" / "mobilenet-v2" / "tensorflow2" / "130-224-classification" / "1"
        model_dir.mkdir(parents=True)
        (model_dir / "saved_model.pb").write_bytes(b"fake")
        data, _ = pack_source(ROOT / "examples" / "public-resources", "inspect_resources.py", 2_000_000)
        script = make_bootstrap(data, entry="inspect_resources.py", arguments=[], datasets=[DATA],
                                models=[MODEL], install_requirements=False)
        script = script.replace("/kaggle/working", str(working)).replace("/kaggle/input", str(fake / "input"))
        path = self.root / "run.py"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run([sys.executable, str(path)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads((working / "public_resources_report.json").read_text())
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["dataset_rows"], 2)
        self.assertTrue(payload["model_path"].endswith("/tensorflow2/130-224-classification/1"))
        self.assertTrue((working / "iris_species_counts.csv").exists())
        self.assertEqual(json.loads((working / "kgr_run_result.json").read_text())["status"], "ok")

    def test_missing_model_mount_causes_failure_not_false_success(self):
        fake = self.root / "kaggle"
        (fake / "working").mkdir(parents=True)
        (fake / "input" / "iris").mkdir(parents=True)
        script = make_bootstrap(*[pack_source(ROOT / "examples" / "public-resources", "inspect_resources.py", 2_000_000)[0]],
                                entry="inspect_resources.py", arguments=[], datasets=[DATA],
                                models=[MODEL], install_requirements=False)
        path = self.root / "run.py"
        path.write_text(script.replace("/kaggle/working", str(fake / "working")).replace("/kaggle/input", str(fake / "input")))
        result = subprocess.run([sys.executable, str(path)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        report = json.loads((fake / "working" / "kgr_run_result.json").read_text())
        self.assertEqual(report["status"], "failed")
        self.assertIn("model not mounted", report["error"])

    def test_canonical_owner_and_one_command_wait_fetch(self):
        from kaggle_runner.api import KaggleCLI
        output = self.root / "download"
        argv = ["--state-dir", str(self.settings.state), "run", "--source", str(ROOT / "examples" / "public-resources"),
                "--entry", "inspect_resources.py", "--owner", "wrong_alias", "--dataset", DATA,
                "--model", MODEL, "--wait", "--fetch", "--poll-seconds", "1", "--output", str(output / "{job_id}")]
        out = StringIO()
        with patch.object(KaggleCLI, "call", self.api.call), redirect_stdout(out):
            self.assertEqual(main(argv), 0)
        record = self.store.list()[0]
        self.assertTrue(record["kernel_ref"].startswith("canonicaluser/"))
        self.assertEqual(record["status"], "complete")
        self.assertEqual(json.loads((output / record["job_id"] / "test_result.json").read_text()), {"ok": True})
        self.assertEqual(json.loads((Path(record["staging_dir"]).parent / "manifest.json").read_text())["kernel_ref"], record["kernel_ref"])
        self.assertIn("Job ", out.getvalue())
        self.assertIn(": complete", out.getvalue())

    def test_public_discovery_commands_use_kaggle_cli(self):
        from kaggle_runner.api import KaggleCLI
        with patch.object(KaggleCLI, "call", self.api.call), redirect_stdout(StringIO()):
            self.assertEqual(main(["model", "files", MODEL]), 0)
            self.assertEqual(main(["model", "search", "mobilenet"]), 0)
            self.assertEqual(main(["dataset", "search", "iris"]), 0)
        self.assertIn(("models", "variations", "versions", "files", MODEL), self.api.calls)
        self.assertIn(("models", "list", "--search", "mobilenet"), self.api.calls)
        self.assertIn(("datasets", "list", "--search", "iris"), self.api.calls)


if __name__ == "__main__":
    unittest.main()
