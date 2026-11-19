from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from kaggle_runner.api import Result
from kaggle_runner.config import RunnerError, Settings
from kaggle_runner.datasets import inspect_local, prepare_dataset, upload_dataset
from kaggle_runner.jobs import JobStore
from kaggle_runner.packaging import make_bootstrap, pack_source, source_files, validate_ref
from kaggle_runner.runner import create_job, fetch_job, parse_kernel_status, refresh_status


class FakeKaggle:
    def __init__(self, settings):
        self.settings = settings
        self.calls = []
        self.fail = False

    def call(self, *args, **kwargs):
        self.calls.append(args)
        if self.fail:
            raise RunnerError("simulated API error")
        if args[:2] == ("kernels", "status"):
            return Result(list(args), 0, "KernelWorkerStatus.COMPLETE", "")
        if args[:2] == ("kernels", "output"):
            dest = Path(args[args.index("-p") + 1])
            (dest / "metrics.json").write_text('{"accuracy": 0.5}', encoding="utf-8")
        return Result(list(args), 0, "ok", "")


class P0Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "my-project"
        (self.project / "pkg").mkdir(parents=True)
        (self.project / "main.py").write_text('from pkg.core import main\nmain()\n', encoding="utf-8")
        (self.project / "pkg" / "__init__.py").touch()
        (self.project / "pkg" / "core.py").write_text('def main():\n    print("ok")\n', encoding="utf-8")
        (self.project / ".env").write_text("SECRET=YES", encoding="utf-8")
        (self.project / "kaggle.json").write_text('"secret"', encoding="utf-8")
        (self.project / "data").mkdir()
        (self.project / "data" / "secret.txt").write_text("do not upload", encoding="utf-8")
        self.settings = Settings(self.root / "state", None, None)
        self.store = JobStore(self.settings.state)
        self.cli = FakeKaggle(self.settings)

    def test_source_packaging_is_safe_and_reproducible(self):
        files = source_files(self.project, "main.py")
        self.assertEqual(sorted(p.as_posix() for p in files), ["main.py", "pkg/__init__.py", "pkg/core.py"])
        data, manifest = pack_source(self.project, "main.py", 100000)
        with zipfile.ZipFile(__import__("io").BytesIO(data)) as bundle:
            self.assertEqual(sorted(bundle.namelist()), ["main.py", "pkg/__init__.py", "pkg/core.py"])
        script = make_bootstrap(data, entry="main.py", arguments=[], datasets=[], install_requirements=False)
        compile(script, "generated_run.py", "exec")
        self.assertNotIn("SECRET=YES", script)

    def test_generated_bootstrap_runs_real_python_script(self):
        data, _ = pack_source(self.project, "main.py", 100000)
        bootstrap = make_bootstrap(data, entry="main.py", arguments=["--epochs", "2"],
                                   datasets=[], install_requirements=False)
        fake_kaggle = self.root / "fake-kaggle"
        working = fake_kaggle / "working"
        working.mkdir(parents=True)
        compiled = bootstrap.replace("/kaggle/working", str(working)).replace("/kaggle/input", str(fake_kaggle / "input"))
        runner = self.root / "generated_run.py"
        runner.write_text(compiled, encoding="utf-8")
        process = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("ok", process.stdout)
        report = json.loads((working / "kgr_run_result.json").read_text())
        self.assertEqual(report["status"], "ok")

    def test_prevent_path_traversal_and_bad_refs(self):
        with self.assertRaises(RunnerError):
            source_files(self.project, "../main.py")
        with self.assertRaises(RunnerError):
            validate_ref("https://kaggle.com/u/d")

    def test_job_dry_run_offline_and_remote_lifecycle(self):
        dry = create_job(self.settings, self.cli, self.store, source=str(self.project),
                         entry="main.py", owner="my-user", gpu="t4x2", dataset_refs=["other/data"],
                         arguments=["--epochs", "2"], dry_run=True)
        self.assertEqual(dry["dry_run"], True)
        self.assertEqual(self.cli.calls, [])
        meta = json.loads((Path(dry["staging_dir"]) / "kernel-metadata.json").read_text())
        self.assertEqual(meta["machine_shape"], "NvidiaTeslaT4")
        self.assertEqual(meta["dataset_sources"], ["other/data"])
        self.assertEqual(meta["is_private"], "true")
        self.assertEqual(self.store.get(dry["job_id"])["status"], "prepared")
        live = create_job(self.settings, self.cli, self.store, source=str(self.project),
                          entry="main.py", owner="my-user", gpu="cpu", dataset_refs=[],
                          arguments=[])
        self.assertEqual(self.store.get(live["job_id"])["status"], "submitted")
        self.assertNotEqual(dry["kernel_ref"], live["kernel_ref"])
        status = refresh_status(self.cli, self.store, live["job_id"])
        self.assertEqual(status["status"], "complete")
        fetched = fetch_job(self.cli, self.store, self.settings.state, live["job_id"], None)
        self.assertEqual(fetched["files"][0]["name"], "metrics.json")
        self.assertTrue((Path(fetched["output_dir"]) / "kgr_download_manifest.json").exists())

    def test_submit_failure_is_recorded(self):
        self.cli.fail = True
        with self.assertRaises(RunnerError):
            create_job(self.settings, self.cli, self.store, source=str(self.project),
                       entry="main.py", owner="x", gpu="cpu", dataset_refs=[], arguments=[])
        self.assertEqual(self.store.list()[0]["status"], "submit_failed")

    def test_status_permission_error_marks_record_and_guides_owner_check(self):
        live = create_job(self.settings, self.cli, self.store, source=str(self.project),
                          entry="main.py", owner="my-user", gpu="cpu", dataset_refs=[], arguments=[])
        class ForbiddenKaggle(FakeKaggle):
            def call(self, *args, **kwargs):
                raise RunnerError("Permission 'kernels.get' was denied")
        with self.assertRaisesRegex(RunnerError, "profile URL"):
            refresh_status(ForbiddenKaggle(self.settings), self.store, live["job_id"])
        self.assertEqual(self.store.get(live["job_id"])["status"], "status_unavailable")

    def test_dataset_manager_keeps_nested_data_and_private(self):
        ds = self.root / "dataset"
        (ds / "data" / "deep").mkdir(parents=True)
        (ds / "data" / "deep" / "a.csv").write_text("a,b\n1,2\n")
        (ds / ".env").write_text("private", encoding="utf-8")
        preview = inspect_local(ds)
        self.assertEqual(preview["file_count"], 1)
        staging, manifest = prepare_dataset(ds, self.settings.state, "owner/private-data",
                                            "Private research data", "copyright-authors")
        with zipfile.ZipFile(staging / "payload.zip") as z:
            self.assertEqual(z.namelist(), ["data/deep/a.csv"])
        self.assertTrue((staging / "source-manifest.local.json").exists())
        upload_dataset(self.cli, staging, "owner/private-data")
        self.assertFalse((staging / "source-manifest.local.json").exists())
        self.assertEqual(self.cli.calls[-1][:2], ("datasets", "create"))
        self.assertNotIn("--public", self.cli.calls[-1])

    def test_status_parser(self):
        self.assertEqual(parse_kernel_status("KernelWorkerStatus.RUNNING"), "running")
        self.assertEqual(parse_kernel_status("KernelWorkerStatus.ERROR"), "error")
        self.assertEqual(parse_kernel_status("other"), "unknown")

    def test_cli_portable_dry_run(self):
        env = __import__("os").environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        proc = subprocess.run([sys.executable, "-m", "kaggle_runner", "--state-dir", str(self.root / "cli-state"),
                               "run", "--source", str(self.project), "--entry", "main.py",
                               "--owner", "my-user", "--dry-run"], text=True, capture_output=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["dry_run"])


if __name__ == "__main__":
    unittest.main()
