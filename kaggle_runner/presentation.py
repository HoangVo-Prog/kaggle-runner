"""Concise human-facing CLI output; machine-readable detail lives behind --json.

Keep this module pure: formatting must not read credentials, make API calls or
change job state. The CLI owns the full JSON contract.
"""
from __future__ import annotations

from typing import Any


def _num(value: Any) -> str:
    if value is None:
        return "?"
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def _line(key: str, value: Any) -> str:
    return f"{key:<13} {value}"


def _table(rows: list[list[str]], headings: list[str], max_rows: int = 20) -> list[str]:
    shown = rows[:max_rows]
    widths = [max(len(str(header)), *(len(str(row[i])) for row in shown)) for i, header in enumerate(headings)]
    def make(row: list[str]) -> str:
        return "  ".join(str(val).ljust(widths[i]) for i, val in enumerate(row)).rstrip()
    parts = [make(headings), make(["-" * width for width in widths])]
    parts.extend(make(row) for row in shown)
    if len(rows) > max_rows:
        parts.append(f"... {len(rows) - max_rows} more (use --json for all)")
    return parts


def _quota(data: dict) -> str:
    remaining = _num(data.get("remaining_hours"))
    total = data.get("total_hours")
    used = data.get("used_hours")
    result = f"GPU quota: {remaining}h remaining"
    if total is not None:
        result += f" / {_num(total)}h total"
    if used is not None:
        result += f" ({_num(used)}h used)"
    if data.get("refresh_at"):
        result += f"\nRefresh:   {data['refresh_at']}"
    return result


def _active(data: dict, *, compact: bool = False) -> str:
    source = "runner jobs" if data.get("source") == "tracked_jobs" else "remote batch scan"
    lines = [f"Batch jobs ({source}): {data.get('running_count', 0)}/{data.get('max_concurrent', 2)} active"]
    if data.get("cancelling_count"):
        lines.append(f"Cancelling (not counted active): {data['cancelling_count']}")
    if data.get("missing_session_count"):
        lines.append(f"Missing historical sessions: {data['missing_session_count']}")
    if data.get("unverified_count"):
        lines.append(f"Needs attention: {data['unverified_count']} inaccessible tracked job(s)")
        if not compact:
            for item in (data.get("unverified") or [])[:5]:
                lines.append(f"  {str(item['job_id'])[:10]}  {item['kernel_ref']}")
            lines.append("Fix: kgr repair JOB_ID --owner CORRECT_OWNER")
            lines.append("Or:  kgr archive JOB_ID --confirm-inactive (only after verifying inactive)")
    if not compact:
        entries = data.get("active") or []
        if entries:
            lines.extend(_table([[str(j.get("job_id", "-")[:10]), str(j.get("status", "?")), str(j.get("kernel_ref", "?"))]
                                 for j in entries], ["JOB", "STATUS", "KERNEL"], max_rows=10))
        if not data.get("slot_guaranteed", False):
            lines.append("Note: estimate only; Kaggle decides actual availability.")
    return "\n".join(lines)


def _check(data: dict) -> str:
    lines = ["READY - local preflight passed" if data.get("ok") else "BLOCKED - cannot submit"]
    lines.append(_active(data.get("active") or {}, compact=True))
    if data.get("gpu") != "cpu":
        quota = data.get("gpu_quota")
        if quota:
            lines.append(f"GPU:       {_num(quota.get('remaining_hours'))}h remaining; "
                         f"required {_num(data.get('required_gpu_hours'))}h")
        else:
            lines.append("GPU:       quota unavailable")
    if data.get("reasons"):
        lines.extend(f"Reason:    {why}" for why in data["reasons"])
    elif data.get("warnings"):
        # Avoid an entire paragraph of diagnostics for a successful check.
        lines.append("Note:      Only runner-tracked jobs are checked unless --remote is used.")
    return "\n".join(lines)


