"""Repository-root .env resolution: isolated unit tests, no live credentials."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kaggle_runner.accounts import _looks_like_project_root, _project_root, preferred_env_file
from kaggle_runner.config import RunnerError
from kaggle_runner.team import add_profile, get_profile, profiles


class RepositoryEnvTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.worktree = self.root / "kaggle-runner"
        (self.worktree / "kaggle_runner").mkdir(parents=True)
        (self.worktree / "kaggle_runner" / "accounts.py").write_text("# placeholder\n")
        (self.worktree / "pyproject.toml").write_text('[project]\nname = "kaggle-runner"\n')
        self.other = self.root / "unrelated-project"
        self.other.mkdir()
        (self.other / ".env").write_text("KGR_TEAM_PROFILES=other\n")
        sandbox = patch.dict(os.environ, {key: value for key, value in os.environ.items()
                                        if key not in ("KGR_ENV_FILE", "KAGGLE_OWNER", "KAGGLE_API_TOKEN", "EMAIL")}, clear=True)
        sandbox.start()
        self.addCleanup(sandbox.stop)

    def test_nested_cwd_finds_checkout_root_without_existing_env(self):
        nested = self.worktree / "examples" / "smoke"
        nested.mkdir(parents=True)
        with patch("kaggle_runner.accounts.Path.cwd", return_value=nested):
            self.assertEqual(_project_root(), self.worktree)
            self.assertEqual(preferred_env_file(), self.worktree / ".env")

    def test_unrelated_dotenv_does_not_override_editable_install(self):
        # From outside a checkout, code installed editable points to its
        # original source root, not the unrelated project's .env.
        checkout = Path(__file__).resolve().parents[1]
        with patch("kaggle_runner.accounts.Path.cwd", return_value=self.other):
            self.assertEqual(preferred_env_file(), checkout / ".env")
            self.assertNotEqual(preferred_env_file(), self.other / ".env")

    def test_explicit_and_environment_overrides_have_precedence(self):
        custom = self.root / "custom.env"
        with patch("kaggle_runner.accounts.Path.cwd", return_value=self.worktree):
            os.environ["KGR_ENV_FILE"] = str(self.root / "from_env.env")
            self.assertEqual(preferred_env_file(), self.root / "from_env.env")
            self.assertEqual(preferred_env_file(str(custom)), custom)

    def test_create_team_profiles_at_repo_root_by_default(self):
        with patch("kaggle_runner.accounts.Path.cwd", return_value=self.worktree):
            add_profile("alice", token="fake-alice", owner="alice-username")
            add_profile("bob", token="fake-bob", owner="bob-username")
            self.assertEqual(profiles(), ["alice", "bob"])
            self.assertEqual(get_profile("bob").token, "fake-bob")
            target = self.worktree / ".env"
            self.assertTrue(target.is_file())
            self.assertFalse((self.other / ".env").read_text().find("fake-alice") >= 0)
            self.assertFalse((self.root / "from_env.env").exists())
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_non_runner_project_rejected_and_no_silent_home_fallback(self):
        with patch("kaggle_runner.accounts.Path.cwd", return_value=self.other), \
             patch("kaggle_runner.accounts.__file__", str(self.other / "not-installed" / "accounts.py")):
            with self.assertRaisesRegex(RunnerError, "Cannot locate the kaggle-runner project root"):
                preferred_env_file()

    def test_reject_lookalike_root_with_wrong_pyproject_name(self):
        impostor = self.root / "impostor"
        (impostor / "kaggle_runner").mkdir(parents=True)
        (impostor / "kaggle_runner" / "accounts.py").write_text("# no\n")
        (impostor / "pyproject.toml").write_text('[project]\nname = "a-different-project"\n')
        self.assertFalse(_looks_like_project_root(impostor))


if __name__ == "__main__":
    unittest.main()
