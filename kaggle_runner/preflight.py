"""Fail-closed Kaggle preflight: active batch kernels and GPU quota.

Kaggle's kernel listing/status APIs do not expose all interactive sessions,
so this is a best-effort batch preflight, not a platform-wide guarantee.
"""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

from .api import KaggleCLI
from .config import RunnerError
from .status import parse_kernel_status

# Count cancellation transitions conservatively: a cancellation request or
# acknowledgement is not proof that the Kaggle worker released its slot.
ACTIVE = {"running", "queued", "pending", "initializing", "starting",
          "cancel_requested", "cancel_acknowledged"}
TERMINAL = {"complete", "error", "cancelled", "failed", "new_script"}


def _hours(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.0, float(value))
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if re.fullmatch(r"[0-9.]+", s):
        return float(s)
    # Protobuf durations, including 3600s or 3600.0s.
    if re.fullmatch(r"\d+(?:\.\d+)?s", s):
        return float(s[:-1]) / 3600
    if re.fullmatch(r"\d+(?:\.\d+)?\s*(?:h|hr|hrs|hours?)", s):
        return float(re.match(r"[0-9.]+", s).group())
    if re.fullmatch(r"\d+:\d\d(?::\d\d)?", s):
        parts = [int(v) for v in s.split(":")]
        if len(parts) == 2:
            return parts[0] + parts[1] / 60
        return parts[0] + parts[1] / 60 + parts[2] / 3600
    match = re.fullmatch(r"(?:(\d+)\s*h(?:ours?)?\s*)?(?:(\d+)\s*m(?:in(?:utes?)?)?\s*)?", s)
    if match and any(match.groups()):
        return int(match.group(1) or 0) + int(match.group(2) or 0) / 60
    return None


def _norm(data: dict) -> dict[str, Any]:
    return {re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in data.items()}


def parse_gpu_quota(raw: str) -> dict:
    """Accept native CLI JSON variants; reject ambiguous/missing GPU values."""
    try:
        root = json.loads(raw)
    except ValueError as exc:
        raise RunnerError("Could not parse `kaggle quota --format json`; update kaggle CLI") from exc
    candidates: list[dict] = []
    if isinstance(root, list):
        for item in root:
            if isinstance(item, dict):
                normalized = _norm(item)
                tag = str(normalized.get("accelerator") or normalized.get("name") or normalized.get("type") or "").lower()
                if "gpu" in tag:
                    candidates.append(item)
    elif isinstance(root, dict):
        norm = _norm(root)
        for key, value in norm.items():
            if key in {"gpu", "gpuquota", "gpuquotahours", "gpuaccelerator"} and isinstance(value, dict):
                candidates.append(value)
        if not candidates and any("gpu" in key and ("remaining" in key or "used" in key) for key in norm):
            candidates.append({re.sub(r"^gpu", "", k): v for k, v in norm.items() if k.startswith("gpu")})
    if not candidates:
        raise RunnerError("Kaggle quota response has no recognizable GPU quota")
    n = _norm(candidates[0])
    used = next((_hours(n[key]) for key in ("usedhours", "timeused", "used") if key in n), None)
    total = next((_hours(n[key]) for key in ("totalhours", "totaltimeallowed", "total", "allowedhours") if key in n), None)
    remaining = next((_hours(n[key]) for key in ("remaininghours", "availablehours", "remaining", "available") if key in n), None)
    if remaining is None and total is not None and used is not None:
        remaining = max(0.0, total - used)
    if remaining is None:
        raise RunnerError("GPU quota remaining hours not available in Kaggle response")
    return {"remaining_hours": round(remaining, 3), "used_hours": used, "total_hours": total,
            "note": "Snapshot only; running sessions may reserve additional hours"}


def quota_report(cli: KaggleCLI) -> dict:
    result = cli.call("quota", "--format", "json", check=False)
    if result.returncode:
        raise RunnerError("Cannot retrieve GPU quota from Kaggle CLI; upgrade `kaggle` and confirm API token. "
                          + result.text[-300:])
    return parse_gpu_quota(result.stdout)


def _list_page(cli: KaggleCLI, page: int, size: int) -> list[dict]:
    response = cli.call("kernels", "list", "--mine", "--format", "json", "--sort-by", "dateRun",
                        "--page", str(page), "--page-size", str(size))
    try:
        obj = json.loads(response.stdout)
    except ValueError as exc:
        raise RunnerError("Could not parse Kaggle kernels list JSON; update kaggle CLI") from exc
    if isinstance(obj, dict):
        obj = obj.get("kernels", obj.get("results", obj.get("items")))
    if not isinstance(obj, list) or any(not isinstance(item, dict) for item in obj):
        raise RunnerError("Unexpected Kaggle kernels list JSON schema")
    return obj


def _kernel_ref(row: dict) -> str | None:
    n = _norm(row)
    for key in ("ref", "kernelref", "id", "url"):
        val = n.get(key)
        if isinstance(val, str):
            m = re.search(r"(?:kaggle\.com/code/)?([A-Za-z0-9_-]+/[A-Za-z0-9_-]+)(?:/\d+)?$", val)
            if m:
                return m.group(1)
    return None


