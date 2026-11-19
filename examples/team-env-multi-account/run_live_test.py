"""Explicit opt-in: submit and verify one real Kaggle job on EACH of two team accounts.

Requires two authorized API tokens already configured in the selected .env.
Only invokes `kgr`/`kaggle` locally; never prints or exports tokens.
This test is not executed by unit tests or by an ordinary package install.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def cli(*args: str) -> dict | list:
    command = [sys.executable, "-m", "kaggle_runner", *args, "--json"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = result.stderr.strip()[-700:] or result.stdout.strip()[-700:]
        raise RuntimeError(f"kgr command failed ({result.returncode}): {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"kgr returned non-JSON output: {result.stdout[:300]}") from exc


def main() -> int:
    p = argparse.ArgumentParser(description="Opt-in live Kaggle smoke test across two team profiles")
    p.add_argument("--accounts", nargs=2, required=True, metavar=("ACCOUNT_A", "ACCOUNT_B"))
    p.add_argument("--env-file", help="Private team .env (default: kaggle-runner repo root)")
    p.add_argument("--state-dir", help="Use a specific kgr job ledger (default runner state)")
    p.add_argument("--output-dir", default="./kgr-team-live-results")
    p.add_argument("--gpu", choices=("cpu", "t4x2"), default="cpu")
    p.add_argument("--required-gpu-hours", type=float, default=0.25)
    p.add_argument("--poll-seconds", type=int, default=10)
    p.add_argument("--max-wait-minutes", type=int, default=12)
    p.add_argument("--no-wait", action="store_true", help="Submit both jobs only, then stop")
    p.add_argument("--confirm-live", action="store_true", help="Explicitly authorize TWO real Kaggle submissions")
    a = p.parse_args()
    if not a.confirm_live:
        p.error("This will push two real Kaggle kernels. Add --confirm-live to proceed.")
    if a.accounts[0] == a.accounts[1]:
        p.error("Select two distinct account profiles")
    if a.poll_seconds < 1 or a.max_wait_minutes < 1:
        p.error("Polling and wait limits must be positive")
    # A repo checkout is required for default discovery; callers can still
    # explicitly select another private .env with --env-file.
    from kaggle_runner.accounts import preferred_env_file
    root = preferred_env_file(a.env_file)
    if not root.is_file():
        p.error(f".env not found: {root}")
    base = ["--env-file", str(root)]
    if a.state_dir:
        base.extend(["--state-dir", str(Path(a.state_dir).expanduser().resolve())])
    names = cli(*base, "team", "list")
    known = {row["account_profile"]: row for row in names}
    for alias in a.accounts:
        if alias not in known:
            p.error(f"Unknown profile {alias!r} in {root}; use `kgr team list`")
    if known[a.accounts[0]]["account_owner"].lower() == known[a.accounts[1]]["account_owner"].lower():
        p.error("The two selected profiles must represent different Kaggle usernames")

    source = Path(__file__).with_name("smoke.py").resolve()
    submitted = []
    for alias in a.accounts:
        # Checks each profile separately; failure leaves the other account untouched.
        print(f"Submitting CPU/GPU smoke as [{alias}]...", flush=True)
        result = cli(*base, "run", str(source), "--account", alias,
                     "--gpu", a.gpu, "--required-gpu-hours", str(a.required_gpu_hours))
        if result.get("account_profile") != alias:
            raise RuntimeError("Job account routing mismatch")
        submitted.append(result)
        print(f"  {alias}: {result['job_id'][:12]}  https://www.kaggle.com/code/{result['kernel_ref']}", flush=True)

    if a.no_wait:
        print("Both submissions returned successfully. Use `kgr status JOB_ID` to track them.")
        return 0

    destination = Path(a.output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + a.max_wait_minutes * 60
    pending = {job["job_id"]: job for job in submitted}
    completed = set()
    while pending and time.monotonic() < deadline:
        for job_id, job in list(pending.items()):
            try:
                status = cli(*base, "status", job_id)
            except RuntimeError as exc:
                print(f"  {job['account_profile']}: status temporarily unavailable ({str(exc)[:120]})")
                continue
            state = status.get("status")
            print(f"  {job['account_profile']}: {state}", flush=True)
            if state == "complete":
                completed.add(job_id)
                del pending[job_id]
            elif state in {"error", "cancelled", "failed"}:
                raise RuntimeError(f"{job['account_profile']} job {job_id} ended as {state}")
        if pending:
            time.sleep(a.poll_seconds)
    if pending:
        raise RuntimeError("Timed out waiting for: " + ", ".join(j["account_profile"] for j in pending.values()))

    for job in submitted:
        alias, job_id = job["account_profile"], job["job_id"]
        output = destination / alias / job_id
        result = cli(*base, "fetch", job_id, "--output", str(output))
        if result.get("account_profile") != alias:
            raise RuntimeError("Downloaded manifest account mismatch")
        report_file = output / "team_smoke_result.json"
        if not report_file.is_file() or not json.loads(report_file.read_text()).get("ok"):
            raise RuntimeError(f"Missing/invalid smoke artifact for {alias} at {report_file}")
        logs = cli(*base, "logs", job_id)
        (output / "job_logs.txt").write_text(logs.get("logs", ""), encoding="utf-8")
        manifest = json.loads((output / "kgr_download_manifest.json").read_text())
        if manifest.get("account_profile") != alias:
            raise RuntimeError("Downloaded manifest missing correct account metadata")
        print(f"PASS [{alias}] job={job_id[:12]}  artifacts={output}", flush=True)
    print("PASS: two different Kaggle profiles submitted, executed and downloaded independently.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"LIVE TEST FAILED: {exc}", file=sys.stderr)
        raise SystemExit(2)
