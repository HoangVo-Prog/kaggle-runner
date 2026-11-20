# AGENTS.md — Kaggle Runner agent operating contract

> Applies to the `kaggle-runner` repository. Grounded against branch `cleanup/runner-only`, commit `cff2a661a3e6357febd9c574158fd58d020781ac` (2026-11-20). This document describes **current commands** and **required safeguards**; aspirational capabilities are explicitly marked **NOT IMPLEMENTED**.

## Mission

Operate reproducible Kaggle experiments through the `kgr` CLI: inspect authorized resources, validate a submission, submit, track a persistent job ID, retrieve logs/artifacts, extract metrics and publish an evidence-based report. Never claim that submitting a kernel is equivalent to completing an experiment.

## Mandatory operating rules

1. **Use a pre-authorized account.** The repo root `.env` defines team profiles via `KGR_TEAM_PROFILES` and `KGR_TEAM_<ALIAS>_USERNAME` / `_API_TOKEN`. `kgr team diagnose` and `kgr team list` can inspect configuration without exposing tokens. Run only under profiles whose owners explicitly authorized the agent's workload. `--account auto` is permitted only inside that pre-authorized scope; today it considers all configured team profiles, so isolate `.env` or select an explicit alias when authorization is narrower.
2. **Never read, display, commit, transmit, or embed secrets.** Do not print `.env`, `KAGGLE_API_TOKEN`, shell environments, or raw CI secret stores. Avoid `set -x` when credentials may be present. Never store `.env` in Kaggle source, notebook cells, artifacts or reports. Use synthetic credentials in tests.
3. **Prefer machine-readable output:** append `--json` to commands; parse JSON fields and process exit codes. Human-oriented terminal summaries are not a stable protocol. Note: `kgr logs --follow` streams plain text and rejects `--json`.
4. **Do not infer free GPU/CPU slots from quota alone.** `kgr team check` estimates account-level eligibility from tracked local jobs plus GPU quota. This is *not* an authoritative reservation of Kaggle capacity. Interactive or out-of-band runs may be invisible. CPU and GPU share the same two-job *local policy cap* per profile.
5. **Do not submit until inputs are validated.** Check expected source, entrypoint, GPU type, required GPU hours, dataset/model references, intended profile, privacy/internet settings, and output schema. Run `--dry-run` with an **explicit** account alias (not `auto`) before real submission when feasible.
6. **Persist job identity before doing follow-up work.** Record `job_id`, `kernel_ref`, account profile and owner, input/source revision, launch time, and output location. Do not assume a locally generated Job ID proves the remote kernel exists; verify the submit response and subsequent status.
7. **Never blindly retry `kgr run` after timeout or an ambiguous response.** A remote kernel may already have been created. First inspect `kgr jobs`, `kgr status`, `kgr show`, and the Kaggle URL. A durable idempotency key is **NOT IMPLEMENTED**.
8. **Bound monitoring.** `kgr run --wait` has a deadline. A monitoring timeout does not cancel the remote run. Save the job ID and resume with `kgr status JOB_ID`; do not create a replacement run by default.
9. **Treat uncertainty explicitly.** Distinguish `submitted`, `running`, `complete`, `error`, `cancelled`, `status_unavailable`, `archived`, and unknown states; do not report a successful experiment without a completed remote execution and validated output.
10. **Do not archive or repair automatically.** `kgr repair` edits a local kernel reference; `kgr archive JOB_ID --confirm-inactive` excludes a job from local preflight but does **not** cancel it on Kaggle. Both require independent verification or human authorization.
11. **Never fabricate metrics.** Report only metrics extracted from validated artifacts (e.g., `result.json`) and label missing metrics as `unavailable`; report the metric definition, source artifact, seed, dataset split, and code revision whenever provided.
12. **Do not use automatic account selection to evade platform limits.** Respect each member's authorization and Kaggle's account and resource terms. Avoid mixing different owners' work without permission.

