"""CLI entrypoint for portable Kaggle remote execution."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from .api import KaggleCLI
from .config import RunnerError, build_settings, load_config
from .datasets import download_dataset, inspect_local, prepare_dataset, upload_dataset
from .jobs import JobStore, display_job
from .packaging import validate_ref, validate_model_ref
from .runner import create_job, fetch_job, refresh_status


def _emit(value: dict | list, *, as_json: bool = True) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str), flush=True)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kgr", description="Portable Kaggle Python repo runner")
    p.add_argument("--config", help="Optional TOML config (default: XDG_CONFIG_HOME/kaggle-runner/config.toml)")
    p.add_argument("--state-dir", help="Override state directory; paths are resolved on this machine")
    p.add_argument("--profile", help="Saved Kaggle credential alias (isolated per command)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="Check active Python, Kaggle CLI and optional remote authentication")
    d.add_argument("--online", action="store_true", help="Also query your Kaggle kernels (needs login)")

    r = sub.add_parser("run", help="Prepare or push Python repository as a Kaggle script kernel")
    r.add_argument("--source", required=True, help="Local folder or plain GitHub HTTPS URL")
    r.add_argument("--entry", required=True, help="Relative Python entrypoint, e.g. train.py")
    r.add_argument("--owner", help="Your Kaggle username; can be set as [runner].owner in config")
    r.add_argument("--ref", help="Git branch or tag for GitHub HTTPS source")
    r.add_argument("--gpu", choices=("cpu", "t4x2"), default="cpu")
    r.add_argument("--dataset", action="append", default=[], metavar="OWNER/SLUG", help="Attach a Kaggle dataset (repeatable; public resources need no download)")
    r.add_argument("--model", action="append", default=[], metavar="OWNER/MODEL/FRAMEWORK/VARIATION/VERSION", help="Attach a pinned Kaggle Model variation version (repeatable)")
    r.add_argument("--unpack-datasets", action="store_true", help="Unzip payload.zip attached datasets into Kaggle working")
    r.add_argument("--arg", dest="arguments", action="append", default=[], help="Script argument (repeatable)")
    r.add_argument("--install-requirements", action="store_true", help="pip install requirements.txt inside kernel")
    r.add_argument("--enable-internet", action="store_true", help="Enable internet access on Kaggle kernel")
    r.add_argument("--timeout", type=int, default=0, help="Kaggle push timeout in seconds (0 = CLI default)")
    r.add_argument("--max-source-mb", type=int, default=3, help="Max compressed source ZIP in MB (default 3)")
    r.add_argument("--dry-run", action="store_true", help="Generate code/manifest only; no Kaggle calls")
    r.add_argument("--wait", action="store_true", help="Poll the submitted job until it finishes")
    r.add_argument("--fetch", action="store_true", help="With --wait, download outputs after completion or error")
    r.add_argument("--output", help="With --fetch, output folder (supports {job_id} for unique runs)")
    r.add_argument("--poll-seconds", type=int, default=20, help="Polling interval for --wait")
    r.add_argument("--max-wait-minutes", type=int, default=45, help="Maximum monitoring time; does not cancel remote runs")

    j = sub.add_parser("jobs", help="List locally tracked jobs")
    j.add_argument("--limit", type=int, default=30)
    sub.add_parser("status", help="Query Kaggle job state").add_argument("job_id")
    logs = sub.add_parser("logs", help="Get Kaggle kernel logs")
    logs.add_argument("job_id")
    logs.add_argument("--follow", action="store_true")
    f = sub.add_parser("fetch", help="Download output from latest kernel version")
    f.add_argument("job_id")
    f.add_argument("--output", help="Output folder (default: state/outputs/JOB_ID)")
    f.add_argument("--force", action="store_true", help="Overwrite existing files")
    sub.add_parser("show", help="Print saved job manifest and metadata").add_argument("job_id")

    ds = sub.add_parser("dataset", help="Manage local/public/accessible-private Kaggle datasets")
    ds_sub = ds.add_subparsers(dest="dataset_command", required=True)
    ds_search = ds_sub.add_parser("search", help="Find public Kaggle datasets")
    ds_search.add_argument("query", help="Search phrase")
    ins = ds_sub.add_parser("inspect", help="Inspect local folder or remote accessible dataset")
    ins.add_argument("source", help="Folder or owner/slug")
    upl = ds_sub.add_parser("upload", help="Bundle local folder and create PRIVATE Kaggle dataset")
    upl.add_argument("--path", required=True, help="Local dataset directory")
    upl.add_argument("--ref", required=True, help="Kaggle destination owner/slug")
    upl.add_argument("--title", required=True)
    upl.add_argument("--license", default="copyright-authors")
    upl.add_argument("--max-gb", type=int, default=5)
    upl.add_argument("--version-note", help="Create new version of existing dataset (requires confirmation)")
    upl.add_argument("--confirm-upload", action="store_true", help="Explicitly authorize sending data to Kaggle")
    dl = ds_sub.add_parser("download", help="Download Kaggle public/accessible-private dataset")
    dl.add_argument("--ref", required=True)
    dl.add_argument("--output", required=True)
    dl.add_argument("--unzip", action="store_true", help="Unzip download archive")

    models = sub.add_parser("model", help="Explore Kaggle Models (not the Model Proxy API)")
    models_sub = models.add_subparsers(dest="model_command", required=True)
    m_search = models_sub.add_parser("search", help="Find Kaggle models")
    m_search.add_argument("query", help="Search phrase")
    m_files = models_sub.add_parser("files", help="List files in a pinned model version")
    m_files.add_argument("ref", help="owner/model/framework/variation/version")
    return p


def dispatch(a: argparse.Namespace) -> int:
    settings = build_settings(state_dir=a.state_dir, profile=a.profile, config=a.config)
    cli = KaggleCLI(settings)
    if a.command == "doctor":
        check = cli.call("--version", check=False)
        report = {"python": sys.executable, "python_version": sys.version.split()[0],
                  "state_dir": str(settings.state), "profile": settings.profile,
                  "kaggle_cli_ok": check.returncode == 0, "kaggle_cli_version": check.text}
        if a.online and check.returncode == 0:
            remote = cli.call("kernels", "list", "--mine", check=False)
            report["kaggle_auth_ok"] = remote.returncode == 0
            report["remote_message"] = remote.text[:1500]
        _emit(report)
        # An online auth check must fail when credentials are not accepted.
        # Previously doctor --online exited successfully even after auth failure.
        return 0 if check.returncode == 0 and (not a.online or report.get("kaggle_auth_ok", False)) else 1
    if a.command == "dataset":
        if a.dataset_command == "search":
            result = cli.call("datasets", "list", "--search", a.query)
            _emit({"query": a.query, "results": result.text})
            return 0
        if a.dataset_command == "inspect":
            path = Path(a.source).expanduser()
            if path.is_dir():
                _emit(inspect_local(path))
            else:
                ref = validate_ref(a.source, "dataset ref")
                result = cli.call("datasets", "files", ref)
                _emit({"type": "kaggle", "ref": ref, "files_output": result.text})
            return 0
        if a.dataset_command == "upload":
            source = Path(a.path).expanduser().resolve()
            staging, manifest = prepare_dataset(source, settings.state, a.ref, a.title,
                                                 a.license, max_bytes=a.max_gb * 1024**3)
            result = {"staging_dir": str(staging), "dataset_ref": validate_ref(a.ref),
                      "private": True, "file_count": len(manifest["files"]),
                      "bytes": manifest["total_bytes"], "uploaded": False}
            if a.confirm_upload:
                result["cli_output"] = upload_dataset(cli, staging, a.ref, version_note=a.version_note)
                result["uploaded"] = True
            else:
                result["next_step"] = "Review staging and rerun with --confirm-upload to send a PRIVATE dataset"
            _emit(result)
            return 0
        if a.dataset_command == "download":
            msg = download_dataset(cli, a.ref, Path(a.output).expanduser().resolve(), unzip=a.unzip)
            _emit({"ref": a.ref, "output": str(Path(a.output).expanduser().resolve()), "message": msg})
            return 0
    if a.command == "model":
        if a.model_command == "search":
            result = cli.call("models", "list", "--search", a.query)
            _emit({"query": a.query, "results": result.text})
        elif a.model_command == "files":
            ref = validate_model_ref(a.ref)
            result = cli.call("models", "variations", "versions", "files", ref)
            _emit({"model_ref": ref, "files_output": result.text})
        return 0
    store = JobStore(settings.state)
    if a.command == "run":
        if a.fetch and not a.wait:
            raise RunnerError("--fetch requires --wait")
        if (a.wait or a.fetch) and a.dry_run:
            raise RunnerError("--wait/--fetch cannot be used with --dry-run")
        if a.poll_seconds < 1 or a.max_wait_minutes < 1:
            raise RunnerError("--poll-seconds and --max-wait-minutes must be positive")
        if a.output and not a.fetch:
            raise RunnerError("--output requires --fetch")
        config = load_config(a.config)
        owner = a.owner or config.get("runner", {}).get("owner")
        result = create_job(settings, cli, store, source=a.source, entry=a.entry, owner=owner,
                            gpu=a.gpu, dataset_refs=a.dataset, arguments=a.arguments,
                            install_requirements=a.install_requirements, enable_internet=a.enable_internet,
                            max_source_mb=a.max_source_mb, timeout_seconds=a.timeout,
                            dry_run=a.dry_run, git_ref=a.ref, unpack_datasets=a.unpack_datasets,
                            model_refs=a.model)
        _emit(result)
        if a.wait:
            job_id = result["job_id"]
            deadline = time.monotonic() + a.max_wait_minutes * 60
            while True:
                current = refresh_status(cli, store, job_id)
                _emit({"job_id": job_id, "status": current["status"]})
                if current["status"] in {"complete", "error", "cancelled"}:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    print(f"kgr: watch timed out; remote job {job_id} may still be running", file=sys.stderr)
                    return 3
                time.sleep(min(remaining, a.poll_seconds))
            if a.fetch:
                target = a.output.replace("{job_id}", job_id) if a.output else None
                _emit(fetch_job(cli, store, settings.state, job_id, target, force=False))
            if current["status"] != "complete":
                return 2
    elif a.command == "jobs":
        if a.limit < 1:
            raise RunnerError("--limit must be >= 1")
        _emit([display_job(j) for j in store.list(a.limit)])
    elif a.command == "status":
        _emit(refresh_status(cli, store, a.job_id))
    elif a.command == "logs":
        job = store.get(a.job_id)
        args = ["kernels", "logs", job["kernel_ref"]]
        if a.follow:
            args.append("--follow")
        result = cli.call(*args, stream=a.follow)
        if not a.follow:
            print(result.text)
            store.update(a.job_id, last_message=result.text[-4000:])
    elif a.command == "fetch":
        _emit(fetch_job(cli, store, settings.state, a.job_id, a.output, force=a.force))
    elif a.command == "show":
        record = store.get(a.job_id)
        manifest = Path(record["staging_dir"]).parent / "manifest.json"
        _emit({"job": display_job(record), "manifest": json.loads(manifest.read_text(encoding="utf-8"))})
    else:
        raise RunnerError(f"Unsupported command: {a.command}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = parser()
    try:
        return dispatch(p.parse_args(argv))
    except RunnerError as exc:
        print(f"kgr: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("kgr: interrupted", file=sys.stderr)
        return 130
