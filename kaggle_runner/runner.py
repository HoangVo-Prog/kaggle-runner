"""Prepare and submit portable Python-source Kaggle kernels."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path

from .api import KaggleCLI
from .config import RunnerError, Settings
from .jobs import JobStore
from .notebooks import stage_notebook
from .status import parse_kernel_status
from .packaging import (make_bootstrap, pack_source, resolve_source,
                        slugify, validate_ref, validate_model_ref)


def create_job(settings: Settings, cli: KaggleCLI, store: JobStore, *, source: str,
               entry: str, owner: str, gpu: str, dataset_refs: list[str], arguments: list[str],
               install_requirements: bool = False, enable_internet: bool = False,
               max_source_mb: int = 3, timeout_seconds: int = 0, dry_run: bool = False,
               git_ref: str | None = None, unpack_datasets: bool = False,
               model_refs: list[str] | None = None, notebook_file: str | None = None,
               account_profile: str | None = None, account_owner: str | None = None,
               account_email: str | None = None) -> dict:
    if not owner:
        raise RunnerError("Kaggle username is not configured. Pass --owner with the username from your Kaggle profile URL, or set [runner].owner in config.toml (not your display name).")
    if not owner.replace("-", "").replace("_", "").isalnum():
        raise RunnerError("--owner must be your Kaggle profile URL username (alphanumeric, dash, underscore), not display name")
    if max_source_mb < 1 or max_source_mb > 25:
        raise RunnerError("--max-source-mb must be in [1,25]")
    if timeout_seconds < 0:
        raise RunnerError("--timeout must not be negative")
    if install_requirements and not enable_internet:
        raise RunnerError("--install-requirements requires --enable-internet")
    if gpu not in {"cpu", "t4x2"}:
        raise RunnerError("Only cpu and t4x2 are supported in P0")
    refs = list(dict.fromkeys(validate_ref(d, "dataset reference") for d in dataset_refs))
    models = list(dict.fromkeys(validate_model_ref(m) for m in (model_refs or [])))
    job_id = uuid.uuid4().hex
    job_dir = settings.state / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    src, revision = resolve_source(source, job_dir, ref=git_ref)
    # Native .ipynb kernels preserve notebook cells and outputs in Kaggle UI.
    if notebook_file:
        npath = Path(notebook_file).expanduser().resolve()
        if npath.parent != src:
            raise RunnerError("Notebook must be a direct file within the specified source directory")
        data, files = b"", []
    else:
        data, files = pack_source(src, entry, max_source_mb * 1024 * 1024)
    slug = f"kgr-{slugify(src.name, 24)}-{job_id[:10]}"
    kernel_ref = validate_ref(f"{owner}/{slug}", "kernel reference")
    staging = job_dir / "kernel"
    staging.mkdir()
    if notebook_file:
        if arguments or install_requirements or unpack_datasets:
            raise RunnerError("Notebook mode does not support --arg/--install-requirements/--unpack-datasets; use notebook cells")
        kernel_type, code_file = "notebook", "run.ipynb"
        files = stage_notebook(npath, staging / code_file, refs, models)
    else:
        kernel_type, code_file = "script", "run.py"
        (staging / code_file).write_text(make_bootstrap(data, entry=entry, arguments=arguments,
                        datasets=refs, install_requirements=install_requirements,
                        unpack_datasets=unpack_datasets, models=models), encoding="utf-8")
    metadata = {"id": kernel_ref, "title": slug.replace("-", " ").title(),
                "code_file": code_file, "language": "python", "kernel_type": kernel_type,
                "is_private": "true", "enable_gpu": "false", "enable_tpu": "false",
                "enable_internet": "true" if enable_internet else "false",
                "dataset_sources": refs, "competition_sources": [], "kernel_sources": [], "model_sources": models}
    if gpu == "t4x2":
        metadata["machine_shape"] = "NvidiaTeslaT4"
    (staging / "kernel-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    manifest = {"job_id": job_id, "kernel_ref": kernel_ref, "source": source,
                "revision": revision, "entry": entry, "kernel_type": kernel_type, "gpu": gpu,
                "dataset_refs": refs, "model_refs": models, "files": files, "account_profile": account_profile, "account_owner": account_owner,
                "account_email": account_email, "source_zip_sha256": hashlib.sha256(data).hexdigest() if data else None,
                "source_zip_bytes": len(data), "arguments": arguments, "install_requirements": install_requirements,
                "enable_internet": enable_internet, "unpack_datasets": unpack_datasets,
                "timeout_seconds": timeout_seconds}
    (job_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    store.add({"job_id": job_id, "kernel_ref": kernel_ref, "source": source,
               "source_revision": revision, "dataset_refs": json.dumps(refs),
               "status": "prepared", "profile": settings.profile,
               "account_profile": account_profile, "account_owner": account_owner,
               "account_email": account_email,
               "staging_dir": str(staging), "output_dir": None, "last_message": None})
    manifest["staging_dir"] = str(staging)
    manifest["dry_run"] = dry_run
    if dry_run:
        manifest["next_command"] = f"{cli.settings.python} -m kaggle kernels push -p <staging_dir>"
        return manifest
    command = ["kernels", "push", "-p", str(staging)]
    if timeout_seconds:
        command.extend(["-t", str(timeout_seconds)])
    try:
        response = cli.call(*command)
    except RunnerError as exc:
        store.update(job_id, status="submit_failed", last_message=str(exc)[-4000:])
        raise RunnerError(f"Job {job_id}: {exc}") from exc
    # Trust the canonical notebook URL printed by Kaggle, not a user-supplied owner alias.
    remote = re.search(r"https?://(?:www\.)?kaggle\.com/code/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)", response.text)
    if remote:
        kernel_ref = f"{remote.group(1)}/{remote.group(2)}"
        store.update(job_id, kernel_ref=kernel_ref)
        manifest["kernel_ref"] = kernel_ref
        (job_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    store.update(job_id, status="submitted", last_message=response.text[-4000:])
    manifest["submit_output"] = response.text
    return manifest



def refresh_status(cli: KaggleCLI, store: JobStore, job_id: str) -> dict:
    record = store.get(job_id)
    try:
        result = cli.call("kernels", "status", record["kernel_ref"])
    except RunnerError as exc:
        detail = str(exc)
        store.update(job_id, status="status_unavailable", last_message=detail[-4000:])
        if "kernels.get" in detail or "Cannot access kernel" in detail:
            raise RunnerError(
                f"Cannot read remote kernel {record['kernel_ref']!r} (local job {job_id}). "
                "Verify that --owner matches the username in the authenticated Kaggle account's "
                "profile URL, and check `python -m kaggle kernels list --mine`. "
                "Also inspect the original submit_output (if available): a locally generated "
                "job ID does not confirm a Kaggle notebook exists. "
                f"Kaggle error: {detail}"
            ) from exc
        raise
    status = parse_kernel_status(result.text)
    store.update(job_id, status=status, last_message=result.text[-4000:])
    return {"job_id": job_id, "kernel_ref": record["kernel_ref"],
            "status": status, "raw": result.text}


def fetch_job(cli: KaggleCLI, store: JobStore, state: Path, job_id: str, output_path: str | None,
              force: bool = False, file_pattern: str | None = None) -> dict:
    record = store.get(job_id)
    target = Path(output_path).expanduser().resolve() if output_path else (state / "outputs" / job_id)
    target.mkdir(parents=True, exist_ok=True)
    command = ["kernels", "output", record["kernel_ref"], "-p", str(target)]
    if force:
        command.append("-o")
    if file_pattern:
        import re as _re
        try:
            _re.compile(file_pattern)
        except _re.error as exc:
            raise RunnerError(f"Invalid --file-pattern regex: {exc}") from exc
        command.extend(["--file-pattern", file_pattern])
    result = cli.call(*command)
    entries = []
    for p in sorted(target.rglob("*")):
        if not p.is_file() or p.name == "kgr_download_manifest.json":
            continue
        h = hashlib.sha256()
        with p.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
        entries.append({"name": p.relative_to(target).as_posix(), "bytes": p.stat().st_size,
                        "sha256": h.hexdigest()})
    payload = {"job_id": job_id, "kernel_ref": record["kernel_ref"],
               "account_profile": record.get("account_profile"),
               "account_owner": record.get("account_owner"),
               "account_email": record.get("account_email"),
               "output_dir": str(target), "files": entries, "cli_output": result.text}
    (target / "kgr_download_manifest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    store.update(job_id, output_dir=str(target))
    return payload
