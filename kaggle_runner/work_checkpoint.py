"""Persistent objective-level work checkpoints in the shared job SQLite ledger.

This layer stores plans and evidence links only. Experiment/job states remain
authoritative in AgentLedger and are always read from there.
"""
from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import getpass
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import RunnerError, Settings
from .jobs import JobStore
from .source_contract import canonical, inspect_source
from .agent_workflow import load_spec
from .agent_ledger import AgentLedger

OBJECTIVE_STATES = {"draft", "planned", "awaiting_approval", "running", "paused",
                    "blocked", "needs_review", "completed", "failed", "cancelled"}
STEP_STATES = {"todo", "in_progress", "done", "blocked", "skipped", "stale"}
TRANSITIONS = {
    "draft": {"planned", "awaiting_approval", "cancelled"}, "planned": {"awaiting_approval", "running", "paused", "blocked", "cancelled"},
    "awaiting_approval": {"planned", "running", "blocked", "cancelled"},
    "running": {"paused", "blocked", "needs_review", "completed", "failed", "cancelled"},
    "paused": {"planned", "running", "blocked", "cancelled"}, "blocked": {"planned", "running", "paused", "needs_review", "failed", "cancelled"},
    "needs_review": {"planned", "running", "paused", "completed", "failed", "cancelled"},
    "completed": set(), "failed": set(), "cancelled": set(),
}
SECRET_RE = re.compile(r"(?i)(?:api.?token|password|secret|credential|private.?key)")


@dataclass(frozen=True)
class WorkObjective:
    objective_id: str
    title: str
    description: str
    acceptance_criteria: list[str]
    state: str
    revision: int
    owner: str
    metadata: dict
    created_at: str
    updated_at: str
    last_activity: str

    def as_dict(self) -> dict:
        return {"objective_id": self.objective_id, "title": self.title,
                "description": self.description, "acceptance_criteria": self.acceptance_criteria,
                "state": self.state, "revision": self.revision, "owner": self.owner,
                "metadata": self.metadata, "created_at": self.created_at,
                "updated_at": self.updated_at, "last_activity": self.last_activity}


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "[redacted]" if SECRET_RE.search(str(k)) else _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(x) for x in value]
    if isinstance(value, str):
        return re.sub(r"(?i)(KAGGLE_API_TOKEN\s*=\s*)\S+", r"\1[redacted]", value)[:2000]
    return value


