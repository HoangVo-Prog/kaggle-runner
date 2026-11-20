"""Persistent, secret-free setup verification for this local Kaggle Runner install."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Iterator
from urllib.parse import quote

from . import __version__
from .accounts import load_account, preferred_env_file
from .api import KaggleCLI
from .config import RunnerError, Settings
from .team import diagnose_profiles, get_profile

SCHEMA_VERSION = 1
AUTH_MAX_AGE = timedelta(hours=24)
CHECK_STATUSES = {"verified", "stale", "missing", "failed", "unverified"}
SENSITIVE_ENV_NAMES = {".kaggle", "credentials", "kaggle.json",
                       "setup-checkpoint.json", "jobs.sqlite3"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _status(status: str, method: str, *, reason: str | None = None,
            inputs: dict | None = None, **extra) -> dict:
    if status not in CHECK_STATUSES:
        raise ValueError(f"invalid setup check status: {status}")
    result = {"status": status, "timestamp": _now(), "method": method}
    if reason:
        result["reason"] = reason
    if inputs is not None:
        result["inputs"] = inputs
    result.update(extra)
    return result


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+(?:\.\d+)*)", value)
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def _distribution(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _python_identity() -> dict:
    return {"executable": str(Path(sys.executable).resolve()),
            "version": ".".join(map(str, sys.version_info[:3])),
            "implementation": sys.implementation.name,
            "prefix": str(Path(sys.prefix).resolve()),
            "base_prefix": str(Path(sys.base_prefix).resolve()),
            "platform": sys.platform}


def _repo_source_identity() -> dict | None:
    root = Path(__file__).resolve().parent.parent
    if not (root / "pyproject.toml").is_file() or not (root / "tests").is_dir():
        return None
    try:
        safe_root = str(root).replace("\\", "/")
        git_prefix = ["git", "-c", f"safe.directory={safe_root}", "-C", str(root)]
        head_result = subprocess.run([*git_prefix, "rev-parse", "HEAD"],
                                     capture_output=True, text=True, timeout=5, check=False)
        if head_result.returncode:
            return None
        head = head_result.stdout.strip()
        status_result = subprocess.run(
            [*git_prefix, "status", "--porcelain", "--untracked-files=all"],
            capture_output=True, text=True, timeout=5, check=False)
        if status_result.returncode:
            return None
        paths = []
        for line in status_result.stdout.splitlines():
            raw = line[3:].strip().strip('"')
            if " -> " in raw:
                raw = raw.rsplit(" -> ", 1)[-1]
            normalized = raw.replace("\\", "/")
            if (any(part.casefold() in SENSITIVE_ENV_NAMES for part in normalized.split("/"))
                    or any(part.casefold().startswith(".env") or "credential" in part.casefold()
                           or "token" in part.casefold() for part in normalized.split("/"))
                    or "setup-checkpoint.json" in normalized.casefold()
                    or "jobs.sqlite3" in normalized.casefold()):
                continue
            candidate = root / Path(raw)
            try:
                stat = candidate.stat()
                paths.append({"path": normalized, "size": stat.st_size,
                              "mtime_ns": stat.st_mtime_ns, "exists": True})
            except OSError:
                paths.append({"path": normalized, "exists": False})
        metadata_fingerprint = _sha256(json.dumps(paths, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        return {"head": head, "working_tree_fingerprint": metadata_fingerprint,
                "dirty_file_count": len(paths)}
    except (OSError, subprocess.TimeoutExpired):
        return None


def _credential_snapshot(settings: Settings, env_file: str | None) -> tuple[dict, dict, list[str]]:
    path = preferred_env_file(env_file).expanduser().absolute()
    file_info = {"path": str(path), "exists": path.is_file(), "symlink": path.is_symlink()}
    if path.is_file() and not path.is_symlink():
        try:
            stat = path.stat()
            file_info.update({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
            if os.name == "nt":
                file_info["permissions"] = "unverified_windows_acl"
            else:
                file_info["permissions"] = "private" if not stat.st_mode & 0o077 else "too_open"
        except OSError:
            file_info["permissions"] = "unreadable"
    else:
        file_info["permissions"] = "missing" if not path.exists() else "unsafe_symlink"
    config_root = settings.config_dir or Path(os.getenv("KAGGLE_CONFIG_DIR", str(Path.home() / ".kaggle"))).expanduser()
    kaggle_file = config_root / "kaggle.json"
    kaggle_info = {"path": str(kaggle_file), "exists": kaggle_file.is_file(), "symlink": kaggle_file.is_symlink()}
    if kaggle_file.is_file() and not kaggle_file.is_symlink():
        try:
            stat = kaggle_file.stat()
            kaggle_info.update({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                                "permissions": "private" if os.name != "nt" and not stat.st_mode & 0o077
                                else "unverified_windows_acl" if os.name == "nt" else "too_open"})
        except OSError:
            kaggle_info["permissions"] = "unreadable"
    else:
        kaggle_info["permissions"] = "missing" if not kaggle_file.exists() else "unsafe_symlink"

    profile_records: list[dict] = []
    errors: list[str] = []
    try:
        diag = diagnose_profiles(str(path))
        for item in diag["profiles"]:
            profile_records.append({"alias": item["profile"], "owner": item.get("username"),
                                   "token_configured": bool(item.get("token_configured")),
                                   "ready": bool(item.get("ready"))})
            if not item.get("ready"):
                errors.append(f"Profile {item['profile']} needs a username or token configured")
    except RunnerError:
        errors.append("Credential profile file could not be parsed; inspect it locally without printing token values")
    default_configured = bool(os.getenv("KAGGLE_API_TOKEN")) and bool(os.getenv("KAGGLE_OWNER"))
    default_owner = os.getenv("KAGGLE_OWNER")
    try:
        default_account = load_account(str(path))
        if default_account.owner and default_account.token:
            default_configured = True
            default_owner = default_account.owner
    except (RunnerError, OSError, UnicodeError):
        errors.append("Default account configuration could not be read; inspect the selected credential file locally")
    if not profile_records and (default_configured or kaggle_info["exists"]):
        profile_records.append({"alias": "default", "owner": default_owner,
                               "token_configured": default_configured or None, "ready": True,
                               "credential_source": "environment" if default_configured else "kaggle_config"})
    snapshot = {"credential_file": file_info, "kaggle_config_file": kaggle_info, "profiles": profile_records,
                "default_environment_credentials_configured": default_configured}
    return file_info, snapshot, errors


def _sqlite_snapshot(settings: Settings) -> dict:
    path = settings.state / "jobs.sqlite3"
    base = {"state_directory": str(settings.state.resolve()), "database_exists": path.is_file()}
    if not path.is_file():
        return base | {"status": "missing", "reason": "No jobs.sqlite3 exists yet; setup verification will not create or reset job history"}
    try:
        uri = "file:" + quote(str(path.resolve()).replace("\\", "/"), safe="/:\\") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            integrity = con.execute("PRAGMA quick_check").fetchone()[0]
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {}
            for table in ("jobs", "agent_attempts", "agent_leases", "agent_events"):
                if table in tables:
                    columns[table] = sorted(row[1] for row in con.execute(f"PRAGMA table_info({table})"))
            base.update({"status": "verified" if integrity == "ok" else "failed",
                         "integrity": integrity, "tables": sorted(tables), "columns": columns})
            if "jobs" in tables:
                required = {"job_id", "kernel_ref", "status", "account_profile"}
                if not required.issubset(columns.get("jobs", [])):
                    base.update(status="stale", reason="Job schema needs the normal additive migration; preserve and back up jobs.sqlite3 before repair")
            agent_tables = {"agent_attempts", "agent_leases", "agent_events"} & tables
            if agent_tables and agent_tables != {"agent_attempts", "agent_leases", "agent_events"}:
                base.update(status="failed", reason="Agent ledger tables are incomplete; do not reset or recreate jobs.sqlite3")
            return base
        finally:
            con.close()
    except (sqlite3.Error, OSError) as exc:
        return base | {"status": "failed", "reason": f"SQLite is not readable ({type(exc).__name__}); do not reset the database"}


def _collect(settings: Settings, env_file: str | None) -> tuple[dict, dict]:
    py = _python_identity()
    root = Path(__file__).resolve().parent.parent
    pyproject = root / "pyproject.toml"
    dependency_spec = _sha256(pyproject.read_bytes()) if pyproject.is_file() else None
    package_version = _distribution("kaggle-runner") or __version__
    package_path = str(Path(__import__("kaggle_runner").__file__).resolve())
    jsonschema_version = _distribution("jsonschema")
    kaggle_version = _distribution("kaggle")
    kaggle_module = importlib.util.find_spec("kaggle") is not None
    kaggle_executable = shutil.which("kaggle")
    file_info, profile_snapshot, profile_errors = _credential_snapshot(settings, env_file)
    sqlite_info = _sqlite_snapshot(settings)
    source = _repo_source_identity()
    source_test_inputs = {"source": source, "python": py["executable"],
                          "dependency_config_sha256": dependency_spec}

    observations = {
        "python_interpreter": _status(
            "verified" if Path(sys.executable).is_file() and sys.version_info >= (3, 11) else "failed",
            "sys.executable and sys.version_info",
            reason=None if Path(sys.executable).is_file() and sys.version_info >= (3, 11)
            else "Python 3.11+ interpreter is required; select an existing supported environment",
            inputs={"executable": py["executable"], "version": py["version"]}),
        "python_environment": _status(
            "verified" if py["prefix"] else "failed", "sys.prefix/sys.base_prefix",
            reason=None if py["prefix"] else "Python environment identity could not be determined",
            inputs=py),
        "package": _status("verified", "importlib.metadata and package import",
                           inputs={"version": package_version, "module_path": package_path}),
        "dependencies": _status(
            "verified" if jsonschema_version and _version_tuple(jsonschema_version) >= (4, 20) else "failed",
            "installed distribution metadata and pyproject.toml",
            reason=None if jsonschema_version and _version_tuple(jsonschema_version) >= (4, 20)
            else "Install the declared jsonschema>=4.20 dependency only after reviewing the active Python environment",
            inputs={"python": py["executable"], "dependency_config_sha256": dependency_spec,
                    "jsonschema_version": jsonschema_version}),
        "kaggle_cli": _status(
            "verified" if kaggle_module and kaggle_version and _version_tuple(kaggle_version) >= (1, 7) else "missing",
            "importlib.util.find_spec, package metadata and shutil.which",
            reason=None if kaggle_module and kaggle_version and _version_tuple(kaggle_version) >= (1, 7)
            else "Kaggle CLI package is unavailable in this interpreter; local-only commands remain usable",
            inputs={"python": py["executable"], "module_available": kaggle_module,
                    "version": kaggle_version, "executable": kaggle_executable}),
        "credential_file": _status(
            "missing" if not file_info["exists"] else
            "failed" if file_info["symlink"] or file_info["permissions"] in {"too_open", "unreadable", "unsafe_symlink"} else
            "verified" if file_info["permissions"] == "private" else "unverified",
            "credential path metadata and filesystem permission bits; token values are not read into checkpoint",
            reason=("Credential file is missing; select an existing file with --env-file/KGR_ENV_FILE or configure team profiles explicitly"
                    if not file_info["exists"] else
                    "Credential file is symlinked or unreadable/too broadly accessible; inspect locally and repair only with approval"
                    if file_info["symlink"] or file_info["permissions"] in {"too_open", "unreadable", "unsafe_symlink"} else
                    "Windows ACL permissions are not verified by POSIX mode bits" if file_info["permissions"] == "unverified_windows_acl" else None),
            inputs=file_info),
        "kaggle_config_file": _status(
            "missing" if not profile_snapshot["kaggle_config_file"]["exists"] else
            "failed" if profile_snapshot["kaggle_config_file"]["symlink"] or
            profile_snapshot["kaggle_config_file"]["permissions"] in {"too_open", "unreadable", "unsafe_symlink"} else
            "verified" if profile_snapshot["kaggle_config_file"]["permissions"] == "private" else "unverified",
            "KAGGLE_CONFIG_DIR metadata and filesystem permission bits; file contents are not read",
            reason=("Kaggle CLI credential file is symlinked, too broadly accessible, or unreadable" if
                    profile_snapshot["kaggle_config_file"]["symlink"] or
                    profile_snapshot["kaggle_config_file"]["permissions"] in {"too_open", "unreadable", "unsafe_symlink"} else
                    "Windows ACL permissions are not verified by POSIX mode bits" if
                    profile_snapshot["kaggle_config_file"]["permissions"] == "unverified_windows_acl" else
                    "No Kaggle CLI credential file at the selected config path" if
                    not profile_snapshot["kaggle_config_file"]["exists"] else None),
            inputs=profile_snapshot["kaggle_config_file"]),
        "account_profiles": _status(
            "failed" if profile_errors else "verified" if profile_snapshot["profiles"] else "missing",
            "team profile diagnostics (safe fields only)",
            reason=("; ".join(profile_errors) if profile_errors else
                    None if profile_snapshot["profiles"] else "No configured account profile; add one only if remote Kaggle access is needed"),
            inputs=profile_snapshot, profiles=profile_snapshot["profiles"]),
        "sqlite": _status(sqlite_info["status"], "read-only SQLite quick_check and schema inspection",
                          reason=sqlite_info.get("reason"),
                          inputs={k: v for k, v in sqlite_info.items() if k not in {"status", "reason"}},
                          database_exists=sqlite_info["database_exists"]),
        "source_tests": _status(
            "unverified" if source else "unverified", "Git revision and working-tree file metadata",
            reason="Run offline unittest discovery for this source revision" if source else
                   "No repository test tree is available in this installation",
            inputs=source_test_inputs if source else {"available": False},
            revision=source["head"] if source else None),
    }
    live = {"python": py, "dependency_config_sha256": dependency_spec,
            "credential_file": file_info, "account_profiles": profile_snapshot,
            "credential_configuration": {"credential_file": file_info,
                                          "kaggle_config_file": profile_snapshot["kaggle_config_file"]},
            "source": source, "sqlite": sqlite_info}
    return observations, live


def _load_checkpoint(path: Path) -> tuple[dict | None, str | None]:
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION
                or not isinstance(data.get("checks"), dict) or not isinstance(data.get("history"), list)):
            return None, "Checkpoint JSON does not match supported schema version 1; preserve it and inspect before repair"
        return data, None
    except (OSError, ValueError, UnicodeError):
        return None, "Checkpoint is unreadable or corrupt; it was left untouched. Back it up and repair explicitly"


def _stale_status(name: str, observed: dict, saved: dict | None, *, now: datetime) -> dict:
    if observed["status"] in {"failed", "missing"}:
        return observed
    if saved is None:
        return observed | {"status": "missing", "reason": observed.get("reason") or "No successful checkpoint; run kgr setup verify --offline"}
    if saved.get("inputs") != observed.get("inputs"):
        return observed | {"status": "stale", "reason": f"Inputs for {name} changed; verify this component again"}
    if name.startswith("online_auth:"):
        last_success = saved.get("last_success_at")
        if not last_success:
            return saved | {"status": "unverified", "reason": "No successful online authentication check has been recorded"}
        try:
            when = datetime.fromisoformat(last_success)
            if now - when > AUTH_MAX_AGE:
                return saved | {"status": "stale", "reason": "Online authentication check is older than 24 hours; recheck this account"}
        except ValueError:
            return saved | {"status": "stale", "reason": "Online authentication timestamp is invalid; recheck this account"}
    return saved


def status(settings: Settings, env_file: str | None = None) -> dict:
    path = settings.state / "setup-checkpoint.json"
    checkpoint, corrupt = _load_checkpoint(path)
    if corrupt:
        return {"schema_version": SCHEMA_VERSION, "ok": False, "checkpoint_status": "failed",
                "state_directory": str(settings.state), "reason": corrupt,
                "action": "Preserve the file and request operator review; setup status will not overwrite it"}
    observed, live = _collect(settings, env_file)
    old_checks = checkpoint["checks"] if checkpoint else {}
    effective = {name: _stale_status(name, item, old_checks.get(name), now=datetime.now(timezone.utc))
                 for name, item in observed.items()}
    for name, saved in old_checks.items():
        if name.startswith("online_auth:"):
            current = live["account_profiles"].get("profiles", [])
            alias = name.split(":", 1)[1]
            profile = next((p for p in current if p["alias"] == alias), None)
            inputs = {"alias": alias, "owner": profile.get("owner") if profile else None,
                      "credential_configuration": live["credential_configuration"],
                      "credential_source": "environment" if alias == "default" and os.getenv("KAGGLE_API_TOKEN") else "configured_file"}
            if inputs["credential_source"] == "environment" and saved.get("last_success_process_id") != os.getpid():
                effective[name] = saved | {"status": "stale", "reason": "Environment-sourced credentials must be rechecked in this process"}
                continue
            effective[name] = _stale_status(name, {"status": "verified", "timestamp": _now(),
                                                   "method": "credential metadata comparison", "inputs": inputs},
                                               saved, now=datetime.now(timezone.utc))
    core = ("python_interpreter", "python_environment", "package", "dependencies", "source_tests")
    ready = all(effective.get(key, {}).get("status") == "verified" for key in core)
    credential_permissions_verified = any(effective.get(name, {}).get("status") == "verified"
                                          for name in ("credential_file", "kaggle_config_file"))
    remote_ready = (ready and effective.get("kaggle_cli", {}).get("status") == "verified"
                    and effective.get("account_profiles", {}).get("status") == "verified"
                    and credential_permissions_verified)
    return {"schema_version": SCHEMA_VERSION, "ok": True, "checkpoint_status": "verified" if checkpoint else "missing",
            "state_directory": str(settings.state), "ready": ready,
            "remote_prerequisites_ready": remote_ready,
            "last_successfully_tested_git_revision": effective.get("source_tests", {}).get("last_successful_revision"),
            "online_auth_ready": any(name.startswith("online_auth:") and item.get("status") == "verified"
                                      for name, item in effective.items()),
            "history_count": len(checkpoint["history"]) if checkpoint else 0,
            "checks": effective,
            "next_action": None if ready and remote_ready else _next_action(effective, None) if ready else
            "Run kgr setup verify --offline; it rechecks only stale or missing components"}


@contextmanager
def _checkpoint_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            if lock_path.stat().st_size == 0:
                lock.seek(0)
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl as _fcntl
            _fcntl.flock(lock.fileno(), _fcntl.LOCK_EX)
            try:
                yield
            finally:
                _fcntl.flock(lock.fileno(), _fcntl.LOCK_UN)


def _atomic_write(path: Path, document: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".setup-checkpoint-", suffix=".tmp", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _run_offline_tests() -> tuple[str, str | None]:
    root = Path(__file__).resolve().parent.parent
    if not (root / "tests").is_dir():
        return "unverified", "No offline test suite is available in this installation"
    try:
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                                cwd=root, capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "failed", "Offline test command could not complete; inspect local test environment"
    if result.returncode == 0:
        return "verified", None
    return "failed", "Offline regression suite failed; inspect its output by running unittest discovery manually"


def _online_check(settings: Settings, alias: str, env_file: str | None,
                  inputs: dict, previous: dict | None) -> dict:
    method = "read-only Kaggle kernels list --mine --page-size 1"
    stamp = _now()
    try:
        if alias == "default":
            account = load_account(env_file)
            owner, token = account.owner, account.token
        else:
            profile = get_profile(alias, env_file)
            owner, token = profile.owner, profile.token
        client = KaggleCLI(settings, token=token)
        client.call("kernels", "list", "--mine", "--page-size", "1", timeout_seconds=30)
        return _status("verified", method, inputs=inputs, account=alias,
                       last_success_at=stamp, last_success_method=method,
                       last_success_process_id=os.getpid() if inputs.get("credential_source") == "environment" else None)
    except (RunnerError, OSError) as exc:
        detail = str(exc).casefold() if isinstance(exc, RunnerError) else ""
        rejected = any(part in detail for part in ("401", "403", "unauthorized", "invalid token", "authentication failed"))
        missing = any(part in detail for part in ("missing api token", "unknown team profile", "cannot locate"))
        reason = ("Kaggle rejected this account or denied access; inspect the selected profile explicitly" if rejected
                  else "Selected account credentials are missing or incomplete; configure them only with explicit operator approval" if missing
                  else "Online authentication could not be verified (network/service unavailable); local checks remain intact")
        return _status("failed" if rejected else "missing" if missing else "unverified", method, reason=reason, inputs=inputs,
                       account=alias, last_success_at=(previous or {}).get("last_success_at"),
                       last_success_method=(previous or {}).get("last_success_method"),
                       last_success_process_id=(previous or {}).get("last_success_process_id"))


def verify(settings: Settings, *, env_file: str | None = None,
           online_account: str | None = None) -> dict:
    path = settings.state / "setup-checkpoint.json"
    checkpoint, corrupt = _load_checkpoint(path)
    if corrupt:
        return {"schema_version": SCHEMA_VERSION, "ok": False, "checkpoint_status": "failed",
                "state_directory": str(settings.state), "reason": corrupt,
                "action": "Preserve the file and request operator review; verify will not overwrite it"}
    observed, live = _collect(settings, env_file)
    old_checks = checkpoint["checks"] if checkpoint else {}
    current_time = datetime.now(timezone.utc)
    checks = dict(old_checks)
    reused, revalidated, stale_findings = [], [], []
    for name, item in observed.items():
        old = old_checks.get(name)
        same_inputs = old and old.get("inputs") == item.get("inputs")
        can_reuse = bool(same_inputs and (
            (name == "source_tests" and old.get("status") in {"verified", "failed"}) or
            (old.get("status") == item.get("status") and old.get("status") in CHECK_STATUSES)))
        if can_reuse:
            checks[name] = old
            reused.append(name)
            continue
        effective = _stale_status(name, item, old, now=current_time)
        if effective["status"] == "stale":
            stale_findings.append({"check": name, "reason": effective.get("reason")})
        if name == "source_tests" and item.get("revision"):
            prerequisites = ("python_interpreter", "package", "dependencies")
            if all(checks.get(key, {}).get("status") == "verified" for key in prerequisites):
                test_status, reason = _run_offline_tests()
                last_successful = item.get("revision") if test_status == "verified" else (
                    old.get("last_successful_revision") if old else None)
                if not last_successful and old and old.get("status") == "verified":
                    last_successful = old.get("revision")
                item = _status(test_status, "python -m unittest discover -s tests -v",
                               reason=reason, inputs=item["inputs"], revision=item.get("revision"),
                               last_successful_revision=last_successful)
            else:
                item = _status("unverified", "offline test prerequisites", 
                               reason="Resolve interpreter, package and dependency checks before running source tests",
                               inputs=item["inputs"], revision=item.get("revision"),
                               last_successful_revision=(old or {}).get("last_successful_revision"))
        checks[name] = item
        revalidated.append(name)

    if online_account:
        configured_aliases = {p["alias"].casefold() for p in live["account_profiles"]["profiles"]}
        if online_account.casefold() != "default" and online_account.casefold() not in configured_aliases:
            online_record = _status("missing", "explicit account profile selection",
                                    reason=f"Account profile {online_account!r} is not configured in the selected credential file",
                                    inputs={"alias": online_account, "credential_configuration": live["credential_configuration"]},
                                    account=online_account)
            prior_success = old_checks.get(f"online_auth:{online_account}", {}).get("last_success_at")
            if prior_success:
                online_record["last_success_at"] = prior_success
        else:
            profile = next((p for p in live["account_profiles"]["profiles"]
                            if p["alias"].casefold() == online_account.casefold()), None)
            if online_account == "default":
                account = load_account(env_file)
                owner = account.owner
            else:
                owner = profile.get("owner") if profile else None
            inputs = {"alias": online_account, "owner": owner,
                      "credential_configuration": live["credential_configuration"],
                      "credential_source": "environment" if online_account == "default" and os.getenv("KAGGLE_API_TOKEN") else "configured_file"}
            online_record = _online_check(settings, online_account, env_file, inputs,
                                          old_checks.get(f"online_auth:{online_account}"))
        checks[f"online_auth:{online_account}"] = online_record
        revalidated.append(f"online_auth:{online_account}")

    failed_core = [name for name in ("python_interpreter", "python_environment", "package", "dependencies", "source_tests")
                   if checks.get(name, {}).get("status") != "verified"]
    online_ok = not online_account or checks.get(f"online_auth:{online_account}", {}).get("status") == "verified"
    result_ok = not failed_core and online_ok
    entry = {"timestamp": _now(), "mode": "online" if online_account else "offline",
             "revision": live["source"].get("head") if live["source"] else None,
             "revalidated": revalidated, "reused": reused, "stale_findings": stale_findings,
             "check_results": {name: {key: value[key] for key in ("status", "timestamp", "method", "reason", "last_success_at")
                                      if key in value}
                               for name, value in checks.items()},
             "ok": result_ok}

    try:
        with _checkpoint_lock(path):
            latest, latest_corrupt = _load_checkpoint(path)
            if latest_corrupt:
                return {"schema_version": SCHEMA_VERSION, "ok": False, "checkpoint_status": "failed",
                        "reason": latest_corrupt,
                        "action": "Preserve the file and request operator review; verify did not overwrite it"}
            if latest:
                merged = dict(latest.get("checks", {}))
                # Keep concurrent records that this invocation did not refresh.
                merged.update({name: checks[name] for name in revalidated if name in checks})
                for name in reused:
                    merged.setdefault(name, checks[name])
                history = list(latest.get("history", []))
                history.append(entry)
            else:
                merged = checks
                history = [entry]
            document = {"schema_version": SCHEMA_VERSION, "updated_at": _now(),
                        "checks": merged, "history": history}
            _atomic_write(path, document)
    except (OSError, sqlite3.Error) as exc:
        return {"schema_version": SCHEMA_VERSION, "ok": False, "checkpoint_status": "failed",
                "reason": f"Could not write setup checkpoint ({type(exc).__name__}); existing data was preserved",
                "action": "Check state-directory permissions; do not reset the job ledger"}

    report = status(settings, env_file)
    report.update({"ok": result_ok, "checkpoint_status": "verified", "verification": entry,
                   "revalidated": revalidated, "reused": reused,
                   "next_action": _next_action(report["checks"], online_account)})
    return report


def _next_action(checks: dict, online_account: str | None) -> str:
    for name in ("python_interpreter", "python_environment", "package", "dependencies", "source_tests"):
        item = checks.get(name, {})
        if item.get("status") != "verified":
            return item.get("reason") or f"Revalidate {name} in the selected environment"
    if online_account and checks.get(f"online_auth:{online_account}", {}).get("status") != "verified":
        return checks[f"online_auth:{online_account}"].get("reason") or "Verify the explicitly selected account when online"
    if checks.get("kaggle_cli", {}).get("status") != "verified":
        return "Remote Kaggle operations need the Kaggle CLI package in this interpreter; inspect the active environment before an explicitly approved install"
    if checks.get("account_profiles", {}).get("status") != "verified":
        return checks.get("account_profiles", {}).get("reason") or "Configure an explicit account only if remote Kaggle access is needed"
    if not any(checks.get(name, {}).get("status") == "verified"
               for name in ("credential_file", "kaggle_config_file")):
        return "Credential-file permissions are not verified; review the selected file ACL locally before remote use"
    return "Review setup status and resolve only the named failed checks"
