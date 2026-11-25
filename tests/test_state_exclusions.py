from __future__ import annotations

import subprocess
import tempfile
import unittest
import zipfile
import os
from pathlib import Path

from kaggle_runner.packaging import pack_source, source_files


REPO = Path(__file__).resolve().parents[1]


class StateExclusionTests(unittest.TestCase):
    def _git(self, *args):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_CONFIG_")}
        return subprocess.run(
            ["git", "-c", f"safe.directory={REPO.as_posix()}", *args],
            cwd=REPO, env=env, capture_output=True, text=True,
        )

    def test_synthetic_state_files_never_enter_source_package(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "main.py").write_text("print('ok')\n", encoding="utf-8")
            state = root / "_runner-state"
            (state / "work" / "fixture-objective" / "reports").mkdir(parents=True)
            (state / "ledger.json").write_text('{"objective":"fixture"}', encoding="utf-8")
            (state / "report.md").write_text("synthetic report", encoding="utf-8")
            (state / "jobs.sqlite3").write_bytes(b"synthetic sqlite bytes")
            (state / "opaque.bin").write_bytes(b"synthetic binary state")
            (state / "work" / "fixture-objective" / "reports" / "summary.md").write_text(
                "synthetic nested artifact", encoding="utf-8")

            files = source_files(root, "main.py")
            self.assertEqual([path.as_posix() for path in files], ["main.py"])
            packed, _manifest = pack_source(root, "main.py", 100_000)
            with zipfile.ZipFile(__import__("io").BytesIO(packed)) as archive:
                self.assertEqual(archive.namelist(), ["main.py"])

    def test_repo_gitignore_excludes_representative_persistent_state(self):
        for relative in (
            "_runner-state/jobs.sqlite3",
            "_runner-state/work/fixture-objective/spec.json",
            "_runner-state/work/fixture-objective/reports/report.md",
            "_runner-state/setup-checkpoint.json",
        ):
            with self.subTest(path=relative):
                result = self._git("check-ignore", "--no-index", "-q", relative)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_repo_source_ignore_excludes_state_directory(self):
        self.assertIn("_runner-state", __import__("kaggle_runner.packaging", fromlist=["EXCLUDE_DIRS"]).EXCLUDE_DIRS)
        rules = (REPO / ".kaggle-runnerignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("_runner-state/", rules)


if __name__ == "__main__":
    unittest.main()
