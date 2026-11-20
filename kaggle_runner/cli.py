"""CLI entrypoint for portable Kaggle remote execution."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
import json
import re
import sys
import time
from pathlib import Path

from . import __version__
from .accounts import Account, interactive_init, load_account
from .team import add_profile, interactive_add, get_profile, profiles, public_profiles, migrate_legacy_profiles, diagnose_profiles
from .preflight import check_preflight, active_report, tracked_active_report, quota_report
from .api import KaggleCLI
from .config import RunnerError, build_settings, load_config
from .datasets import download_dataset, inspect_local, prepare_dataset, upload_dataset
from .jobs import JobStore, display_job
from .packaging import validate_ref, validate_model_ref
from .runner import create_job, fetch_job, refresh_status
from .presentation import format_output


_JSON_OUTPUT: ContextVar[bool] = ContextVar("kgr_json_output", default=False)
_COMMAND: ContextVar[str] = ContextVar("kgr_command", default="")


def _emit(value: dict | list, *, as_json: bool | None = None) -> None:
    machine = _JSON_OUTPUT.get() if as_json is None else as_json
    output = (json.dumps(value, indent=2, ensure_ascii=False, default=str)
              if machine else format_output(value, _COMMAND.get()))
    print(output, flush=True)


def _progress(message: str) -> None:
    print(f"kgr: {message}", file=sys.stderr, flush=True)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kgr", description="Portable Kaggle Python repo runner")
    p.add_argument("--config", help="Optional TOML config (default: XDG_CONFIG_HOME/kaggle-runner/config.toml)")
    p.add_argument("--env-file", help="Private .env for default account AND team profiles")
    p.add_argument("--state-dir", help="Override state directory; paths are resolved on this machine")
    p.add_argument("--profile", help="Saved Kaggle credential alias (isolated per command)")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--json", action="store_true", help="Full machine-readable JSON (also accepted after commands)")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="Check active Python, Kaggle CLI and optional remote authentication")
    d.add_argument("--online", action="store_true", help="Also query your Kaggle kernels (needs login)")

    auth = sub.add_parser("auth", help="Configure one Kaggle account using API token")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    init = auth_sub.add_parser("init", help="Interactively create/update private .env")
    init.add_argument("--owner", help="Kaggle profile username")
    init.add_argument("--email", help="Report email, metadata only")
    init.add_argument("--token-stdin", action="store_true", help="Read API token from stdin for CI (not argv)")
    auth_sub.add_parser("show", help="Show account metadata without revealing token")

    team = sub.add_parser("team", help="Manage explicitly authorized Kaggle account profiles")
    tm = team.add_subparsers(dest="team_command", required=True)
    t_add = tm.add_parser("add", help="Save profile username/token in the shared private .env")
    t_add.add_argument("alias", help="Short local label, e.g. gpu-alice")
    t_add.add_argument("--username", required=True, help="Kaggle profile URL username (required for kernel ID)")
    t_add.add_argument("--email", help="Optional contact metadata; never used for authentication")
    t_add.add_argument("--token-stdin", action="store_true", help="Read API token from stdin without command history")
    t_add.add_argument("--replace", action="store_true", help="Explicitly replace an existing credential profile")
    tm.add_parser("list", help="Show .env profile metadata (never prints tokens)")
    tm.add_parser("diagnose", help="Show selected .env location and configured key presence (no tokens)")
    t_migrate = tm.add_parser("migrate", help="Import legacy team/*.env profiles into one .env (keeps originals)")
    t_migrate.add_argument("--from-dir", help="Old team profile directory (default legacy location)")
    t_check = tm.add_parser("check", help="Check slots and GPU quota separately for each team profile")
    t_check.add_argument("--gpu", choices=("cpu", "t4x2"), default="t4x2")
    t_check.add_argument("--required-gpu-hours", type=float, default=0.25)
    t_check.add_argument("--max-concurrent", type=int, choices=(1, 2), default=2)
    check = sub.add_parser("check", help="Check running batch kernels and GPU quota before submit")
    check.add_argument("--gpu", choices=("cpu", "t4x2"), default="t4x2")
    check.add_argument("--required-gpu-hours", type=float, default=0.25)
    check.add_argument("--max-concurrent", type=int, choices=(1, 2), default=2)
    check.add_argument("--remote", action="store_true", help="Scan remote notebook history (slow, diagnostic)")
    for command in (check,):
        command.add_argument("--workers", type=int, default=6, help="Concurrent status requests (1-16)")
        command.add_argument("--max-seconds", type=float, default=45, help="Total active-scan time budget")
        command.add_argument("--strict", action="store_true", help="Verify historical terminal entries live (slower)")
        command.add_argument("--quiet", action="store_true", help="Disable progress messages on stderr")
    sub.add_parser("quota", help="Check remaining GPU quota")
    active = sub.add_parser("active", help="Check runner-tracked active jobs (or remote account with --remote)")
    active.add_argument("--remote", action="store_true", help="Scan all visible Kaggle notebooks, including untracked jobs")
    active.add_argument("--workers", type=int, default=6, help="Concurrent status requests (1-16)")
    active.add_argument("--max-seconds", type=float, default=45, help="Total active-scan time budget")
    active.add_argument("--strict", action="store_true", help="Verify historical terminal entries live (slower)")
    active.add_argument("--quiet", action="store_true", help="Disable progress messages on stderr")
    report = sub.add_parser("report", help="Summarize local jobs and optionally refresh remote status")
    report.add_argument("report_target", nargs="?", choices=("experiment",))
    report.add_argument("report_experiment_id", nargs="?")
    report.add_argument("--format", choices=("md", "json"), default="md")
    report.add_argument("--refresh", action="store_true", help="Query remote statuses")
    report.add_argument("--limit", type=int, default=100)
    report.add_argument("--output", help="Write report JSON file")
    sync = sub.add_parser("sync", help="Refresh all tracked job statuses and print report")
    sync.add_argument("--limit", type=int, default=100)

    r = sub.add_parser("run", help="Submit a .py/.ipynb file or a Python repository to Kaggle")
    r.add_argument("file", nargs="?", help="Local .py or .ipynb file (simple workflow)")
    r.add_argument("--file", dest="file_option", help="Same as positional file")
    r.add_argument("--source", help="Local folder or plain GitHub HTTPS URL (advanced)")
    r.add_argument("--entry", help="Relative Python entrypoint, e.g. train.py (advanced)")
    r.add_argument("--owner", help="Your Kaggle username; can be set as [runner].owner in config")
    r.add_argument("--account", help="Team profile alias, or auto to choose an available authorized profile")
    r.add_argument("--ref", help="Git branch or tag for GitHub HTTPS source")
    r.add_argument("--gpu", choices=("cpu", "t4x2"), default="cpu")
    r.add_argument("--required-gpu-hours", type=float, default=0.25,
                   help="Minimum hours needed before GPU submit (default 0.25)")
    r.add_argument("--max-concurrent", type=int, choices=(1, 2), default=2,
                   help="Local safety cap on concurrently active batch kernels (default 2)")
    r.add_argument("--remote", action="store_true", help="Use full remote notebook history preflight (slow)")
    r.add_argument("--workers", type=int, default=6, help="Concurrent preflight status requests (1-16)")
    r.add_argument("--max-seconds", type=float, default=45, help="Preflight active-scan time budget")
    r.add_argument("--strict", action="store_true", help="Verify historical terminal entries live")
    r.add_argument("--quiet", action="store_true", help="Disable preflight progress messages")
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
    f.add_argument("--file-pattern", help="Optional regex to download selected files")
    files = sub.add_parser("outputs", help="List remote output artifacts for a job")
    files.add_argument("job_id")
    sub.add_parser("show", help="Print saved job manifest and metadata").add_argument("job_id")
    repair = sub.add_parser("repair", help="Correct the Kaggle owner/ref of a tracked job")
    repair.add_argument("job_id", help="Full Job ID or unique prefix (8+ characters)")
    corrected = repair.add_mutually_exclusive_group(required=True)
    corrected.add_argument("--owner", help="Correct username; keeps existing notebook slug")
    corrected.add_argument("--kernel-ref", help="Canonical owner/slug from Kaggle notebook URL")
    archive = sub.add_parser("archive", help="Exclude a confirmed inactive historical job from slot checks")
    archive.add_argument("job_id", help="Full Job ID or unique prefix (8+ characters)")
    archive.add_argument("--confirm-inactive", action="store_true",
                         help="You checked Kaggle Active Events and confirm the run is inactive")

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
    pkg = sub.add_parser("package", help="Inspect exact safe .py/.ipynb upload contents")
    pkg_sub = pkg.add_subparsers(dest="package_command", required=True)
    pk = pkg_sub.add_parser("inspect")
    pk.add_argument("source")
    pk.add_argument("--root")
    pk.add_argument("--entry")
    pk.add_argument("--max-source-mb", type=int, default=3)
    pk.add_argument("--include", action="append")
    pk.add_argument("--exclude", action="append")

    exp = sub.add_parser("experiment", help="Durable experiment lifecycle")
    exp_sub = exp.add_subparsers(dest="experiment_command", required=True)
    exp_sub.add_parser("validate").add_argument("spec_file")
    ex_submit = exp_sub.add_parser("submit")
    ex_submit.add_argument("spec_file")
    ex_submit.add_argument("--account", default="auto")
    ex_submit.add_argument("--approve", action="store_true")
    exp_sub.add_parser("show").add_argument("experiment_id")
    ex_resume = exp_sub.add_parser("resume")
    ex_resume.add_argument("experiment_id")
    ex_resume.add_argument("--wait", action="store_true")
    ex_resume.add_argument("--poll-seconds", type=int, default=20)
    ex_resume.add_argument("--max-wait-minutes", type=int, default=45)

    results = sub.add_parser("results", help="Validate and aggregate scientific outputs")
    rs_sub = results.add_subparsers(dest="results_command", required=True)
    rs_sub.add_parser("validate").add_argument("job_id")
    rs_agg = rs_sub.add_parser("aggregate")
    rs_agg.add_argument("--experiment-id", required=True)
    rec = sub.add_parser("reconcile", help="Read-only remote reconciliation; never resubmit")
    rec.add_argument("--experiment-id")

    # Accept both `kgr --json check` and `kgr check --json`, including nested
    # subcommands. SUPPRESS preserves the parent value when the child omits it.
    def child_json_flags(parser: argparse.ArgumentParser) -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    child.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                                       help="Full machine-readable JSON")
                    child_json_flags(child)

    child_json_flags(p)
    return p


def _client_for_job(settings, fallback: KaggleCLI, job: dict, env_file: str | None = None) -> KaggleCLI:
    """Rebind every follow-up to the original account token; fail if it is missing."""
    alias = job.get("account_profile")
    if alias:
        return KaggleCLI(settings, token=get_profile(alias, env_file).token)
    if env_file and not fallback.token:
        raise RunnerError("Historical job has no account profile and explicit .env has no default token")
    return fallback


def _team_statuses(settings, store: JobStore, *, gpu: str, hours: float, limit: int,
                   env_file: str | None = None) -> list[dict]:
    # Different tokens use independent Kaggle processes and SQLite connections.
    # A small pool avoids a slow serial scan across a whole research group.
    aliases = profiles(env_file)
    if not aliases:
        return []

    def inspect(alias: str) -> dict:
        try:
            profile = get_profile(alias, env_file)
            client = KaggleCLI(settings, token=profile.token)
            checked = check_preflight(client, store=store, gpu=gpu,
                        required_gpu_hours=hours, max_concurrent=limit,
                        owner=profile.owner, account_profile=alias, remote=False)
            return {**profile.metadata(), "ok": checked["ok"],
                    "active": checked["active"], "gpu_quota": checked["gpu_quota"],
                    "reasons": checked["reasons"], "warnings": checked.get("warnings", [])}
        except RunnerError as exc:
            return {"account_profile": alias, "ok": False,
                    "reasons": [str(exc)]}

    with ThreadPoolExecutor(max_workers=min(3, len(aliases))) as pool:
        return list(pool.map(inspect, aliases))


def dispatch(a: argparse.Namespace) -> int:
    settings = build_settings(state_dir=a.state_dir, profile=a.profile, config=a.config)
    from .commands.agent import dispatch_agent_command
    agent_code = dispatch_agent_command(a, settings, _emit, json_output=_JSON_OUTPUT.get())
    if agent_code is not None:
        return agent_code
    # Team profiles are self-contained and must never depend on, or accidentally
    # load, credentials from an unrelated single-account .env in the checkout.
    uses_team = a.command == "team" or (a.command == "run" and bool(a.account))
    account = Account(None, None, None, None) if uses_team else load_account(a.env_file)
    # Explicit .env may contain only team profiles, without a default token.
    # Job follow-ups are rebound to their saved team profile, so do not require
    # an unrelated KAGGLE_API_TOKEN at CLI startup.
    requires_default = (a.command in {"check", "quota", "active"} or
                        (a.command == "run" and not a.account) or
                        (a.command == "doctor" and a.online))
    if not uses_team and a.env_file and requires_default and not account.token:
        raise RunnerError("Explicit --env-file has no default KAGGLE_API_TOKEN; "
                          "use --account ALIAS for team jobs or `kgr auth init`")
    if not uses_team and a.profile and account.token:
        raise RunnerError("--profile cannot be combined with KAGGLE_API_TOKEN in .env; choose one authentication method")
    cli = KaggleCLI(settings, token=account.token)
    if a.command == "auth":
        if a.auth_command == "init":
            _emit(interactive_init(a.env_file, a.owner, a.email, a.token_stdin))
        else:
            _emit({"owner": account.owner, "email": account.email,
                   "env_file": str(account.env_file) if account.env_file else None,
                   "token_configured": bool(account.token)})
        return 0
    if a.command == "team":
        if a.team_command == "add":
            _emit(interactive_add(a.alias, owner=a.username, email=a.email,
                                  stdin_token=a.token_stdin, replace=a.replace, env_file=a.env_file))
            return 0
        if a.team_command == "list":
            _emit(public_profiles(a.env_file))
            return 0
        if a.team_command == "diagnose":
            _emit(diagnose_profiles(a.env_file))
            return 0
        if a.team_command == "migrate":
            _emit(migrate_legacy_profiles(env_file=a.env_file, from_dir=a.from_dir))
            return 0
        store = JobStore(settings.state)
        rows = _team_statuses(settings, store, gpu=a.gpu,
                              hours=a.required_gpu_hours, limit=a.max_concurrent, env_file=a.env_file)
        _emit({"accounts": rows, "ready_count": sum(1 for r in rows if r["ok"]),
               "total": len(rows)})
        return 0 if rows and any(r["ok"] for r in rows) else 3
    if a.command in {"check", "quota", "active"}:
        if a.command == "quota":
            _emit(quota_report(cli))
            return 0
        scan_kwargs = {"workers": a.workers, "max_seconds": a.max_seconds,
                       "strict": a.strict,
                       "progress": _progress if not a.quiet and (a.remote or a.strict) else None}
        store = JobStore(settings.state)
        if a.command == "active":
            if a.remote or a.strict:
                _emit(active_report(cli, **scan_kwargs))
            else:
                _emit(tracked_active_report(cli, store, owner=account.owner,
                                            workers=a.workers, max_seconds=a.max_seconds,
                                            progress=scan_kwargs["progress"]))
            return 0
        result = check_preflight(cli, gpu=a.gpu, required_gpu_hours=a.required_gpu_hours,
                                 max_concurrent=a.max_concurrent, store=store,
                                 owner=account.owner, remote=a.remote, **scan_kwargs)
        _emit(result)
        return 0 if result["ok"] else 3
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
        file = a.file_option or a.file
        if a.file_option and a.file:
            raise RunnerError("Pass a single file (positional or --file, not both)")
        if file and (a.source or a.entry):
            raise RunnerError("Use either a .py/.ipynb file or --source with --entry")
        if file:
            candidate = Path(file).expanduser().resolve()
            if candidate.suffix.lower() not in {".py", ".ipynb"} or not candidate.is_file():
                raise RunnerError("File must be an existing .py or .ipynb")
            source, entry = str(candidate.parent), candidate.name
            notebook_file = str(candidate) if candidate.suffix.lower() == ".ipynb" else None
        else:
            if not a.source or not a.entry:
                raise RunnerError("Provide a .py/.ipynb file, or both --source and --entry")
            source, entry, notebook_file = a.source, a.entry, None
            if entry.endswith(".ipynb"):
                raise RunnerError("For notebooks use `kgr run ./path/to/notebook.ipynb`")
        if a.fetch and not a.wait:
            raise RunnerError("--fetch requires --wait")
        if (a.wait or a.fetch) and a.dry_run:
            raise RunnerError("--wait/--fetch cannot be used with --dry-run")
        if a.poll_seconds < 1 or a.max_wait_minutes < 1:
            raise RunnerError("--poll-seconds and --max-wait-minutes must be positive")
        if a.output and not a.fetch:
            raise RunnerError("--output requires --fetch")
        config = load_config(a.config)
        selected_profile = None
        owner = a.owner or account.owner or config.get("runner", {}).get("owner")
        if a.account:
            if a.account == "auto":
                if a.dry_run:
                    raise RunnerError("--account auto requires online preflight (not available with --dry-run)")
                candidates = _team_statuses(settings, store, gpu=a.gpu,
                                hours=a.required_gpu_hours, limit=a.max_concurrent, env_file=a.env_file)
                eligible = [r for r in candidates if r["ok"]]
                if not eligible:
                    summary = "; ".join(r.get("account_profile", "?") + ": " +
                               ", ".join(r.get("reasons", [])) for r in candidates)
                    raise RunnerError("No eligible team account. " + (summary or "Use `kgr team add`"))
                # Prefer more free slots, then more GPU headroom; tie breaks by alias.
                eligible.sort(key=lambda r: (-(a.max_concurrent - r["active"]["running_count"]),
                                        -(r["gpu_quota"] or {}).get("remaining_hours", 0),
                                        r["account_profile"]))
                selected_profile = get_profile(eligible[0]["account_profile"], a.env_file)
            else:
                selected_profile = get_profile(a.account, a.env_file)
            if a.owner and a.owner != selected_profile.owner:
                raise RunnerError("--owner cannot override the username of a saved team profile")
            owner = selected_profile.owner
            cli = KaggleCLI(settings, token=selected_profile.token)
        if not a.dry_run and (not a.account or a.account != "auto"):
            preflight = check_preflight(cli, gpu=a.gpu, required_gpu_hours=a.required_gpu_hours,
                                        max_concurrent=a.max_concurrent, store=store,
                                        owner=owner, account_profile=selected_profile.alias if selected_profile else None,
                                        remote=a.remote, workers=a.workers,
                                        max_seconds=a.max_seconds, strict=a.strict,
                                        progress=_progress if not a.quiet and (a.remote or a.strict) else None)
            if not preflight["ok"]:
                raise RunnerError("Preflight blocked submission: " + "; ".join(preflight["reasons"]))
        result = create_job(settings, cli, store, source=source, entry=entry, owner=owner,
                            gpu=a.gpu, dataset_refs=a.dataset, arguments=a.arguments,
                            install_requirements=a.install_requirements, enable_internet=a.enable_internet,
                            max_source_mb=a.max_source_mb, timeout_seconds=a.timeout,
                            dry_run=a.dry_run, git_ref=a.ref, unpack_datasets=a.unpack_datasets,
                            model_refs=a.model, notebook_file=notebook_file,
                            account_profile=selected_profile.alias if selected_profile else None,
                            account_owner=owner,
                            account_email=selected_profile.email if selected_profile else account.email,
                            max_concurrent=a.max_concurrent)
        _emit(result)
        if a.wait:
            job_id = result["job_id"]
            deadline = time.monotonic() + a.max_wait_minutes * 60
            while True:
                current = refresh_status(_client_for_job(settings, cli, store.get(job_id), a.env_file), store, job_id)
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
                _emit(fetch_job(_client_for_job(settings, cli, store.get(job_id), a.env_file), store, settings.state, job_id, target, force=False))
            if current["status"] != "complete":
                return 2
    elif a.command in {"report", "sync"}:
        if a.limit < 1:
            raise RunnerError("--limit must be positive")
        rows = [display_job(j) for j in store.list(a.limit)]
        for item in rows:
            if a.command == "sync" or a.refresh:
                if item["status"] in {"prepared", "submit_failed"}:
                    continue
                try:
                    result = refresh_status(_client_for_job(settings, cli, item, a.env_file), store, item["job_id"])
                    item["status"] = result["status"]
                except RunnerError as exc:
                    item["refresh_error"] = str(exc)
                    item["status"] = "status_unavailable"
            item["url"] = "https://www.kaggle.com/code/" + item["kernel_ref"]
        tally = {}
        per_account: dict[str, dict] = {}
        for item in rows:
            tally[item["status"]] = tally.get(item["status"], 0) + 1
            alias = item.get("account_profile") or "default"
            group = per_account.setdefault(alias, {"owner": item.get("account_owner"),
                                                   "email": item.get("account_email"),
                                                   "total": 0, "counts": {}})
            group["total"] += 1
            counts = group["counts"]
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        report = {"owner": account.owner, "email": account.email,
                  "accounts": sorted(per_account), "by_account": per_account,
                  "total": len(rows), "counts": tally, "jobs": rows}
        if a.command == "report" and a.output:
            target = Path(a.output).expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report, indent=2), encoding="utf-8")
        _emit(report)
    elif a.command == "jobs":
        if a.limit < 1:
            raise RunnerError("--limit must be >= 1")
        _emit([display_job(j) for j in store.list(a.limit)])
    elif a.command == "status":
        record = store.get(a.job_id)
        result = refresh_status(_client_for_job(settings, cli, record, a.env_file), store, record["job_id"])
        result.update({k: record.get(k) for k in ("account_profile", "account_owner", "account_email")})
        _emit(result)
    elif a.command == "logs":
        job = store.get(a.job_id)
        args = ["kernels", "logs", job["kernel_ref"]]
        if a.follow:
            args.append("--follow")
        job_cli = _client_for_job(settings, cli, job, a.env_file)
        if not _JSON_OUTPUT.get():
            print(f"Account: {job.get('account_profile') or 'default'} / {job.get('account_owner') or job['kernel_ref'].split('/')[0]}"
                  + (f" ({job['account_email']})" if job.get('account_email') else ""), flush=True)
        if a.follow and _JSON_OUTPUT.get():
            raise RunnerError("--follow streams plain text; remove --json")
        result = job_cli.call(*args, stream=a.follow)
        if not a.follow:
            safe_logs = (result.text.replace(job_cli.token, "[REDACTED]")
                         if job_cli.token else result.text)
            if _JSON_OUTPUT.get():
                _emit({"job_id": job["job_id"], "account_profile": job.get("account_profile"),
                       "account_owner": job.get("account_owner"),
                       "account_email": job.get("account_email"), "logs": safe_logs})
            else:
                print(safe_logs)
            store.update(a.job_id, last_message=safe_logs[-4000:])
    elif a.command == "fetch":
        record = store.get(a.job_id)
        outcome = fetch_job(_client_for_job(settings, cli, record, a.env_file), store, settings.state,
                            record["job_id"], a.output, force=a.force, file_pattern=a.file_pattern)
        outcome.update({k: record.get(k) for k in ("account_profile", "account_owner", "account_email")})
        _emit(outcome)
    elif a.command == "outputs":
        job = store.get(a.job_id)
        res = _client_for_job(settings, cli, job, a.env_file).call("kernels", "files", job["kernel_ref"], "--format", "json")
        try:
            _emit({"job_id": job["job_id"], "account_profile": job.get("account_profile"),
                   "account_owner": job.get("account_owner"),
                   "account_email": job.get("account_email"), "files": json.loads(res.stdout)})
        except json.JSONDecodeError:
            _emit({"job_id": job["job_id"], "account_profile": job.get("account_profile"),
                   "account_owner": job.get("account_owner"),
                   "account_email": job.get("account_email"), "raw": res.text})
    elif a.command == "repair":
        record = store.get(a.job_id)
        slug = record["kernel_ref"].split("/", 1)[-1]
        if a.owner:
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", a.owner):
                raise RunnerError("Invalid Kaggle owner")
            new_ref = f"{a.owner}/{slug}"
        else:
            new_ref = validate_ref(a.kernel_ref, "kernel ref")
            if new_ref.split("/", 1)[1] != slug:
                raise RunnerError("Kernel slug differs from original; refusing to retarget a different notebook")
        store.set_kernel_ref(record["job_id"], new_ref)
        _emit({"job_id": record["job_id"], "kernel_ref": new_ref,
               "status": "ref_corrected", "note": "Local correction; remote status not verified"})
    elif a.command == "archive":
        if not a.confirm_inactive:
            raise RunnerError("Archive requires --confirm-inactive after checking Kaggle Active Events. "
                              "This does NOT stop a remote job.")
        record = store.get(a.job_id)
        store.update(record["job_id"], status="archived",
                     last_message="Manually confirmed inactive and archived locally")
        _emit({"job_id": record["job_id"], "kernel_ref": record["kernel_ref"],
               "status": "archived", "note": "Excluded from local preflight; remote job unchanged"})
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
        try:
            args = p.parse_args(argv)
        except SystemExit as exc:
            if int(exc.code or 0) != 0 and "--json" in (argv or sys.argv[1:]):
                print(json.dumps({"schema_version": "1.0", "ok": False,
                                  "error": {"code": "INVALID_ARGUMENTS",
                                            "message": "Invalid CLI arguments",
                                            "retryable": False, "action_required": "correct_arguments",
                                            "job_id": None, "account_profile": None,
                                            "details_redacted": {}}}))
                return int(exc.code)
            raise
        json_token = _JSON_OUTPUT.set(bool(args.json))
        command_token = _COMMAND.set(args.command or "")
        try:
            return dispatch(args)
        finally:
            _JSON_OUTPUT.reset(json_token)
            _COMMAND.reset(command_token)
    except RunnerError as exc:
        is_json = bool(getattr(locals().get("args"), "json", False)) or "--json" in (argv or sys.argv[1:])
        if is_json:
            raw = str(exc)
            code = raw.split(":", 1)[0].strip() if re.match(r"^[A-Z][A-Z0-9_]+:", raw) else "RUNNER_ERROR"
            # Raw Kaggle exception text may include secrets or private runtime context.
            # Do not echo diagnostics without guaranteed redaction.
            print(json.dumps({"schema_version": "1.0", "ok": False,
                "error": {"code": code, "message": "Operation failed; inspect redacted diagnostics",
                          "retryable": False, "action_required": "inspect_or_reconcile",
                          "job_id": None, "account_profile": None, "details_redacted": {}}}))
        else:
            print(f"kgr: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        if "--json" in (argv or sys.argv[1:]):
            print(json.dumps({"schema_version": "1.0", "ok": False,
                              "error": {"code": "INTERRUPTED", "message": "Interrupted",
                                        "retryable": False, "action_required": "reconcile",
                                        "job_id": None, "account_profile": None, "details_redacted": {}}}))
        else:
            print("kgr: interrupted", file=sys.stderr)
        return 130
