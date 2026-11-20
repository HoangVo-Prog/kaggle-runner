# Operations and recovery

## Persistent setup checkpoint

Start each new Codex session with:

```bash
kgr setup status --json
```

If the checkpoint is ready, reuse the installed interpreter and credentials. If it reports stale components, run `kgr setup verify --offline --json`; unchanged checks retain their prior timestamps, and the offline test suite runs only when its source/dependency fingerprint changed. For remote authentication, choose one configured profile explicitly:

```bash
kgr setup verify --online --account alice --json
```

This issues one read-only `kernels list --mine` request. It does not submit work, check GPU quota, rotate accounts or edit credentials. Credential presence is not authentication: rejected checks are `failed`; network outages are `unverified` and preserve local records. Successful authentication has a separate timestamp and becomes stale after 24 hours. Local setup readiness does not depend on recent online authentication.

`ready` reports local Python/package/dependency/source-test readiness. `remote_prerequisites_ready` additionally requires the Kaggle CLI, a configured account and a credential file whose permissions were verified. `online_auth_ready` is separate and reports a recent successful online check.

Example: a first session sees `checkpoint_status: missing`, then runs `kgr setup verify --offline --json`. A later session sees `checkpoint_status: verified` and reuses checks in place. With unchanged Python/dependencies/source, its offline verify response has an empty `revalidated` list and lists the existing checks under `reused`.

The checkpoint is under the selected `Settings.state` (`KGR_STATE_DIR` or `--state-dir`). `setup status` is read-only. Verification writes the checkpoint atomically and keeps history; it does not install packages, create credentials, or create/reset `jobs.sqlite3`. A corrupt checkpoint is preserved for operator review.

## Persistent research objectives

Objective checkpoints extend the same `jobs.sqlite3`; they do not copy or invent experiment/job states. The agent ledger remains authoritative for remote state and scientifically validated results. Work rows use append-only events, revision checks, and stable objective/experiment/attempt IDs. Specs are saved as immutable snapshots under the selected state directory.

```bash
kgr work create --title "Notebook evaluation" \
  --acceptance-criterion "All validated runs use the pinned protocol" \
  --authorized-profile alice --json
kgr work show OBJECTIVE_ID --json
kgr work plan OBJECTIVE_ID --file plan.json --expected-revision 1 --json
kgr work approve OBJECTIVE_ID --expected-revision 2 --json
kgr work execute OBJECTIVE_ID --account auto --wait --json
kgr work show OBJECTIVE_ID --json
kgr work report OBJECTIVE_ID --format md
```

`plan.json` may contain one immutable `template` ExperimentSpec plus a unique `seeds` array, or an `experiments` array of `{ "template": ExperimentSpec, "seeds": [...] }` entries for multiple scientific configurations. Each experiment ID must be unique; attempt IDs are stable within that experiment. Shared settings include `total_gpu_hours`, `planned_gpu_hours_per_attempt`, `max_concurrent` (1 or 2), and optional polling, timeout, approval, failure, partial-completion and checklist settings. An experiment entry may override the per-attempt GPU-hour estimate. For example, ten seeds with a 0.5 GPU-hour estimate each require an approved total budget of at least 5 GPU-hours. Planning pins the inspected source SHA into each per-seed spec and rejects plans outside the objective profile allowlist or budget. Each submit uses the existing fresh preflight and atomic per-account lease; quota/slot errors stop the batch. `--wait` uses the existing reconcile, fetch and scientific-result validation lifecycle for every experiment represented in each bounded batch.

If execution is interrupted, use `kgr work show OBJECTIVE_ID --json`, reconcile linked experiments, then rerun `kgr work execute OBJECTIVE_ID --wait`. Existing attempt IDs are never submitted again. `unknown_remote`, failed attempts, changed snapshots and inaccessible kernels require operator review. The objective remains `needs_review` or `blocked`; completion requires all checklist steps and evidence for every acceptance criterion. Use `kgr work complete OBJECTIVE_ID --expected-revision N --acceptance-review-json '[...]'` only after checking the authoritative attempt results. Objective reports include each experiment's authoritative aggregation statistics and excluded attempts, and are content-addressed under `Settings.state/work/OBJECTIVE_ID/reports/`.

`KGR_SEED` is injected into both script and notebook runtime environments. It corresponds to the top-level ExperimentSpec seed and the result validator checks that value. Aggregation compares invariant source, protocol, dataset/model references, metric definition and parameters while allowing distinct attempt seeds. Offline fake-provider tests cover planning and bounded execution; real Kaggle quota, remote behavior and artifact fetching remain unverified here.

The objective shows observed GPU-hours only when validated result metadata includes runtime. This is a runtime-derived estimate (T4x2 multiplies runtime by two devices), not Kaggle's quota ledger; unknown usage remains `null`. With `--wait`, before each later batch the executor compares that estimate plus remaining planned estimates to the approved total and stops if it would exceed the budget.

## Credentials and preflight

Use private project-root `.env` for team credentials unless `--env-file` or `KGR_ENV_FILE` selects a protected file. `kgr team diagnose` checks local syntax and key presence only; `kgr team check` calls Kaggle for each profile. Never display tokens or copy them into specs, notebooks, logs or reports. Do not rely on default account selection to widen a spec's `authorization.allowed_profiles`.

Preflight is a point-in-time check, not a Kaggle reservation. Kaggle controls actual capacity. CPU and GPU jobs share the local slot count per account alias. Agent and legacy admissions serialize within a shared `jobs.sqlite3`; separate machines do not coordinate.

## Recovery after interruption

For an agent attempt, first inspect persisted records:

```bash
kgr experiment show EXPERIMENT_ID --json
kgr reconcile --experiment-id EXPERIMENT_ID --json
```

`submission.json` records `not_pushed` before staging, `submission_unknown` at the durable push boundary, or `remote_confirmed` after a successful response. A crash after the unknown marker requires reconciliation. A local job ID alone is not evidence that Kaggle created a kernel. `manual_review` or `ok: false` requires operator action under the original account; do not push again, switch accounts or release the lease based on timeout/expiry.

When remote visibility is confirmed, continue the same experiment:

```bash
kgr experiment resume EXPERIMENT_ID --wait --max-wait-minutes 45 --json
```

Resume may fetch outputs and validate results. For status lookup errors, inaccessible kernels, changed ownership or unknown status, stop for operator review. Do not interpret Kaggle `COMPLETE` as scientific success.

## Result and artifact failures

Check the result and artifacts:

```bash
kgr results validate JOB_ID --json
kgr results aggregate --experiment-id EXPERIMENT_ID --json
```

Missing, malformed, empty, inconsistent or hash-mismatched data invalidates scientific success. Preserve downloaded evidence and resolve source, protocol, expected output or kernel problems before planning a new distinct attempt. Never rewrite a failed attempt's history.

## Legacy `kgr run`

Legacy jobs participate in atomic local slot admission and shared history, but they do not use immutable experiment attempts or the agent reconciliation contract. An ambiguous submit failure can leave a remote kernel. Use `kgr jobs`, `kgr show`, `kgr status` and the original profile to investigate; never assume retry is safe.

## Operator incident notes

Record timestamp, job and experiment/attempt IDs, profile alias, command without credentials, exit code, redacted error, local state, observed remote state, artifact hashes and the human decision. Keep assumptions separate from observed facts. Live tests and destructive recovery actions require separate explicit authorization.
