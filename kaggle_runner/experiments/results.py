"""Scientific result and artifact validation for versioned experiment attempts."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re

from ..agent_ledger import AgentLedger
from ..config import RunnerError, Settings
from ..jobs import JobStore
from ..schema_validation import validate_contract

SPEC_VERSION = "1.0"
RESULT_NAME = re.compile(r"^[A-Za-z0-9._/-]{1,160}$")

def _safe_name(name: str) -> str:
    if not isinstance(name, str) or not RESULT_NAME.fullmatch(name) or name.startswith("/") or ".." in Path(name).parts:
        raise RunnerError("UNSAFE_ARTIFACT: Output filename must be a safe relative POSIX path")
    return name

def _checksum(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as inp:
        for block in iter(lambda: inp.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def validate_result(settings: Settings, job_id: str, *, save: bool = True) -> dict:
    ledger = AgentLedger(settings.state / "jobs.sqlite3")
    attempt = ledger.by_job(job_id)
    spec = json.loads(attempt["spec_json"])
    errors = []
    if attempt["state"] not in {"remote_complete", "succeeded", "invalid_result", "fetch_failed"}:
        errors.append("REMOTE_NOT_COMPLETE: Cannot claim scientific success without confirmed COMPLETE")
    try:
        job = JobStore(settings.state).get(job_id)
        base = Path(job.get("output_dir") or "")
    except RunnerError:
        base = Path(settings.state / "outputs" / job_id)
    result_name = _safe_name(spec.get("result_file", "result.json"))
    metrics = {}
    fingerprint = ""
    artifact_inventory = []
    result = None
    result_read = False
    if not base.is_dir() or not (base / result_name).is_file():
        errors.append(f"MISSING_ARTIFACT: {result_name}")
    else:
        try:
            result = json.loads((base / result_name).read_text(encoding="utf-8"),
                                parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
            result_read = True
            fingerprint = _checksum(base / result_name)
        except (OSError, ValueError, UnicodeError):
            errors.append("INVALID_EXPERIMENT_RESULT: malformed JSON or non-finite numeric literal")
    if result_read and not isinstance(result, dict):
        errors.append("INVALID_EXPERIMENT_RESULT: top-level JSON must be an object")
    if isinstance(result, dict):
        try:
            validate_contract(result, "experiment_result")
        except RunnerError as exc:
            errors.append(str(exc))
        for name, expected in (("schema_version", SPEC_VERSION),
                               ("experiment_id", spec["experiment_id"]),
                               ("attempt_id", spec["attempt_id"]),
                               ("status", "succeeded"), ("seed", spec["seed"]),
                               ("protocol_id", spec["protocol"]["id"]),
                               ("source_sha256", spec["source"]["sha256"])):
            if result.get(name) != expected:
                errors.append(f"RESULT_MISMATCH: {name}")
        dataset = result.get("dataset")
        if not isinstance(dataset, dict) or dataset.get("split") != spec["protocol"]["dataset_split"]:
            errors.append("RESULT_MISMATCH: dataset split")
        if spec.get("datasets") and (not isinstance(dataset, dict) or
                                    dataset.get("ref") != spec["datasets"][0]["ref"]):
            errors.append("RESULT_MISMATCH: dataset ref")
        if not isinstance(result.get("samples_evaluated"), int) or isinstance(result.get("samples_evaluated"), bool) or result.get("samples_evaluated", 0) <= 0:
            errors.append("INVALID_EXPERIMENT_RESULT: samples_evaluated must be positive")
        runtime = result.get("runtime_seconds")
        if isinstance(runtime, bool) or not isinstance(runtime, (int, float)) or not math.isfinite(runtime) or runtime < 0:
            errors.append("INVALID_EXPERIMENT_RESULT: runtime_seconds")
        metrics = result.get("metrics", {})
        if not isinstance(metrics, dict) or not metrics:
            errors.append("INVALID_EXPERIMENT_RESULT: empty metrics")
            metrics = {}
        primary = spec["primary_metric"]
        if primary["name"] not in metrics:
            errors.append("INVALID_EXPERIMENT_RESULT: missing primary metric")
        for metric_name, value in metrics.items():
            if not isinstance(value, dict):
                errors.append(f"INVALID_METRIC: {metric_name} not an object")
                continue
            v = value.get("value")
            if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v):
                errors.append(f"INVALID_METRIC: {metric_name} is not finite")
                continue
            if value.get("unit") not in {"fraction", "percent", "count", "seconds", "custom"} or type(value.get("higher_is_better")) is not bool:
                errors.append(f"INVALID_METRIC: invalid unit/direction for {metric_name}")
            if value.get("unit") == "fraction" and not 0 <= v <= 1:
                errors.append(f"INVALID_METRIC: {metric_name} fraction outside [0,1]")
            if value.get("unit") == "percent" and not 0 <= v <= 100:
                errors.append(f"INVALID_METRIC: {metric_name} percent outside [0,100]")
        primary_value = metrics.get(primary["name"], {})
        if not isinstance(primary_value, dict) or primary_value.get("unit") != primary["unit"] or primary_value.get("higher_is_better") != primary["higher_is_better"]:
            errors.append("RESULT_MISMATCH: primary metric definition")
    for rel in spec["expected_outputs"]:
        try:
            relative = _safe_name(rel)
            candidate = base / relative
            if candidate.is_symlink() or not candidate.is_file() or not candidate.resolve().is_relative_to(base.resolve()):
                errors.append(f"MISSING_ARTIFACT: {rel}")
                continue
            artifact_inventory.append({"path": rel, "bytes": candidate.stat().st_size,
                                       "sha256": _checksum(candidate)})
        except (OSError, ValueError):
            errors.append(f"UNSAFE_ARTIFACT: {rel}")
    if isinstance(result, dict):
        art = result.get("artifacts", [])
        if not isinstance(art, list):
            errors.append("INVALID_EXPERIMENT_RESULT: artifacts must be list")
        else:
            inventory = {x["path"]: x["sha256"] for x in artifact_inventory}
            for entry in art:
                name = entry.get("path") if isinstance(entry, dict) else entry
                try:
                    name = _safe_name(name)
                except RunnerError:
                    errors.append("UNSAFE_ARTIFACT: declared artifact")
                    continue
                if name not in inventory:
                    errors.append(f"MISSING_ARTIFACT: {name}")
                elif isinstance(entry, dict) and entry.get("sha256") and entry["sha256"] != inventory[name]:
                    errors.append(f"ARTIFACT_HASH_MISMATCH: {name}")
    manifest = base / "kgr_download_manifest.json"
    if manifest.is_file():
        try:
            download = json.loads(manifest.read_text(encoding="utf-8"))
            if download.get("job_id") != job_id:
                errors.append("RESULT_MISMATCH: download manifest job ID")
            manifest_hashes = {i["name"]: i["sha256"] for i in download["files"]}
            for info in artifact_inventory:
                if manifest_hashes.get(info["path"]) != info["sha256"]:
                    errors.append(f"ARTIFACT_HASH_MISMATCH: download manifest {info['path']}")
        except (OSError, ValueError, KeyError, TypeError):
            errors.append("INVALID_DOWNLOAD_MANIFEST")
    else:
        errors.append("MISSING_ARTIFACT: kgr_download_manifest.json")
    report = {"schema_version": SPEC_VERSION, "ok": not errors,
              "job_id": job_id, "experiment_id": spec["experiment_id"],
              "attempt_id": spec["attempt_id"], "scientific_status": "valid" if not errors else "invalid",
              "primary_metric": spec["primary_metric"], "metrics": metrics,
              "runtime_seconds": result.get("runtime_seconds") if isinstance(result, dict) else None,
              "artifact_inventory": artifact_inventory, "result_sha256": fingerprint,
              "errors": sorted(set(errors))}
    if save and attempt["state"] in {"remote_complete", "fetch_failed", "invalid_result", "succeeded"}:
        new_state = "succeeded" if not errors else "invalid_result"
        if attempt["state"] == "succeeded" and errors:
            # Never overwrite a previously validated immutable result; flag conflict.
            _fail("EVIDENCE_CONFLICT", "Previously validated result has changed")
        if new_state != attempt["state"]:
            ledger.transition(job_id, new_state, scientific_status=report["scientific_status"], validation=report)
    return report


