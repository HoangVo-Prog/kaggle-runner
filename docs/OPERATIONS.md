# Operations and recovery

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
