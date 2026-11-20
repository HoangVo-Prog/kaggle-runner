"""Regression for manually written lowercase team aliases and .env selection.

No real Kaggle credential is required or used in these tests.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from kaggle_runner.cli import main
from kaggle_runner.config import RunnerError
from kaggle_runner.team import add_profile, diagnose_profiles, get_profile, profiles


class CaseInsensitiveTeamEnvTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = Path(tmp.name) / ".env"
        isolated = dict(os.environ)
        for name in ("KGR_ENV_FILE", "KAGGLE_API_TOKEN", "KAGGLE_OWNER", "EMAIL"):
            isolated.pop(name, None)
        env_patch = patch.dict(os.environ, isolated, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def write(self, content: str):
        self.env.write_text(content, encoding="utf-8")
        if os.name != "nt":
            self.env.chmod(0o600)

    def test_photo_lowercase_alias_keys_are_recognized(self):
        self.write("KAGGLE_OWNER=hoanggvo\nKAGGLE_API_TOKEN=default-fake\n"
                   "KGR_TEAM_PROFILES=toanltv\n"
                   "KGR_TEAM_toanltv_USERNAME=toanltv\n"
                   "KGR_TEAM_toanltv_API_TOKEN=case-test-secret\n")
        profile = get_profile("toanltv", str(self.env))
        self.assertEqual(profile.owner, "toanltv")
        self.assertEqual(profile.token, "case-test-secret")
        self.assertEqual(profiles(str(self.env)), ["toanltv"])

    def test_all_uppercase_and_mixed_case_alias_keys_work(self):
        for prefix in ("KGR_TEAM_TOANLTV_", "kgr_team_TOANLTV_"):
            with self.subTest(prefix=prefix):
                self.write("KGR_TEAM_PROFILES=toanltv\n"
                           f"{prefix}USERNAME=toanltv\n"
                           f"{prefix}API_TOKEN=case-test-secret\n")
                self.assertEqual(get_profile("toanltv", str(self.env)).token, "case-test-secret")

    def test_wrong_env_file_is_clear_and_diagnosable(self):
        self.write("KGR_TEAM_PROFILES=toanltv\nKGR_TEAM_TOANLTV_USERNAME=toanltv\n"
                   "KGR_TEAM_TOANLTV_API_TOKEN=case-test-secret\n")
        other = self.env.parent / "other.env"
        other.write_text("KGR_TEAM_PROFILES=toanltv\nKGR_TEAM_TOANLTV_USERNAME=toanltv\n")
        if os.name != "nt":
            other.chmod(0o600)
        os.environ["KGR_ENV_FILE"] = str(other)
        with self.assertRaisesRegex(RunnerError, str(other)):
            get_profile("toanltv")
        report = diagnose_profiles()
        self.assertEqual(report["source"], "KGR_ENV_FILE")
        self.assertEqual(report["env_file"], str(other))
        self.assertFalse(report["profiles"][0]["token_configured"])
        self.assertTrue(diagnose_profiles(str(self.env))["profiles"][0]["ready"])

    def test_cli_diagnose_no_api_calls_or_secret_output(self):
        self.write("KGR_TEAM_PROFILES=toanltv\n"
                   "KGR_TEAM_toanltv_USERNAME=toanltv\n"
                   "KGR_TEAM_toanltv_API_TOKEN=case-test-secret\n")
        for options in ([], ["--json"]):
            buf = StringIO()
            with redirect_stdout(buf):
                self.assertEqual(main(["--env-file", str(self.env), "team", "diagnose", *options]), 0)
            output = buf.getvalue()
            self.assertIn(str(self.env), output)
            self.assertIn("toanltv", output)
            self.assertNotIn("case-test-secret", output)
            if options:
                self.assertEqual(json.loads(output)["profiles"][0]["ready"], True)

    def test_duplicate_case_definitions_are_rejected_without_leaks(self):
        self.write("KGR_TEAM_PROFILES=toanltv\n"
                   "KGR_TEAM_toanltv_USERNAME=toanltv\n"
                   "KGR_TEAM_toanltv_API_TOKEN=secret-a\n"
                   "KGR_TEAM_TOANLTV_API_TOKEN=secret-b\n")
        with self.assertRaises(RunnerError) as ctx:
            get_profile("toanltv", str(self.env))
        msg = str(ctx.exception)
        self.assertIn("Conflicting definitions", msg)
        self.assertNotIn("secret-a", msg)
        self.assertNotIn("secret-b", msg)

    def test_replace_normalizes_old_lowercase_keys(self):
        self.write("KGR_TEAM_PROFILES=toanltv\n"
                   "KGR_TEAM_toanltv_USERNAME=toanltv\n"
                   "KGR_TEAM_toanltv_API_TOKEN=case-test-secret\n"
                   "CUSTOM_SETTING=safe\n")
        add_profile("toanltv", token="new-secret", owner="toanltv", replace=True,
                    env_file=str(self.env))
        content = self.env.read_text()
        self.assertNotIn("KGR_TEAM_toanltv_API_TOKEN", content)
        self.assertIn("KGR_TEAM_TOANLTV_API_TOKEN=new-secret", content)
        self.assertIn("CUSTOM_SETTING=safe", content)
        self.assertEqual(get_profile("toanltv", str(self.env)).token, "new-secret")


if __name__ == "__main__":
    unittest.main()
