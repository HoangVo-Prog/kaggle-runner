# Agent experiment lifecycle

The CLI implements a foreground lifecycle. It does not run an unattended scheduler, coordinate separate machines or prove Kaggle-wide capacity. A successful push means only that the push command returned; scientific success requires a valid result.

## Lifecycle map

| Transition | Implementation and evidence | Status |
| --- | --- | --- |
| `SPEC → VALIDATE` | `ExperimentSpec` JSON Schema, authorization, refs, paths, source and output checks | Implemented; offline tests |
| `VALIDATE → PACKAGE` | Deterministic `.py`/`.ipynb` source inspection and staging, hashes and exclusions | Implemented; offline tests |
| `PACKAGE → AUTHORIZE` | Explicit `allowed_profiles`; `auto` is scoped to that list | Implemented; remote account credentials unverified |
| `AUTHORIZE → PREFLIGHT` | Profile quota and local active job checks | Implemented; Kaggle remains capacity authority |
| `PREFLIGHT → RESERVE` | SQLite `BEGIN IMMEDIATE` admission across agent leases and legacy job rows | Implemented and locally tested for competing processes |
| `RESERVE → SUBMIT` | Idempotent `(experiment_id, attempt_id)` ledger record; durable push evidence | Implemented; fake/offline coverage, Kaggle unverified |
| `SUBMIT → RECONCILE` | Original owner and kernel ref are persisted; `not_pushed`, `submission_unknown`, `remote_confirmed` marker | Implemented; remote visibility unverified |
| `RECONCILE → MONITOR` | Read-only status reconciliation and lease renewal; `manual_review` sets `ok: false` | Implemented; remote access unverified |
| `MONITOR → FETCH` | Resume polls and fetches with original profile; download manifest hashes files | Implemented; remote transfer unverified |
| `FETCH → VALIDATE` | Versioned result schema, spec identity, source, dataset, metric and artifact checks | Implemented; offline tested |
| `VALIDATE → AGGREGATE → REPORT` | Compatible attempts only; evidence bundle has hashes and state events | Implemented; statistical assumptions require scientific review |

## Recommended sequence

1. Read the spec and confirm research objective, privacy, compute budget, explicit authorized profiles, data/model refs, parameters and outputs.
2. Inspect both files and schema before any external action:

   ```bash
   kgr package inspect ./experiment --entry train.py --json
   kgr experiment validate ./experiment/experiment.json --json
   ```

3. Check credentials and account resources. `team diagnose` is local; `team check` queries Kaggle. Platform quotas may change after preflight.
4. Submit exactly once and save all returned identity fields:

   ```bash
   kgr experiment submit ./experiment/experiment.json --account alice --approve --json
   ```

5. For timeout or interruption, do not resubmit. Inspect `kgr experiment show ID --json` and run `kgr reconcile --experiment-id ID --json`. A nonzero exit or `ok: false`, especially `manual_review`, requires operator review.
6. Resume the existing experiment using `kgr experiment resume ID --wait --max-wait-minutes 45 --json`. Confirm remote completion before artifact validation.
7. Validate each job result, aggregate only compatible attempts, then generate a report and evidence bundle:

   ```bash
   kgr results validate JOB_ID --json
   kgr results aggregate --experiment-id ID --json
   kgr report experiment ID --format md
   ```

## Fail closed

Stop for missing/invalid credentials, authorization mismatch, quota or slot blocking, source hash drift, suspected secrets, unknown remote state, inaccessible kernel, schema mismatch, missing/inconsistent artifacts, budget excess or scientifically invalid metrics. Do not release an uncertain lease, rotate profiles, cancel/archive a remote kernel, upload private data or retry failed experiment code automatically.

## Limits

The admission count is local to processes sharing the same `jobs.sqlite3`. It does not run an unattended scheduler, coordinate separate machines or prove Kaggle-wide capacity. Legacy `kgr run` now joins atomic local slot admission when it stages a job, but it does not have agent idempotency/reconciliation guarantees. Never retry an ambiguous legacy push. Live Kaggle stress testing remains unverified and requires separate operator authorization.
