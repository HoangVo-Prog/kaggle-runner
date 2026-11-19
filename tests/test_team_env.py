"""Shared .env storage and multi-account CLI isolation regressions (offline)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from kaggle_runner.accounts import load_account, write_account
from kaggle_runner.api import KaggleCLI, Result
from kaggle_runner.cli import main
from kaggle_runner.config import RunnerError
from kaggle_runner.team import add_profile, get_profile, migrate_legacy_profiles, profiles


class TeamEnvTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env_file = self.root / "team.env"
        self.state = self.root / "state"
        env = {k: v for k, v in os.environ.items() if k not in
               {"KAGGLE_OWNER", "KAGGLE_API_TOKEN", "EMAIL", "KGR_ENV_FILE", "KGR_TEAM_DIR"}}
        env["KGR_ENV_FILE"] = str(self.env_file)
        sandbox = patch.dict(os.environ, env, clear=True)
        sandbox.start()
        self.addCleanup(sandbox.stop)
        self.calls = []
        self.script = self.root / "smoke.py"
        self.script.write_text("print('team env smoke')\n")

    def invoke(self, *args):
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = main(["--state-dir", str(self.state), *args])
        return code, buffer.getvalue()

    def mock_api(self, client, *args, **kwargs):
        self.calls.append((client.token, args))
        if args[0] == "quota":
            remaining = {"token-a": 8, "token-b": 7}.get(client.token, 0)
            return Result(list(args), 0, json.dumps([{"resource": "GPU", "remaining": f"{remaining}h"}]), "")
        if args[:2] == ("kernels", "push"):
            staging = Path(args[args.index("-p") + 1])
            ref = json.loads((staging / "kernel-metadata.json").read_text())["id"]
            return Result(list(args), 0, f"Pushed to https://www.kaggle.com/code/{ref}", "")
        if args[:2] == ("kernels", "status"):
            return Result(list(args), 0, "KernelWorkerStatus.COMPLETE", "")
        if args[:2] == ("kernels", "logs"):
            return Result(list(args), 0, f"executed by {client.token}", "")
        if args[:2] == ("kernels", "output"):
            target = Path(args[args.index("-p") + 1])
            (target / "result.txt").write_text(client.token)
            return Result(list(args), 0, "downloaded", "")
        if args[:2] == ("kernels", "files"):
            return Result(list(args), 0, '[{"name":"result.txt"}]', "")
        raise AssertionError(f"Unexpected call {args}")

    def test_manually_edited_env_is_read_and_tokens_never_printed(self):
        self.env_file.write_text("# Team members\nKGR_TEAM_PROFILES=alice,bob\n"
                                 "KGR_TEAM_ALICE_USERNAME=alice-kaggle\n"
                                 "KGR_TEAM_ALICE_API_TOKEN=token-a\n"
                                 "KGR_TEAM_BOB_USERNAME=bob-kaggle\n"
                                 "KGR_TEAM_BOB_API_TOKEN=token-b\n"
                                 "KGR_TEAM_BOB_EMAIL=bob@example.test\n")
        if os.name != "nt":
            self.env_file.chmod(0o600)
        self.assertEqual(profiles(), ["alice", "bob"])
        self.assertEqual(get_profile("bob").email, "bob@example.test")
        code, output = self.invoke("team", "list", "--json")
        self.assertEqual(code, 0)
        self.assertNotIn("token-a", output)
        self.assertNotIn("token-b", output)
        self.assertEqual(len(json.loads(output)), 2)

    def test_team_add_updates_single_file_preserving_default_and_comments(self):
        self.env_file.write_text("# Keep me\nCUSTOM_FLAG=hello\n")
        if os.name != "nt":
            self.env_file.chmod(0o600)
        add_profile("alice", token="token-a", owner="alice-kaggle", email="alice@example.test")
        add_profile("bob", token="token-b", owner="bob-kaggle")
        write_account(None, "default_user", "default@example.test", "default-token")
        body = self.env_file.read_text()
        self.assertIn("# Keep me", body)
        self.assertIn("CUSTOM_FLAG=hello", body)
        self.assertIn("KGR_TEAM_PROFILES=alice,bob", body)
        self.assertIn("KGR_TEAM_BOB_API_TOKEN=token-b", body)
        self.assertEqual(load_account().token, "default-token")
        self.assertEqual(get_profile("alice").token, "token-a")
        if os.name != "nt":
            self.assertEqual(self.env_file.stat().st_mode & 0o777, 0o600)
        add_profile("alice", token="token-a-v2", owner="alice-kaggle", replace=True)
        self.assertEqual(get_profile("alice").token, "token-a-v2")
        self.assertEqual(get_profile("bob").token, "token-b")
        self.assertNotIn("token-a\n", self.env_file.read_text())

    def test_only_team_tokens_with_explicit_env_file_followup(self):
        path = self.root / "explicit.env"
        add_profile("alice", token="token-a", owner="alice-kaggle", env_file=str(path))
        add_profile("bob", token="token-b", owner="bob-kaggle", env_file=str(path))
        self.assertEqual(profiles(), [])  # another env_file remains independent
        self.assertEqual(profiles(str(path)), ["alice", "bob"])
        with patch.object(KaggleCLI, "call", lambda client, *args, **kwargs: self.mock_api(client, *args, **kwargs)):
            jobs = []
            for alias in ("alice", "bob"):
                code, output = self.invoke("--env-file", str(path), "run", str(self.script),
                                           "--account", alias, "--gpu", "cpu", "--json")
                self.assertEqual(code, 0, output)
                jobs.append(json.loads(output))
            code, output = self.invoke("--env-file", str(path), "report", "--refresh", "--json")
            self.assertEqual(code, 0, output)
            self.assertEqual(json.loads(output)["accounts"], ["alice", "bob"])
            for job, token in zip(jobs, ("token-a", "token-b")):
                self.calls.clear()
                code, output = self.invoke("--env-file", str(path), "status", job["job_id"], "--json")
                self.assertEqual(code, 0, output)
                self.assertEqual(self.calls[-1][0], token)
                code, output = self.invoke("--env-file", str(path), "logs", job["job_id"], "--json")
                self.assertEqual(code, 0, output)
                self.assertEqual(self.calls[-1][0], token)
                self.assertNotIn(token, output)
                code, output = self.invoke("--env-file", str(path), "fetch", job["job_id"],
                                           "--output", str(self.root / job["account_profile"]), "--json")
                self.assertEqual(code, 0, output)
                self.assertEqual(self.calls[-1][0], token)
        self.assertFalse(any(args[:2] == ("kernels", "list") for _, args in self.calls))

    def test_migrate_old_profiles_once_without_deleting_originals(self):
        legacy = self.root / "old-team"
        legacy.mkdir()
        for alias, username, token in (("alice", "alice-kaggle", "token-a"),
                                       ("bob", "bob-kaggle", "token-b")):
            path = legacy / f"{alias}.env"
            path.write_text(f"KAGGLE_OWNER={username}\nKAGGLE_API_TOKEN={token}\n")
            if os.name != "nt":
                path.chmod(0o600)
        outcome = migrate_legacy_profiles(from_dir=str(legacy))
        self.assertEqual(outcome["count"], 2)
        self.assertEqual(profiles(), ["alice", "bob"])
        self.assertEqual(get_profile("bob").token, "token-b")
        self.assertTrue((legacy / "bob.env").exists())
        with self.assertRaisesRegex(RunnerError, "already exists"):
            migrate_legacy_profiles(from_dir=str(legacy))

    def test_live_smoke_requires_explicit_confirmation(self):
        import subprocess
        import sys
        runner = Path(__file__).resolve().parents[1] / "examples" / "team-env-multi-account" / "run_live_test.py"
        result = subprocess.run([sys.executable, str(runner), "--env-file", str(self.env_file),
                                 "--accounts", "alice", "bob"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--confirm-live", result.stderr)
        self.assertFalse(self.env_file.exists())

    def test_explicit_env_migration_command_and_json_output(self):
        old = self.root / "legacy"
        old.mkdir()
        p = old / "bob.env"
        p.write_text("KAGGLE_OWNER=bob_kaggle\nKAGGLE_API_TOKEN=bob-token\n")
        if os.name != "nt":
            p.chmod(0o600)
        code, output = self.invoke("team", "migrate", "--from-dir", str(old), "--json")
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["imported_profiles"], ["bob"])
        self.assertEqual(get_profile("bob").owner, "bob_kaggle")
        self.assertNotIn("bob-token", output)

    def test_public_env_rejected_and_alias_collisions_rejected(self):
        self.env_file.write_text("# bad permissions\n")
        if os.name != "nt":
            self.env_file.chmod(0o644)
            with self.assertRaisesRegex(RunnerError, "permissions too open"):
                add_profile("alice", token="token-a", owner="alice-kaggle")
            self.env_file.chmod(0o600)
        add_profile("lab-a", token="token-a", owner="alice-kaggle")
        with self.assertRaisesRegex(RunnerError, "conflicts"):
            add_profile("lab_a", token="token-b", owner="bob-kaggle")


if __name__ == "__main__":
    unittest.main()
