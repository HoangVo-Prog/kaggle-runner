"""Foreground, restart-safe experiment lifecycle for authorized Kaggle agents.

Only remote reads may be retried. All ambiguous pushes become action-required;
this module does not rotate accounts, cancel runs or make unsafe re-submissions.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import random
import statistics
import time
from typing import Callable

from .accounts import load_account
from .agent_ledger import AgentLedger
from .api import KaggleCLI
from .config import RunnerError, Settings
from .jobs import JobStore
from .packaging import slugify, validate_model_ref, validate_ref
from .preflight import check_preflight
from .runner import create_job, fetch_job
from .source_contract import canonical, digest, inspect_source
from .schema_validation import validate_contract
from .experiments.results import _checksum
from .status import parse_kernel_status
from .team import get_profile

SPEC_VERSION = "1.0"
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
RESULT_NAME = re.compile(r"^[A-Za-z0-9._/-]{1,160}$")


def _fail(code: str, message: str) -> None:
    raise RunnerError(f"{code}: {message}")


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or not RESULT_NAME.fullmatch(name) or name.startswith("/") or ".." in Path(name).parts:
        _fail("UNSAFE_ARTIFACT", "Output filename must be a safe relative POSIX path")
    return name


def _reference_list(items: list, kind: str) -> list[dict]:
    if not isinstance(items, list):
        _fail("INVALID_SPEC", f"{kind}s must be a list")
    result = []
    for item in items:
        if isinstance(item, str):
            item = {"ref": item}
        if not isinstance(item, dict) or not isinstance(item.get("ref"), str):
            _fail("INVALID_SPEC", f"Each {kind} requires a ref")
        ref = validate_model_ref(item["ref"]) if kind == "model" else validate_ref(item["ref"], kind)
        result.append({**item, "ref": ref})
    if len({x['ref'] for x in result}) != len(result):
        _fail("INVALID_SPEC", f"Duplicate {kind} references")
    return result


def load_spec(spec_file: str | Path) -> tuple[dict, dict]:
    path = Path(spec_file).expanduser().resolve()
    try:
        spec = json.loads(path.read_text(encoding="utf-8"),
                          parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (OSError, ValueError, UnicodeError) as exc:
        _fail("INVALID_SPEC", f"Cannot read JSON experiment spec: {path.name}")
    if not isinstance(spec, dict) or spec.get("schema_version") != SPEC_VERSION:
        _fail("INVALID_SPEC", "Unsupported ExperimentSpec version (expected 1.0)")
    validate_contract(spec, "experiment_spec")
    for key in ("experiment_id", "attempt_id", "project", "objective"):
        val = spec.get(key)
        if not isinstance(val, str) or not val or (key in {"experiment_id", "attempt_id"} and not ID.fullmatch(val)):
            _fail("INVALID_SPEC", f"Invalid or missing {key}")
    src = spec.get("source")
    if not isinstance(src, dict) or not isinstance(src.get("root"), str) or not isinstance(src.get("entrypoint"), str):
        _fail("INVALID_SPEC", "source requires root and entrypoint")
    root = (path.parent / src["root"]).resolve()
    entry = src["entrypoint"]
    kind = src.get("kind")
    if kind not in {"python", "notebook"}:
        _fail("INVALID_SPEC", "source.kind must be python or notebook")
    if not isinstance(spec.get("parameters", {}), dict):
        _fail("INVALID_SPEC", "parameters must be a JSON object")
    if type(spec.get("seed")) is not int or spec["seed"] < 0:
        _fail("INVALID_SPEC", "seed must be a nonnegative integer")
    protocol = spec.get("protocol")
    if not isinstance(protocol, dict) or not isinstance(protocol.get("id"), str) or not isinstance(protocol.get("dataset_split"), str):
        _fail("INVALID_SPEC", "protocol requires id and dataset_split")
    auth = spec.get("authorization")
    if not isinstance(auth, dict) or not isinstance(auth.get("allowed_profiles"), list) or not auth["allowed_profiles"]:
        _fail("UNAUTHORIZED_ACCOUNT", "authorization.allowed_profiles is required")
    allowed = auth["allowed_profiles"]
    if any(not isinstance(x, str) or not PROFILE.fullmatch(x) for x in allowed) or len(set(allowed)) != len(allowed):
        _fail("UNAUTHORIZED_ACCOUNT", "Invalid or duplicate authorized account aliases")
    if not isinstance(auth.get("requires_submit_approval", False), bool):
        _fail("INVALID_SPEC", "requires_submit_approval must be boolean")
    res = spec.get("resources", {})
    if not isinstance(res, dict) or res.get("accelerator", "cpu") not in {"cpu", "t4x2"}:
        _fail("INVALID_SPEC", "resources.accelerator must be cpu or t4x2")
    if type(res.get("max_concurrent", 2)) is not int or res.get("max_concurrent", 2) not in {1, 2}:
        _fail("INVALID_SPEC", "resources.max_concurrent must be 1 or 2")
    if not isinstance(res.get("required_gpu_hours", 0.25), (int, float)) or res.get("required_gpu_hours", 0.25) <= 0:
        _fail("INVALID_SPEC", "required_gpu_hours must be positive")
    meta = spec.get("expected_outputs")
    if not isinstance(meta, list) or not meta or any(not isinstance(x, str) for x in meta):
        _fail("INVALID_SPEC", "expected_outputs must contain relative file names")
    expected = [_safe_name(x) for x in meta]
    if len(set(expected)) != len(expected):
        _fail("INVALID_SPEC", "Duplicate expected outputs")
    result_file = _safe_name(spec.get("result_file", "result.json"))
    if result_file not in expected:
        _fail("INVALID_SPEC", "expected_outputs must include result_file")
    metric = spec.get("primary_metric")
    if not isinstance(metric, dict) or not isinstance(metric.get("name"), str) or not isinstance(metric.get("unit"), str) or type(metric.get("higher_is_better")) is not bool:
        _fail("INVALID_SPEC", "primary_metric requires name, unit and higher_is_better")
    if metric["unit"] not in {"fraction", "percent", "count", "seconds", "custom"}:
        _fail("INVALID_SPEC", "Unsupported metric unit")
    datasets = _reference_list(spec.get("datasets", []), "dataset")
    models = _reference_list(spec.get("models", []), "model")
    for rule_name in ("include", "exclude"):
        rules = src.get(rule_name, [])
        if not isinstance(rules, list) or any(not isinstance(x, str) or not x or
                                              x.startswith("/") or ".." in Path(x).parts
                                              for x in rules):
            _fail("INVALID_SPEC", f"source.{rule_name} must contain safe relative globs")
    if any(re.search(r"(?i)(?:password|secret|api.?key|api.?token|credential)", key)
           for key in spec.get("parameters", {})):
        _fail("INVALID_SPEC", "Credentials must not be provided as runtime parameters")
    install = src.get("install_requirements", False)
    internet = res.get("enable_internet", False)
    if type(install) is not bool or type(internet) is not bool or (install and not internet):
        _fail("INVALID_DEPENDENCY", "Installing requirements needs enable_internet=true")
    inspected = inspect_source(root, entry=entry, dataset_refs=[d['ref'] for d in datasets],
                               model_refs=[m['ref'] for m in models], max_mb=src.get("max_source_mb", 3),
                               include=src.get("include"), exclude=src.get("exclude"))
    if inspected["source_kind"] != kind:
        _fail("INVALID_SPEC", "Declared source.kind does not match entrypoint")
    expected_sha = src.get("sha256")
    if expected_sha and expected_sha != inspected["source_sha256"]:
        _fail("SOURCE_REVISION_MISMATCH", "Source content differs from pinned source.sha256")
    if src.get("revision") and src["revision"] != inspected["source_revision"]:
        _fail("SOURCE_REVISION_MISMATCH", "Git revision differs from pinned source.revision")
    if install and "requirements.txt" not in inspected["required_dependencies"]:
        _fail("INVALID_DEPENDENCY", "requirements.txt not found in package")
    # Snapshot resolved metadata for retry comparison; no credentials or absolute paths in fingerprints.
    normalized = json.loads(canonical(spec))
    normalized["datasets"] = datasets
    normalized["models"] = models
    normalized["source"]["sha256"] = inspected["source_sha256"]
    normalized["source"]["revision"] = inspected["source_revision"]
    fingerprint = digest(canonical(normalized))
    return normalized, {**inspected, "fingerprint": fingerprint, "spec_path": str(path),
                        "resolved_root": str(root), "result_file": result_file}


def validate(spec_path: str | Path) -> dict:
    spec, inspection = load_spec(spec_path)
    return {"schema_version": SPEC_VERSION, "ok": True, "experiment_id": spec["experiment_id"],
            "attempt_id": spec["attempt_id"], "fingerprint": inspection["fingerprint"],
            "package": inspection, "authorization": spec["authorization"],
            "resource": spec.get("resources", {}), "remote_submitted": False}


def _credentials(alias: str, env_file: str | None):
    if alias == "default":
        acc = load_account(env_file)
        if not acc.token or not acc.owner:
            _fail("CREDENTIAL_MISSING", "Default account requires owner and API token")
        return acc.owner, acc.token, acc.email, None
    profile = get_profile(alias, env_file)
    return profile.owner, profile.token, profile.email, profile.alias


def _read_with_retry(action: Callable[[], object], *, attempts: int = 3, delay: float = 0.5):
    for n in range(attempts):
        try:
            return action()
        except RunnerError as exc:
            msg = str(exc).lower()
            transient = any(token in msg for token in ("timed out", "timeout", "429", "rate limit", "502", "503", "504", "500"))
            if not transient or n == attempts - 1:
                raise
            time.sleep(delay * 2 ** n + random.uniform(0, delay * 0.2))  # read-only retry with jitter


def _choose_account(settings: Settings, store: JobStore, spec: dict, env_file: str | None,
                    requested: str) -> tuple[str, str, str | None, str | None, KaggleCLI]:
    allowed = spec["authorization"]["allowed_profiles"]
    if requested != "auto" and requested not in allowed:
        _fail("UNAUTHORIZED_ACCOUNT", "Requested profile is outside experiment authorization")
    aliases = sorted(allowed) if requested == "auto" else [requested]
    problems = []
    res = spec.get("resources", {})
    for alias in aliases:
        try:
            owner, token, email, profile = _credentials(alias, env_file)
            cli = KaggleCLI(settings, token=token)
            report = _read_with_retry(lambda: check_preflight(
                cli, gpu=res.get("accelerator", "cpu"),
                required_gpu_hours=res.get("required_gpu_hours", .25),
                max_concurrent=res.get("max_concurrent", 2), store=store,
                owner=owner, account_profile=profile, remote=False))
            if report["ok"]:
                return owner, email, profile, alias, cli
            problems.append(f"{alias}: " + "; ".join(report["reasons"]))
        except RunnerError as exc:
            problems.append(f"{alias}: {str(exc)[:240]}")
    _fail("ACCOUNT_PREFLIGHT_BLOCKED", "; ".join(problems) or "No authorized account eligible")


def submit(spec_file: str | Path, *, settings: Settings, env_file: str | None = None,
           account: str = "auto", approve: bool = False) -> dict:
    spec, inspected = load_spec(spec_file)
    ledger = AgentLedger(settings.state / "jobs.sqlite3")
    store = JobStore(settings.state)
    exp, attempt = spec["experiment_id"], spec["attempt_id"]
    old = ledger.get(exp, attempt)
    if old is not None:
        # Re-submission cannot switch profiles or mutate source/params under same attempt ID.
        saved = json.loads(old["spec_json"])
        if canonical(saved) != canonical(spec):
            _fail("SUBMISSION_CONFLICT", "Attempt already exists with different inputs")
        return {"schema_version": SPEC_VERSION, "ok": True, "existing": True,
                "job_id": old["job_id"], "state": old["state"],
                "kernel_ref": old["kernel_ref"], "action": "resume_or_reconcile_not_resubmit"}
    if spec["authorization"].get("requires_submit_approval", False) and not approve:
        _fail("APPROVAL_REQUIRED", "Operator must supply explicit --approve for this experiment")
    owner, email, profile, alias, cli = _choose_account(settings, store, spec, env_file, account)
    job_id = digest(canonical({"experiment_id": exp, "attempt_id": attempt}))[:32]
    root = Path(inspected["resolved_root"])
    ref = f"{owner}/kgr-{slugify(root.name,24)}-{job_id[:10]}".lower()
    fp = digest(canonical({"spec": inspected["fingerprint"], "account": alias, "owner": owner}))
    record, created = ledger.reserve(experiment_id=exp, attempt_id=attempt, fingerprint=fp,
                                      job_id=job_id, spec=spec, account_profile=profile,
                                      account_owner=owner, kernel_ref=ref,
                                      limit=spec.get("resources", {}).get("max_concurrent", 2))
    if not created:
        return {"schema_version": SPEC_VERSION, "ok": True, "existing": True,
                "job_id": job_id, "state": record["state"], "kernel_ref": ref}
    try:
        ledger.transition(job_id, "staging")
        # runner.py records not_pushed while staging, then records the irreversible
        # ambiguity boundary before invoking this callback and Kaggle's push.
        result = create_job(
            settings, cli, store, source=str(root), entry=spec["source"]["entrypoint"],
            owner=owner, gpu=spec.get("resources", {}).get("accelerator", "cpu"),
            dataset_refs=[d["ref"] for d in spec.get("datasets", [])],
            model_refs=[m["ref"] for m in spec.get("models", [])],
            arguments=spec.get("arguments", []),
            install_requirements=spec["source"].get("install_requirements", False),
            enable_internet=spec.get("resources", {}).get("enable_internet", False),
            max_source_mb=spec["source"].get("max_source_mb", 3),
            timeout_seconds=spec.get("budgets", {}).get("push_timeout_seconds", 0),
            account_profile=profile, account_owner=owner, account_email=email,
            notebook_file=str(root / spec["source"]["entrypoint"]) if spec["source"]["kind"] == "notebook" else None,
            max_concurrent=spec.get("resources", {}).get("max_concurrent", 2),
            job_id=job_id, experiment_id=exp, attempt_id=attempt,
            parameters=spec.get("parameters", {}),
            seed=spec["seed"],
            expected_source_sha256=inspected["source_sha256"],
            expected_result=inspected["result_file"],
            source_include=spec["source"].get("include"),
            source_exclude=spec["source"].get("exclude"),
            on_submission_start=lambda: ledger.transition(
                job_id, "submitting", note="Durable submission_unknown marker written before push"))
        if result["kernel_ref"].casefold() != ref.casefold():
            ledger.transition(job_id, "unknown_remote", note="Unexpected remote kernel identity")
            _fail("REMOTE_IDENTITY_MISMATCH", "Remote owner/slug differs from reserved identity; reconcile")
        ledger.transition(job_id, "submitted", note="Kaggle push returned successfully")
        return {"schema_version": SPEC_VERSION, "ok": True, "existing": False,
                "job_id": job_id, "kernel_ref": ref, "state": "submitted", "account_profile": alias,
                "url": "https://www.kaggle.com/code/" + ref}
    except Exception:
        current = ledger.by_job(job_id)
        if current["state"] not in {"unknown_remote", "submitted"}:
            ledger.transition(job_id, "unknown_remote", note="Submission outcome uncertain; operator reconciliation required")
        raise


def _remote_status(settings: Settings, ledger: AgentLedger, attempt: dict,
                   env_file: str | None) -> str:
    profile = attempt["account_profile"] or "default"
    owner, token, _, _ = _credentials(profile, env_file)
    if owner.casefold() != attempt["account_owner"].casefold():
        _fail("CREDENTIAL_OWNER_MISMATCH", "Original profile has a different owner; no fallback")
    cli = KaggleCLI(settings, token=token)
    response = _read_with_retry(lambda: cli.call("kernels", "status", attempt["kernel_ref"], timeout_seconds=15))
    return parse_kernel_status(response.text)


def reconcile(settings: Settings, env_file: str | None = None,
              experiment_id: str | None = None) -> dict:
    ledger = AgentLedger(settings.state / "jobs.sqlite3")
    entries = []
    for item in ledger.list(experiment_id):
        if item["state"] in {"succeeded", "invalid_result", "failed", "cancelled", "blocked"}:
            continue
        remote = None
        action = "continue_monitoring"
        evidence = "unknown"
        try:
            job = JobStore(settings.state).get(item["job_id"])
            marker = Path(job["staging_dir"]).parent / "submission.json"
            if marker.is_file():
                state = json.loads(marker.read_text(encoding="utf-8")).get("state")
                if state in {"not_pushed", "submission_unknown", "remote_confirmed"}:
                    evidence = state
        except (RunnerError, OSError, ValueError, TypeError):
            pass
        try:
            remote = _remote_status(settings, ledger, item, env_file)
            if remote is not None and evidence == "submission_unknown":
                evidence = "remote_confirmed"
            transition = {"queued": "submitted", "pending": "submitted", "new_script": "submitted",
                          "initializing": "running", "starting": "running", "running": "running",
                          "complete": "remote_complete", "error": "failed", "failed": "failed", "cancelled": "cancelled"}.get(remote)
            if transition is None:
                outcome = "manual_review"
                action = "operator_must_verify_remote_state"
            elif transition == item["state"]:
                outcome = transition
            else:
                # Conservative forward transitions only; unexpected state is manual review.
                try:
                    ledger.transition(item["job_id"], transition, note=f"Remote status: {remote}")
                    if transition in {"remote_complete", "failed", "cancelled"}:
                        # Keep the legacy job ledger aligned so completed jobs do not
                        # occupy the next local slot after the agent lease is released.
                        try:
                            JobStore(settings.state).update(item["job_id"], status=remote)
                        except RunnerError:
                            pass
                    else:
                        ledger.renew(item["job_id"])
                    outcome = transition
                except RunnerError:
                    outcome = "manual_review"
                    action = "operator_must_review_state_transition"
        except RunnerError:
            outcome = "manual_review"
            action = "verify_original_account_and_kernel_access"
        entries.append({"job_id": item["job_id"], "kernel_ref": item["kernel_ref"],
                        "experiment_id": item["experiment_id"], "attempt_id": item["attempt_id"],
                        "outcome": outcome, "original_state": item["state"],
                        "remote_status": remote, "submission_evidence": evidence,
                        "action": action})
    manual_review = any(x["outcome"] == "manual_review" for x in entries)
    return {"schema_version": SPEC_VERSION, "ok": not manual_review,
            "outcome": "manual_review" if manual_review else "reconciled",
            "attempts": entries, "stale_leases_held": len(ledger.stale_leases())}


def show(settings: Settings, experiment_id: str) -> dict:
    ledger = AgentLedger(settings.state / "jobs.sqlite3")
    attempts = ledger.list(experiment_id)
    return {"schema_version": SPEC_VERSION, "ok": True, "experiment_id": experiment_id,
            "attempts": [{**{k:v for k,v in a.items() if k not in ("spec_json", "validation_json")},
                          "events": ledger.events(a["job_id"])} for a in attempts]}


def validate_result(settings: Settings, job_id: str, *, save: bool = True) -> dict:
    """Compatibility wrapper; implementation lives in experiments.results."""
    from .experiments.results import validate_result as validate
    return validate(settings, job_id, save=save)

def resume(settings: Settings, experiment_id: str, *, env_file: str | None = None,
           wait: bool = False, poll_seconds: int = 20, max_wait_minutes: int = 45) -> dict:
    if poll_seconds < 1 or max_wait_minutes < 1:
        _fail("INVALID_SPEC", "poll_seconds and max_wait_minutes must be positive")
    ledger = AgentLedger(settings.state / "jobs.sqlite3")
    entries = ledger.list(experiment_id)
    if not entries:
        _fail("UNKNOWN_EXPERIMENT", "No recorded experiment attempts")
    results = []
    deadline = time.monotonic() + max_wait_minutes * 60
    for row in entries:
        state = row["state"]
        if state in {"succeeded", "invalid_result", "failed", "cancelled", "blocked"}:
            results.append({"job_id": row["job_id"], "state": state})
            continue
        while state not in {"remote_complete", "fetch_failed", "succeeded", "invalid_result", "failed", "cancelled"}:
            outcome = reconcile(settings, env_file, experiment_id)
            current = ledger.by_job(row["job_id"])
            state = current["state"]
            if state == "unknown_remote" or any(i["job_id"] == row["job_id"] and i["outcome"] == "manual_review" for i in outcome["attempts"]):
                results.append({"job_id": row["job_id"], "state": state,
                                "action_required": "manual_review_remote_submission"})
                break
            if not wait or time.monotonic() >= deadline:
                results.append({"job_id": row["job_id"], "state": state,
                                "action_required": "resume_later" if state not in {"remote_complete", "failed", "cancelled"} else None})
                break
            if state not in {"remote_complete", "failed", "cancelled"}:
                time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))
        if state in {"remote_complete", "fetch_failed"}:
            try:
                owner, token, _, _ = _credentials(row["account_profile"] or "default", env_file)
                if owner.lower() != row["account_owner"].lower():
                    _fail("CREDENTIAL_OWNER_MISMATCH", "Cannot fetch with another account")
                cli = KaggleCLI(settings, token=token)
                _read_with_retry(lambda: fetch_job(cli, JobStore(settings.state), settings.state,
                                                   row["job_id"], None, force=True))
                validation = validate_result(settings, row["job_id"])
                results.append({"job_id": row["job_id"], "state": ledger.by_job(row["job_id"])["state"],
                                "validation": validation})
            except RunnerError:
                if ledger.by_job(row["job_id"])["state"] == "remote_complete":
                    ledger.transition(row["job_id"], "fetch_failed", note="Artifact download unsuccessful; safe to resume")
                results.append({"job_id": row["job_id"], "state": "fetch_failed",
                                "action_required": "retry_fetch_with_original_credentials"})
    return {"schema_version": SPEC_VERSION, "ok": all(r["state"] == "succeeded" for r in results),
            "experiment_id": experiment_id, "attempts": results}


def aggregate(settings: Settings, experiment_id: str) -> dict:
    ledger = AgentLedger(settings.state / "jobs.sqlite3")
    attempts = ledger.list(experiment_id)
    if not attempts:
        _fail("UNKNOWN_EXPERIMENT", "Experiment has no tracked attempts")
    valid = []
    excluded = []
    expected_definition = None
    for entry in attempts:
        verdict = validate_result(settings, entry["job_id"], save=False) if entry["state"] == "succeeded" else None
        if not verdict or not verdict["ok"]:
            excluded.append({"job_id": entry["job_id"], "state": entry["state"]})
            continue
        spec = json.loads(entry["spec_json"])
        key = canonical({"source": spec["source"]["sha256"], "protocol": spec["protocol"],
                         "datasets": spec.get("datasets", []), "models": spec.get("models", []),
                         "primary_metric": spec["primary_metric"],
                         "parameters": spec.get("parameters", {})})
        if expected_definition is None:
            expected_definition = key
        if key != expected_definition:
            _fail("INCOMPATIBLE_RESULTS", "Different dataset/protocol/source/metric/parameters; refusing aggregation")
        name = spec["primary_metric"]["name"]
        value = verdict["metrics"][name]["value"]
        valid.append({"job_id": entry["job_id"], "attempt_id": entry["attempt_id"],
                      "seed": spec["seed"], "value": value,
                      "result_sha256": verdict["result_sha256"]})
    vals = [r["value"] for r in valid]
    mean = statistics.mean(vals) if vals else None
    stdev = statistics.stdev(vals) if len(vals) >= 2 else None
    # Illustrative t-free normal approximation only for at least 3 independent attempts.
    ci = [mean - 1.96 * stdev / math.sqrt(len(vals)), mean + 1.96 * stdev / math.sqrt(len(vals))] if len(vals) >= 3 else None
    return {"schema_version": SPEC_VERSION, "ok": bool(vals), "experiment_id": experiment_id,
            "valid_runs": len(valid), "invalid_or_missing_runs": len(excluded),
            "runs": valid, "excluded": excluded, "mean": mean,
            "sample_stddev": stdev, "ci95_normal_approx": ci,
            "warnings": (["Normal CI assumes independent comparable runs; n>=3 is not a guarantee of coverage"] if ci else
                         ["Confidence interval omitted for fewer than 3 valid runs"])}


def report(settings: Settings, experiment_id: str, *, format: str = "md") -> str | dict:
    ledger = AgentLedger(settings.state / "jobs.sqlite3")
    attempts = ledger.list(experiment_id)
    if not attempts:
        _fail("UNKNOWN_EXPERIMENT", "No such experiment")
    aggregation = aggregate(settings, experiment_id)
    first = json.loads(attempts[0]["spec_json"])
    data = {"schema_version": SPEC_VERSION, "experiment_id": experiment_id,
            "objective": first["objective"], "specification": first,
            "attempts": [{"job_id": a["job_id"], "attempt_id": a["attempt_id"],
                           "state": a["state"], "scientific_status": a["scientific_status"],
                           "account_profile": a["account_profile"] or "default",
                           "account_owner": a["account_owner"], "kernel_ref": a["kernel_ref"],
                           "url": "https://www.kaggle.com/code/" + a["kernel_ref"],
                           "events": ledger.events(a["job_id"]),
                           "validation": json.loads(a["validation_json"]) if a["validation_json"] else None}
                          for a in attempts], "aggregation": aggregation}
    if format == "json":
        return data
    if format != "md":
        _fail("INVALID_SPEC", "report format must be md or json")
    lines = [f"# Experiment {experiment_id}", "", "## Objective", first["objective"], "",
             "## Reproducibility", f"- Source SHA256: `{first['source']['sha256']}`",
             f"- Revision: `{first['source'].get('revision')}`",
             f"- Protocol: `{first['protocol']['id']}` / `{first['protocol']['dataset_split']}`",
             f"- Parameters: `{json.dumps(first.get('parameters', {}), sort_keys=True)}`", "",
             "## Execution and validation"]
    for item in data["attempts"]:
        lines += [f"### Attempt {item['attempt_id']}",
                  f"- Job: `{item['job_id']}`; account: `{item['account_profile']}`",
                  f"- Kaggle: {item['url']}",
                  f"- Execution: **{item['state']}**; scientific result: **{item['scientific_status']}**",
                  f"- Events: {len(item['events'])}",
                  f"- Validation: `{json.dumps((item['validation'] or {}).get('errors', []))}`", ""]
    lines += ["## Aggregated evidence",
              f"- Valid: {aggregation['valid_runs']}; excluded: {aggregation['invalid_or_missing_runs']}",
              f"- Mean: {aggregation['mean']}; sample SD: {aggregation['sample_stddev']}",
              f"- 95% normal-approximation CI (if justified): {aggregation['ci95_normal_approx']}", "",
              "## Limitations", "Local SQLite reservations do not represent authoritative Kaggle capacity.",
              "Remote COMPLETE is not scientific success unless validated; unknown submissions require reconciliation."]
    return "\n".join(lines) + "\n"


def evidence_bundle(settings: Settings, experiment_id: str) -> dict:
    """Content-addressed evidence manifest. Never includes raw tokens or log output.

    Large artifacts stay in output directories and are referenced by checksum.
    Status event history, frozen specs, validations and download checksums are frozen.
    """
    from .source_contract import _make_zip
    ledger = AgentLedger(settings.state / "jobs.sqlite3")
    rows = ledger.list(experiment_id)
    if not rows:
        _fail("UNKNOWN_EXPERIMENT", "No evidence for requested experiment")
    sources: list[tuple[str, bytes]] = []
    index = {"schema_version": SPEC_VERSION, "experiment_id": experiment_id,
             "attempts": [], "limitations": ["Kaggle access is not independently attested by local SQLite",
                                              "Raw logs omitted to avoid credential or private-output leakage"]}
    for r in rows:
        jid = r["job_id"]
        spec = json.loads(r["spec_json"])
        events = ledger.events(jid)
        item = {"job_id": jid, "state": r["state"], "scientific_status": r["scientific_status"],
                "fingerprint": r["fingerprint"], "kernel_ref": r["kernel_ref"],
                "account_profile": r["account_profile"] or "default",
                "spec_file": f"{jid}/spec.json", "events_file": f"{jid}/events.json"}
        sources.append((f"{jid}/spec.json", canonical(spec)))
        sources.append((f"{jid}/events.json", canonical(events)))
        if r["validation_json"]:
            sources.append((f"{jid}/validation.json", canonical(json.loads(r["validation_json"]))))
            item["validation_file"] = f"{jid}/validation.json"
        try:
            job = JobStore(settings.state).get(jid)
            folder = Path(job.get("output_dir") or "")
        except RunnerError:
            folder = Path(settings.state / "outputs" / jid)
        download = folder / "kgr_download_manifest.json"
        if download.is_file():
            try:
                doc = json.loads(download.read_text(encoding="utf-8"))
                # Discard remote CLI output, which is untrusted text.
                reduced = {"job_id": doc.get("job_id"), "kernel_ref": doc.get("kernel_ref"),
                           "files": doc.get("files", [])}
                sources.append((f"{jid}/download_checksums.json", canonical(reduced)))
                item["download_checksums_file"] = f"{jid}/download_checksums.json"
            except (OSError, ValueError):
                item["download_manifest_error"] = True
        index["attempts"].append(item)
    sources.append(("index.json", canonical(index)))
    data = _make_zip(sources)
    sha = digest(data)
    root = settings.state / "evidence" / experiment_id
    root.mkdir(parents=True, exist_ok=True)
    dest = root / (sha + ".zip")
    if dest.exists() and _checksum(dest) != sha:
        _fail("EVIDENCE_CONFLICT", "Existing evidence content differs from its filename")
    if not dest.exists():
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    return {"schema_version": SPEC_VERSION, "ok": True, "experiment_id": experiment_id,
            "bundle_path": str(dest), "sha256": sha, "bytes": len(data),
            "attempt_count": len(rows)}
