"""Fail-closed Kaggle preflight: active batch kernels and GPU quota.

Kaggle's kernel listing/status APIs do not expose all interactive sessions,
so this is a best-effort batch preflight, not a platform-wide guarantee.
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from typing import Any, Callable

from .api import KaggleCLI
from .jobs import JobStore
from .config import RunnerError
from .status import parse_kernel_status

# Count cancellation transitions conservatively: a cancellation request or
# acknowledgement is not proof that the Kaggle worker released its slot.
ACTIVE = {"running", "queued", "pending", "initializing", "starting",
          "cancel_requested"}
# Cancellation acknowledgement can persist in the historical kernel status after
# Kaggle Active Events has no running session. It is not proof of a live slot.
CANCELLING = {"cancel_acknowledged"}
LOCAL_CANDIDATES = ACTIVE | CANCELLING | {"submitted", "status_unavailable", "unknown", "new_script"}
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
                tag = str(normalized.get("resource") or normalized.get("accelerator") or normalized.get("name") or normalized.get("type") or "").lower()
                if "gpu" in tag:
                    candidates.append(item)
    elif isinstance(root, dict):
        norm = _norm(root)
        # A single row is also valid input for diagnostics. The official CLI
        # normally returns a list of rows labelled with `resource: GPU`.
        if str(norm.get("resource", "")).strip().lower() == "gpu":
            candidates.append(root)
        for key, value in norm.items():
            if key in {"gpu", "gpuquota", "gpuquotahours", "gpuaccelerator"} and isinstance(value, dict):
                candidates.append(value)
        if not candidates and any("gpu" in key and ("remaining" in key or "used" in key) for key in norm):
            candidates.append({re.sub(r"^gpu", "", k): v for k, v in norm.items() if k.startswith("gpu")})
    if not candidates:
        raise RunnerError(
            "Kaggle quota response has no recognizable GPU row "
            "(expected resource=GPU with used/remaining/total fields). "
            "Check `python -m kaggle quota --format json`."
        )
    n = _norm(candidates[0])
    used = next((_hours(n[key]) for key in ("usedhours", "timeused", "used") if key in n), None)
    total = next((_hours(n[key]) for key in ("totalhours", "totaltimeallowed", "total", "allowedhours") if key in n), None)
    remaining = next((_hours(n[key]) for key in ("remaininghours", "availablehours", "remaining", "available") if key in n), None)
    if remaining is None and total is not None and used is not None:
        remaining = max(0.0, total - used)
    if remaining is None:
        raise RunnerError("GPU quota remaining hours not available in Kaggle response")
    return {"remaining_hours": round(remaining, 3), "used_hours": used, "total_hours": total,
            "refresh_at": n.get("refreshat"),
            "note": "Snapshot only; running sessions may reserve additional hours"}


def quota_report(cli: KaggleCLI) -> dict:
    result = cli.call("quota", "--format", "json", check=False)
    if result.returncode:
        raise RunnerError("Cannot retrieve GPU quota from Kaggle CLI; upgrade `kaggle` and confirm API token. "
                          + result.text[-300:])
    return parse_gpu_quota(result.stdout)


def _list_page(cli: KaggleCLI, page: int, size: int, timeout_seconds: float = 12) -> list[dict]:
    response = cli.call("kernels", "list", "--mine", "--format", "json", "--sort-by", "dateRun",
                        "--page", str(page), "--page-size", str(size), timeout_seconds=timeout_seconds)
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


def active_report(cli: KaggleCLI, *, page_size: int = 100, max_pages: int = 5,
                  limit: int = 2, workers: int = 6, max_seconds: float = 45,
                  status_timeout_seconds: float = 10, strict: bool = False,
                  progress: Callable[[str], None] | None = None) -> dict:
    """Count active batch kernels without sequentially querying hundreds of runs.

    Status lookups are parallel but bounded in small batches. Progress is caller-
    supplied (the CLI writes it to stderr, keeping stdout machine-readable).
    Older *explicitly terminal* list entries can be treated as historical in
    default mode; `strict=True` verifies every entry live, with a time limit.
    This remains a best-effort batch snapshot, not a reservation of Kaggle slots.
    """
    if workers < 1 or workers > 16 or max_seconds <= 0 or status_timeout_seconds <= 0:
        raise RunnerError("--workers must be 1-16, --max-seconds and timeout must be positive")
    if max_pages < 1 or page_size < 1:
        raise RunnerError("Invalid Kaggle pagination settings")

    def notify(message: str) -> None:
        if progress is not None:
            progress(message)

    deadline = time.monotonic() + max_seconds
    seen: set[str] = set()
    active: list[dict] = []
    cancelling: list[dict] = []
    not_started: list[dict] = []
    missing_sessions: list[dict] = []
    scanned = 0
    live_checked = 0
    listed_terminal = 0
    # Kaggle list --sort-by dateRun places recent runs first. Even in fast mode
    # verify the newest entries, including those listed as COMPLETE. This helps
    # avoid trusting an immediately stale list snapshot during a new submission.
    recent_verify = 20
    batch_size = workers * 2

    def snapshot() -> dict:
        return {"running_count": len(active), "max_concurrent": limit,
                "has_slot": len(active) < limit, "active": active, "scanned": scanned,
                "cancelling": cancelling, "cancelling_count": len(cancelling),
                "source": "remote_batch_scan", "slot_guaranteed": False,
                "live_checked_count": live_checked,
                "listed_terminal_count": listed_terminal,
                "missing_sessions": missing_sessions,
                "missing_session_count": len(missing_sessions),
                "not_started": not_started, "not_started_count": len(not_started),
                "strict": strict,
                "note": "Best-effort batch-kernel snapshot, not a slot reservation. "
                        "Older completed entries may be taken from list status in fast mode; "
                        "interactive sessions may not be included"}

    def probe(ref: str, row: dict, timeout: float) -> tuple[str, str, str]:
        try:
            res = cli.call("kernels", "status", ref, check=False, timeout_seconds=timeout)
        except RunnerError as exc:
            raise RunnerError(f"Could not verify remote kernel status for {ref}: {exc}") from exc
        if res.returncode:
            if _session_not_found(res.text):
                listed = _listed_status(row)
                if listed in ACTIVE:
                    raise RunnerError(
                        f"Kaggle reports {ref} as {listed} in its notebook list "
                        "but the live session status is missing; refusing to guess "
                        "whether a concurrent slot is free"
                    )
                return ref, "missing", listed
            raise RunnerError(f"Could not verify remote kernel status for {ref}: {res.text[-250:]}")
        status = _status(res.text)
        if status == "unknown":
            detail = " ".join(res.text.split())[:350] or "<empty response>"
            raise RunnerError(
                f"Unrecognized Kaggle kernel status for {ref}; cannot safely count running jobs. "
                f"Kaggle response: {detail}. "
                f"Inspect with: python -m kaggle kernels status {ref}"
            )
        return ref, status, ""

    notify(f"Inspecting Kaggle batch kernels (up to {max_seconds:g}s, {workers} workers).")
    for page in range(1, max_pages + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RunnerError(f"Active run check exceeded {max_seconds:g}s; cannot verify free slots. "
                              "Retry or use `kgr active --max-seconds 120`.")
        notify(f"Listing notebook page {page}/{max_pages}...")
        try:
            rows = _list_page(cli, page, page_size,
                              timeout_seconds=min(12, max(0.1, remaining)))
        except RunnerError as exc:
            raise RunnerError(f"Could not list Kaggle notebooks (page {page}): {exc}") from exc
        pending: list[tuple[str, dict]] = []
        for row in rows:
            ref = _kernel_ref(row)
            if not ref:
                raise RunnerError("Kaggle kernel listing contains a row without a canonical ref")
            if ref in seen:
                continue
            seen.add(ref)
            scanned += 1
            listed = _listed_status(row)
            if (not strict and scanned > recent_verify
                    and listed in {"complete", "error", "cancelled", "failed"}):
                # Old explicit terminal statuses avoid a redundant status API
                # roundtrip. Never skip unknown or possibly in-flight states.
                listed_terminal += 1
                continue
            pending.append((ref, row))
        if pending:
            notify(f"Page {page}: {len(pending)} notebook statuses to verify, "
                   f"{listed_terminal} historical terminal entries skipped.")

        for start in range(0, len(pending), batch_size):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RunnerError(f"Active run check exceeded {max_seconds:g}s after {live_checked} "
                                  "status requests; cannot verify free slots. "
                                  "Retry or increase --max-seconds.")
            group = pending[start:start + batch_size]
            per_call = max(0.1, min(status_timeout_seconds, remaining))
            # Query only a bounded number of kernels at once to avoid putting
            # pressure on Kaggle's API or multiplying Python startup latency.
            with ThreadPoolExecutor(max_workers=min(workers, len(group))) as executor:
                future_map = {
                    executor.submit(probe, ref, row, per_call): ref for ref, row in group
                }
                try:
                    for future in as_completed(future_map, timeout=remaining):
                        ref, status, listed = future.result()
                        live_checked += 1
                        if status == "missing":
                            missing_sessions.append({"kernel_ref": ref, "listed_status": listed})
                        elif status in ACTIVE:
                            active.append({"kernel_ref": ref, "status": status})
                        elif status in CANCELLING:
                            cancelling.append({"kernel_ref": ref, "status": status})
                        elif status == "new_script":
                            not_started.append({"kernel_ref": ref, "status": status})
                except FuturesTimeout as exc:
                    for future in future_map:
                        future.cancel()
                    raise RunnerError(
                        f"Active run check exceeded {max_seconds:g}s while querying status; "
                        "cannot verify free slots. Retry or increase --max-seconds."
                    ) from exc
            notify(f"Verified {live_checked} live statuses; {len(active)} active, "
                   f"{len(cancelling)} cancelling, {len(missing_sessions)} missing sessions.")
            if len(active) >= limit:
                notify("Concurrent limit reached; stopping early without scanning older notebooks.")
                return snapshot()
        if len(rows) < page_size:
            notify(f"Active scan complete: {len(active)} active out of {scanned} listed.")
            return snapshot()
    raise RunnerError(
        f"Kaggle kernel listing exceeded {max_pages} pages ({scanned} entries); "
        "cannot verify active session count. Increase pagination support or use --strict "
        "for diagnostic inspection."
    )


def _ref_candidates(record: dict, owner: str | None) -> list[str]:
    """Possible corrected refs from explicit account metadata or stored push URLs.

    Never treat a candidate as valid until Kaggle confirms its status.
    The notebook slug must match the original one exactly.
    """
    ref = record["kernel_ref"]
    if "/" not in ref:
        return []
    original_owner, slug = ref.split("/", 1)
    possible: list[str] = []
    if owner and re.fullmatch(r"[a-zA-Z0-9_-]+", owner) and owner.lower() != original_owner.lower():
        possible.append(f"{owner}/{slug}")
    texts = [record.get("last_message") or ""]
    path = Path(record["staging_dir"]).parent / "manifest.json"
    if path.is_file():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if manifest.get("job_id") == record["job_id"]:
                texts += [manifest.get("submit_output") or "", manifest.get("kernel_ref") or ""]
        except (OSError, ValueError, TypeError):
            pass
    for value in texts:
        for match in re.finditer(r"https?://(?:www\.)?kaggle\.com/code/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+)", value):
            if match.group(2) == slug:
                possible.append(f"{match.group(1)}/{slug}")
        if value.count("/") == 1 and value.endswith("/" + slug):
            possible.append(value)
    return list(dict.fromkeys(candidate for candidate in possible if candidate != ref))


def tracked_active_report(cli: KaggleCLI, store: JobStore, *, owner: str | None = None,
                          account_profile: str | None = None,
                          limit: int = 2, workers: int = 6, max_seconds: float = 45,
                          progress: Callable[[str], None] | None = None) -> dict:
    """Fast best-effort check of runner-tracked jobs, NOT all Kaggle sessions.

    Only statuses that might still run are refreshed. Completed/cancelled jobs
    are excluded from the status requests. Cancellation acknowledgements are
    presented separately as uncertain; they do not prove an occupied slot.
    The Kaggle service remains the final authority at submission time.
    """
    if not 1 <= workers <= 16 or max_seconds <= 0:
        raise RunnerError("--workers must be 1-16 and --max-seconds must be positive")
    records = store.list(10001)
    if len(records) > 10000:
        raise RunnerError("Local ledger exceeds safe inspection limit; archive old jobs first")
    # Never filter local jobs by owner metadata: Kaggle can return a different
    # canonical owner during submit, and mixed CPU/GPU runs share one cap.
    candidates = [r for r in records if r["status"] in LOCAL_CANDIDATES
                  and (account_profile is None or r.get("account_profile") == account_profile)]
    if len(candidates) > 50:
        raise RunnerError("More than 50 local jobs need verification; use `kgr sync` to refresh old records")
    running: list[dict] = []
    cancelling: list[dict] = []
    missing: list[dict] = []
    not_started: list[dict] = []
    checked = 0
    unverified: list[dict] = []
    deadline = time.monotonic() + max_seconds
    if progress:
        progress(f"Checking {len(candidates)} runner-tracked jobs (no notebook listing).")

    def probe(record: dict, seconds: float) -> tuple[dict, str, str, str | None]:
        ref = record["kernel_ref"]
        refs = [ref, *_ref_candidates(record, owner)]
        errors: list[str] = []
        for index, candidate in enumerate(refs):
            try:
                result = cli.call("kernels", "status", candidate, check=False,
                                  timeout_seconds=seconds)
            except RunnerError as exc:
                errors.append(str(exc))
                continue
            if result.returncode:
                errors.append(result.text)
                continue
            state = _status(result.text)
            if state == "unknown":
                errors.append("unrecognized status")
                continue
            return record, state, candidate, None
        # A definite missing session with no other permission error is not an
        # active job. Permission errors remain uncertain and block preflight.
        if errors and all(_session_not_found(error) for error in errors):
            return record, "missing", ref, None
        # Do not print verbose Kaggle SDK advice in interactive CLI output.
        kind = "access_denied" if any(re.search(r"denied|forbidden|permission|\b403\b", e, re.I)
                                          for e in errors) else "unverified"
        return record, "unverified", ref, kind

    # At most `workers` concurrent subprocesses, and each has its own timeout.
    # Very large local histories are explicitly rejected above rather than
    # falsely declaring an available slot after checking a partial history.
    for start in range(0, len(candidates), workers):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RunnerError("Tracked job check timed out; slot state unverified")
        group = candidates[start:start + workers]
        with ThreadPoolExecutor(max_workers=min(workers, len(group))) as executor:
            futures = [executor.submit(probe, rec, min(8, max(0.1, remaining))) for rec in group]
            try:
                for future in as_completed(futures, timeout=remaining):
                    record, state, ref, issue = future.result()
                    checked += 1
                    if ref != record["kernel_ref"] and state not in {"missing", "unverified"}:
                        store.set_kernel_ref(record["job_id"], ref)
                    if state == "missing":
                        missing.append({"kernel_ref": ref, "job_id": record["job_id"]})
                        store.update(record["job_id"], status="missing_session",
                                     last_message="Remote kernel session no longer exists")
                    elif state == "unverified":
                        unverified.append({"kernel_ref": ref, "job_id": record["job_id"],
                                           "reason": issue})
                        store.update(record["job_id"], status="status_unavailable")
                    else:
                        # Immediately after a successful push, Kaggle may still
                        # report NEW_SCRIPT before the queued worker is visible.
                        # Hold a brief local reservation to avoid accepting a
                        # third submission during that propagation window.
                        if state == "new_script":
                            try:
                                age = (datetime.now(timezone.utc) -
                                       datetime.fromisoformat(record["created_at"])).total_seconds()
                            except (ValueError, TypeError):
                                age = 0  # Invalid timestamp: conservative
                            if age < 120:
                                state = "pending"
                        store.update(record["job_id"], status=state)
                        item = {"kernel_ref": ref, "job_id": record["job_id"], "status": state}
                        if state in ACTIVE:
                            running.append(item)
                        elif state in CANCELLING:
                            cancelling.append(item)
                        elif state == "new_script":
                            not_started.append(item)
            except FuturesTimeout as exc:
                for future in futures:
                    future.cancel()
                raise RunnerError("Tracked job check timed out; slot state unverified") from exc
        if len(running) >= limit:
            break
    return {"running_count": len(running), "max_concurrent": limit,
            "has_slot": len(running) < limit and not unverified, "active": running,
            "unverified": unverified, "unverified_count": len(unverified),
            "cancelling": cancelling, "cancelling_count": len(cancelling),
            "missing_sessions": missing, "missing_session_count": len(missing),
            "not_started": not_started, "not_started_count": len(not_started),
            "scanned": len(candidates), "live_checked_count": checked,
            "source": "tracked_jobs", "slot_guaranteed": False,
            "note": "Only runner-tracked jobs are checked. Other notebooks and interactive "
                    "sessions are invisible here. Cancelling acknowledgement is not proof "
                    "of an occupied or available slot; Kaggle enforces real capacity at push."}


def check_preflight(cli: KaggleCLI, *, gpu: str, required_gpu_hours: float = 0.25,
                    max_concurrent: int = 2, workers: int = 6,
                    max_seconds: float = 45, strict: bool = False,
                    progress: Callable[[str], None] | None = None,
                    store: JobStore | None = None, owner: str | None = None,
                    account_profile: str | None = None,
                    remote: bool | None = None) -> dict:
    if required_gpu_hours <= 0 or max_concurrent < 1 or max_concurrent > 2:
        raise RunnerError("--required-gpu-hours must be positive and --max-concurrent must be 1 or 2")
    # Backward compatibility for Python callers without a local JobStore;
    # the CLI supplies it and therefore defaults to the fast tracked-job mode.
    use_remote = (store is None) if remote is None else remote
    if strict:
        use_remote = True
    if use_remote:
        runs = active_report(cli, limit=max_concurrent, workers=workers,
                             max_seconds=max_seconds, strict=strict, progress=progress)
    else:
        if store is None:
            raise RunnerError("Fast preflight needs a local JobStore")
        runs = tracked_active_report(cli, store, owner=owner, account_profile=account_profile,
                                     limit=max_concurrent, workers=workers,
                                     max_seconds=max_seconds, progress=progress)
    reasons: list[str] = []
    if runs["running_count"] >= max_concurrent:
        reasons.append(f"Concurrent batch jobs {runs['running_count']}/{max_concurrent}")
    if runs.get("unverified_count"):
        reasons.append(f"{runs['unverified_count']} historical job(s) need attention; "
                       "use `kgr repair JOB_ID --owner CORRECT_OWNER` or "
                       "`kgr archive JOB_ID --confirm-inactive`")
    quota = None
    quota_error = None
    if gpu != "cpu":
        try:
            quota = quota_report(cli)
        except RunnerError as exc:
            quota_error = str(exc)
            reasons.append(f"GPU quota unavailable: {quota_error}")
        else:
            if quota["remaining_hours"] < required_gpu_hours:
                reasons.append(f"GPU remaining {quota['remaining_hours']}h < required {required_gpu_hours}h")
    warnings: list[str] = []
    if runs.get("cancelling_count", 0):
        warnings.append(f"{runs['cancelling_count']} cancel-acknowledged notebooks: "
                        "their live resource allocation is not confirmed")
    if not use_remote:
        warnings.append("Fast mode checks runner-tracked jobs only; Kaggle may have "
                        "other active sessions. Acceptance is determined by Kaggle at submit.")
    return {"ok": not reasons, "gpu": gpu, "active": runs, "gpu_quota": quota,
            "gpu_quota_error": quota_error,
            "required_gpu_hours": required_gpu_hours if gpu != "cpu" else 0,
            "slot_verified": False,
            "warnings": warnings, "reasons": reasons}
