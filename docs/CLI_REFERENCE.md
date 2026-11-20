# CLI reference

This is the concise agent command surface. Run `kgr COMMAND --help` for the parser's exact flags on the installed version.

| Command | Purpose | Machine result |
| --- | --- | --- |
| `kgr setup status --json` | Read local setup checkpoint; no network or database writes | Check statuses, stale findings, readiness and last tested revision |
| `kgr setup verify --offline --json` | Recheck only missing/stale local components; run tests only for a new source fingerprint | Persisted check history and reused/revalidated lists |
| `kgr setup verify --online --account ALIAS --json` | Add one read-only authentication check for an explicit configured account | Local checks plus account-specific auth status and last-success timestamp |
| `kgr team diagnose [--env-file FILE]` | Check local profile file structure and key presence; no Kaggle API call | JSON object when `--json` is supplied |
| `kgr team list --json` | List profile aliases and readiness without tokens | JSON |
| `kgr team check [--gpu cpu\|t4x2] --json` | Remote profile preflight and local activity check | Per-profile outcomes |
| `kgr package inspect PATH [--root ROOT] [--entry FILE] --json` | Inspect source members, exclusions, hashes and resource refs | `ok` inspection object or error envelope |
| `kgr experiment validate SPEC --json` | Validate schema, authorization, source and package | `ok`, fingerprint, package and resource data |
| `kgr experiment submit SPEC [--account ALIAS] [--approve] --json` | Idempotently submit one attempt | Job, experiment, attempt, account, kernel ref and state |
| `kgr experiment show EXPERIMENT_ID --json` | Read attempts and transition events | Attempt list |
| `kgr experiment resume EXPERIMENT_ID [--wait] --json` | Reconcile, monitor, fetch and validate existing attempts | Aggregate completion status |
| `kgr reconcile [--experiment-id ID] --json` | Read-only remote reconciliation | Per-attempt outcome, action and submission evidence |
| `kgr results validate JOB_ID --json` | Validate fetched versioned result and artifacts | Scientific validity and errors |
| `kgr results aggregate --experiment-id ID --json` | Aggregate compatible valid attempts | Counts, statistics and exclusions |
| `kgr report experiment EXPERIMENT_ID --format md` | Generate report and evidence bundle | Markdown in text mode; JSON when `--json` is supplied |
| `kgr work create --title TITLE --acceptance-criterion TEXT` | Create durable objective draft | Objective ID and revision |
| `kgr work list [--active] --json` | List persisted objectives | Objective IDs, states and next actions |
| `kgr work show OBJECTIVE_ID --json` | Compact session handoff | Checklist, attempt links, blockers, progress and timeline |
| `kgr work plan OBJECTIVE_ID --file PLAN --expected-revision N --json` | Validate and persist immutable per-seed specs | Stable attempt IDs, plan budget and checklist |
| `kgr work approve OBJECTIVE_ID --expected-revision N --json` | Record explicit plan approval | New objective revision |
| `kgr work pause OBJECTIVE_ID --expected-revision N --reason TEXT --json` | Pause an objective with an auditable reason | Paused state and next action |
| `kgr work execute OBJECTIVE_ID [--account ALIAS\|auto] [--wait] --json` | Submit bounded batches via fresh preflight and leases | Attempt outcomes and handoff actions |
| `kgr work resume OBJECTIVE_ID --json` | Continue from persisted state | Required reconciliation and safe next action; never resubmits |
| `kgr work checkpoint OBJECTIVE_ID --step ID --status STATUS --expected-revision N` | Record checklist evidence | Updated step and revision |
| `kgr work complete OBJECTIVE_ID --expected-revision N --acceptance-review-json JSON` | Complete after explicit criterion review | Completed status and evidence |
| `kgr work report OBJECTIVE_ID --format md` | Generate content-addressed objective report | Markdown plus saved evidence path/hash |
| `kgr run ...` | Legacy direct run interface | Compatibility output; see its help |

Python and notebook inspection accept a project root or entrypoint. Notebook examples use `kgr package inspect ./experiment.ipynb --json`. Both formats use the standardized result path from the spec and runtime environment. See [CONTRACTS.md](CONTRACTS.md).

The objective plan file and bounded batch behavior are documented in [Operations](OPERATIONS.md#persistent-research-objectives). Objective rows share `jobs.sqlite3`; experiment and result states are joined from the existing agent ledger and must not be inferred from checklist completion.

## JSON and exits

Successful JSON operations return an object containing `ok: true` (some read-only legacy commands retain historical shapes). `RunnerError` under `--json` returns a redacted envelope with `ok: false` and exits 2. Result validation, aggregation and resume return exit 3 when the emitted outcome has `ok: false`; reconciliation also exits 3 for `manual_review`. Invalid argparse input with `--json` emits `INVALID_ARGUMENTS`. Always inspect both process exit status and `ok`.

JSON error text is intentionally redacted and may omit diagnostic details. Resolve with the command's guided local diagnostics; never turn a generic failure into success or retry a possibly submitted push.
