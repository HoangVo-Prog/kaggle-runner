# Security, credentials and agent authorization

**Baseline:** reviewed source of `cff2a661a3e6357febd9c574158fd58d020781ac`. Running agents with multiple member credentials increases operational risk: protecting secrets and ensuring user consent is part of correctness.

## Assets and trust boundaries

| Asset | Sensitivity | Recommended handling |
| --- | --- | --- |
| `KAGGLE_API_TOKEN`, `KGR_TEAM_<ALIAS>_API_TOKEN` | Secret bearer credentials | Never commit, print, upload, put in model prompts, or save in DB/reports. |
| `.env` (repo root by default) | Private secrets and metadata | Local filesystem only, 0600 on POSIX, protected backups; encrypted secret store for long-term production use. |
| SQLite `jobs.sqlite3` | Operational history, source paths/account metadata | Access control, backup, avoid broad multi-user write access. |
| `manifest.json`, `kgr_download_manifest.json` | Source/evaluation provenance | Retain for audits; may include paths/other metadata, but should not contain tokens. |
| Kaggle source kernels + output files | May contain code, private/derived data | Private by default; review contents and permission before sharing/export. |
| Agent prompts/reports/logs | Operational artifacts | Redact tokens and private data, preserve exact relevant failure evidence. |

## Credential selection (implemented)

- Team profiles come from **one selected `.env`**, not `config.toml` or old `team/*.env` files.
- `KGR_TEAM_PROFILES` is the roster. Required: `USERNAME` and `API_TOKEN`; optional `EMAIL`.
- `--env-file PATH` > `KGR_ENV_FILE` > project-root `.env`.
- `kgr run --account ALIAS` uses that profile token. `kgr status/logs/outputs/fetch` look up the stored profile metadata and rebind to the same profile credential.
- A deleted/missing profile must fail explicitly; never silently substitute the default account.
- Legacy `--profile` (Kaggle config directory) and `--account` (team profile alias) are different mechanisms.
- `KAGGLE_OWNER` / `EMAIL` are metadata. A Kaggle account's authenticated identity is governed by its token, not by a username string typed into `.env`.

## Access control and consent

A team may have distinct members, each owning their own Kaggle account. Agents should receive explicit authorization for **which member's account** may execute **which project/workload** and for how long. Having another person's token in a file is not by itself adequate authorization for an arbitrary agent action.

Kaggle's [Terms of Use](https://www.kaggle.com/terms) prohibit sharing accounts/passwords and operating multiple accounts as one individual to bypass limits. The team's security model should respect the platform's terms and each member's permissions. For team deployments, prefer members running a local worker with their own credential and submitting an approved queue item, rather than pooling everybody's tokens in one central shell. Evaluate Kaggle's supported organization/access features before scaling.

## What is and is not currently protected

**Implemented:** sensitive `.env` is Git-ignored, POSIX permission checks, symlink rejection, atomic updates, subprocess token redaction, no team tokens in job manifests/SQLite, profile-specific fetch routing.

**Still required for stronger agent operation:** workload-level account allowlists, separate read-only/submit/admin roles, explicit approvals, audit logs, comprehensive secret-scanning for uploaded files and dependency output, stable error redaction, key rotation, secure cross-machine coordinator, output sanitization.

## Windows / WSL note

When the project is on `/mnt/d/...`, standard `chmod 600 .env` may not work without DrvFs `metadata`. Avoid resolving this by disabling permissions checks. Prefer keeping secret material in a protected local user filesystem, making sure your intended `KGR_ENV_FILE` points there, or enable appropriate mount permissions. Be cautious with synced/cloud-backed folders and shared workstations.

## Agent guardrails

Before submit, require: authorized profile, expected source revision, expected dataset/model references, allowed network/privacy settings, compute budget and maximum attempts. Before dangerous actions, require approval to cancel, archive, repair, delete, publish, overwrite private data, or re-run an ambiguous submission. All failure messages and exception paths must redact credentials. Use non-privileged service identities for automation.

## If a token leaks

Stop automated runs using the exposed profile, revoke/rotate the token in Kaggle account settings, update the private `.env`, inspect logs/artifacts/CI outputs for copies, and restore profile access only after verification. Do not paste live tokens into issue trackers or agent conversations.
