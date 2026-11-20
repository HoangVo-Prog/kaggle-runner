# Roadmap: from Kaggle CLI to autonomous experiment management

**Goal:** AGENTS can safely plan, submit, monitor, recover, retrieve, validate, compare and report Kaggle experiments with minimal manual babysitting. **This is a gap analysis, not a claim that proposed features exist.** Baseline: commit `cff2a661a3e6357febd9c574158fd58d020781ac`.

## Current readiness snapshot

| Capability | Current state | Implication for unattended agents |
| --- | --- | --- |
| Team credentials in root `.env` | **IMPLEMENTED** | Username/token required; email metadata optional. |
| Per-profile GPU quota and local slot check | **IMPLEMENTED, best-effort** | No authoritative free-slot API or cross-machine visibility. |
| Automatic profile selection | **IMPLEMENTED, with constraints** | Chooses from configured accounts, not an explicit per-request authorization allowlist. |
| Submit `.py`, native `.ipynb`, attach datasets/models | **IMPLEMENTED** | Native notebooks need self-contained dependencies. |
| Job ledger, status, logs, fetch, job report | **IMPLEMENTED** | Per-command polling and local SQLite state only. |
| Source/job/download manifests with metadata | **IMPLEMENTED** | Good foundation for provenance; no experiment result contract. |
| Structured successful CLI output | **IMPLEMENTED** | `--json`, but errors still printed as unstructured stderr. |
| Agent authorization and action approvals | **PARTIAL** | Manual discipline only. |
| Duplicate-submit protection / idempotency | **MISSING** | Highest-risk remote action on timeout/restart. |
| Atomic per-profile slot reservations | **MISSING** | Parallel agents can over-admit. |
| Durable scheduler / queued runs / recovery | **MISSING** | Requires external process to stay alive. |
| Standard metrics, validation and aggregation | **MISSING** | Cannot reliably report scientific outcome. |
| Unified alerting / audit / observability | **MISSING** | Difficult to diagnose failure/retry histories. |

## P0 — must-have before agents can submit unattended

### P0.1 Durable experiment specification + idempotency

**Build:** versioned `ExperimentSpec`, stable `experiment_id` + `attempt_id`, content fingerprint of source revision/params/inputs/account scope. Add a unique SQLite constraint on `(experiment_id, attempt_id)`; write intent to the ledger *before* any remote push; reconcile unknown pushes against canonical refs rather than auto-retrying.

**Acceptance tests:** (a) same command retried after a client timeout does not create two remote kernels; (b) process crash after push but before local commit is recoverable with deterministic reconciliation or marked `manual_review`; (c) intentionally new attempt creates a distinct run.

### P0.2 Atomic scheduler, per-account scoping and explicit authorization

**Build:** job queue in SQLite with `BEGIN IMMEDIATE` lease/reservation transactions for each account profile, lease expiry and heartbeat, explicit per-experiment allowlist of accounts, max slots per profile and quotas. Convert selected profile + successful reservation into one atomic admission decision. Fix the existing `account_profile=None` behavior: default-account checks must use `account_profile IS NULL` and team checks must use exact alias, never a global mixed-ledger candidate set under one token. Reject unauthorized owner even when token exists in local `.env`.

**Acceptance tests:** two agents submit concurrently against the same ledger and never reserve >2 local jobs on the same account; accounts are never silently substituted outside the authorization allowlist; crash releases expired leases only after remote reconciliation.

**Important:** SQLite transactions coordinate only processes sharing the same database; multi-machine execution needs a shared durable database or central coordinator, not just separate SQLite files.

### P0.3 Structured error envelope + typed state machine

**Build:** JSON error contract (`code`, `message`, `retryable`, `action_required`, `job_id`, `account_profile`, `details_redacted`); stable versioned state machine with `queued`, `preflighting`, `staging`, `submitting`, `submitted`, `running`, `succeeded`, `failed`, `blocked`, `unknown_remote`, `cancelled` and validated-result substates. Distinguish read retries from submit retries. All operations should return machine-readable errors when `--json` is supplied.

**Acceptance tests:** malformed/denied/expired token, 403, 404, 429, 5xx, timeout, stale ref, unknown Kaggle status and invalid result all produce known typed outcomes; errors contain no secrets.

### P0.4 Standardized outputs and verifiable result reports

**Build:** `experiment_spec` and `experiment_result` versioned JSON Schemas; validate required outputs, metrics, units, seed, split, source revision, artifact hashes. Add `kgr results validate` and `kgr results aggregate`. Keep an immutable evidence bundle of input spec, submit response, status timeline, logs, file checksums, validation report and final summary.

