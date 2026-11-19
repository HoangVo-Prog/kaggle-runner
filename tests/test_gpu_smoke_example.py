"""Offline tests for the Kaggle GPU smoke example."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "gpu-smoke-test"


class SmokeExampleTests(unittest.TestCase):
    def test_local_without_gpu_creates_failure_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            env = os.environ.copy()
            env["KGR_OUTPUT_DIR"] = temp
            # -S deliberately excludes optional site packages including torch.
            process = subprocess.run([sys.executable, "-S", str(EXAMPLE / "gpu_probe.py")],
                                     env=env, check=False, text=True, capture_output=True)
            self.assertEqual(process.returncode, 1, process.stdout + process.stderr)
            result = json.loads((Path(temp) / "gpu_smoke_result.json").read_text())
            self.assertEqual(result["status"], "failed")
            self.assertTrue((Path(temp) / "gpu_devices.csv").is_file())
            self.assertTrue((Path(temp) / "gpu_smoke.log").is_file())
            self.assertTrue(result["errors"])

    def test_bootstrap_treats_clean_sys_exit_as_success(self):
        from kaggle_runner.packaging import make_bootstrap, pack_source
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            src = base / "input"
            src.mkdir()
            (src / "main.py").write_text("import sys\nprint('done')\nsys.exit(0)\n", encoding="utf-8")
            data, _ = pack_source(src, "main.py", 1024 * 1024)
            script = make_bootstrap(data, entry="main.py", arguments=[], datasets=[], install_requirements=False)
            fake = base / "fake_kaggle"
            working = fake / "working"
            working.mkdir(parents=True)
            script_path = base / "run.py"
            script_path.write_text(script.replace("/kaggle/working", str(working)).replace("/kaggle/input", str(fake / "input")))
            p = subprocess.run([sys.executable, str(script_path)], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(json.loads((working / "kgr_run_result.json").read_text())["status"], "ok")

    def test_example_cli_dry_run_is_offline_and_sets_t4x2(self):
        with tempfile.TemporaryDirectory() as temp:
            env = os.environ.copy()
            env["KGR_STATE_DIR"] = str(Path(temp) / "state")
            env["PYTHONPATH"] = str(ROOT)
            proc = subprocess.run([sys.executable, str(EXAMPLE / "run_example.py"),
                                   "--owner", "example-owner", "--dry-run"],
                                  env=env, check=False, text=True, capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            jobs = list((Path(temp) / "state" / "jobs").iterdir())
            self.assertEqual(len(jobs), 1)
            manifest = json.loads((jobs[0] / "kernel" / "kernel-metadata.json").read_text())
            self.assertEqual(manifest["machine_shape"], "NvidiaTeslaT4")
            self.assertEqual(manifest["is_private"], "true")
            self.assertIn("Job ID:", proc.stdout)

    def test_dry_run_owner_from_user_config_without_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            config.write_text('[runner]\nowner = "sample-kaggle-user"\n', encoding="utf-8")
            env = os.environ.copy()
            env["KGR_STATE_DIR"] = str(root / "state")
            env["KGR_CONFIG"] = str(config)
            env["PYTHONPATH"] = str(ROOT)
            proc = subprocess.run([sys.executable, str(EXAMPLE / "run_example.py"), "--dry-run"],
                                  env=env, text=True, capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("sample-kaggle-user/", proc.stdout)

    def test_online_doctor_fails_if_auth_fails(self):
        from kaggle_runner.cli import main
        from kaggle_runner.api import Result
        with tempfile.TemporaryDirectory() as temp:
            def fake_call(_self, *argv, **_kw):
                if argv == ("--version",):
                    return Result(list(argv), 0, "kaggle 2.0", "")
                return Result(list(argv), 1, "", "Authentication required")
            with mock.patch("kaggle_runner.api.KaggleCLI.call", fake_call):
                with mock.patch("sys.stdout"):
                    self.assertEqual(main(["--state-dir", temp, "doctor", "--online"]), 1)
                    self.assertEqual(main(["--state-dir", temp, "doctor"]), 0)


if __name__ == "__main__":
    unittest.main()
