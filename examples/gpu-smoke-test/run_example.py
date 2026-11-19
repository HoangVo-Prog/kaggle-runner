"""Submit the companion GPU smoke test, poll Kaggle, then fetch outputs.

Usage (from anywhere, with kaggle-runner installed):
    python path/to/examples/gpu-smoke-test/run_example.py --owner YOUR_KAGGLE_USERNAME
    # --owner may be omitted when [runner].owner is configured
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

EXAMPLE_DIR = Path(__file__).resolve().parent
TERMINAL = {"complete", "error", "cancelled"}


def call_kgr(*args: str, profile: str | None = None) -> dict:
    command = [sys.executable, "-m", "kaggle_runner"]
    if profile:
        command += ["--profile", profile]
    command += list(args)
    p = subprocess.run(command, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        raise RuntimeError(f"Command failed (exit {p.returncode}): {' '.join(command)}\n{p.stderr.strip()}\n{p.stdout.strip()}")
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Expected JSON from kaggle-runner; received:\n{p.stdout[-2000:]}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Submit, monitor and fetch a short Kaggle T4x2 GPU smoke test")
    parser.add_argument("--owner", help="Kaggle profile URL username (NOT display name); defaults to [runner].owner")
    parser.add_argument("--profile", help="Optional existing kaggle-runner auth profile alias")
    parser.add_argument("--dry-run", action="store_true", help="Package locally without contacting Kaggle")
    parser.add_argument("--interval-seconds", type=int, default=20)
    parser.add_argument("--max-wait-minutes", type=int, default=45)
    parser.add_argument("--output", help="Download destination (default: ./gpu-smoke-results/JOB_ID)")
    args = parser.parse_args(argv)
    if args.interval_seconds < 1 or args.max_wait_minutes < 1:
        parser.error("--interval-seconds and --max-wait-minutes must both be >= 1")

    # A dry-run deliberately works without a Kaggle account or Kaggle CLI installed.
    if not args.dry_run:
        try:
            doctor = call_kgr("doctor", "--online", profile=args.profile)
        except RuntimeError as exc:
            raise RuntimeError(
                "Kaggle CLI/authentication check failed. Install with pip install -e '.[kaggle]' "
                "and log in using: python -m kaggle auth login. "
                f"Details: {exc}"
            ) from exc
        if not doctor.get("kaggle_auth_ok"):
            raise RuntimeError("Kaggle authentication failed. Run: python -m kaggle auth login")

    run_args = ["run", "--source", str(EXAMPLE_DIR), "--entry", "gpu_probe.py", "--gpu", "t4x2"]
    if args.owner:
        run_args += ["--owner", args.owner]
    if args.dry_run:
        run_args.append("--dry-run")
    response = call_kgr(*run_args, profile=args.profile)
    job_id = response["job_id"]
    print(f"Job ID: {job_id}", flush=True)
    print(f"Kaggle kernel: {response['kernel_ref']}", flush=True)
    if response.get("submit_output"):
        print("Kaggle submit response:\n" + response["submit_output"], flush=True)
    if args.dry_run:
        print(f"Dry-run staging: {response['staging_dir']}", flush=True)
        return 0

    deadline = time.monotonic() + args.max_wait_minutes * 60
    status = "unknown"
    while True:
        try:
            report = call_kgr("status", job_id, profile=args.profile)
        except RuntimeError as exc:
            print("Kaggle status request failed. The local Job ID does not prove the remote kernel exists.", file=sys.stderr)
            print("Check the Kaggle submit response above and try:", file=sys.stderr)
            print("  python -m kaggle kernels list --mine", file=sys.stderr)
            print(f"  python -m kaggle kernels status {response['kernel_ref']}", file=sys.stderr)
            print("Verify the account's username from its Kaggle profile URL, not its display name.", file=sys.stderr)
            raise
        status = report.get("status", "unknown")
        print(f"[{time.strftime('%H:%M:%S')}] {job_id[:10]}: {status}", flush=True)
        if status in TERMINAL:
            break
        if time.monotonic() >= deadline:
            print(f"Timed out watching job {job_id}. It may still run remotely.", file=sys.stderr)
            print(f"Check later with: kgr status {job_id}", file=sys.stderr)
            return 3
        time.sleep(min(args.interval_seconds, max(0.0, deadline - time.monotonic())))

    target = Path(args.output).expanduser().resolve() if args.output else (Path.cwd() / "gpu-smoke-results" / job_id)
    try:
        downloaded = call_kgr("fetch", job_id, "--output", str(target), "--force", profile=args.profile)
        print(f"Downloaded to: {downloaded['output_dir']}", flush=True)
        print("Files: " + ", ".join(item["name"] for item in downloaded["files"]), flush=True)
        report_file = target / "gpu_smoke_result.json"
        if report_file.is_file():
            payload = json.loads(report_file.read_text(encoding="utf-8"))
            print(f"GPU report: status={payload.get('status')}, devices={payload.get('cuda_device_count')}", flush=True)
    except Exception as exc:
        print(f"Output download failed: {exc}", file=sys.stderr)
        return 4
    if status != "complete":
        print(f"Remote Kaggle job finished as {status}; inspect downloaded logs/report.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, KeyboardInterrupt) as exc:
        print(f"gpu-smoke-example: {exc}", file=sys.stderr)
        raise SystemExit(1)
