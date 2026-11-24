"""Offline diagnostics; never initialize or modify the real job ledger."""
from __future__ import annotations

from contextlib import closing
import getpass
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import uuid


def probe_directory(path: Path) -> dict:
    """Probe the exact existing path, not a silently selected fallback."""
    result = {"path": str(path), "ok": False, "operator_access": "unverified_cross_identity"}
    scratch = None
    created = False
    try:
        path.stat()
        if not path.is_dir():
            raise NotADirectoryError(str(path))
        scratch = path / (".kgr-probe-" + uuid.uuid4().hex)
        scratch.mkdir()
        created = True
        src = scratch / "write"
        src.write_bytes(b"kgr-local-probe")
        if src.read_bytes() != b"kgr-local-probe":
            raise OSError("Read-back mismatch")
        dest = scratch / "renamed"
        src.rename(dest)
        dest.unlink()
        db = scratch / "probe.sqlite3"
        with closing(sqlite3.connect(db)) as con:
            with con:
                con.execute("CREATE TABLE probe (v INTEGER)")
                con.execute("INSERT INTO probe VALUES (1)")
            if con.execute("SELECT v FROM probe").fetchone()[0] != 1:
                raise sqlite3.DatabaseError("Read-back mismatch")
        db.unlink()
        scratch.rmdir()
        scratch = None
        result.update(ok=True, status="verified", sqlite_scratch="verified")
    except FileNotFoundError:
        result.update(status="missing", action="Confirm the intended directory with the operator; provision it explicitly, never select a fallback ledger")
    except (OSError, sqlite3.Error) as exc:
        result.update(status="failed", error_type=type(exc).__name__, errno=getattr(exc, "errno", None),
                      action="Ask the operator to verify the exact directory and execution identity; do not reset state or alter global permissions")
    finally:
        if scratch is not None and created:
            try:
                # This random, invocation-owned directory contains only probe files.
                for child in scratch.iterdir():
                    child.unlink()
                scratch.rmdir()
            except OSError:
                result.update(ok=False, status="failed", cleanup_failed=True)
    return result


def ledger_readiness(state: Path) -> dict:
    db = state / "jobs.sqlite3"
    try:
        db.stat()
    except FileNotFoundError:
        return {"status": "missing", "objectives": [], "existing_ledger": False}
    except OSError as exc:
        return {"status": "failed", "error_type": type(exc).__name__}
    try:
        # Read only; do not migrate/create schema or open the database in write mode.
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as con:
            if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                return {"status": "failed", "reason": "SQLite quick_check failed"}
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            ids = ([r[0] for r in con.execute("SELECT objective_id FROM work_objectives ORDER BY objective_id")]
                   if "work_objectives" in tables else [])
            return {"status": "verified", "existing_ledger": True, "objectives": ids,
                    "existing_ledger_write_access": "unverified_read_only_probe"}
    except (OSError, sqlite3.Error) as exc:
        return {"status": "failed", "existing_ledger": True, "error_type": type(exc).__name__,
                "action": "Preserve the ledger; check access and SQLite integrity in the intended state context"}


def execution_identity() -> dict:
    # getpass is only an environment hint; Windows whoami reports the process token.
    if os.name == "nt":
        try:
            probe = subprocess.run(["whoami"], capture_output=True, text=True, timeout=5, check=False)
            if probe.returncode == 0 and probe.stdout.strip():
                return {"effective_identity": probe.stdout.strip(), "identity_source": "whoami"}
        except (OSError, subprocess.TimeoutExpired):
            pass
        return {"effective_identity": None, "identity_source": "unverified"}
    return {"effective_uid": os.geteuid() if hasattr(os, "geteuid") else None, "identity_source": "effective_uid"}


def diagnose(settings, *, env_file: str | None = None, audit_dir: str | None = None,
             expected_state_dir: str | None = None) -> dict:
    import kaggle_runner
    state = settings.state
    expected = Path(expected_state_dir).expanduser().resolve() if expected_state_dir else None
    same = expected is None or expected == state.resolve()
    report = {"schema_version": "1.0", "state_directory": str(state),
              "expected_state_directory": str(expected) if expected is not None else None,
              "state_path_matches": same, "python": sys.executable,
              "python_version": platform.python_version(), "os": platform.platform(),
              "identity_hint": getpass.getuser(), **execution_identity(),
              "wsl_hint": "microsoft" in platform.release().lower(),
              "runner_module": str(Path(kaggle_runner.__file__).resolve()),
              "checkout_candidate": str(Path(kaggle_runner.__file__).resolve().parent.parent),
              "kgr_executable": shutil.which("kgr"), "offline": True,
              "operator_access": "unverified_cross_identity", "checks": {}, "blockers": []}
    if not same:
        report.update(ok=False, blockers=["STATE_PATH_DRIFT"], action="Use the original explicit state path; never initialize a replacement ledger")
        return report
    checks = report["checks"]
    checks["state_directory"] = probe_directory(state)
    if audit_dir is not None:
        checks["audit_directory"] = probe_directory(Path(audit_dir).expanduser().resolve())
    checks["ledger"] = ledger_readiness(state)
    # Reuse the existing local diagnostics. Never return raw credentials or subprocess output.
    if checks["state_directory"]["ok"]:
        from . import setup_checkpoint
        try:
            setup = setup_checkpoint.status(settings, env_file)
            checks["setup"] = {k: setup.get(k) for k in ("ok", "checkpoint_status", "ready", "remote_prerequisites_ready", "online_auth_ready")}
            checks["credential_configuration"] = {name: setup.get("checks", {}).get(name, {}).get("status", "unverified") for name in ("credential_file", "kaggle_config_file", "account_profiles")}
        except (OSError, sqlite3.Error) as exc:
            checks["setup"] = {"ok": False, "error_type": type(exc).__name__}
    report["blockers"] = [name for name, check in checks.items() if name in {"state_directory", "audit_directory", "ledger", "setup"} and (check.get("ok") is False or check.get("status") == "failed")]
    report["ok"] = not report["blockers"]
    report["action"] = ("Resolve the listed local blockers in the same state context" if report["blockers"] else "Review setup readiness and existing objectives; this is not remote execution authorization")
    return report
