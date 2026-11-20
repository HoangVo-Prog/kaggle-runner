# CLI reference

This is the concise agent command surface. Run `kgr COMMAND --help` for the parser's exact flags on the installed version.

| Command | Purpose | Machine result |
| --- | --- | --- |
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
| `kgr run ...` | Legacy direct run interface | Compatibility output; see its help |

Python and notebook inspection accept a project root or entrypoint. Notebook examples use `kgr package inspect ./experiment.ipynb --json`. Both formats use the standardized result path from the spec and runtime environment. See [CONTRACTS.md](CONTRACTS.md).

## JSON and exits

Successful JSON operations return an object containing `ok: true` (some read-only legacy commands retain historical shapes). `RunnerError` under `--json` returns a redacted envelope with `ok: false` and exits 2. Result validation, aggregation and resume return exit 3 when the emitted outcome has `ok: false`; reconciliation also exits 3 for `manual_review`. Invalid argparse input with `--json` emits `INVALID_ARGUMENTS`. Always inspect both process exit status and `ok`.

JSON error text is intentionally redacted and may omit diagnostic details. Resolve with the command's guided local diagnostics; never turn a generic failure into success or retry a possibly submitted push.
