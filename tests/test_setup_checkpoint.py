"""Offline persistence, invalidation and safety tests for setup checkpoints."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from kaggle_runner.config import RunnerError, Settings
from kaggle_runner.setup_checkpoint import _online_check, _status, status, verify


def observations():
    source = {"head": "abc123", "working_tree": []}
    py = "C:/Python/python.exe"
    dep = "dependency-config-v1"
    file_info = {"path": "C:/private/.env", "exists": False, "permissions": "missing"}
    profiles = {"credential_file": file_info, "kaggle_config_file": {"path": "C:/kaggle.json", "exists": False,
                "symlink": False, "permissions": "missing"}, "profiles": [],
                "default_environment_credentials_configured": False}
    names = {
        "python_interpreter": ({"executable": py, "version": "3.13.1"}, "verified"),
        "python_environment": ({"prefix": "C:/Python/env", "base_prefix": "C:/Python"}, "verified"),
        "package": ({"version": "0.1.0", "module_path": "C:/repo/kaggle_runner"}, "verified"),
        "dependencies": ({"python": py, "dependency_config_sha256": dep, "jsonschema_version": "4.26.0"}, "verified"),
        "kaggle_cli": ({"python": py, "module_available": False, "version": None, "executable": None}, "missing"),
        "credential_file": (file_info, "missing"),
        "kaggle_config_file": (profiles["kaggle_config_file"], "missing"),
        "account_profiles": (profiles, "missing"),
        "sqlite": ({"state_directory": "C:/state", "database_exists": False}, "missing"),
        "source_tests": ({"source": source, "python": py, "dependency_config_sha256": dep}, "unverified"),
    }
    items = {}
    for name, (inputs, state) in names.items():
        items[name] = _status(state, "test snapshot", inputs=deepcopy(inputs),
                              reason="test fixture is missing" if state == "missing" else
                              "run offline tests" if state == "unverified" else None,
                              revision=source["head"] if name == "source_tests" else None)
    live = {"python": {"executable": py}, "dependency_config_sha256": dep,
            "credential_file": file_info, "account_profiles": profiles,
            "credential_configuration": {"credential_file": file_info,
                                          "kaggle_config_file": profiles["kaggle_config_file"]},
            "source": source}
    return items, live


class SetupCheckpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(self.root / "state", None, None)
        self.env_file = self.root / ".env"

    def test_second_session_reuses_verified_checks_without_reinstall_or_credential_setup(self):
        observed, live = observations()
        with patch("kaggle_runner.setup_checkpoint._collect", return_value=(observed, live)), \
             patch("kaggle_runner.setup_checkpoint._run_offline_tests", return_value=("verified", None)) as tests:
            first = verify(self.settings, env_file=str(self.env_file))
            self.assertTrue(first["ok"])
            self.assertIn("source_tests", first["revalidated"])
            second = verify(self.settings, env_file=str(self.env_file))
            self.assertTrue(second["ok"])
            self.assertEqual(second["revalidated"], [])
            self.assertIn("source_tests", second["reused"])
            self.assertEqual(tests.call_count, 1)
        checkpoint = json.loads((self.settings.state / "setup-checkpoint.json").read_text())
        self.assertEqual(len(checkpoint["history"]), 2)
        self.assertEqual(checkpoint["checks"]["source_tests"]["last_successful_revision"], "abc123")

    def test_python_environment_change_invalidates_only_dependent_checks(self):
        old, live = observations()
        changed = deepcopy(old)
        new_python = "D:/Other/python.exe"
        for name in ("python_interpreter", "python_environment", "package", "dependencies", "kaggle_cli"):
            changed[name]["inputs"]["executable" if name == "python_interpreter" else "python"] = new_python
        changed["source_tests"]["inputs"]["python"] = new_python
        changed_live = deepcopy(live)
        changed_live["python"]["executable"] = new_python
        with patch("kaggle_runner.setup_checkpoint._collect", side_effect=[(old, live), (old, live),
                                                                            (changed, changed_live), (changed, changed_live)]), \
             patch("kaggle_runner.setup_checkpoint._run_offline_tests", return_value=("verified", None)):
            self.assertTrue(verify(self.settings)["ok"])
            result = verify(self.settings)
        self.assertEqual(set(result["revalidated"]), {
            "python_interpreter", "python_environment", "package", "dependencies", "kaggle_cli", "source_tests"})
        self.assertNotIn("credential_file", result["revalidated"])
        self.assertNotIn("account_profiles", result["revalidated"])

    def test_dependency_configuration_change_stales_dependencies_and_tests_not_credentials(self):
        old, live = observations()
        changed = deepcopy(old)
        changed["dependencies"]["inputs"]["dependency_config_sha256"] = "dependency-config-v2"
        changed["source_tests"]["inputs"]["dependency_config_sha256"] = "dependency-config-v2"
        changed_live = deepcopy(live)
        changed_live["dependency_config_sha256"] = "dependency-config-v2"
        with patch("kaggle_runner.setup_checkpoint._collect", side_effect=[(old, live), (old, live),
                                                                            (changed, changed_live), (changed, changed_live)]), \
             patch("kaggle_runner.setup_checkpoint._run_offline_tests", return_value=("verified", None)):
            verify(self.settings)
            result = verify(self.settings)
        self.assertEqual(set(result["revalidated"]), {"dependencies", "source_tests"})
        self.assertNotIn("credential_file", result["revalidated"])

    def test_credential_metadata_change_invalidates_credentials_and_auth_only(self):
        old, live = observations()
        old["credential_file"] = _status("verified", "test", inputs={"path": "C:/private/.env", "mtime_ns": 1})
        old["account_profiles"] = _status("verified", "test", inputs={"mtime_ns": 1}, profiles=[{"alias": "alice", "owner": "owner"}])
        old["kaggle_config_file"] = _status("missing", "test", inputs={"exists": False})
        changed = deepcopy(old)
        changed["credential_file"] = _status("verified", "test", inputs={"path": "C:/private/.env", "mtime_ns": 2})
        changed["account_profiles"] = _status("verified", "test", inputs={"mtime_ns": 2}, profiles=[{"alias": "alice", "owner": "owner"}])
        live_changed = deepcopy(live)
        live_changed["credential_file"] = {"path": "C:/private/.env", "mtime_ns": 2}
        live_changed["account_profiles"]["credential_file"] = live_changed["credential_file"]
        live_changed["account_profiles"]["profiles"] = [{"alias": "alice", "owner": "owner"}]
        live_changed["credential_configuration"]["credential_file"] = live_changed["credential_file"]
        with patch("kaggle_runner.setup_checkpoint._collect", side_effect=[(old, live), (old, live),
                                                                            (changed, live_changed), (changed, live_changed)]), \
             patch("kaggle_runner.setup_checkpoint._run_offline_tests", return_value=("verified", None)):
            verify(self.settings)
            result = verify(self.settings)
        self.assertIn("credential_file", result["revalidated"])
        self.assertIn("account_profiles", result["revalidated"])
        self.assertNotIn("dependencies", result["revalidated"])
        self.assertNotIn("source_tests", result["revalidated"])

    def test_sqlite_check_is_read_only(self):
        self.settings.state.mkdir(parents=True)
        db = self.settings.state / "jobs.sqlite3"
        con = sqlite3.connect(db)
        try:
            con.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, kernel_ref TEXT, status TEXT, account_profile TEXT)")
            con.commit()
        finally:
            con.close()
        before = db.stat().st_size
        with patch("kaggle_runner.setup_checkpoint._collect", return_value=observations()):
            # Check the production inspector directly with a state-local ledger.
            from kaggle_runner.setup_checkpoint import _sqlite_snapshot
            snapshot = _sqlite_snapshot(self.settings)
        self.assertEqual(snapshot["status"], "verified")
        self.assertEqual(db.stat().st_size, before)
        con = sqlite3.connect(db)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        finally:
            con.close()

    def test_setup_cli_status_json_and_online_requires_explicit_account(self):
        from kaggle_runner.cli import main
        observed, live = observations()
        output = StringIO()
        with patch("kaggle_runner.cli.build_settings", return_value=self.settings), \
             patch("kaggle_runner.setup_checkpoint._collect", return_value=(observed, live)), \
             redirect_stdout(output):
            self.assertEqual(main(["--state-dir", str(self.settings.state), "setup", "status", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["checkpoint_status"], "missing")
        output = StringIO()
        with patch("kaggle_runner.cli.build_settings", return_value=self.settings), redirect_stdout(output):
            self.assertEqual(main(["--state-dir", str(self.settings.state), "setup", "verify", "--online", "--json"]), 2)
        self.assertEqual(json.loads(output.getvalue())["ok"], False)

    def test_online_network_failure_preserves_local_records_and_never_claims_auth(self):
        self.env_file.write_text("KGR_TEAM_PROFILES=alice\nKGR_TEAM_ALICE_USERNAME=owner\nKGR_TEAM_ALICE_API_TOKEN=synthetic-secret\n")
        observed, live = observations()
        live["account_profiles"]["profiles"] = [{"alias": "alice", "owner": "owner", "token_configured": True}]
        with patch("kaggle_runner.setup_checkpoint._collect", return_value=(observed, live)), \
             patch("kaggle_runner.setup_checkpoint._run_offline_tests", return_value=("verified", None)), \
             patch("kaggle_runner.setup_checkpoint.KaggleCLI.call", side_effect=RunnerError("request timed out")):
            result = verify(self.settings, env_file=str(self.env_file), online_account="alice")
        self.assertFalse(result["ok"])
        self.assertEqual(result["checks"]["online_auth:alice"]["status"], "unverified")
        self.assertEqual(result["checks"]["dependencies"]["status"], "verified")
        serialized = (self.settings.state / "setup-checkpoint.json").read_text()
        self.assertNotIn("synthetic-secret", serialized)

    def test_rejected_auth_is_failed_and_keeps_only_historical_success_timestamp(self):
        previous = {"last_success_at": "2026-01-01T00:00:00+00:00", "last_success_method": "prior check"}
        result = _online_check(self.settings, "alice", None,
                               {"alias": "alice", "owner": "owner", "credential_source": "configured_file"},
                               previous)
        self.assertIn(result["status"], {"missing", "unverified"})
        self.assertEqual(result.get("last_success_at"), previous["last_success_at"])
        with patch("kaggle_runner.setup_checkpoint.get_profile", return_value=type(
                "Profile", (), {"owner": "owner", "token": "synthetic-token"})()), \
             patch("kaggle_runner.setup_checkpoint.KaggleCLI.call", side_effect=RunnerError("401 Unauthorized")):
            rejected = _online_check(self.settings, "alice", None,
                                     {"alias": "alice", "owner": "owner", "credential_source": "configured_file"},
                                     previous)
        self.assertEqual(rejected["status"], "failed")
        self.assertEqual(rejected["last_success_at"], previous["last_success_at"])

    def test_expired_authentication_is_stale_separate_from_local_readiness(self):
        observed, live = observations()
        auth_inputs = {"alias": "alice", "owner": "owner",
                       "credential_configuration": live["credential_configuration"],
                       "credential_source": "configured_file"}
        checkpoint = {"schema_version": 1, "updated_at": "now", "checks": {
            "online_auth:alice": {"status": "verified", "timestamp": "old", "method": "test",
                                  "inputs": auth_inputs,
                                  "last_success_at": (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()}
        }, "history": []}
        self.settings.state.mkdir(parents=True)
        (self.settings.state / "setup-checkpoint.json").write_text(json.dumps(checkpoint))
        live["account_profiles"]["profiles"] = [{"alias": "alice", "owner": "owner"}]
        with patch("kaggle_runner.setup_checkpoint._collect", return_value=(observed, live)):
            result = status(self.settings)
        self.assertEqual(result["checks"]["online_auth:alice"]["status"], "stale")
        self.assertFalse(result["online_auth_ready"])
        self.assertTrue(result["ok"])

    def test_corrupt_checkpoint_is_preserved_by_status_and_verify(self):
        self.settings.state.mkdir(parents=True)
        path = self.settings.state / "setup-checkpoint.json"
        path.write_text("not json")
        with patch("kaggle_runner.setup_checkpoint._collect", return_value=observations()):
            self.assertFalse(status(self.settings)["ok"])
            self.assertFalse(verify(self.settings)["ok"])
        self.assertEqual(path.read_text(), "not json")

    def test_concurrent_verifications_preserve_both_history_entries(self):
        observed, live = observations()
        gate = threading.Barrier(2)
        def collect(*args):
            gate.wait(timeout=5)
            return deepcopy(observed), deepcopy(live)
        with patch("kaggle_runner.setup_checkpoint._collect", side_effect=collect), \
             patch("kaggle_runner.setup_checkpoint._run_offline_tests", return_value=("verified", None)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                outputs = list(pool.map(lambda _: verify(self.settings), range(2)))
        self.assertTrue(all(item["ok"] for item in outputs))
        checkpoint = json.loads((self.settings.state / "setup-checkpoint.json").read_text())
        self.assertEqual(len(checkpoint["history"]), 2)

    def test_state_directory_change_uses_separate_checkpoint(self):
        observed, live = observations()
        with patch("kaggle_runner.setup_checkpoint._collect", return_value=(observed, live)), \
             patch("kaggle_runner.setup_checkpoint._run_offline_tests", return_value=("verified", None)):
            verify(self.settings)
            other = Settings(self.root / "other-state", None, None)
            result = status(other)
        self.assertEqual(result["checkpoint_status"], "missing")
        self.assertFalse(other.state.exists())

    def test_status_is_read_only_and_never_prints_or_queries_secrets(self):
        self.env_file.write_text("KGR_TEAM_PROFILES=alice\nKGR_TEAM_ALICE_USERNAME=owner\nKGR_TEAM_ALICE_API_TOKEN=checkpoint-secret-sentinel\n")
        with patch("kaggle_runner.setup_checkpoint.KaggleCLI.call") as remote:
            result = status(self.settings, str(self.env_file))
        self.assertFalse(self.settings.state.exists())
        self.assertEqual(remote.call_count, 0)
        self.assertNotIn("checkpoint-secret-sentinel", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