def _assert_secret_free(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if SECRET_RE.search(str(key)):
                raise RunnerError("Secret-like fields are not allowed in objective plans or snapshots")
            _assert_secret_free(item)
    elif isinstance(value, list):
        for item in value:
            _assert_secret_free(item)
    elif isinstance(value, str) and re.search(r"(?i)(KAGGLE_API_TOKEN\s*=\s*\S+|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----)", value):
        raise RunnerError("Credential-like values are not allowed in objective plans or snapshots")


class WorkLedger:
    """Additive, transactional work tables sharing Settings.state/jobs.sqlite3."""

    def __init__(self, path: Path, *, create: bool = True):
        self.path = Path(path)
        if create:
            # JobStore owns legacy schema migrations; only additive migrations are made.
            try:
                JobStore(self.path.parent)
                self._migrate()
            except sqlite3.DatabaseError as exc:
                raise RunnerError("Existing jobs.sqlite3 is unreadable or corrupt; it was not reset. Stop, make a copy, and use the database recovery procedure") from exc

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        try:
            yield con
        finally:
            con.close()

    def _migrate(self) -> None:
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("""CREATE TABLE IF NOT EXISTS work_objectives (
                objective_id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
                acceptance_json TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL,
                owner TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, last_activity TEXT NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS work_steps (
                objective_id TEXT NOT NULL, step_id TEXT NOT NULL, title TEXT NOT NULL,
                status TEXT NOT NULL, evidence_json TEXT NOT NULL, dependencies_json TEXT NOT NULL,
                updated_at TEXT NOT NULL, PRIMARY KEY(objective_id,step_id),
                FOREIGN KEY(objective_id) REFERENCES work_objectives(objective_id))""")
            con.execute("""CREATE TABLE IF NOT EXISTS work_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, objective_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL, actor TEXT NOT NULL, event_type TEXT NOT NULL,
                from_state TEXT, to_state TEXT, revision INTEGER NOT NULL, detail_json TEXT NOT NULL,
                FOREIGN KEY(objective_id) REFERENCES work_objectives(objective_id))""")
            con.execute("""CREATE TABLE IF NOT EXISTS work_attempt_links (
                objective_id TEXT NOT NULL, ordinal INTEGER NOT NULL, experiment_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL, spec_path TEXT NOT NULL, spec_sha256 TEXT NOT NULL,
                planned_gpu_hours REAL NOT NULL, job_id TEXT,
                PRIMARY KEY(objective_id,experiment_id,attempt_id), UNIQUE(objective_id,ordinal),
                FOREIGN KEY(objective_id) REFERENCES work_objectives(objective_id))""")
            con.execute("CREATE INDEX IF NOT EXISTS work_objectives_state ON work_objectives(state,updated_at)")
            con.execute("CREATE INDEX IF NOT EXISTS work_events_objective ON work_events(objective_id,event_id)")
            con.commit()

    @staticmethod
    def _obj(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        return WorkObjective(item["objective_id"], item["title"], item["description"],
            json.loads(item["acceptance_json"]), item["state"], item["revision"], item["owner"],
            json.loads(item["metadata_json"]), item["created_at"], item["updated_at"],
            item["last_activity"]).as_dict()

    def get(self, objective_id: str) -> dict:
        if not self.path.exists():
            raise RunnerError(f"Unknown objective: {objective_id}")
        try:
            with self._connect() as con:
                table = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_objectives'").fetchone()
                if table is None:
                    raise RunnerError(f"Unknown objective: {objective_id}")
                row = con.execute("SELECT * FROM work_objectives WHERE objective_id=?", (objective_id,)).fetchone()
        except RunnerError:
            raise
        except sqlite3.DatabaseError as exc:
            raise RunnerError("Existing jobs.sqlite3 is unreadable or corrupt; preserve it and follow the database recovery procedure") from exc
        item = self._obj(row)
        if item is None:
            raise RunnerError(f"Unknown objective: {objective_id}")
        return item

    def list(self, *, active: bool = False) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            with self._connect() as con:
                table = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_objectives'").fetchone()
                if table is None:
                    return []
                query = "SELECT * FROM work_objectives"
                params: tuple = ()
                if active:
                    query += " WHERE state NOT IN ('completed','failed','cancelled')"
                rows = con.execute(query + " ORDER BY updated_at DESC, objective_id", params).fetchall()
        except sqlite3.DatabaseError as exc:
            raise RunnerError("Existing jobs.sqlite3 is unreadable or corrupt; preserve it and follow the database recovery procedure") from exc
        return [self._obj(row) for row in rows]

    def create(self, *, title: str, description: str = "", acceptance: list[str] | None = None,
               metadata: dict | None = None, objective_id: str | None = None) -> dict:
        if not title.strip() or len(title) > 200:
            raise RunnerError("Objective title must be 1-200 characters")
        acceptance = acceptance or []
        if not isinstance(acceptance, list) or any(not isinstance(x, str) or not x.strip() for x in acceptance):
            raise RunnerError("acceptance_criteria must be a list of non-empty strings")
        _assert_secret_free({"title": title, "description": description,
                             "acceptance_criteria": acceptance, "metadata": metadata or {}})
        if objective_id is None:
            slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:36] or "objective"
            objective_id = f"obj-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{slug}-{secrets.token_hex(3)}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,119}", objective_id):
            raise RunnerError("Invalid objective_id")
        now = stamp()
        safe_meta = _redact(metadata or {})
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute("""INSERT INTO work_objectives
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (objective_id, title.strip(), description, _json(acceptance), "draft", 1,
                     getpass.getuser(), _json(safe_meta), now, now, now))
                con.execute("""INSERT INTO work_events(objective_id,occurred_at,actor,event_type,
                    from_state,to_state,revision,detail_json) VALUES(?,?,?,?,?,?,?,?)""",
                    (objective_id, now, getpass.getuser(), "created", None, "draft", 1, "{}"))
                con.commit()
            except sqlite3.IntegrityError as exc:
                con.rollback()
                if objective_id is not None:
                    with self._connect() as check:
                        old = check.execute("SELECT * FROM work_objectives WHERE objective_id=?", (objective_id,)).fetchone()
                    prior = self._obj(old)
                    if (prior and prior["title"] == title.strip() and prior["description"] == description
                            and prior["acceptance_criteria"] == acceptance and prior["metadata"] == safe_meta):
                        return prior
                raise RunnerError("Objective ID already exists with different inputs") from exc
        return self.get(objective_id)

    def change(self, objective_id: str, *, expected_revision: int,
               state: str | None = None, metadata: dict | None = None,
               event_type: str = "updated", detail: dict | None = None) -> dict:
        if state is not None and state not in OBJECTIVE_STATES:
            raise RunnerError(f"Unknown objective state: {state}")
        now = stamp()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM work_objectives WHERE objective_id=?", (objective_id,)).fetchone()
            if row is None:
                con.rollback()
                raise RunnerError(f"Unknown objective: {objective_id}")
            if row["revision"] != expected_revision:
                con.rollback()
                raise RunnerError(f"REVISION_CONFLICT: expected {expected_revision}, current {row['revision']}")
            old_state = row["state"]
            new_state = state or old_state
            if new_state != old_state and new_state not in TRANSITIONS[old_state]:
                con.rollback()
                raise RunnerError(f"Illegal objective transition {old_state} -> {new_state}")
            meta = json.loads(row["metadata_json"])
            if metadata is not None:
                meta.update(_redact(metadata))
            revision = row["revision"] + 1
            con.execute("""UPDATE work_objectives SET state=?,revision=?,metadata_json=?,
                updated_at=?,last_activity=? WHERE objective_id=? AND revision=?""",
                (new_state, revision, _json(meta), now, now, objective_id, expected_revision))
            if con.execute("SELECT changes()").fetchone()[0] != 1:
                con.rollback()
                raise RunnerError("REVISION_CONFLICT: objective changed concurrently")
            con.execute("""INSERT INTO work_events(objective_id,occurred_at,actor,event_type,
                from_state,to_state,revision,detail_json) VALUES(?,?,?,?,?,?,?,?)""",
                (objective_id, now, getpass.getuser(), event_type, old_state, new_state,
                 revision, _json(_redact(detail or {}))))
            con.commit()
        return self.get(objective_id)

    def put_plan(self, objective_id: str, *, expected_revision: int, plan: dict,
                 steps: list[dict], attempts: list[dict]) -> dict:
        obj = self.get(objective_id)
        if obj["revision"] != expected_revision:
            raise RunnerError(f"REVISION_CONFLICT: expected {expected_revision}, current {obj['revision']}")
        if obj["state"] not in {"draft", "planned", "paused", "blocked", "awaiting_approval"}:
            raise RunnerError(f"Cannot replace plan while objective is {obj['state']}")
        if not obj["acceptance_criteria"]:
            raise RunnerError("Plan requires explicit objective acceptance_criteria")
        if not steps:
            raise RunnerError("Plan must include at least one checklist step")
        ids = [s["step_id"] for s in steps]
        if len(ids) != len(set(ids)) or any(s["status"] not in STEP_STATES for s in steps):
            raise RunnerError("Checklist step IDs must be unique and use a valid status")
        step_ids = set(ids)
        for s in steps:
            if any(dep not in step_ids for dep in s.get("dependencies", [])):
                raise RunnerError(f"Unknown checklist dependency in {s['step_id']}")
        if not attempts:
            raise RunnerError("Plan must contain at least one attempt")
        now = stamp()
        safe_plan = _redact(plan)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT revision,state,metadata_json FROM work_objectives WHERE objective_id=?", (objective_id,)).fetchone()
            if row is None or row["revision"] != expected_revision:
                con.rollback()
                raise RunnerError("REVISION_CONFLICT: objective changed while the plan was being validated")
            meta = json.loads(row["metadata_json"])
            meta["plan"] = safe_plan
            meta["planned_gpu_hours"] = float(safe_plan.get("planned_gpu_hours", 0.0))
            meta["concurrency_limit"] = safe_plan.get("max_concurrent")
            meta["polling_seconds"] = safe_plan.get("polling_seconds")
            meta["timeout_policy"] = safe_plan.get("timeout_minutes")
            meta["failure_policy"] = safe_plan.get("failure_policy", meta.get("failure_policy"))
            meta["partial_completion_policy"] = safe_plan.get("partial_completion_policy", meta.get("partial_completion_policy"))
            next_state = "awaiting_approval" if plan.get("approval_required", True) else "planned"
            if next_state != row["state"] and next_state not in TRANSITIONS[row["state"]]:
                con.rollback()
                raise RunnerError(f"Illegal objective transition {row['state']} -> {next_state}")
            revision = expected_revision + 1
            con.execute("UPDATE work_objectives SET state=?,revision=?,metadata_json=?,updated_at=?,last_activity=? WHERE objective_id=?",
                        (next_state, revision, _json(meta), now, now, objective_id))
            # Plan replacement is permitted only before any linked job exists.
            old_jobs = con.execute("SELECT COUNT(*) FROM work_attempt_links WHERE objective_id=? AND job_id IS NOT NULL", (objective_id,)).fetchone()[0]
            if old_jobs:
                con.rollback()
                raise RunnerError("Plan is immutable after any attempt has a job; create a new objective for changed inputs")
            con.execute("DELETE FROM work_attempt_links WHERE objective_id=?", (objective_id,))
            con.execute("DELETE FROM work_steps WHERE objective_id=?", (objective_id,))
            for s in steps:
                con.execute("INSERT INTO work_steps VALUES(?,?,?,?,?,?,?)",
                            (objective_id, s["step_id"], s["title"], s["status"],
                             _json(_redact(s.get("evidence", []))), _json(s.get("dependencies", [])), now))
            for index, a in enumerate(attempts):
                con.execute("INSERT INTO work_attempt_links VALUES(?,?,?,?,?,?,?,NULL)",
                            (objective_id, index, a["experiment_id"], a["attempt_id"],
                             str(a["spec_path"]), a["spec_sha256"], float(a["planned_gpu_hours"])))
            con.execute("INSERT INTO work_events(objective_id,occurred_at,actor,event_type,from_state,to_state,revision,detail_json) VALUES(?,?,?,?,?,?,?,?)",
                        (objective_id, now, getpass.getuser(), "plan_created", row["state"], next_state,
                         revision, _json({"attempt_count": len(attempts), "step_count": len(steps)})))
            con.commit()
        return self.get(objective_id)

    def steps(self, objective_id: str) -> list[dict]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM work_steps WHERE objective_id=? ORDER BY rowid", (objective_id,)).fetchall()
        return [{"step_id": r["step_id"], "title": r["title"], "status": r["status"],
                 "evidence": json.loads(r["evidence_json"]), "dependencies": json.loads(r["dependencies_json"]),
                 "updated_at": r["updated_at"]} for r in rows]

    def links(self, objective_id: str) -> list[dict]:
        with self._connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM work_attempt_links WHERE objective_id=? ORDER BY ordinal", (objective_id,))]

    def events(self, objective_id: str) -> list[dict]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM work_events WHERE objective_id=? ORDER BY event_id", (objective_id,)).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail_json"])} for r in rows]

    def checkpoint_step(self, objective_id: str, step_id: str, *, expected_revision: int,
                        status: str, evidence: list[dict] | None = None) -> dict:
        if status not in STEP_STATES:
            raise RunnerError(f"Invalid step status: {status}")
        now = stamp()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            obj = con.execute("SELECT revision,state FROM work_objectives WHERE objective_id=?", (objective_id,)).fetchone()
            if obj is None:
                con.rollback(); raise RunnerError(f"Unknown objective: {objective_id}")
            if obj["revision"] != expected_revision:
                con.rollback(); raise RunnerError(f"REVISION_CONFLICT: expected {expected_revision}, current {obj['revision']}")
            row = con.execute("SELECT * FROM work_steps WHERE objective_id=? AND step_id=?", (objective_id, step_id)).fetchone()
            if row is None:
                con.rollback(); raise RunnerError(f"Unknown work step: {step_id}")
            dependencies = json.loads(row["dependencies_json"])
            if status == "done" and dependencies:
                marks = ",".join("?" for _ in dependencies)
                done = {r[0] for r in con.execute(f"SELECT step_id FROM work_steps WHERE objective_id=? AND status='done' AND step_id IN ({marks})", (objective_id, *dependencies))}
                if len(done) != len(dependencies):
                    con.rollback(); raise RunnerError("Cannot complete step before its dependencies are done")
            merged = json.loads(row["evidence_json"])
            additions = _redact(evidence or [])
            known = {_json(item) for item in merged}
            additions = [item for item in additions if _json(item) not in known]
            if row["status"] == status and not additions:
                con.rollback()
                return self.get(objective_id)
            merged.extend(additions)
            con.execute("UPDATE work_steps SET status=?,evidence_json=?,updated_at=? WHERE objective_id=? AND step_id=?",
                        (status, _json(merged), now, objective_id, step_id))
            revision = expected_revision + 1
            con.execute("UPDATE work_objectives SET revision=?,updated_at=?,last_activity=? WHERE objective_id=? AND revision=?",
                        (revision, now, now, objective_id, expected_revision))
            con.execute("INSERT INTO work_events(objective_id,occurred_at,actor,event_type,from_state,to_state,revision,detail_json) VALUES(?,?,?,?,?,?,?,?)",
                        (objective_id, now, getpass.getuser(), "step_checkpointed", obj["state"], obj["state"], revision,
                         _json({"step_id": step_id, "status": status, "evidence_count": len(evidence or [])})))
            con.commit()
        return self.get(objective_id)

    def link_job(self, objective_id: str, experiment_id: str, attempt_id: str, job_id: str) -> None:
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute("UPDATE work_attempt_links SET job_id=COALESCE(job_id,?) WHERE objective_id=? AND experiment_id=? AND attempt_id=? AND (job_id IS NULL OR job_id=?)",
                              (job_id, objective_id, experiment_id, attempt_id, job_id))
            if cur.rowcount != 1:
                con.rollback(); raise RunnerError("Attempt link is missing or conflicts with another job ID")
            con.commit()


def create(settings: Settings, *, title: str, description: str = "", acceptance: list[str] | None = None,
           objective_id: str | None = None, authorized_profiles: list[str] | None = None,
           approval_policy: dict | None = None) -> dict:
    meta = {"authorized_profiles": authorized_profiles or [], "approval_policy": approval_policy or {"required": True},
            "current_blocker": None, "pending_operator_decision": None, "planned_gpu_hours": 0.0,
            "observed_gpu_hours": None, "concurrency_limit": None, "polling_seconds": None,
            "timeout_policy": None, "failure_policy": "stop_and_review", "partial_completion_policy": "report_validated_attempts",
            "last_successful_report": None}
    return WorkLedger(settings.state / "jobs.sqlite3").create(title=title, description=description,
        acceptance=acceptance, metadata=meta, objective_id=objective_id)


def _expanded_plan(plan: dict) -> tuple[list[dict], list[dict], dict]:
    # A work objective may contain several distinct scientific configurations.
    # Each experiment still expands to stable per-seed attempts and remains a
    # separate aggregation group in the authoritative experiment ledger.
    experiments = plan.get("experiments")
    if experiments is not None:
        if not isinstance(experiments, list) or not experiments:
            raise RunnerError("experiments must be a non-empty list")
        budget = plan.get("total_gpu_hours")
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget <= 0:
            raise RunnerError("A positive total_gpu_hours is required")
        shared = {k: deepcopy(v) for k, v in plan.items() if k not in {"experiments", "checklist"}}
        all_attempts: list[dict] = []
        ids: list[str] = []
        per_experiment: list[dict] = []
        for item in experiments:
            if not isinstance(item, dict) or not isinstance(item.get("template"), dict):
                raise RunnerError("Each experiment requires a template ExperimentSpec and seeds")
            local = {**deepcopy(shared), **deepcopy(item)}
            local.pop("checklist", None)
            attempts, _, summary = _expanded_plan(local)
            experiment_id = item["template"].get("experiment_id")
            if experiment_id in ids:
                raise RunnerError("Each planned experiment must have a unique experiment_id")
            ids.append(experiment_id)
            all_attempts.extend(attempts)
            per_experiment.append(summary)
        if len(all_attempts) > 100:
            raise RunnerError("A plan may contain at most 100 attempts")
        planned_hours = sum(x["planned_gpu_hours"] for x in per_experiment)
        if planned_hours > budget:
            raise RunnerError("Planned attempts exceed the approved total GPU-hour budget")
        steps = plan.get("checklist")
        if steps is None:
            steps = [{"step_id": "source_review", "title": "Review pinned source and refs", "status": "todo", "dependencies": []},
                     {"step_id": "execute_attempts", "title": "Execute planned attempts", "status": "todo", "dependencies": ["source_review"]},
                     {"step_id": "validate_results", "title": "Validate scientific results", "status": "todo", "dependencies": ["execute_attempts"]},
                     {"step_id": "aggregate_report", "title": "Aggregate compatible attempts and report", "status": "todo", "dependencies": ["validate_results"]}]
        if not isinstance(steps, list):
            raise RunnerError("checklist must be a list")
        summary = {k: plan.get(k) for k in ("total_gpu_hours", "max_concurrent", "polling_seconds", "timeout_minutes", "failure_policy", "partial_completion_policy") if k in plan}
        summary.update({"attempt_count": len(all_attempts), "experiment_ids": ids,
                        "planned_gpu_hours": planned_hours,
                        "authorized_profiles": sorted({profile for item in per_experiment for profile in item["authorized_profiles"]}),
                        "max_concurrent": max(item["max_concurrent"] for item in per_experiment),
                        "experiments": [{"experiment_id": exp_id,
                                         "attempt_count": item["attempt_count"],
                                         "planned_gpu_hours": item["planned_gpu_hours"],
                                         "accelerator": item["accelerator"]}
                                        for exp_id, item in zip(ids, per_experiment)]})
        return all_attempts, steps, summary

    template = plan.get("template")
    seeds = plan.get("seeds")
    if not isinstance(template, dict) or not isinstance(seeds, list) or not seeds:
        raise RunnerError("Plan requires template ExperimentSpec and non-empty seeds")
    if len(seeds) > 100:
        raise RunnerError("A plan may contain at most 100 attempts")
    if any(type(seed) is not int or seed < 0 or seed > 2**63 - 1 for seed in seeds) or len(set(seeds)) != len(seeds):
        raise RunnerError("Plan seeds must be unique nonnegative integers")
    if template.get("schema_version") != "1.0":
        raise RunnerError("Plan template must be ExperimentSpec schema_version 1.0")
    _assert_secret_free(template)
    resources = template.setdefault("resources", {})
    accelerator = resources.get("accelerator", "cpu")
    if accelerator not in {"cpu", "t4x2"}:
        raise RunnerError("Unsupported accelerator")
    max_concurrent = plan.get("max_concurrent", resources.get("max_concurrent", 1))
    if type(max_concurrent) is not int or max_concurrent < 1 or max_concurrent > 2:
        raise RunnerError("max_concurrent must be 1 or 2")
    budget = plan.get("total_gpu_hours")
    per_run = plan.get("planned_gpu_hours_per_attempt", resources.get("required_gpu_hours", .25))
    if (isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget <= 0
            or isinstance(per_run, bool) or not isinstance(per_run, (int, float)) or not math.isfinite(per_run) or per_run <= 0):
        raise RunnerError("A positive total_gpu_hours and planned_gpu_hours_per_attempt are required")
    if accelerator != "t4x2" and budget < 0:
        raise RunnerError("Invalid GPU budget")
    if accelerator == "t4x2" and per_run * len(seeds) > budget:
        raise RunnerError("Planned attempts exceed the approved total GPU-hour budget")
    if "polling_seconds" in plan and (type(plan["polling_seconds"]) is not int or plan["polling_seconds"] < 1):
        raise RunnerError("polling_seconds must be a positive integer")
    if "timeout_minutes" in plan and (type(plan["timeout_minutes"]) is not int or plan["timeout_minutes"] < 1):
        raise RunnerError("timeout_minutes must be a positive integer")
    if plan.get("failure_policy", "stop_and_review") != "stop_and_review":
        raise RunnerError("Only the fail-closed stop_and_review failure policy is currently supported")
    if plan.get("partial_completion_policy", "report_validated_attempts") not in {"report_validated_attempts", "allow_explicit_partial"}:
        raise RunnerError("Unsupported partial_completion_policy")
    allowed = template.get("authorization", {}).get("allowed_profiles")
    if not isinstance(allowed, list) or not allowed:
        raise RunnerError("Template requires explicit authorization.allowed_profiles")
    attempts, specs = [], []
    exp_id = template["experiment_id"]
    for index, seed in enumerate(seeds, 1):
        spec = deepcopy(template)
        spec["seed"] = seed
        spec["attempt_id"] = f"run-{index:02d}-seed-{seed}"
        spec.setdefault("resources", {})["max_concurrent"] = max_concurrent
        # The runner submits its per-attempt required estimate to fresh preflight.
        spec["resources"]["required_gpu_hours"] = float(per_run)
        attempts.append({"experiment_id": exp_id, "attempt_id": spec["attempt_id"],
                         "spec": spec, "planned_gpu_hours": float(per_run)})
    steps = plan.get("checklist")
    if steps is None:
        steps = [{"step_id": "source_review", "title": "Review pinned source and refs", "status": "todo", "dependencies": []},
                 {"step_id": "execute_attempts", "title": "Execute planned attempts", "status": "todo", "dependencies": ["source_review"]},
                 {"step_id": "validate_results", "title": "Validate scientific results", "status": "todo", "dependencies": ["execute_attempts"]},
                 {"step_id": "aggregate_report", "title": "Aggregate compatible attempts and report", "status": "todo", "dependencies": ["validate_results"]}]
    if not isinstance(steps, list):
        raise RunnerError("checklist must be a list")
    summary = {k: plan.get(k) for k in ("total_gpu_hours", "planned_gpu_hours_per_attempt", "max_concurrent", "polling_seconds", "timeout_minutes", "failure_policy", "partial_completion_policy") if k in plan}
    summary["approval_required"] = plan.get("approval_required", True)
    summary.update({"attempt_count": len(attempts), "accelerator": accelerator,
                    "max_concurrent": max_concurrent,
                    "planned_gpu_hours_per_attempt": float(per_run),
                    "total_gpu_hours": float(budget), "authorized_profiles": allowed,
                    "experiment_ids": [exp_id], "planned_gpu_hours": per_run * len(attempts)})
    return attempts, steps, summary


def _spec_comparison(value: dict) -> dict:
    item = deepcopy(value)
    source = item.get("source", {})
    source.pop("sha256", None)
    source.pop("revision", None)
    for key in ("datasets", "models"):
        item[key] = [x if isinstance(x, dict) else {"ref": x} for x in item.get(key, [])]
    return item


def _check_snapshot_source(spec_path: Path) -> dict:
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"),
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        source = spec["source"]
        inspected = inspect_source(source["root"], entry=source["entrypoint"],
            dataset_refs=[x["ref"] if isinstance(x, dict) else x for x in spec.get("datasets", [])],
            model_refs=[x["ref"] if isinstance(x, dict) else x for x in spec.get("models", [])],
            max_mb=source.get("max_source_mb", 3), include=source.get("include"), exclude=source.get("exclude"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RunnerError("Immutable attempt snapshot is unreadable; preserve it and request review") from exc
    if source.get("sha256") != inspected["source_sha256"]:
        raise RunnerError("SOURCE_REVISION_MISMATCH: source content changed after plan approval")
    if source.get("revision") and source["revision"] != inspected["source_revision"]:
        raise RunnerError("SOURCE_REVISION_MISMATCH: repository revision changed after plan approval")
    return spec


def plan(settings: Settings, objective_id: str, plan_file: str | Path, *, expected_revision: int) -> dict:
    path = Path(plan_file).expanduser().resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"),
                         parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (OSError, ValueError, UnicodeError) as exc:
        raise RunnerError("Cannot read work plan JSON") from exc
    if not isinstance(raw, dict):
        raise RunnerError("Plan must be a JSON object")
    ledger = WorkLedger(settings.state / "jobs.sqlite3")
    obj = ledger.get(objective_id)
    plan_hash = hashlib.sha256(canonical(raw)).hexdigest()
    saved_plan = obj["metadata"].get("plan")
    if saved_plan:
        if saved_plan.get("plan_sha256") == plan_hash:
            return show(settings, objective_id)
        raise RunnerError("Persisted plans are immutable; create a new objective for changed source, model, dataset, protocol or parameters")
    attempts, steps, summary = _expanded_plan(raw)
    if obj["metadata"].get("authorized_profiles") and set(summary["authorized_profiles"]) - set(obj["metadata"]["authorized_profiles"]):
        raise RunnerError("Plan exceeds the objective's authorized profile allowlist")
    if obj["acceptance_criteria"] == []:
        raise RunnerError("Create objective with explicit acceptance criteria before planning")
    planned = []
    snapshot_dir = settings.state / "work" / objective_id / "specs"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(attempts):
        spec = item["spec"]
        source_root = Path(spec["source"]["root"]).expanduser()
        if not source_root.is_absolute():
            source_root = (path.parent / source_root).resolve()
        spec["source"]["root"] = str(source_root.resolve())
        # Resolve through the existing ExperimentSpec validator. A temp snapshot is
        # written first because load_spec validates a file path and source contract.
        filename = f"{item['experiment_id']}-{item['attempt_id']}.json"
        spec_path = snapshot_dir / filename
        if spec_path.exists():
            try:
                existing = json.loads(spec_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RunnerError("Existing planned spec is corrupt; preserve it for operator review") from exc
            if _json(_spec_comparison(existing)) != _json(_spec_comparison(spec)):
                raise RunnerError("Plan conflicts with an existing immutable attempt snapshot")
        else:
            # Existing plans can be retried idempotently; new snapshots are exclusive.
            try:
                with spec_path.open("x", encoding="utf-8", newline="\n") as stream:
                    json.dump(spec, stream, indent=2, ensure_ascii=False)
                    stream.write("\n")
            except FileExistsError:
                raise RunnerError("Concurrent attempt plan creation; retry work show")
        normalized, inspected = load_spec(spec_path)
        # Pin the inspected source content into the immutable snapshot so later
        # source/model/dataset changes fail closed before execution.
        with spec_path.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(normalized, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        digest = hashlib.sha256(spec_path.read_bytes()).hexdigest()
        planned.append({"experiment_id": item["experiment_id"], "attempt_id": item["attempt_id"],
                        "spec_path": spec_path, "spec_sha256": digest,
                        "planned_gpu_hours": item["planned_gpu_hours"]})
    # Nothing mutates the objective if a later attempt failed validation.
    summary["acceptance_criteria"] = obj["acceptance_criteria"]
    summary["source_sha256"] = inspected["source_sha256"] if planned else None
    updated = ledger.put_plan(objective_id, expected_revision=expected_revision,
                              plan=summary | {"plan_sha256": plan_hash},
                              steps=steps, attempts=planned)
    return show(settings, objective_id)


def _linked_attempts(settings: Settings, links: list[dict]) -> list[dict]:
    ledger_path = settings.state / "jobs.sqlite3"
    out = []
    source_checks: dict[tuple, bool] = {}
    for link in links:
        spec_file = Path(link["spec_path"])
        item = {k: link[k] for k in ("ordinal", "experiment_id", "attempt_id", "job_id", "planned_gpu_hours")}
        item["plan_status"] = "planned"
        item["attempt"] = None
        if link["job_id"] and ledger_path.exists():
            actual = AgentLedger(ledger_path).by_job(link["job_id"])
            item["attempt"] = {k: actual.get(k) for k in ("job_id", "state", "scientific_status", "account_profile", "account_owner", "kernel_ref", "updated_at")}
            item["attempt"]["validation"] = _redact(json.loads(actual["validation_json"])) if actual.get("validation_json") else None
            item["attempt"]["events"] = AgentLedger(ledger_path).events(link["job_id"])
            item["plan_status"] = "linked"
        elif spec_file.is_file():
            item["spec_available"] = hashlib.sha256(spec_file.read_bytes()).hexdigest() == link["spec_sha256"]
            if item["spec_available"]:
                try:
                    raw = json.loads(spec_file.read_text(encoding="utf-8"))
                    src = raw["source"]
                    identity = (src.get("root"), src.get("entrypoint"), src.get("sha256"), src.get("revision"),
                                tuple(src.get("include") or ()), tuple(src.get("exclude") or ()))
                    current = source_checks.get(identity)
                    if current is None:
                        try:
                            _check_snapshot_source(spec_file)
                            source_checks[identity] = True
                        except RunnerError:
                            source_checks[identity] = False
                            raise
                    elif not current:
                        raise RunnerError("Pinned source or specification inputs changed after planning")
                    item["plan_status"] = "ready"
                except RunnerError:
                    item["plan_status"] = "stale"
                    item["stale_reason"] = "Pinned source or specification inputs changed after planning"
            else:
                item["plan_status"] = "stale"
        else:
            item["plan_status"] = "stale"
        out.append(item)
    return out


def show(settings: Settings, objective_id: str) -> dict:
    ledger = WorkLedger(settings.state / "jobs.sqlite3", create=False)
    obj = ledger.get(objective_id)
    steps = ledger.steps(objective_id)
    link_rows = ledger.links(objective_id)
    attempts = _linked_attempts(settings, link_rows)
    if any(a["plan_status"] == "stale" for a in attempts):
        for step in steps:
            if step["status"] != "skipped":
                step["status"] = "stale"
    spec_paths = {link["attempt_id"]: link["spec_path"] for link in link_rows}
    planned = obj["metadata"].get("planned_gpu_hours", 0.0)
    valid = sum(1 for a in attempts if (a.get("attempt") or {}).get("scientific_status") == "succeeded")
    observed = 0.0
    has_observed = False
    for item in attempts:
        attempt = item.get("attempt") or {}
        validation = attempt.get("validation") or {}
        runtime = validation.get("runtime_seconds")
        if validation.get("ok") is True and isinstance(runtime, (int, float)) and runtime >= 0:
            try:
                spec = json.loads(Path(spec_paths[item["attempt_id"]]).read_text(encoding="utf-8"))
                multiplier = 2 if spec.get("resources", {}).get("accelerator") == "t4x2" else 0
                observed += runtime / 3600.0 * multiplier
                has_observed = True
            except (OSError, ValueError, StopIteration):
                pass
    outstanding = [s for s in steps if s["status"] not in {"done", "skipped"}]
    next_action = obj["metadata"].get("next_action")
    if not next_action:
        if obj["state"] == "awaiting_approval": next_action = "Review the plan and explicitly approve it with kgr work approve"
        elif obj["state"] in {"draft", "planned"}: next_action = "Create or review the persisted plan"
        elif any(a["plan_status"] == "stale" for a in attempts): next_action = "Request operator review of changed or missing immutable attempt specs"
        elif any((a.get("attempt") or {}).get("state") == "unknown_remote" for a in attempts): next_action = "Reconcile ambiguous remote submissions before any retry"
        elif outstanding: next_action = f"Continue checklist step {outstanding[0]['step_id']}"
        else: next_action = "Review acceptance criteria and evidence before completion"
    return {"schema_version": 1, "ok": True, "objective": obj,
            "steps": steps, "attempts": attempts,
            "progress": {"steps_done": sum(s["status"] == "done" for s in steps), "steps_total": len(steps),
                         "planned_attempts": len(attempts), "linked_jobs": sum(bool(a["job_id"]) for a in attempts),
                         "scientifically_validated_attempts": valid, "planned_gpu_hours": planned,
                         "observed_gpu_hours": observed if has_observed else None},
            "current_blocker": obj["metadata"].get("current_blocker"),
            "pending_operator_decision": obj["metadata"].get("pending_operator_decision"),
            "next_action": next_action, "timeline": ledger.events(objective_id)}


def status(settings: Settings, objective_id: str) -> dict:
    result = show(settings, objective_id)
    return {k: result[k] for k in ("schema_version", "ok", "objective", "progress", "current_blocker",
                                  "pending_operator_decision", "next_action")}


def list_work(settings: Settings, *, active: bool = False) -> dict:
    ledger = WorkLedger(settings.state / "jobs.sqlite3", create=False)
    return {"schema_version": 1, "ok": True,
            "objectives": [{"objective_id": x["objective_id"], "title": x["title"], "state": x["state"],
                            "revision": x["revision"], "last_activity": x["last_activity"],
                            "next_action": x["metadata"].get("next_action")} for x in ledger.list(active=active)]}


def checkpoint(settings: Settings, objective_id: str, step_id: str, *, expected_revision: int,
               step_status: str, evidence: list[dict] | None = None) -> dict:
    ledger = WorkLedger(settings.state / "jobs.sqlite3", create=False)
    obj = ledger.checkpoint_step(objective_id, step_id, expected_revision=expected_revision,
                                 status=step_status, evidence=evidence)
    return show(settings, obj["objective_id"])


def approve(settings: Settings, objective_id: str, *, expected_revision: int) -> dict:
    ledger = WorkLedger(settings.state / "jobs.sqlite3", create=False)
    current = ledger.get(objective_id)
    if current["revision"] != expected_revision:
        raise RunnerError(f"REVISION_CONFLICT: expected {expected_revision}, current {current['revision']}")
    if not current["metadata"].get("plan"):
        raise RunnerError("There is no persisted plan to approve")
    if current["state"] == "planned":
        return show(settings, objective_id)
    if current["state"] != "awaiting_approval":
        raise RunnerError(f"Objective state {current['state']} is not awaiting plan approval")
    obj = ledger.change(objective_id, expected_revision=expected_revision, state="planned",
                        event_type="plan_approved", detail={"explicit_operator_approval": True})
    return show(settings, obj["objective_id"])


def pause(settings: Settings, objective_id: str, *, expected_revision: int, reason: str) -> dict:
    reason = reason.strip()
    if not reason or len(reason) > 500:
        raise RunnerError("Pause reason must be 1-500 characters")
    _assert_secret_free({"reason": reason})
    ledger = WorkLedger(settings.state / "jobs.sqlite3", create=False)
    current = ledger.get(objective_id)
    if current["revision"] != expected_revision:
        raise RunnerError(f"REVISION_CONFLICT: expected {expected_revision}, current {current['revision']}")
    if current["state"] == "paused":
        return show(settings, objective_id)
    updated = ledger.change(objective_id, expected_revision=expected_revision, state="paused",
        metadata={"current_blocker": reason, "next_action": "Resume after resolving the pause reason"},
        event_type="paused", detail={"reason": reason})
    return show(settings, updated["objective_id"])


def resume(settings: Settings, objective_id: str) -> dict:
    result = show(settings, objective_id)
    obj = result["objective"]
    links = result["attempts"]
    if obj["state"] in {"completed", "failed", "cancelled"}:
        return result
    unknown = [a for a in links if (a.get("attempt") or {}).get("state") in {"unknown_remote", "manual_review"}]
    if unknown:
        result["current_blocker"] = "Ambiguous or manual-review attempts must be reconciled by experiment ID before execution continues"
        result["next_action"] = "Run kgr reconcile --experiment-id EXPERIMENT_ID --json for each linked experiment"
        return result
    if any(a["plan_status"] == "stale" for a in links):
        result["current_blocker"] = "An immutable planned spec is missing or changed"
        result["next_action"] = "Request operator review; do not regenerate attempt IDs or specs"
        return result
    pending = [a for a in links if (a.get("attempt") or {}).get("state") not in
               {None, "succeeded", "failed", "cancelled", "invalid_result", "blocked"} and a.get("job_id")]
    result["reconciliation_required"] = sorted({a["experiment_id"] for a in pending})
    ready = [a for a in links if a["plan_status"] == "ready" and not a["job_id"]]
    if pending:
        result["next_action"] = "Reconcile linked experiments, then resume/fetch and validate through the experiment lifecycle; submit only ready planned attempts with a fresh preflight"
    elif ready:
        result["next_action"] = f"Submit {len(ready)} remaining planned attempts with kgr work execute {objective_id} --wait"
    elif result["progress"]["scientifically_validated_attempts"] == len(links) and links:
        result["next_action"] = "Review acceptance evidence, complete the objective explicitly, and generate its report"
    else:
        result["next_action"] = "Inspect terminal failures and acceptance evidence; request operator review where needed"
    return result


def execute(settings: Settings, objective_id: str, *, env_file: str | None = None,
            account: str = "auto", approve: bool = False, wait: bool = False,
            poll_seconds: int | None = None, max_wait_minutes: int | None = None) -> dict:
    """Submit one bounded batch through the existing safe experiment lifecycle.

    Every submission performs its own existing Kaggle preflight and atomic lease
    reservation. Existing attempt IDs are never submitted a second time.
    """
    from . import agent_workflow
    work = WorkLedger(settings.state / "jobs.sqlite3", create=False)
    obj = work.get(objective_id)
    if obj["state"] == "completed":
        return show(settings, objective_id)
    if obj["state"] == "awaiting_approval":
        raise RunnerError("Plan awaits approval; run kgr work approve with the current revision")
    if obj["state"] not in {"planned", "running", "paused", "blocked", "needs_review"}:
        raise RunnerError(f"Objective state {obj['state']} does not permit execution")
    plan = obj["metadata"].get("plan")
    if not isinstance(plan, dict):
        raise RunnerError("Objective has no persisted plan")
    poll_seconds = poll_seconds or plan.get("polling_seconds") or 20
    max_wait_minutes = max_wait_minutes or plan.get("timeout_minutes") or 45
    if poll_seconds < 1 or max_wait_minutes < 1:
        raise RunnerError("Polling interval and timeout must be positive")
    if not obj["acceptance_criteria"]:
        raise RunnerError("Objective cannot execute without explicit acceptance criteria")
    links = work.links(objective_id)
    experiment_ledger = AgentLedger(settings.state / "jobs.sqlite3")
    ready = []
    already = []
    pending_existing = []
    for link in links:
        path = Path(link["spec_path"])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != link["spec_sha256"]:
            raise RunnerError(f"Immutable planned spec is missing or changed: {link['attempt_id']}; operator review required")
        existing = experiment_ledger.get(link["experiment_id"], link["attempt_id"])
        if existing:
            work.link_job(objective_id, link["experiment_id"], link["attempt_id"], existing["job_id"])
            if existing["state"] == "succeeded" and existing["scientific_status"] == "succeeded":
                already.append(link["attempt_id"])
                continue
            if existing["state"] in {"failed", "cancelled", "invalid_result", "blocked"}:
                raise RunnerError(f"Attempt {link['attempt_id']} already exists in terminal state {existing['state']}; it will not be resubmitted")
            pending_existing.append(existing)
            continue
        # Verify pinned source/dataset/model/protocol inputs before changing state
        # or launching any worker threads. Once a remote job exists, its immutable
        # ledger snapshot is authoritative and must remain reconcilable even if the
        # user's current source tree has since changed.
        _check_snapshot_source(path)
        if link["job_id"]:
            raise RunnerError(f"Linked attempt {link['attempt_id']} has no matching authoritative ledger record; operator review required")
        ready.append(link)
    if pending_existing:
        if not wait:
            result = show(settings, objective_id)
            result["next_action"] = "Reconcile linked experiments, then rerun kgr work execute --wait; never create a replacement attempt"
            return result
        by_experiment = sorted({a["experiment_id"] for a in pending_existing})
        for experiment_id in by_experiment:
            resumed = agent_workflow.resume(settings, experiment_id, env_file=env_file,
                wait=True, poll_seconds=poll_seconds, max_wait_minutes=max_wait_minutes)
            if not resumed.get("ok"):
                result = show(settings, objective_id)
                result["current_blocker"] = "An existing attempt needs manual review or remains scientifically invalid"
                result["next_action"] = "Inspect reconciliation and validated artifacts; do not resubmit this attempt"
                return result
        return execute(settings, objective_id, env_file=env_file, account=account, approve=approve,
                       wait=wait, poll_seconds=poll_seconds, max_wait_minutes=max_wait_minutes)
    if not ready:
        return show(settings, objective_id)
    # We never schedule more submissions than the objective's local concurrency
    # limit at a time. The shared agent ledger remains the cross-process authority.
    limit = plan.get("max_concurrent", 1)
    current_state = obj["state"]
    if current_state != "running":
        work.change(objective_id, expected_revision=obj["revision"], state="running",
                    metadata={"current_blocker": None, "pending_operator_decision": None,
                              "next_action": None},
                    event_type="execution_started", detail={"attempts_ready": len(ready)})
    outcomes = []
    stop = False
    stop_reason = "preflight_or_attempt_failure"
    deferred = False
    for start in range(0, len(ready), limit):
        batch = ready[start:start + limit]
        def submit_one(link: dict) -> dict:
            spec_path = Path(link["spec_path"])
            try:
                spec, _ = agent_workflow.load_spec(spec_path)
                response = agent_workflow.submit(spec_path, settings=settings, env_file=env_file,
                                                 account=account, approve=approve)
                job_id = response.get("job_id")
                if job_id:
                    work.link_job(objective_id, link["experiment_id"], link["attempt_id"], job_id)
                return {"attempt_id": link["attempt_id"], "ok": bool(response.get("ok")),
                        "job_id": job_id, "state": response.get("state"),
                        "action": response.get("action")}
            except Exception as exc:
                # The ledger is authoritative even when the push response was lost.
                tracked = experiment_ledger.get(link["experiment_id"], link["attempt_id"])
                if tracked:
                    work.link_job(objective_id, link["experiment_id"], link["attempt_id"], tracked["job_id"])
                msg = str(exc).casefold()
                category = ("quota_or_capacity_blocked" if any(x in msg for x in ("quota", "slot", "concurrent", "preflight"))
                            else "submission_requires_review" if tracked and tracked["state"] == "unknown_remote"
                            else "attempt_failed")
                return {"attempt_id": link["attempt_id"], "ok": False,
                        "job_id": tracked["job_id"] if tracked else None,
                        "state": tracked["state"] if tracked else None, "reason": category}
        with ThreadPoolExecutor(max_workers=limit) as pool:
            futures = [pool.submit(submit_one, item) for item in batch]
            batch_outcomes = [future.result() for future in as_completed(futures)]
        outcomes.extend(batch_outcomes)
        if any(not item["ok"] or item.get("state") in {"unknown_remote", "blocked", "failed"} for item in batch_outcomes):
            stop = True
            break
        # A bounded wait uses the existing reconciliation/fetch/result validator.
        # If the caller asked for nonblocking execution, leave leases and evidence
        # in place and return the handoff needed by the next session.
        if wait:
            for experiment_id in sorted({item["experiment_id"] for item in batch}):
                resumed = agent_workflow.resume(settings, experiment_id, env_file=env_file,
                    wait=True, poll_seconds=poll_seconds, max_wait_minutes=max_wait_minutes)
                outcomes.append({"experiment_id": experiment_id, "resume": resumed})
                if not resumed.get("ok"):
                    stop = True
                    stop_reason = "resume_or_result_validation_failed"
                    break
            if stop:
                break
            progress = show(settings, objective_id)["progress"]
            observed_hours = progress["observed_gpu_hours"]
            if observed_hours is not None:
                successful_ids = {a["attempt_id"] for a in _linked_attempts(settings, work.links(objective_id))
                                  if (a.get("attempt") or {}).get("state") == "succeeded"
                                  and (a.get("attempt") or {}).get("scientific_status") == "succeeded"}
                remaining_estimate = sum(link["planned_gpu_hours"] for link in work.links(objective_id)
                                         if link["attempt_id"] not in successful_ids)
                budget = plan.get("total_gpu_hours")
                if budget is not None and observed_hours + remaining_estimate > budget:
                    stop = True
                    stop_reason = "observed_gpu_budget_exceeded"
                    break
        else:
            deferred = start + len(batch) < len(ready) or any(
                item.get("state") in {"submitted", "running", "remote_complete", "unknown_remote"}
                for item in batch_outcomes)
            break
    result = show(settings, objective_id)
    result["execution"] = {"submitted_or_reused": outcomes, "already_succeeded": already,
                           "stopped_after_blocker": stop,
                           "fresh_preflight_per_submission": True}
    if stop:
        result["next_action"] = "Inspect the blocker and authoritative experiment ledger; reconcile uncertain attempts before resuming"
        result["current_blocker"] = ("Observed runtime-derived GPU-hour use plus remaining plan estimates exceeds the approved budget"
                                     if stop_reason == "observed_gpu_budget_exceeded" else
                                     "A result fetch or scientific validation failed" if stop_reason == "resume_or_result_validation_failed" else
                                     "Batch stopped safely after a submission or preflight failure")
        needs_review = any((a.get("attempt") or {}).get("state") in {"unknown_remote", "invalid_result"}
                           for a in result["attempts"])
        latest = work.get(objective_id)
        target_state = "needs_review" if needs_review else "blocked"
        try:
            work.change(objective_id, expected_revision=latest["revision"], state=target_state,
                metadata={"current_blocker": result["current_blocker"], "next_action": result["next_action"],
                          "pending_operator_decision": "Reconcile remote existence and review attempt evidence" if needs_review else None},
                event_type="execution_blocked", detail={"reason": "ambiguous_remote" if needs_review else stop_reason})
            result["objective"] = work.get(objective_id)
        except RunnerError:
            pass
    elif deferred:
        result["next_action"] = "Continue with kgr work execute OBJECTIVE_ID --wait after reconciling the active batch"
        result["current_blocker"] = "Waiting for the current bounded batch to reach a validated terminal state"
        latest = work.get(objective_id)
        try:
            work.change(objective_id, expected_revision=latest["revision"],
                metadata={"current_blocker": result["current_blocker"], "next_action": result["next_action"]},
                event_type="batch_submitted", detail={"submitted_count": len(outcomes)})
            result["objective"] = work.get(objective_id)
        except RunnerError:
            pass
    elif len(ready) > len(outcomes):
        result["next_action"] = "Continue remaining planned attempts after reconciling linked experiment state"
    else:
        result["next_action"] = ("All planned attempts are scientifically validated; review acceptance criteria and generate the objective report"
                                  if result["progress"]["scientifically_validated_attempts"] == len(links)
                                  else "Reconcile linked experiments, then resume/fetch and validate scientific results")
    return result


def report(settings: Settings, objective_id: str) -> str:
    result = show(settings, objective_id)
    obj = result["objective"]
    from . import agent_workflow
    experiment_ids = sorted({a["experiment_id"] for a in result["attempts"] if a.get("job_id")})
    aggregates = []
    for experiment_id in experiment_ids:
        try:
            aggregates.append(agent_workflow.aggregate(settings, experiment_id))
        except RunnerError as exc:
            # Reports remain useful for partial or inaccessible runs, but the
            # failure is explicit and cannot be mistaken for successful evidence.
            aggregates.append({"experiment_id": experiment_id, "ok": False,
                               "error": _redact(str(exc))})
    lines = [f"# Work objective: {obj['title']}", "", f"- ID: `{obj['objective_id']}`",
             f"- State: **{obj['state']}** (revision {obj['revision']})", "",
             "## Acceptance criteria", *[f"- {x}" for x in obj["acceptance_criteria"]], "",
             "## Progress", f"- Validated attempts: {result['progress']['scientifically_validated_attempts']} / {result['progress']['planned_attempts']}",
             f"- Planned GPU hours: {result['progress']['planned_gpu_hours']}; observed: {result['progress']['observed_gpu_hours']}", "",
             "## Experiment aggregates"]
    if not aggregates:
        lines.append("- No submitted experiment attempts are available for aggregation.")
    for aggregation in aggregates:
        lines.append(f"- `{aggregation['experiment_id']}`: {aggregation.get('valid_runs', 0)} valid; {aggregation.get('invalid_or_missing_runs', 0)} excluded; mean `{aggregation.get('mean')}`; sample standard deviation `{aggregation.get('sample_stddev')}`; 95% normal approximation `{aggregation.get('ci95_normal_approx')}`")
        for excluded in aggregation.get("excluded", []):
            lines.append(f"  - Excluded job `{excluded.get('job_id')}`: `{excluded.get('state')}`")
        if aggregation.get("error"):
            lines.append(f"  - Aggregation unavailable: {aggregation['error']}")
    lines += ["", "## Attempts"]
    for a in result["attempts"]:
        attempt = a.get("attempt") or {}
        validation = attempt.get("validation") or {}
        lines.append(f"- `{a['experiment_id']}/{a['attempt_id']}`: {attempt.get('state', a['plan_status'])}; job `{a.get('job_id') or 'not submitted'}`; scientific status `{attempt.get('scientific_status', 'not validated')}`; result SHA256 `{validation.get('result_sha256', 'unavailable')}`")
    lines += ["", "## Checklist"]
    for s in result["steps"]:
        lines.append(f"- [{ 'x' if s['status'] == 'done' else ' ' }] {s['title']} (`{s['status']}`)")
    lines += ["", "## Next action", result["next_action"], ""]
    body = "\n".join(lines)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    report_dir = settings.state / "work" / objective_id / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    target = report_dir / f"{digest}.md"
    if not target.exists():
        temporary = target.with_suffix(".md.tmp")
        temporary.write_text(body, encoding="utf-8", newline="\n")
        os.replace(temporary, target)
    work = WorkLedger(settings.state / "jobs.sqlite3", create=False)
    current = work.get(objective_id)
    record = {"path": str(target), "sha256": digest, "generated_at": stamp()}
    previous = current["metadata"].get("last_successful_report")
    if not previous or previous.get("sha256") != digest:
        work.change(objective_id, expected_revision=current["revision"],
                    metadata={"last_successful_report": record}, event_type="report_generated",
                    detail={"report_sha256": digest})
    return body + f"\nEvidence file: {target}\nEvidence SHA256: {digest}\n"


def complete(settings: Settings, objective_id: str, *, expected_revision: int,
             acceptance_review: list[dict]) -> dict:
    ledger = WorkLedger(settings.state / "jobs.sqlite3", create=False)
    obj = ledger.get(objective_id)
    if obj["state"] == "completed":
        if obj["revision"] != expected_revision:
            raise RunnerError(f"REVISION_CONFLICT: expected {expected_revision}, current {obj['revision']}")
        return show(settings, objective_id)
    if not obj["acceptance_criteria"]:
        raise RunnerError("Objective cannot complete without explicit acceptance criteria")
    if not obj["metadata"].get("plan") or not ledger.links(objective_id):
        raise RunnerError("Objective cannot complete before a persisted plan has attempts")
    if obj["state"] not in {"running", "paused", "needs_review", "blocked"}:
        raise RunnerError(f"Objective state {obj['state']} cannot be completed")
    if not isinstance(acceptance_review, list):
        raise RunnerError("Acceptance review must be a JSON array")
    mapped = {}
    for item in acceptance_review:
        if not isinstance(item, dict) or not isinstance(item.get("criterion"), str) or not item.get("evidence"):
            raise RunnerError("Each acceptance review item requires criterion and evidence")
        mapped[item["criterion"]] = _redact(item["evidence"])
    missing = [criterion for criterion in obj["acceptance_criteria"] if criterion not in mapped]
    extra = set(mapped) - set(obj["acceptance_criteria"])
    if missing or extra:
        raise RunnerError("Acceptance review must provide evidence for exactly every stated criterion")
    steps = ledger.steps(objective_id)
    if any(step["status"] not in {"done", "skipped"} for step in steps):
        raise RunnerError("All required checklist steps must be done or explicitly skipped before completion")
    links = ledger.links(objective_id)
    agent = AgentLedger(settings.state / "jobs.sqlite3")
    outcomes = []
    for link in links:
        attempt = agent.get(link["experiment_id"], link["attempt_id"])
        if not attempt or attempt["state"] != "succeeded" or attempt["scientific_status"] != "succeeded":
            outcomes.append(link["attempt_id"])
    partial = obj["metadata"].get("plan", {}).get("partial_completion_policy") == "allow_explicit_partial"
    if outcomes and not (partial and acceptance_review and any(
            isinstance(item, dict) and item.get("partial_completion_accepted") is True for item in acceptance_review)):
        raise RunnerError("Linked attempts remain unvalidated; objective completion requires validated results or an explicitly approved partial-completion policy")
    if expected_revision != obj["revision"]:
        raise RunnerError(f"REVISION_CONFLICT: expected {expected_revision}, current {obj['revision']}")
    updated = ledger.change(objective_id, expected_revision=expected_revision, state="completed",
        metadata={"acceptance_review": mapped, "completed_at": stamp(),
                  "partial_completion": bool(outcomes)},
        event_type="acceptance_criteria_met", detail={"criteria_count": len(mapped),
            "validated_attempt_count": len(links) - len(outcomes), "unvalidated_attempt_count": len(outcomes)})
    return show(settings, updated["objective_id"])

