# Agent playbook — safely run, retrieve and report experiments

**Audience:** a coding agent running approved shell commands against `kgr`. This is a workflow specification; it does not imply that a resident agent runtime exists.

## Goal and non-goals

An agent should turn a scientific objective into a reproducible experiment run and a verifiable result. Today the repository provides a CLI primitive layer. Experiment planning, logging persistent intent, standardized metric evaluation, scheduling and unattended recovery require agent code or future repo features.

A complete lifecycle means:

```
REQUEST
   -> experiment definition / reproducibility checklist
   -> account authorization + data availability
   -> quota and local-slot preflight
   -> package inspection (`--dry-run`)
   -> submit exactly once
   -> verify remote run identity
   -> bounded polling / resume
   -> fetch logs and artifacts
   -> validate artifact hashes and expected result fields
   -> parse metrics, compare with baseline
   -> produce cited/evidenced experiment report
```

## 1. Make a run plan

Before a real push, keep a run plan (a plain text/JSON file is sufficient today) with: task objective, code revision, command, Kaggle profile alias, resource request and budget, input dataset/model versions, expected artifacts, seed, evaluation protocol, and a reproducible output directory.

**Do not mutate the experiment source or switch datasets after approval without creating a new plan version.** A future experiment-spec schema is proposed in [Result contracts](RESULT_CONTRACT.md).

## 2. Preflight and package

```bash
kgr team diagnose --json
kgr team check --gpu t4x2 --required-gpu-hours 2 --json
kgr run ./train.py --account alice --gpu t4x2 \
  --required-gpu-hours 2 --dry-run --json
```

- Parse output by field, not text line position. Confirm `gpu`, `kernel_type`, `account_profile`, source revision/refs and `staging_dir`.
- A `--dry-run` generates local files; it does not prove remote acceptance.
- Use only accounts the owner authorized for this job. Today's `--account auto` selects among **all configured profiles**. It is not a per-request permission boundary.

## 3. Submit once and retain identity

```bash
kgr run ./train.py --account alice --gpu t4x2 \
  --required-gpu-hours 2 --json
```

The response includes `job_id` and `kernel_ref`; store them with run-plan identity. `job_id` refers to local SQLite history; `kernel_ref` refers to Kaggle. When Kaggle returns a canonical URL, `kgr` updates the stored reference. If the command times out or exits with an ambiguous error, **do not immediately invoke it again**: remote success may have occurred. Inspect the ledger and remote link first. There is no idempotency key yet.

## 4. Monitor with bounded polling

Use `kgr status JOB_ID --json` until `status` is `complete`, `error` or `cancelled`, with a clear maximum wall-clock period. Prefer resuming a known job to repeating a push. A status read error should be classified as transient/recoverable only when evidence supports that conclusion; do not mark a job completed based on elapsed time.

For a convenient foreground session, `kgr run --wait --fetch` exists. An agent process that is restarted must recover from the ledger via `kgr jobs --json` or saved run-plan metadata, not from a now-lost shell process.

## 5. Fetch and inspect outputs

```bash
kgr logs JOB_ID --json
kgr outputs JOB_ID --json
kgr fetch JOB_ID --output ./results/JOB_ID --json
```

- `logs` are diagnostic evidence; **do not infer final metrics solely from loosely formatted log lines**.
- `outputs` lists remote artifact names; `fetch` downloads files and creates `kgr_download_manifest.json` with per-file SHA256 hashes and account metadata.
- Validate expected filenames, JSON syntax and required metrics. A remote `COMPLETE` state **does not guarantee** a valid scientific result.
- Never execute downloaded files from an untrusted kernel as code without inspection; keep per-job output directories isolated.

## 6. Aggregate and report

```bash
kgr report --refresh --output ./reports/jobs.json --json
```

This report is a **job status/inventory report**, not an accuracy or benchmark-results report. To answer scientific questions, an agent must parse application-specific result artifacts and validate their schema. A proposed standard result format and narrative report are in [Result contracts](RESULT_CONTRACT.md).

Every result report should include: `job_id`, account alias + owner (never token), Kaggle URL, source commit/hash, dataset/model refs, seed, GPU mode, execution outcome, artifact paths and hashes, metric definitions and values, errors, and a statement about missing/uncertain data.

## Recovery decision table

| Event | Agent action |
| --- | --- |
| Quota too low or 2/2 tracked jobs | Do not submit on that profile. Report shortage; use a different **authorized** profile only with permission. |
| `CANCEL_ACKNOWLEDGED` seen on a historical notebook | Show `cancelling/uncertain`; do not equate it with a currently allocated GPU, or a guaranteed free slot. |
| Local historical ref inaccessible | Run diagnostics, inspect saved URL; seek approval before `repair` or `archive`. |
| `kgr run` command timed out | Reconcile local ledger and remote run before another attempt. |
| `kgr status` temporarily unavailable | Retry status lookup within bounded read-retry policy; never push again because status read failed. |
| `status=complete` but result file missing | Mark result validation failed; preserve logs and artifacts. |
| `status=error` | Fetch available logs; classify failure; human/agent review before any revised submission. |
| Source commit or dataset version inconsistent with run plan | Reject result from comparison; request a corrected run plan. |
| Token appears in captured logs or reports | Stop, redact, notify owner and rotate credentials as appropriate. |

## Evidence-backed outcome categories

Use one of these report outcomes rather than a binary success flag:

- `SUCCESS_VALIDATED`: remote complete **and** required artifacts/metrics validated.
- `REMOTE_COMPLETE_INVALID_OUTPUT`: remote complete but schema/artifact validation failed.
- `REMOTE_FAILED`: Kaggle reported error/cancellation with evidence.
- `UNKNOWN_REMOTE_STATE`: terminal state could not be verified.
- `NOT_SUBMITTED`: dry run, blocked preflight, or explicit refusal.

These are **recommended reporting values**. They are **not** currently emitted by `kgr`; avoid claiming otherwise.
