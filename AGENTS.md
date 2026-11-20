# Kaggle Runner agent guide

`kgr` is a local CLI for packaging and running Kaggle kernels. The agent workflow is implemented as a CLI lifecycle, not as a resident scheduler or cross-machine coordinator. Read only the task guide needed:

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