**Acceptance tests:** a remote `COMPLETE` with missing/malformed result is *not* reported as success; a valid experiment result produces the same deterministic report across repeated reads; inconsistent dataset/seed/protocol prevents aggregation.

### P0.5 Safe recovery and bounded retry policy

**Build:** state-aware reconciliation on restart; bounded exponential backoff with jitter for *read-only* status/quota/output checks; no automatic re-push after ambiguous submission. Distinguish retryable infrastructure errors from experiment code failures. Provide explicit `resume` and human-reviewed `retry` operations.

**Acceptance tests:** process restart resumes existing job instead of uploading a second one; 429 retry honors backoff budget; persistent 403 becomes an action-required record; reaching timeout keeps job recoverable.

## P1 — unattended team operations

### P1.1 Scheduler/agent daemon

Build optional foreground worker/service (or an external orchestrator adapter) to poll queued jobs, maintain leases, resume after restarts, honor deadlines and collect artifacts. Provide `kgr queue`, `kgr worker run`, `kgr reconcile` as **proposed** commands. Avoid unbounded polling/API pressure.

### P1.2 Cross-machine state and ownership

Use a central coordinator/DB for multiple operators, shared job IDs, account reservations and audit events. Avoid putting every teammate's API token on a shared network drive: credentials should stay with authorized owners / trusted secrets managers. Consider a per-owner worker service that executes only approved jobs and reports metadata back to a central scheduler.

### P1.3 Notifications and human checkpoints

Deliver concise progress, blocked/failed/completed result summaries to chosen channels. Put explicit approvals around data publication, large compute spend, credential changes, destructive cleanup and re-running failed experiments. Timeouts should create follow-up tasks, not silent loops.

### P1.4 Experiment matrix and research-grade reproducibility

Support parameter sweeps with fixed seeds, dataset versioning, source commit pinning, dependency lockfiles, and confidence intervals; budget allocation across repeats. Separate *experiment plan* from *Kaggle execution instance*. Store a unique `run_id` for every attempt.

### P1.5 Audit, privacy and secret hygiene

Structured event log (`who requested`, `who authorized`, `what profile`, `what input`, `what action`, `when`, `result`, `why`); token rotation/revocation and profile disablement; redaction tests for all exception/log/manifest/output paths. Audit updates to `.env` and job controls without retaining secret values.

## P2 — scientific agents at scale

- Benchmark protocol adapters (classification, vision QA, retrieval, etc.) with domain-specific metric validation.
- Failure taxonomy and automatic **analysis** of logs/metrics (not automatic unbounded relaunch).
- Baseline comparison and report generation (Markdown/HTML/CSV), uncertainty calculations and plot artifacts.
- Distributed workers, queue priorities, fairness and per-project budgets.
- Alerting and dashboards for job states and quota snapshots.
- Optional integrations (MCP/agent tool wrapper, GitHub/PR checks, scheduled agent workflows) that call versioned `kgr` APIs.

## Recommended implementation order

| Milestone | Scope | Demo / exit criterion |
| --- | --- | --- |
| M0 | Align docs + current CLI, establish contracts | Agent can follow `AGENTS.md` without calling fictional commands. |
| M1 | P0.1 + P0.2 | Two concurrent agents cannot double-push or over-reserve account slots. |
| M2 | P0.3 + P0.5 | Crash/restart/retry matrix passes; no duplicate remote submission. |
| M3 | P0.4 | Remote COMPLETE + validated metrics yields reproducible scientific result report. |
| M4 | P1.1 + P1.2 | Two authorized machines cooperate safely via coordinator; no shared token files. |
| M5 | P1.3–P2 | Agent executes experiment matrix and produces a complete audit-linked research report. |

## Example future interface (all NOT IMPLEMENTED)

```bash
kgr experiment validate experiments/sweep.json --json
kgr queue submit experiments/sweep.json --allow-account alice --json
kgr worker run --once --json
kgr reconcile --json
kgr results validate JOB_ID --json
kgr results aggregate --experiment-id EXPT_ID --json
kgr report experiment EXPT_ID --format md
```

Do not implement a flashy agent UI before P0 idempotency, reservations, schema validation and recovery. These are what make autonomous operation **correct**, not just convenient.


## Candidate implementation status (post-baseline)

Agent-specific source inspection, durable attempt identities, local transactional reservations, reconcile, result validation/aggregation and reports are implemented in the candidate patch. Full P0 remains **unverified and incomplete** pending regression tests on the actual repository, legacy-run transactional integration, provider-specific error taxonomy and live Kaggle validation. Do not mark P0 complete yet.
