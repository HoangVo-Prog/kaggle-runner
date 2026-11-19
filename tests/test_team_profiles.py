"""End-to-end offline team routing, authorization, isolation and reporting tests."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from kaggle_runner.api import KaggleCLI, Result
from kaggle_runner.cli import main, parser
from kaggle_runner.config import RunnerError
from kaggle_runner.team import add_profile, get_profile, profiles
from kaggle_runner.jobs import JobStore


class AuthorizedTeamTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(self.sandbox.cleanup)
        self.root = Path(self.sandbox.name)
        env = {k: v for k, v in os.environ.items() if k not in (
            "KAGGLE_OWNER", "KAGGLE_API_TOKEN", "EMAIL", "KGR_ENV_FILE", "KGR_PROFILE")}
        env["KGR_TEAM_DIR"] = str(self.root / "team")
        env["XDG_CONFIG_HOME"] = str(self.root / "config")
        self.env_patch = patch.dict(os.environ, env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.state = self.root / "state"
        self.calls: list[tuple[str | None, tuple[str, ...]]] = []
        self.mock_call = lambda client, *args, **kwargs: self.api(client, *args, **kwargs)
        self.remaining = {"alice-token": 15, "bob-token": 6}
        self.remote_status = "KernelWorkerStatus.RUNNING"
        self.source = self.root / "train.py"
        self.source.write_text("print('trained')", encoding="utf-8")

    def invoke(self, *args: str):
        buf = StringIO()
        with redirect_stdout(buf):
            code = main(["--state-dir", str(self.state), *args])
        output = buf.getvalue()
        return code, output

    def api(self, instance, *args, **kwargs):
        self.calls.append((instance.token, args))
        token = instance.token
        if args[:1] == ("quota",):
            remaining = self.remaining.get(token)
            if remaining is None:
                return Result(list(args), 1, "", "no quota")
            row = {"resource": "GPU", "used": "0.0h", "remaining": f"{remaining}h", "total": "30h"}
            return Result(list(args), 0, json.dumps([row]), "")
        if args[:2] == ("kernels", "status"):
            return Result(list(args), 0, self.remote_status, "")
        if args[:2] == ("kernels", "push"):
            path = Path(args[args.index("-p") + 1])
            ref = json.loads((path / "kernel-metadata.json").read_text())["id"]
            return Result(list(args), 0, f"Kernel version 1 successfully pushed at https://www.kaggle.com/code/{ref}", "")
        if args[:2] == ("kernels", "logs"):
            return Result(list(args), 0, f"logs for {token}", "")
        if args[:2] == ("kernels", "files"):
            return Result(list(args), 0, '[{"name":"output.txt"}]', "")
        if args[:2] == ("kernels", "output"):
            path = Path(args[args.index("-p") + 1])
            (path / "output.txt").write_text(f"created by {token}", encoding="utf-8")
            return Result(list(args), 0, "downloaded", "")
        raise AssertionError(f"Unexpected Kaggle CLI args: {args}")

    def setup_accounts(self):
        add_profile("alice", token="alice-token", owner="aliceuser", email="alice@example.test")
        add_profile("bob", token="bob-token", owner="bobuser")

    def test_username_and_token_required_email_optional_private(self):
        with self.assertRaisesRegex(RunnerError, "username"):
            add_profile("x", token="secret", owner="")
        with self.assertRaisesRegex(RunnerError, "API token"):
            add_profile("x", token="", owner="ok")
        self.setup_accounts()
        self.assertEqual(profiles(), ["alice", "bob"])
        self.assertEqual(get_profile("alice").owner, "aliceuser")
        self.assertIsNone(get_profile("bob").email)
        path = self.root / "team" / "alice.env"
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn("KAGGLE_OWNER=aliceuser", path.read_text())
        code, out = self.invoke("team", "list", "--json")
        self.assertEqual(code, 0)
        self.assertNotIn("alice-token", out)
        self.assertNotIn("bob-token", out)
        self.assertIn("aliceuser", out)
        with self.assertRaises(RunnerError):
            add_profile("alice", token="different", owner="aliceuser")
        self.assertEqual(get_profile("alice").token, "alice-token")
        with self.assertRaisesRegex(RunnerError, "already belongs"):
            add_profile("alice2", token="another", owner="ALICEUSER")
        with self.assertRaisesRegex(RunnerError, "auto"):
            add_profile("auto", token="third", owner="thirduser")
        with self.assertRaisesRegex(RunnerError, "API token is already"):
            add_profile("alice3", token="alice-token", owner="distinct-owner")

    def test_team_commands_ignore_unrelated_legacy_env_file(self):
        self.setup_accounts()
        bad_env = self.root / ".env"
        bad_env.write_text("KAGGLE_API_TOKEN=legacy-should-not-be-used\n", encoding="utf-8")
        if os.name != "nt":
            bad_env.chmod(0o644)  # legacy creds deliberately unsafe/unreadable
        with patch.object(KaggleCLI, "call", self.mock_call):
            code, out = self.invoke("team", "list", "--json")
            self.assertEqual(code, 0)
            self.assertIn("aliceuser", out)
            code, out = self.invoke("run", str(self.source), "--gpu", "cpu",
                                    "--account", "alice", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["account_profile"], "alice")
        self.assertFalse(any(token == "legacy-should-not-be-used" for token, _ in self.calls))

    def test_cli_add_stdin_token_without_echo_and_human_output(self):
        with patch("sys.stdin", StringIO("stdin-token-secret\n")):
            code, out = self.invoke("team", "add", "charlie", "--username",
                                    "charliekaggle", "--token-stdin")
        self.assertEqual(code, 0)
        self.assertIn("charlie", out)
        self.assertNotIn("stdin-token-secret", out)
        self.assertIsNone(get_profile("charlie").email)
        code, output = self.invoke("team", "list")
        self.assertEqual(code, 0)
        self.assertIn("USERNAME", output)
        self.assertIn("charliekaggle", output)
        self.assertNotIn("stdin-token-secret", output)

    def test_argparse_requires_username(self):
        with self.assertRaises(SystemExit):
            parser().parse_args(["team", "add", "alice", "--token-stdin"])

    def test_team_check_uses_independent_tokens_without_listing_notebooks(self):
        self.setup_accounts()
        with patch.object(KaggleCLI, "call", self.mock_call):
            code, out = self.invoke("team", "check", "--gpu", "t4x2",
                                    "--required-gpu-hours", "7", "--json")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["ready_count"], 1)
        self.assertEqual(report["accounts"][0]["account_owner"], "aliceuser")
        self.assertTrue(report["accounts"][0]["ok"])
        self.assertFalse(report["accounts"][1]["ok"])
        self.assertTrue(all(args[0] != "kernels" or args[1] != "list"
                            for _, args in self.calls))
        self.assertEqual(set(token for token, args in self.calls if args[0] == "quota"),
                         {"alice-token", "bob-token"})
        self.assertNotIn("alice-token", out)

    def test_gpu_cpu_third_blocked_auto_routes_bob_and_followups_rebind(self):
        self.setup_accounts()
        with patch.object(KaggleCLI, "call", self.mock_call):
            code, out = self.invoke("run", str(self.source), "--gpu", "t4x2", "--account", "alice", "--json")
            self.assertEqual(code, 0, out)
            alice_gpu = json.loads(out)
            code, out = self.invoke("run", str(self.source), "--gpu", "cpu", "--account", "alice", "--json")
            self.assertEqual(code, 0, out)
            alice_cpu = json.loads(out)
            self.assertEqual(alice_gpu["account_profile"], "alice")
            self.assertEqual(alice_cpu["account_profile"], "alice")
            self.assertEqual(alice_gpu["account_owner"], "aliceuser")
            count_before = len([1 for token, args in self.calls if args[:2] == ("kernels", "push")])
            self.assertEqual(count_before, 2)
            # Third same-account submission must be blocked before a push.
            code, out = self.invoke("run", str(self.source), "--gpu", "cpu", "--account", "alice")
            self.assertEqual(code, 2)
            self.assertEqual(len([1 for _, args in self.calls if args[:2] == ("kernels", "push")]), 2)
            code, out = self.invoke("run", str(self.source), "--gpu", "cpu", "--account", "auto", "--json")
            self.assertEqual(code, 0, out)
            bob = json.loads(out)
            self.assertEqual(bob["account_profile"], "bob")
            self.assertEqual(bob["account_owner"], "bobuser")
            self.assertEqual(self.calls[-1][0], "bob-token")
            job = JobStore(self.state).get(bob["job_id"])
            self.assertEqual(job["account_profile"], "bob")
            self.assertEqual(job["account_owner"], "bobuser")
            self.assertIsNone(job["account_email"])
            # Status and logs for Bob must use Bob's token, never default/Alice.
            code, out = self.invoke("status", bob["job_id"], "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["account_profile"], "bob")
            self.assertEqual(self.calls[-1][0], "bob-token")
            code, out = self.invoke("logs", bob["job_id"], "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["account_owner"], "bobuser")
            self.assertEqual(json.loads(out)["logs"], "logs for [REDACTED]")
            self.assertEqual(self.calls[-1][0], "bob-token")
            code, out = self.invoke("outputs", bob["job_id"], "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["account_profile"], "bob")
            self.assertEqual(self.calls[-1][0], "bob-token")
            dest = self.root / "results"
            code, out = self.invoke("fetch", bob["job_id"], "--output", str(dest), "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["account_profile"], "bob")
            self.assertEqual((dest / "output.txt").read_text(), "created by bob-token")
            download_meta = json.loads((dest / "kgr_download_manifest.json").read_text())
            self.assertEqual(download_meta["account_owner"], "bobuser")
            self.assertEqual(download_meta["account_profile"], "bob")
            self.assertEqual(self.calls[-1][0], "bob-token")
            # Report refreshes each job using the *stored* credential profile.
            self.calls.clear()
            code, out = self.invoke("report", "--refresh", "--json")
            self.assertEqual(code, 0)
            report = json.loads(out)
            self.assertEqual(report["total"], 3)
            self.assertEqual(report["accounts"], ["alice", "bob"])
            self.assertEqual(report["by_account"]["alice"]["total"], 2)
            self.assertEqual(report["by_account"]["bob"]["total"], 1)
            self.assertEqual({j["account_profile"] for j in report["jobs"]}, {"alice", "bob"})
            self.assertEqual([token for token, args in self.calls if args[:2] == ("kernels", "status")].count("alice-token"), 2)
            self.assertEqual([token for token, args in self.calls if args[:2] == ("kernels", "status")].count("bob-token"), 1)
            self.assertNotIn("alice-token", json.dumps(report))
            self.assertNotIn("bob-token", json.dumps(report))

    def test_no_eligible_accounts_do_not_push(self):
        self.setup_accounts()
        self.remaining = {"alice-token": 0, "bob-token": 0}
        with patch.object(KaggleCLI, "call", self.mock_call):
            code, _ = self.invoke("run", str(self.source), "--account", "auto",
                                   "--gpu", "t4x2", "--required-gpu-hours", "2")
        self.assertEqual(code, 2)
        self.assertFalse(any(args[:2] == ("kernels", "push") for _, args in self.calls))

    def test_missing_profile_prevents_job_fetch_instead_of_wrong_token(self):
        self.setup_accounts()
        with patch.object(KaggleCLI, "call", self.mock_call):
            code, out = self.invoke("run", str(self.source), "--account", "alice", "--gpu", "cpu", "--json")
        self.assertEqual(code, 0)
        job_id = json.loads(out)["job_id"]
        (self.root / "team" / "alice.env").unlink()
        self.calls.clear()
        code, out = self.invoke("status", job_id)
        self.assertEqual(code, 2)
        self.assertFalse(self.calls)


if __name__ == "__main__":
    unittest.main()
