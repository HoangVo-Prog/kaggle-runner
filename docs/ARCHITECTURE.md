# Architecture and repository map

**Status: audited current implementation**, pinned to `cff2a661a3e6357febd9c574158fd58d020781ac`.

## Runtime model

```text
Human / coding agent
      |
      | kgr CLI (compact output; --json for machines)
      v
kaggle_runner/cli.py
      |                       +----------------------------+
      +-- team.py ------------| root .env: team credentials |
      |                       +----------------------------+
      +-- accounts.py --------| single-account credentials |
      +-- config.py ----------| paths and optional TOML     |
      +-- preflight.py -------| tracked jobs + GPU quota     |
      +-- runner.py ----------| stage, submit, poll, fetch   |
      |      +-- packaging.py | source, refs, source ZIP     |
      |      +-- notebooks.py | native notebook staging     |
      +-- jobs.py ------------| local SQLite job ledger     |
      +-- presentation.py ----| human / JSON formatting     |
      +-- status.py ----------| Kaggle state normalization  |
      +-- api.py -------------| subprocess python -m kaggle  |
                                      |
                                      v
                                Kaggle API / kernels
```

The CLI is invoked per request. **There is no resident scheduler, queue worker, cross-machine coordinator or background monitor.** `kgr sync` and `report --refresh` query on demand.

## Component ownership

| Module | Responsibilities / boundaries |
| --- | --- |
| `accounts.py` | `.env` selection, single-account token loading, private file checks, atomic updates. Priority: explicit `--env-file`, `KGR_ENV_FILE`, then project-root `.env`. |
| `team.py` | Team alias normalization, roster, two required fields (`USERNAME`, `API_TOKEN`), optional email, diagnostics, migrations; no token in reports. |
| `config.py` | Optional legacy TOML and state paths. Team credentials **do not** belong in TOML. |
| `api.py` | Isolated subprocess invocation of `python -m kaggle`; token passed via environment, stdout/stderr redaction, timeout handling. |
| `preflight.py` | Per-profile GPU quota, tracked CPU+GPU candidate statuses, optional remote history scan. Local 2-job policy; no authoritative Kaggle slot reservation. |
| `runner.py` | Generate job IDs/staging, pack source, write manifest, submit kernel, canonicalize ref from Kaggle URL, refresh status, fetch files and manifest. |
| `packaging.py` | Validate remote refs, resolve source, zip safe Python repo, create script bootstrap. |
| `notebooks.py` | Stage native `.ipynb` + input mount setup; does not automatically bundle companion modules. |
| `jobs.py` | SQLite history; stores profile alias/owner/email with job; supports ref repair and abbreviation lookup. No token values. |
| `status.py` | Parse/normalize Kaggle worker states. |
| `presentation.py` | Human summaries and tables; structured `--json` owned by CLI. |

## Persisted data

**Credentials** (per machine): `<repo-root>/.env` unless explicitly overridden; contains tokens. Must never be in Git or remote source packages. Beware shared filesystem permissions and backups.

**Job state**: `KGR_STATE_DIR` or `--state-dir` overrides; otherwise platform default is typically `~/.local/state/kaggle-runner` on Linux. Job ledger is `jobs.sqlite3`, staging data lives under `jobs/<job_id>/`, and fetched output defaults under `outputs/<job_id>/`. `--state-dir` is a state namespace, **not** a team-profile selector. Mixing unrelated accounts into one ledger is supported through per-job `account_profile` metadata, but synchronization across machines is not.

**Job manifest** (`jobs/<job_id>/manifest.json`): records source/revision, entry, input refs, GPU request, flags, local hashes and account metadata. Source packaging embeds no `.env` tokens. The Kaggle submit response may correct an initially constructed `owner/slug` reference; always use the saved canonical `kernel_ref` for follow-up.

**Fetch manifest** (`outputs/<job_id>/kgr_download_manifest.json`): records downloaded file names, byte sizes, SHA256 hashes and account metadata.

## Submission lifecycle (implemented)

```text
start kgr run
  -> parse arguments / resolve account
  -> tracked preflight (+ GPU quota if requested)
  -> resolve local path or GitHub source
  -> stage source + kernel metadata + manifest
  -> insert SQLite row: prepared
  -> kaggle kernels push
     -> on error: submit_failed
     -> on successful response: submitted (+ canonical kernel_ref)
  -> optional --wait polls status until terminal state / deadline
  -> optional --fetch downloads outputs
```

A `--dry-run` creates local staging and a `prepared` record but never calls Kaggle; it is not a remotely submitted job. `kgr run --wait` is foreground waiting, not a reliable background worker. Python `.py` and native `.ipynb` modes have different restrictions; see CLI reference.

## Concurrency/slot semantics

- Two-job cap is a **local configurable safety policy**, shared by CPU/GPU within an account profile; it is not a guaranteed Kaggle quota API.
- `kgr team check` checks each profile separately. `run --account auto` picks a ready profile with more slots and then GPU headroom; Kaggle may still reject placement.
- Team preflight (`kgr team check`, `run --account ALIAS`) scopes tracked jobs to their `account_profile`. **KNOWN BUG:** default-account `kgr active` / `kgr check` passes `account_profile=None`; current `tracked_active_report` then selects *all* locally tracked profiles and may query other owners' jobs using the default token. Avoid these default commands on mixed-account ledgers until corrected. `--remote` performs a slower best-effort history scan; neither mode sees all interactive/out-of-band sessions.
- `CANCEL_ACKNOWLEDGED` is reported as ambiguous/cancelling, not a proven occupied slot. `NEW_SCRIPT` immediately after push may be treated as pending for a short propagation window.
- Simultaneous autonomous agents can both pass preflight before either writes a new job: **atomic reservations are NOT IMPLEMENTED**.

## Failure boundaries

- Local `job_id` is not a remote execution guarantee. Partial push failures may leave a remote kernel; automatic retry is unsafe.
- SQLite lives on one machine; another agent/machine can have a different ledger and conflicting slot view.
- `report` summarizes statuses; it does **not** parse domain metrics such as accuracy/loss/F1 from result artifacts.
- No standardized experiment-spec version, dataset-content checksum enforcement, retry budget, lease/lock, event log, durable queue, notification, or result schema validation exists today.

The [roadmap](ROADMAP_AGENT_FIRST.md) describes how to close these gaps without discarding the existing CLI.


## Candidate agent-first modules

`source_contract.py` selects and stages source; `agent_ledger.py` manages atomic SQLite attempts, leases and events; `agent_workflow.py` handles versioned specs, safe submission, read-only reconciliation, validation, aggregation and evidence reports. All are composed into the existing CLI; credentials remain account-scoped.
