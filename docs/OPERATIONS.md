# Operations runbook — troubleshooting and recovery

This runbook is based on current `kgr` behavior at `cff2a661a3e6357febd9c574158fd58d020781ac`. It distinguishes local state from the authoritative Kaggle service.

## Routine preflight

```bash
kgr team diagnose
kgr team list
kgr team check --gpu t4x2 --required-gpu-hours 2 --json
```

`team diagnose` validates selected `.env` structure **not** live credentials. `team check` calls Kaggle API independently for each profile and checks local tracked jobs. It can fail if a token is rejected, a profile lacks enough GPU hours, or there are two local active CPU/GPU jobs.

## `INCOMPLETE` team profile

- Verify `KGR_TEAM_PROFILES` includes the intended exact alias, separated by commas.
- Expected keys are `KGR_TEAM_<NORMALIZED_ALIAS>_USERNAME` and `_API_TOKEN`. For alias `hggggv`, prefix is `KGR_TEAM_HGGGGV_`; for `toan_ltv`, prefix is `KGR_TEAM_TOAN_LTV_`.
- Use `kgr team diagnose`; inspect *key names only* if necessary, never print token values.
- Check `KGR_ENV_FILE` override and selected `.env` path. If the file is correct but hand-edited lines aren't recognized, run `kgr team add ALIAS --username USER --replace` to rewrite them canonically; remove stale unused key pairs manually after backup.
- `team add` does not auto-delete unrelated `.env` variables.

## `.env` permission error

`kgr` rejects symlinked/overly permissive credential files. On POSIX use `chmod 600 .env`, but confirm filesystem semantics (WSL DrvFs may require `metadata`). Do not turn off protection to silence warnings.

## Running jobs are miscounted

```bash
kgr jobs --json
kgr team check --gpu cpu --json
kgr status JOB_ID --json
```

- One GPU and one CPU job on **the same profile** count as 2/2 by local policy; the third attempt is blocked.
- Completed/failed/cancelled jobs free tracked slots after refresh.
- `CANCEL_ACKNOWLEDGED` on an old notebook is ambiguous and does not prove a live allocation. If Kaggle Active Events shows zero sessions but the notebook history returns old cancellation statuses, separate historical state from real active sessions.
- Interactive jobs, other machines and direct website submissions may be invisible to fast tracked preflight.
- For the default/legacy account only, `kgr active --remote --strict` offers a slow, best-effort history scan; it still cannot guarantee Kaggle slot availability.

## Stale notebook reference / access denied

If an old Job ID points to `old_owner/slug` and the actual URL is `new_owner/slug`, verify the actual Kaggle URL and authenticated account. `kgr repair JOB_ID --owner new_owner` changes the local reference but **does not validate remote access**. `kgr status JOB_ID` checks the new ref. Avoid modifying slug to a different notebook. For inaccessible history which is independently confirmed inactive, `kgr archive JOB_ID --confirm-inactive` excludes it from local slot counts. Never archive simply because status lookup failed while a remote run may still exist.

## Ambiguous submit failure / timeout

A `kgr run` failure does not always prove nothing was created remotely. Before any retry:

```bash
kgr jobs --json
kgr show JOB_ID --json
kgr status JOB_ID --json
```

If the Job ID is unknown or status unavailable, check Kaggle kernels and canonical URLs under the authorized profile. Record reconciliation outcome in your run notes. **There is no automatic idempotent retry today.** Do not create another run until duplication risk is addressed.

## Remote kernel completed, artifacts missing

```bash
kgr status JOB_ID --json
kgr logs JOB_ID --json
kgr outputs JOB_ID --json
kgr fetch JOB_ID --output ./results/JOB_ID --json
```

Inspect Kaggle logs, remote output list and input mounts. `kgr` can download artifacts, but the experiment must write expected files (normally into `/kaggle/working`). A Kaggle `COMPLETE` status is not enough for a scientific PASS: validate your own output schema, job identity, dataset split and metrics.

## Two-account smoke test fails

```bash
python examples/team-env-multi-account/run_live_test.py \
  --accounts alice bob --gpu cpu --confirm-live
```

This is opt-in and creates two real kernels. If one submission completes and the other fails, use saved Job IDs and `kgr status`/`fetch` for recovery; do not automatically re-submit both. Check each account's individual token/username, quota and Kaggle notebook permissions.

## Recommended incident record

Write a safe incident note: timestamp, Job ID, profile alias/owner, command (without token), exit code, redacted error, local state, remote observed state, affected artifacts, human decision and next check. Distinguish observed facts from assumptions. No token or `.env` contents should be included.