def _run(data: dict) -> str:
    dry = data.get("dry_run")
    lines = ["DRY RUN - nothing submitted" if dry else "SUBMITTED - Kaggle job created"]
    for label, field in (("Job ID", "job_id"), ("Kernel", "kernel_ref"),
                         ("GPU", "gpu"), ("Source", "source")):
        if data.get(field) is not None:
            lines.append(_line(label + ":", data[field]))
    if data.get("kernel_ref") and not dry:
        lines.append(_line("Kaggle:", f"https://www.kaggle.com/code/{data['kernel_ref']}"))
    if dry and data.get("staging_dir"):
        lines.append(_line("Staged:", data["staging_dir"]))
    if not dry and data.get("job_id"):
        lines.append(_line("Next:", f"kgr status {data['job_id']}"))
    return "\n".join(lines)


def _jobs(data: list[dict]) -> str:
    if not data:
        return "No locally tracked jobs."
    lines = [f"Tracked jobs: {len(data)}"]
    lines.extend(_table([[str(j.get("job_id", ""))[:10], str(j.get("status", "?")),
                          str(j.get("kernel_ref", "?")), str(j.get("updated_at", ""))[:16]]
                         for j in data], ["JOB ID", "STATUS", "KERNEL", "UPDATED"], max_rows=15))
    return "\n".join(lines)


def _report(data: dict) -> str:
    lines = [f"Job report: {data.get('total', 0)} tracked"]
    counts = data.get("counts") or {}
    if counts:
        lines.append("Statuses: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    jobs = data.get("jobs") or []
    if jobs:
        lines.append(_jobs(jobs))
    return "\n".join(lines)


def format_output(data: dict | list, command: str = "") -> str:
    """Compact, stable-enough human presentation. --json retains original data."""
    if command == "quota" and isinstance(data, dict):
        return _quota(data)
    if command == "active" and isinstance(data, dict):
        return _active(data)
    if command == "check" and isinstance(data, dict):
        return _check(data)
    if command == "run" and isinstance(data, dict):
        # `run --wait` also emits status / fetch dictionaries.
        if "job_id" in data and "source" in data:
            return _run(data)
        if "job_id" in data and "status" in data:
            return f"Job {str(data['job_id'])[:10]}: {data['status']}"
        if "output_dir" in data:
            return f"Downloaded {len(data.get('files') or [])} files to {data['output_dir']}"
    if command == "jobs" and isinstance(data, list):
        return _jobs(data)
    if command in {"report", "sync"} and isinstance(data, dict):
        return _report(data)
    if command in {"repair", "archive"} and isinstance(data, dict):
        return f"{data.get('status', '?')}: {str(data.get('job_id',''))[:10]}  {data.get('kernel_ref', '')}\n{data.get('note', '')}"
    if command == "status" and isinstance(data, dict):
        return f"{str(data.get('job_id', ''))[:10]}  {data.get('status', '?')}  {data.get('kernel_ref', '')}"
    if command == "fetch" and isinstance(data, dict):
        return f"Downloaded {len(data.get('files') or [])} files to {data.get('output_dir', '?')}"
    if command == "doctor" and isinstance(data, dict):
        lines = [f"Kaggle CLI: {'OK' if data.get('kaggle_cli_ok') else 'UNAVAILABLE'}"]
        if "kaggle_auth_ok" in data:
            lines.append(f"Authentication: {'OK' if data.get('kaggle_auth_ok') else 'FAILED'}")
        lines.append(f"Python: {data.get('python_version', '?')}")
        return "\n".join(lines)
    if command == "outputs" and isinstance(data, dict):
        files = data.get("files")
        if isinstance(files, list):
            return "\n".join([f"Output files: {len(files)}", *[str(f.get('name', f)) for f in files[:20]]])
    if isinstance(data, list):
        return "\n".join(str(v)[:250] for v in data[:20]) or "No results."
    if isinstance(data, dict):
        lines = []
        for key, val in data.items():
            if key in {"raw", "submit_output", "remote_message", "cli_output", "last_message"}:
                continue
            if isinstance(val, (dict, list)):
                lines.append(f"{key}: {len(val)} items (use --json for full details)")
            else:
                lines.append(f"{key}: {str(val)[:300]}")
        return "\n".join(lines) or "No details."
    return str(data)
