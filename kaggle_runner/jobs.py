"""SQLite-backed local job ledger; safe across separate CLI processes."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from .config import RunnerError

ACTIVE_JOB_STATES = (
    "prepared", "submitting", "submitted", "pending", "queued", "running",
    "initializing", "starting", "cancel_requested", "cancel_acknowledged",
    "status_unavailable", "unknown", "unknown_remote", "new_script",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, state: Path):
        state.mkdir(parents=True, exist_ok=True)
        self.path = state / "jobs.sqlite3"
        with self._connect() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                kernel_ref TEXT NOT NULL,
                source TEXT NOT NULL,
                source_revision TEXT,
                dataset_refs TEXT NOT NULL,
                status TEXT NOT NULL,
                profile TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                staging_dir TEXT NOT NULL,
                output_dir TEXT,
                last_message TEXT
            )""")
            # Forward-only, non-destructive migration of existing SQLite histories.
            cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
            for name in ("account_profile", "account_owner", "account_email"):
                if name not in cols:
                    con.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT")
            con.execute("CREATE INDEX IF NOT EXISTS jobs_created_idx ON jobs(created_at)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def add(self, record: dict, *, admission_limit: int | None = None) -> None:
        row = {**record, "created_at": utc_now(), "updated_at": utc_now()}
        fields = ", ".join(row)
        markers = ", ".join("?" for _ in row)
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA busy_timeout=30000")
            con.execute("BEGIN IMMEDIATE")
            if admission_limit is not None:
                if not 1 <= admission_limit <= 2:
                    raise RunnerError("Invalid local admission limit")
                profile = row.get("account_profile")
                profile_sql = "account_profile IS NULL" if profile is None else "account_profile=?"
                params = () if profile is None else (profile,)
                active = ACTIVE_JOB_STATES
                marks = ",".join("?" for _ in active)
                held_table = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_leases'").fetchone()
                held_count = 0
                held_exclusion = ""
                if held_table:
                    held_count = con.execute(
                        f"SELECT COUNT(*) FROM agent_leases WHERE {profile_sql} AND state='held' AND job_id<>?",
                        (*params, row["job_id"])).fetchone()[0]
                    held_exclusion = "AND job_id NOT IN (SELECT job_id FROM agent_leases WHERE state='held')"
                jobs_count = con.execute(
                    f"SELECT COUNT(*) FROM jobs WHERE {profile_sql} AND status IN ({marks}) "
                    f"AND job_id<>? {held_exclusion}",
                    (*params, *active, row["job_id"])).fetchone()[0]
                if held_count + jobs_count >= admission_limit:
                    raise RunnerError(f"CONCURRENT_SLOT_EXHAUSTED: {profile or 'default'} "
                                      f"has {held_count + jobs_count}/{admission_limit} held slots")
            con.execute(f"INSERT INTO jobs ({fields}) VALUES ({markers})", tuple(row.values()))
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def update(self, job_id: str, **fields: str) -> None:
        if not fields:
            return
        if any(key not in {"status", "output_dir", "last_message", "kernel_ref", "account_profile", "account_owner", "account_email"} for key in fields):
            raise ValueError("Unsupported update field")
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._connect() as con:
            cur = con.execute(f"UPDATE jobs SET {assignments} WHERE job_id = ?", (*fields.values(), job_id))
            if cur.rowcount == 0:
                raise RunnerError(f"Unknown job ID {job_id}")

    def get(self, job_id: str) -> dict:
        with self._connect() as con:
            row = con.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None and len(job_id) >= 8 and all(c in "0123456789abcdef" for c in job_id.lower()):
            with self._connect() as con:
                matches = con.execute("SELECT * FROM jobs WHERE job_id LIKE ? LIMIT 2", (job_id + "%",)).fetchall()
            if len(matches) > 1:
                raise RunnerError(f"Ambiguous job ID prefix {job_id}; use a longer ID")
            if matches:
                row = matches[0]
        if row is None:
            raise RunnerError(f"Unknown job ID {job_id}. Use `kgr jobs` to list jobs.")
        return dict(row)

    def set_kernel_ref(self, job_id: str, kernel_ref: str) -> None:
        """Correct a historical ref; keep the original job, ID and artifacts."""
        record = self.get(job_id)
        if record["kernel_ref"] == kernel_ref:
            return
        self.update(record["job_id"], kernel_ref=kernel_ref)
        manifest_path = Path(record["staging_dir"]).parent / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("job_id") != record["job_id"]:
                    return
                manifest["kernel_ref"] = kernel_ref
                manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            except (OSError, ValueError, TypeError):
                # The SQLite ledger is authoritative; some legacy staging folders
                # were already deleted and must not block a ref correction.
                pass

    def list(self, limit: int = 30) -> list[dict]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM jobs ORDER BY created_at DESC, job_id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]


def display_job(record: dict) -> dict:
    out = dict(record)
    out["dataset_refs"] = json.loads(out.get("dataset_refs", "[]"))
    return out
