"""SQLite atomic admissions and durable attempt/event records for agent jobs.

Coordination is per shared SQLite file, not cross-machine or Kaggle-wide.
Unknown submissions retain their lease indefinitely until proven resolved.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .config import RunnerError
from .jobs import ACTIVE_JOB_STATES

SCHEMA_VERSION = "1.0"
ACTIVE_LEGACY = ACTIVE_JOB_STATES
ALLOWED = {
    "reserved": {"staging", "blocked", "unknown_remote"},
    "staging": {"submitting", "blocked", "unknown_remote"},
    "submitting": {"submitted", "unknown_remote", "running", "remote_complete", "failed"},
    "unknown_remote": {"submitted", "running", "remote_complete", "failed", "cancelled"},
    "submitted": {"running", "remote_complete", "failed", "cancelled", "unknown_remote"},
    "running": {"remote_complete", "failed", "cancelled", "unknown_remote"},
    "remote_complete": {"succeeded", "invalid_result", "fetch_failed"},
    "fetch_failed": {"remote_complete", "succeeded", "invalid_result"},
    "invalid_result": {"succeeded"},
    "blocked": set(), "failed": set(), "cancelled": set(), "succeeded": set(),
}
REMOTE_TERMINAL = {"remote_complete", "failed", "cancelled"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _matching(profile: str | None) -> tuple[str, tuple]:
    return ("IS NULL", ()) if profile is None else ("= ?", (profile,))


class AgentLedger:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._tx(immediate=True) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS agent_attempts (
                experiment_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                job_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
                spec_json TEXT NOT NULL, account_profile TEXT,
                account_owner TEXT NOT NULL, kernel_ref TEXT NOT NULL,
                state TEXT NOT NULL, scientific_status TEXT NOT NULL DEFAULT 'not_validated',
                validation_json TEXT, last_error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (experiment_id, attempt_id)
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS agent_leases (
                job_id TEXT PRIMARY KEY, account_profile TEXT,
                lease_id TEXT NOT NULL, acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
                state TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES agent_attempts(job_id)
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS agent_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL, before_state TEXT, after_state TEXT NOT NULL,
                note TEXT NOT NULL
            )""")
            con.execute("CREATE INDEX IF NOT EXISTS agent_event_job ON agent_events(job_id,event_id)")

    @contextmanager
    def _tx(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        try:
            con.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def get(self, experiment_id: str, attempt_id: str) -> dict | None:
        with self._tx() as con:
            row = con.execute("SELECT * FROM agent_attempts WHERE experiment_id=? AND attempt_id=?",
                              (experiment_id, attempt_id)).fetchone()
            return dict(row) if row else None

    def by_job(self, job_id: str) -> dict:
        with self._tx() as con:
            row = con.execute("SELECT * FROM agent_attempts WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise RunnerError(f"Unrecognized agent job: {job_id}")
        return dict(row)

    def list(self, experiment_id: str | None = None) -> list[dict]:
        with self._tx() as con:
            if experiment_id is None:
                rows = con.execute("SELECT * FROM agent_attempts ORDER BY created_at, attempt_id").fetchall()
            else:
                rows = con.execute("SELECT * FROM agent_attempts WHERE experiment_id=? ORDER BY created_at, attempt_id",
                                   (experiment_id,)).fetchall()
        return [dict(x) for x in rows]

    def reserve(self, *, experiment_id: str, attempt_id: str, fingerprint: str,
                job_id: str, spec: dict, account_profile: str | None,
                account_owner: str, kernel_ref: str, limit: int = 2,
                lease_seconds: int = 600) -> tuple[dict, bool]:
        """One INSERT and occupancy count under BEGIN IMMEDIATE. Never steals expired leases.

        Returns (attempt, was_created). The same logical attempt is never pushed twice.
        """
        if not 1 <= limit <= 2 or lease_seconds < 30:
            raise RunnerError("Invalid local reservation limit or lease duration")
        profile_sql, profile_params = _matching(account_profile)
        stamp = now()
        expiry = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        with self._tx(immediate=True) as con:
            old = con.execute("SELECT * FROM agent_attempts WHERE experiment_id=? AND attempt_id=?",
                              (experiment_id, attempt_id)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise RunnerError("SUBMISSION_CONFLICT: attempt exists with different immutable inputs")
                return dict(old), False
            lease_count = con.execute(f"SELECT COUNT(*) FROM agent_leases WHERE account_profile {profile_sql} AND state='held'",
                                      profile_params).fetchone()[0]
            # Count legacy jobs that do not already hold an agent lease, by EXACT profile.
            # account_profile IS NULL means DEFAULT account exclusively.
            slots = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()
            existing_count = 0
            if slots:
                marks = ",".join("?" for _ in ACTIVE_LEGACY)
                existing_count = con.execute(
                    f"SELECT COUNT(*) FROM jobs WHERE account_profile {profile_sql} "
                    f"AND status IN ({marks}) AND job_id NOT IN (SELECT job_id FROM agent_leases WHERE state='held')",
                    (*profile_params, *ACTIVE_LEGACY)).fetchone()[0]
            if lease_count + existing_count >= limit:
                raise RunnerError(f"CONCURRENT_SLOT_EXHAUSTED: {account_profile or 'default'} "
                                  f"has {lease_count + existing_count}/{limit} held slots")
            con.execute("""INSERT INTO agent_attempts
                (experiment_id,attempt_id,job_id,fingerprint,spec_json,account_profile,
                 account_owner,kernel_ref,state,created_at,updated_at)
                 VALUES (?,?,?,?,?,?,?,?,'reserved',?,?)""",
                        (experiment_id, attempt_id, job_id, fingerprint,
                         json.dumps(spec, sort_keys=True, separators=(",", ":"), allow_nan=False),
                         account_profile, account_owner, kernel_ref, stamp, stamp))
            con.execute("""INSERT INTO agent_leases
                (job_id,account_profile,lease_id,acquired_at,expires_at,heartbeat_at,state)
                VALUES(?,?,?,?,?,?,'held')""",
                        (job_id, account_profile, secrets.token_hex(12), stamp, expiry, stamp))
            con.execute("INSERT INTO agent_events(job_id,occurred_at,before_state,after_state,note) VALUES (?,?,?,?,?)",
                        (job_id, stamp, None, "reserved", "Local capacity atomically reserved"))
            new = con.execute("SELECT * FROM agent_attempts WHERE job_id=?", (job_id,)).fetchone()
            return dict(new), True

    def transition(self, job_id: str, state: str, *, note: str = "",
                   scientific_status: str | None = None, validation: dict | None = None) -> dict:
        if state not in ALLOWED:
            raise RunnerError(f"Unknown agent state {state}")
        note = note[:1000]  # Do not store raw exceptions or credentials.
        with self._tx(immediate=True) as con:
            row = con.execute("SELECT * FROM agent_attempts WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise RunnerError("Unknown agent job")
            old = row["state"]
            if state != old and state not in ALLOWED[old]:
                raise RunnerError(f"Illegal agent state transition {old} -> {state}")
            con.execute("""UPDATE agent_attempts SET state=?,updated_at=?,
                scientific_status=COALESCE(?,scientific_status),
                validation_json=COALESCE(?,validation_json) WHERE job_id=?""",
                        (state, now(), scientific_status,
                         json.dumps(validation, sort_keys=True) if validation is not None else None, job_id))
            if state != old:
                con.execute("INSERT INTO agent_events(job_id,occurred_at,before_state,after_state,note) VALUES (?,?,?,?,?)",
                            (job_id, now(), old, state, note))
            if state in REMOTE_TERMINAL:
                con.execute("UPDATE agent_leases SET state='released' WHERE job_id=?", (job_id,))
            return dict(con.execute("SELECT * FROM agent_attempts WHERE job_id=?", (job_id,)).fetchone())

    def renew(self, job_id: str, seconds: int = 600) -> None:
        expiry = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")
        with self._tx(immediate=True) as con:
            con.execute("UPDATE agent_leases SET expires_at=?,heartbeat_at=? WHERE job_id=? AND state='held'",
                        (expiry, now(), job_id))

    def stale_leases(self) -> list[dict]:
        """For reconciliation, not automatic capacity release."""
        with self._tx() as con:
            rows = con.execute("SELECT * FROM agent_leases WHERE state='held' AND expires_at < ?",
                               (now(),)).fetchall()
        return [dict(r) for r in rows]

    def events(self, job_id: str) -> list[dict]:
        with self._tx() as con:
            return [dict(x) for x in con.execute("SELECT * FROM agent_events WHERE job_id=? ORDER BY event_id",
                                                 (job_id,))]
