# Quickstart

## Install

Requires Python 3.11+. Install this checkout and the Kaggle CLI extra:

```bash
python -m pip install -e '.[kaggle]'
kgr --help
```

For local inspection, validation, packaging and tests, Kaggle credentials are not needed. Remote commands require a configured Kaggle account. Team profiles live in the private project-root `.env` by default; `--env-file` or `KGR_ENV_FILE` selects another file. Use `kgr team add ALIAS --username USER` to enter credentials without echoing tokens, then `kgr team diagnose` to check configuration without calling Kaggle.

## Run the offline example

```bash
kgr package inspect ./examples/agent_python --entry train.py --json
kgr experiment validate ./examples/agent_python/experiment.json --json
```

These commands inspect and validate only. They do not submit jobs. The example spec uses an explicit profile allowlist; change it only to a profile you are authorized to use. Follow [AGENT_WORKFLOW.md](AGENT_WORKFLOW.md) for approval, actual submission and recovery.

## Tests

```bash
python -m compileall -q kaggle_runner
python -m unittest discover -s tests -v
```

All tests are intended to be offline. Live Kaggle verification is separate and opt-in; it is not part of this quickstart.