## Supported tools and precise commands

```bash
kgr team diagnose
kgr team list --json
kgr team check --gpu t4x2 --required-gpu-hours 2 --json
kgr run ./train.py --account alice --gpu t4x2 --required-gpu-hours 2 --dry-run --json
kgr run ./train.py --account alice --gpu t4x2 --required-gpu-hours 2 --json
kgr run ./train.py --account auto --gpu cpu --json
kgr jobs --json
kgr status JOB_ID --json
kgr logs JOB_ID --json
kgr outputs JOB_ID --json
kgr fetch JOB_ID --output ./results/JOB_ID --json
kgr report --refresh --output ./reports/jobs.json --json
```

`kgr check`, `kgr quota`, and `kgr active` without `--account` operate on the legacy/default account rather than iterating the team. For team-wide eligibility use `kgr team check`. **Known caveat at the pinned commit:** default `kgr active/check` can select team jobs in a mixed SQLite ledger using the default token. Do not use these default commands as a team-wide source of truth. `--json` is available before/after commands and in nested subcommands. Source packaging, fetch, and reports are implemented; a standardized experiment result parser, idempotency key, atomic scheduler, and unattended service are **NOT IMPLEMENTED**.

## Required agent workflow

```
ASSIGN -> VALIDATE -> PREFLIGHT -> DRY_RUN -> SUBMIT -> VERIFY_REMOTE
    -> MONITOR -> FETCH -> VALIDATE_OUTPUT -> ANALYZE -> REPORT -> ARCHIVE_RECORD
```

- **ASSIGN:** Establish experimental objective and explicit authorized account scope.
- **VALIDATE:** Confirm source commit, dataset/model versions, expected outputs and budget.
- **PREFLIGHT:** Use per-account checks; never infer global availability.
- **DRY_RUN:** Check generated kernel metadata and source package; keep credentials out.
- **SUBMIT:** One actual submit per logical experiment; capture `job_id`.
- **VERIFY_REMOTE:** Confirm canonical `kernel_ref` from Kaggle and first status.
- **MONITOR:** Poll with bounded retries for transient read errors, not resubmission.
- **FETCH:** Download logs and output to a unique per-job directory.
- **VALIDATE_OUTPUT:** Check expected filenames/JSON, hashes, metric schema and job success.
- **ANALYZE:** Compare metrics only for compatible datasets/splits/protocols.
- **REPORT:** Include source, settings, account metadata, artifact provenance, failures and limitations.
- **ARCHIVE_RECORD:** Store reports outside credentials and keep recoverable job history.

## Stop conditions requiring operator involvement

- Missing profile/token, unauthorized profile, unrecognized owner, or unexpected credential usage.
- CPU/GPU quota exhausted, uncertain slot availability when strict confidence is required, or policy violation.
- Duplicate/ambiguous Kaggle submission, unexpected source code or dataset, remote execution with a different account.
- Output schema mismatch, inaccessible private data, repeated unknown status, rate limits, or suspicious token exposure.
- Any change that would delete source/artifacts, publish private data, or spend unapproved compute.

## Code contributors: checks before claiming completion

```bash
python -m pip install -e '.[kaggle]'
python -m unittest discover -s tests -v
python -m unittest discover -s tests -p 'test_team_profiles.py' -v
python -m unittest discover -s tests -p 'test_slot_scenarios.py' -v
```

Unit tests mock Kaggle and **do not prove live resource availability**. Live smoke testing is opt-in and must use approved credentials:

```bash
python examples/team-env-multi-account/run_live_test.py \
  --accounts alice bob --gpu cpu --confirm-live
```

**Never run the live smoke test automatically as part of a normal coding-agent test pass.** It creates two real Kaggle jobs.

Read next: [documentation index](docs/INDEX.md), [agent workflow](docs/AGENT_WORKFLOW.md), [roadmap](docs/ROADMAP_AGENT_FIRST.md).
