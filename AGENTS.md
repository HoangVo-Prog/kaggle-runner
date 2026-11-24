# Kaggle Runner agent guide

`kgr` is a local CLI for packaging and running Kaggle kernels. The agent workflow is implemented as a CLI lifecycle, not as a resident scheduler or cross-machine coordinator. Read only the task guide needed:

## Resolve the executable before any command (mandatory)

Codex may run each command in a fresh shell with a different `PATH`. Never assume that `kgr` is available or that a prior `conda activate` persists. Resolve one working Kaggle Runner invocation at session startup and reuse it across **all** setup, work, experiment, and report commands.

1. Try `command -v kgr` and then `kgr --help`. Do not use it if it points to an unrelated or stale installation.
2. Otherwise, try `python -m kaggle_runner --help` (or `python3 -m kaggle_runner --help`). A module invocation does not require the `kgr` console script to be on `PATH`.
3. If there is an existing project virtual environment, try its **explicit** interpreter: `./.venv/bin/python -m kaggle_runner --help` (Linux/WSL) or `.venv\Scripts\python.exe -m kaggle_runner --help` (Windows).
4. If Conda exists, inspect `conda env list` and choose an existing, confirmed project environment. Use `conda run -n ENV_NAME --no-capture-output python -m kaggle_runner --help`. For later commands, reuse that entire `conda run ... python -m kaggle_runner` prefix, or use the selected environment's absolute Python executable. Do not guess the environment name.
5. Check the selected interpreter's identity and imported package path with `python -c 'import sys, kaggle_runner; print(sys.executable); print(kaggle_runner.__file__)'` (replace `python` with the **selected invocation's Python**, including `conda run` when used). Verify that it points to the intended environment and checkout.
6. If nothing works, stop and report the attempted **non-sensitive** executable locations. Do not silently install packages, create a Conda environment, modify credentials, reset SQLite, or use a different machine.

A WSL Conda environment is not automatically accessible to native Windows processes or a remote Codex container. Keep the same `KGR_STATE_DIR`/`--state-dir` and `KGR_ENV_FILE`/`--env-file` throughout an objective. Treat `kgr: command not found` as an environment-resolution issue, **not** as permission to repeat setup.

## Agent session startup

After reading this guide, immediately record its hash, checkout revision, branch and worktree state. For an instrumented task, initialize its audit first. Resolve one interpreter and discover existing state before asking for an exact root. Use `kgr setup discover --objective-id OBJECTIVE_ID --json` (read-only), or add `--remember` to persist a verified secret-free local `.kgr-bootstrap.json` pointer. If a known objective uniquely matches and discovery reports `ok: true`, use `selected.path` as the explicit absolute state path. Check cached interpreter/module hints rather than trusting them blindly. Discovery does not authorize remote access. If multiple matches, inaccessible candidates, pointer/ledger drift or no match, report the concrete candidate evidence and ask only for the unresolved decision. Do not scan other user profiles, bypass ACLs, create a fallback ledger or recreate a known objective. Explicit CLI `--state-dir` still takes precedence for ordinary Runner commands; the bootstrap pointer is a discovery hint, not a hidden override of settings. Keep private credentials/state separate from shared redacted audit artifacts.

After resolving a valid invocation and verified state, run the following commands with the same absolute state path before the subcommand (replace `kgr` with the selected module invocation):

```bash
kgr --state-dir "$STATE" setup preflight --expected-state-dir "$STATE" --json
kgr --state-dir "$STATE" setup status --json
kgr --state-dir "$STATE" work list --active --json
```

The offline preflight probes temporary filesystem and SQLite operations without creating a job ledger. It cannot prove access by another identity. Provide `--audit-dir` for a separately provisioned shared artifact directory. Stop stateful operations on path drift or inaccessible storage; never widen permissions. `package inspect` and `experiment validate` are state-independent; continue safe local source hashing, adaptation and draft preparation even when state is unavailable. `package inspect` verifies structural packaging only; static indicators do not prove runtime readiness, dependencies, scientific validity, resource access or authorization. For a blocked unplanned draft, persist redacted facts and blockers with `work handoff OBJECTIVE_ID --file HANDOFF.json --expected-revision N --json` when the existing state is accessible; otherwise record a local handoff, not a fictitious persisted checkpoint. Never turn an incomplete draft into a submit-ready plan.

Reuse a current setup checkpoint; do not reinstall packages or recreate credentials. Revalidate only stale setup checks. For a selected objective, run `kgr work show OBJECTIVE_ID --json`, reconcile every linked experiment that may have crossed the remote push boundary, then continue from its persisted `next_action`. Apply the resolved invocation to all such commands. Do not create a duplicate objective or attempt after a restart, repeat an ambiguous push, or treat checklist evidence as proof of remote state.

- First setup and offline example: [docs/QUICKSTART.md](docs/QUICKSTART.md)
- Full submit, recovery, result and report lifecycle: [docs/AGENT_WORKFLOW.md](docs/AGENT_WORKFLOW.md)
- Command flags and JSON behavior: [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md)
- Schemas and packaging/runtime contracts: [docs/CONTRACTS.md](docs/CONTRACTS.md)
- Credential, quota and incident handling: [docs/OPERATIONS.md](docs/OPERATIONS.md)
- Module ownership and persisted state: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- All documentation: [docs/INDEX.md](docs/INDEX.md)

## Safety boundaries

1. Read the ExperimentSpec. Confirm objective, data-sharing policy, compute budget, explicit `authorization.allowed_profiles`, dataset/model refs and expected outputs. `--account auto` may choose only among that allowlist.
2. Never expose credentials. Keep tokens in the private project-root `.env` or Kaggle's existing credential configuration. Do not put tokens in specs, notebooks, logs, reports, or remote kernels.
3. Before a push, inspect the source with `kgr package inspect` and `kgr experiment validate`. Check names, hashes, entrypoint, companion files, runtime parameters, refs and outputs.
4. For agent-managed work, use `kgr experiment submit`, then retain its `job_id`, `experiment_id`, `attempt_id`, account alias and kernel ref. Check both process exit status and JSON `ok`.
5. Never repeat a push after timeout or other ambiguous outcome. Use `kgr reconcile --experiment-id ID --json`; `manual_review` and `ok: false` require operator intervention. Never infer remote existence from a local job ID.
6. Resume with `kgr experiment resume ID --json` or bounded `--wait`. Fetch under the original account and validate artifact hashes and versioned `result.json` before aggregation/reporting.
7. No automatic account rotation, remote cancellation/archive, unknown lease release, private data upload, or retry of failed experiment code. Stop for credential, quota, authorization, hash, access, schema, artifact or budget uncertainty.

## Common commands

```bash
kgr team diagnose
kgr team list --json
kgr package inspect ./experiment --entry train.py --json
kgr experiment validate ./experiment/experiment.json --json
kgr experiment submit ./experiment/experiment.json --account alice --approve --json
kgr experiment show EXPERIMENT_ID --json
kgr reconcile --experiment-id EXPERIMENT_ID --json
kgr experiment resume EXPERIMENT_ID --wait --max-wait-minutes 45 --json
kgr results validate JOB_ID --json
kgr results aggregate --experiment-id EXPERIMENT_ID --json
kgr report experiment EXPERIMENT_ID --format md
```

`kgr run` remains for legacy callers. Agent and legacy jobs use the same atomic local admission count when they share `jobs.sqlite3`. Legacy runs still lack agent idempotency and recovery contracts; never retry an ambiguous legacy push. No real Kaggle jobs are part of offline verification.
