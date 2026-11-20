# Kaggle Runner (`kgr`)

Kaggle Runner packages Python scripts and notebooks, submits them through Kaggle's official CLI, tracks local jobs, and validates versioned scientific results. The agent interface supports immutable experiment specs, explicit account authorization, SQLite-backed idempotent attempts, reconciliation, result aggregation and evidence reports.

The CLI is a foreground tool. It does not coordinate separate machines or guarantee Kaggle capacity. Offline tests do not verify live Kaggle behavior. Start with [AGENTS.md](AGENTS.md) for safety and task navigation, or [docs/INDEX.md](docs/INDEX.md) for the full guide.

## Install

```bash
git clone https://github.com/HoangVo-Prog/kaggle-runner.git
cd kaggle-runner
python -m pip install -e '.[kaggle]'
kgr --help
```

Local inspect, validate and offline tests do not require Kaggle credentials. Remote commands require an authorized Kaggle account. Configure credentials in a private project-root `.env` using `kgr team add ALIAS --username USER`, or select a protected credential file with `--env-file` / `KGR_ENV_FILE`. Never commit or expose tokens.

## Validate before any submission

```bash
kgr team diagnose
kgr package inspect ./examples/agent_python --entry train.py --json
kgr experiment validate ./examples/agent_python/experiment.json --json
```

These commands do not submit a kernel. Inspect the complete spec and package report, especially the research objective, sharing policy, compute budget, `authorization.allowed_profiles`, source files and hashes, input refs and expected outputs. Follow [docs/AGENT_WORKFLOW.md](docs/AGENT_WORKFLOW.md) for the complete lifecycle.

## Legacy CLI

`kgr run` remains supported for existing callers and shares local atomic capacity admission with agent submissions when they use the same `jobs.sqlite3`. Autonomous submissions should use `kgr experiment submit`, which also provides idempotency and durable reconciliation. Never retry an ambiguous legacy push.

## Offline verification

```bash
python -m compileall -q kaggle_runner
python -m unittest discover -s tests -v
```

Live Kaggle tests are separate, opt-in, and not run by default. See [docs/OPERATIONS.md](docs/OPERATIONS.md) for recovery boundaries and [docs/CONTRACTS.md](docs/CONTRACTS.md) for data formats.
