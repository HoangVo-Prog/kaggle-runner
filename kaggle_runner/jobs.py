"""SQLite-backed local job ledger; safe across separate CLI processes."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from .config import RunnerError


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

    def add(self, record: dict) -> None:
        row = {**record, "created_at": utc_now(), "updated_at": utc_now()}
        fields = ", ".join(row)
        markers = ", ".join("?" for _ in row)
        with self._connect() as con:
            con.execute(f"INSERT INTO jobs ({fields}) VALUES ({markers})", tuple(row.values()))

    def update(self, job_id: str, **fields: str) -> None:
        if not fields:
            return
        if any(key not in {"status", "output_dir", "last_message", "kernel_ref"} for key in fields):
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
        if row is None:
            raise RunnerError(f"Unknown job ID {job_id}. Use `kgr jobs` to list jobs.")
        return dict(row)

    def list(self, limit: int = 30) -> list[dict]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM jobs ORDER BY created_at DESC, job_id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]


def display_job(record: dict) -> dict:
    out = dict(record)
    out["dataset_refs"] = json.loads(out.get("dataset_refs", "[]"))
    return out