def _status(raw: str) -> str:
    """Compatibility wrapper for a single shared Kaggle CLI status parser."""
    return parse_kernel_status(raw)


def _listed_status(row: dict) -> str:
    """Read an optional status hint from a kernels-list row, if present."""
    normalized = _norm(row)
    for key in ("lastrunstatus", "lastrunstate", "runstatus", "workerstatus", "status"):
        value = normalized.get(key)
        if isinstance(value, str):
            parsed = _status(value)
            if parsed != "unknown":
                return parsed
    return "unknown"


def _session_not_found(message: str) -> bool:
    """Only a definite missing resource is safe to treat as an absent run.

    Kaggle may list historical notebooks that have no live session anymore.
    Authentication, permission, throttling, network and server errors are *not*
    safe to ignore during a concurrency preflight.
    """
    if re.search(r"\b(?:permission|forbidden|unauthorized|denied|unauthenticated)\b|\b(?:401|403|429|5\d\d)\b", message, re.IGNORECASE):
        return False
    return bool(re.search(r"\b(?:resource|kernel|notebook|session)\s+(?:was\s+)?not found\b|\b404\b", message, re.IGNORECASE))


def active_report(cli: KaggleCLI, *, page_size: int = 100, max_pages: int = 5, limit: int = 2) -> dict:
    seen = set()
    active: list[dict] = []
    not_started: list[dict] = []
    missing_sessions: list[dict] = []
    scanned = 0
    for page in range(1, max_pages + 1):
        rows = _list_page(cli, page, page_size)
        for row in rows:
            ref = _kernel_ref(row)
            if not ref:
                raise RunnerError("Kaggle kernel listing contains a row without a canonical ref")
            if ref in seen:
                continue
            seen.add(ref)
            scanned += 1
            # The listing can show stale version metadata; always ask Kaggle
            # for the newest run state, never infer live concurrency from title/date.
            res = cli.call("kernels", "status", ref, check=False)
            if res.returncode:
                if _session_not_found(res.text):
                    listed_status = _listed_status(row)
                    if listed_status in ACTIVE:
                        raise RunnerError(
                            f"Kaggle reports {ref} as {listed_status} in its notebook list "
                            "but the live session status is missing; refusing to guess "
                            "whether a concurrent slot is free"
                        )
                    missing_sessions.append({"kernel_ref": ref, "listed_status": listed_status})
                    continue
                raise RunnerError(f"Could not verify remote kernel status for {ref}: {res.text[-250:]}")
            status = _status(res.text)
            if status == "unknown":
                # Unknown states must not silently free a concurrent slot. Show
                # the real response for diagnosis instead of only the kernel ref.
                detail = " ".join(res.text.split())[:350] or "<empty response>"
                raise RunnerError(
                    f"Unrecognized Kaggle kernel status for {ref}; cannot safely count running jobs. "
                    f"Kaggle response: {detail}. "
                    f"Inspect with: python -m kaggle kernels status {ref}"
                )
            if status in ACTIVE:
                active.append({"kernel_ref": ref, "status": status})
            elif status == "new_script":
                # This is Kaggle's explicit 'no execution started' state, not
                # an unknown or an in-flight worker.
                not_started.append({"kernel_ref": ref, "status": status})
        if len(rows) < page_size:
            return {"running_count": len(active), "max_concurrent": limit,
                    "has_slot": len(active) < limit, "active": active, "scanned": scanned,
                    "missing_sessions": missing_sessions,
                    "missing_session_count": len(missing_sessions),
                    "not_started": not_started,
                    "not_started_count": len(not_started),
                    "note": "Batch kernel snapshots only; historical notebooks with a confirmed missing session are excluded. Interactive sessions may not be included"}
    raise RunnerError("Kaggle kernel listing exceeded safe pagination window; cannot verify active session count")


def check_preflight(cli: KaggleCLI, *, gpu: str, required_gpu_hours: float = 0.25,
                    max_concurrent: int = 2) -> dict:
    if required_gpu_hours <= 0 or max_concurrent < 1 or max_concurrent > 2:
        raise RunnerError("--required-gpu-hours must be positive and --max-concurrent must be 1 or 2")
    runs = active_report(cli, limit=max_concurrent)
    quota = quota_report(cli) if gpu != "cpu" else None
    reasons = []
    if not runs["has_slot"]:
        reasons.append(f"Concurrent batch jobs {runs['running_count']}/{max_concurrent}")
    if quota is not None and quota["remaining_hours"] < required_gpu_hours:
        reasons.append(f"GPU remaining {quota['remaining_hours']}h < required {required_gpu_hours}h")
    return {"ok": not reasons, "gpu": gpu, "active": runs, "gpu_quota": quota,
            "required_gpu_hours": required_gpu_hours if gpu != "cpu" else 0,
            "reasons": reasons}
