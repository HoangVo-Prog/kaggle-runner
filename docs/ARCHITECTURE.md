# Architecture

## Ownership today

```text
kgr CLI (cli.py)
  ├─ agent_workflow.py ─ spec checks, authorization, submission, recovery,
  │                      aggregation and evidence reports
  ├─ experiments/results.py ─ scientific result and artifact validation
  ├─ commands/agent.py ─ package/experiment/results/report CLI handlers
  ├─ agent_ledger.py ─── agent attempts, append-only events and local leases
  ├─ work_checkpoint.py ─ objective plans, checklist evidence and orchestration handoff
  ├─ runner.py ───────── staging, Kaggle push, status and artifact fetch
  ├─ source_contract.py / packaging.py / notebooks.py ─ source inspection/runtime
  ├─ preflight.py ────── local activity and Kaggle resource checks
  ├─ jobs.py ─────────── legacy-compatible SQLite job history
  ├─ accounts.py / team.py ─ credential selection and profile metadata
  └─ api.py ──────────── isolated official Kaggle CLI subprocess
```

This is the current ownership map. `agent_workflow.validate_result` remains as a compatibility wrapper around `experiments/results.py`; agent orchestration and reporting still live together in `agent_workflow.py`. `commands/agent.py` separates agent CLI handling from shared legacy dispatch. `schema_validation.py` centralizes JSON Schema loading, and schemas are bundled as package data for wheel installs. A fuller split of spec/submission/lifecycle/reporting ownership remains planned.

## Lifecycle and persistence

`kgr experiment submit` validates an immutable spec, fingerprints source and inputs, chooses only an allowed profile, runs preflight, then atomically reserves local capacity in `jobs.sqlite3`. The agent ledger persists `(experiment_id, attempt_id)`, chosen owner/profile, deterministic job ID, kernel ref, state and events before pushing.

`runner.py` stages a job under the state directory. `jobs/<job_id>/submission.json` is atomically written as `not_pushed`, changed to `submission_unknown` immediately before the push boundary, and changed to `remote_confirmed` after a successful response. An exception or crash after the ambiguity marker must never trigger a blind push. Local marker absence means the evidence is incomplete, not that a push is safe.

Agent attempts and legacy job rows share `jobs.sqlite3`. Agent reservation transactions count held agent leases plus active legacy rows, and legacy `JobStore.add(..., admission_limit=...)` takes `BEGIN IMMEDIATE` and counts held agent leases plus active legacy rows before inserting its prepared row. Exact account aliases are used (`NULL` is the default account). Competing legacy and agent submissions therefore serialize at local admission when they use the same database. A confirmed remote terminal state releases the agent lease. Expired leases remain held until reconciliation; expiry alone is not evidence that remote compute ended.

The machine-local `setup-checkpoint.json` is stored beside `jobs.sqlite3` under `Settings.state`. It records versioned local checks and an append-only verification history, never experiment state or credentials. Atomic replace plus a sidecar lock preserves concurrent verification history. Checkpoint corruption is reported and never overwritten automatically. `setup status` reads SQLite in read-only mode and never queries Kaggle; online auth success is stored separately from local readiness and becomes stale after 24 hours or credential metadata changes.

Work objectives add `work_objectives`, `work_steps`, `work_events` and `work_attempt_links` to that same database through additive migrations. Attempt links contain stable identifiers and immutable spec paths/hashes only; remote state, leases, validation and results are joined from `agent_attempts` on read. Revision-checked objective changes serialize under `BEGIN IMMEDIATE`. The foreground executor submits bounded groups through `agent_workflow.submit`, which performs a fresh preflight and reservation for every attempt, and uses the existing resume path for reconciliation/fetch/validation when `--wait` is selected.

## Boundaries

- One local SQLite file coordinates only processes using that file. It does not coordinate other machines or reserve Kaggle capacity.
- Legacy `kgr run` participates in atomic local admission at the staging boundary, but does not persist the agent's immutable attempt identity or provide agent reconciliation/idempotency. Its remote push ambiguity still needs manual investigation.
- `remote_complete` and scientific `succeeded` are distinct. `result.json` must pass schema, identity, metric and artifact checks before success.
- Kaggle CLI/API behavior, live credentials, quota and remote visibility are unverified by offline tests.
