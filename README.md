# Kaggle Runner (`kgr`)

> **Agent-first documentation (2026-11-20, audited commit `cff2a66`):**
> Start with [docs/INDEX.md](docs/INDEX.md) for the complete guide and
> [AGENTS.md](AGENTS.md) for agent safety/operation instructions.
> See [docs/ROADMAP_AGENT_FIRST.md](docs/ROADMAP_AGENT_FIRST.md) for the features
> still required before unattended agent execution is reliable.
>
> **Important:** Several historical sections below predate team profiles and
> the root `.env` / fast tracked-job preflight. Follow the linked updated docs
> whenever this README conflicts with the current implementation.

A small, portable CLI to run **Python scripts and Jupyter notebooks on Kaggle**, attach Kaggle Datasets / Models, check resource eligibility before submission, track jobs, and download results. Uses Kaggle's **official CLI/API**; no Kaggle GPU server or local daemon required.

## Install & configure (once per machine)

```bash
git clone -b cleanup/runner-only https://github.com/HoangVo-Prog/kaggle-runner.git
cd kaggle-runner
python -m pip install -e '.[kaggle]'
kgr auth init
kgr auth show
kgr doctor --online
```

`kgr auth init` asks for `KAGGLE_OWNER` (the username in your Kaggle profile URL), `EMAIL` (report-only), and a hidden `KAGGLE_API_TOKEN`. Obtain the token at [Kaggle API Settings](https://www.kaggle.com/settings/api). By default it writes `~/.config/kaggle-runner/.env` (Linux/WSL) or `%APPDATA%/kaggle-runner/.env` (Windows). It is **not uploaded** to Kaggle. The local file is restricted to its owner on Unix.

To put the file in your own checkout instead (never commit it):

```bash
kgr --env-file .env auth init
kgr --env-file .env auth show
```

The `.env` content is simply `KAGGLE_OWNER=...`, `EMAIL=...`, `KAGGLE_API_TOKEN=...`; see [`.env.example`](.env.example) for placeholders. It is ignored by Git. `KGR_ENV_FILE=/path/to/.env` or `--env-file` selects a credential file for your own account. The environment variables themselves can be used instead (e.g. CI secret injection). **Never pass the token on the command line or include it in notebook code.**

## Compact CLI output and slot-limit regression tests

Commands show short summaries by default; append `--json` whenever a script
needs the full machine-readable payload:

```bash
kgr quota
kgr active
kgr check --gpu t4x2 --required-gpu-hours 2
kgr jobs
kgr report --refresh
kgr check --gpu t4x2 --required-gpu-hours 2 --json
```

**CPU and GPU share the same two-job local safety cap.** Keep a separate
`--state-dir` per account if using multiple authorized environments. The
local safety check counts all unfinished jobs in that ledger, not just jobs
matching the optional `KAGGLE_OWNER` report label.
 To verify the exact
"GPU job + CPU job, then block the third job" sequence **without using GPU
quota**:

```bash
python -m unittest discover -s tests -p 'test_slot_scenarios.py' -v
```

See [real-Kaggle slot-test walkthrough](examples/slot-limit-test/README.md)
for the optional live version (it consumes Kaggle quota) and
[CLI output details](docs/CLI_OUTPUT.md) for the display/JSON contract.

## 1. Check before running

```bash
kgr quota                         # Remaining GPU hours
kgr active                        # Running/queued Kaggle batch kernels; progress on stderr
kgr check --gpu t4x2 --required-gpu-hours 1.5
```

`kgr quota` is a direct **account-level** API request: it does not list notebooks.
`kgr active` checks visible batch-kernel sessions and may list/probe notebooks because
this is a separate concurrency check. `kgr check` combines both results. If the
account reaches the configured concurrency threshold, `check` reports that
blocker even when quota cannot be parsed; unverified quota is never treated as
available.

`kgr run` performs the same preflight automatically. The default local safety ceiling is **2 active batch kernels** and the minimum expected GPU budget is **0.25 hours** (adjust using `--required-gpu-hours`). A job is **blocked** if the running count reaches the ceiling, if GPU quota is insufficient, or if the API response cannot be verified. CPU jobs check active batch kernels, but not GPU hours.

**Important:** Kaggle can enforce different resource limits, reserve GPU time, and run interactive sessions that the kernels listing does not enumerate. These checks are conservative estimates, **not** a guarantee of availability or a claim that Kaggle globally allows exactly two sessions. A GPU allocation still depends on Kaggle.

`kgr active` prints scan progress immediately to stderr, probes kernel statuses in
bounded parallel batches, stops early when the concurrent limit is reached, and
applies a default **45-second scan budget**. This avoids waiting silently while
hundreds of historical notebooks are examined. JSON output remains on stdout:

```bash
kgr active                             # Progress + JSON result
kgr active --quiet                     # JSON only (for scripts)
kgr active --workers 4 --max-seconds 90  # More time for slow connections
kgr active --strict                    # Recheck ALL entries; slower
kgr check --gpu t4x2 --required-gpu-hours 2
```

In default mode, the newest 20 notebook entries are always verified live, as are
entries marked active/unknown. For older entries explicitly marked terminal by
Kaggle's sorted listing, the CLI can use that terminal snapshot without a separate
status request. The JSON report includes `live_checked_count` and
`listed_terminal_count` to distinguish these sources. `--strict` disables this
shortcut. If the time budget expires or an unknown status is encountered, preflight
**blocks submit** rather than guessing available capacity. These status checks
cannot reserve a slot or guarantee visibility of interactive sessions.

`kgr active` recognizes Kaggle worker states including `NEW_SCRIPT` (not started), `CANCEL_REQUESTED` and `CANCEL_ACKNOWLEDGED` (both conservatively count as occupied until confirmed finished). A notebook with a confirmed missing historical session is reported separately. If Kaggle introduces an unknown status, preflight **stops** and prints the original status response; diagnose with `python -m kaggle kernels status OWNER/SLUG`. Never assume an unknown state is a free slot.

If `kgr check` encounters a historical notebook for which Kaggle explicitly returns
`The resource was not found`, it records that reference under `active.missing_sessions`
and continues checking other kernels. This does **not** count as an active run.
Authentication/permission, rate-limit and unexpected errors still stop the check;
a contradictory `RUNNING` listing and missing session also stops it. You can inspect
`kgr active` to see the skipped references. This is a best-effort snapshot, not
an authoritative count of interactive sessions.

## 2. Run a Python file or a real notebook

```bash
# Minimal: automatically uses the configured KAGGLE_OWNER
kgr run ./train.py --gpu t4x2 --required-gpu-hours 2
kgr run ./experiment.ipynb --gpu t4x2

# Attach public data/model references or full Kaggle URLs
kgr run ./experiment.ipynb --gpu cpu \
  --dataset https://www.kaggle.com/datasets/uciml/iris \
  --model https://www.kaggle.com/models/google/mobilenet-v2/tensorFlow2/130-224-classification/1

# Submit, wait for completion, download automatically
kgr run ./train.py --gpu t4x2 --wait --fetch \
  --output './results/{job_id}'

# No network or quota used: inspect generated notebook/kernel metadata
kgr run ./experiment.ipynb --gpu t4x2 --dry-run
```

Every submission creates a **new private Kaggle kernel** with its own local Job ID, staging directory, manifest and kernel URL. Existing advanced usage still works:

```bash
kgr run --source ./my-research-repo --entry scripts/train.py --gpu t4x2 \
  --install-requirements --enable-internet --dataset uciml/iris
```

For `.py`, the runner packs allowed source files from the containing project directory, excluding `.env`, credentials and large data. For `.ipynb`, it submits a native notebook, preserving its cells and adding a small setup cell. A notebook must be self-contained (or install/import its dependencies in its own cells); local companion modules are **not** bundled automatically in notebook mode.

### Access attached datasets/models inside your code

```python
import os, json
from pathlib import Path

datasets = json.loads(os.environ['KGR_DATASET_PATHS'])
models = json.loads(os.environ['KGR_MODEL_PATHS'])
output = Path(os.environ['KGR_OUTPUT_DIR'])

iris_dir = Path(datasets['uciml/iris'])
output.joinpath('result.json').write_text('{"ok":true}')
```

A pinned model reference resolves to a directory with model files; loading it with PyTorch/TensorFlow is up to your experiment. Kaggle resources are mounted on `/kaggle/input`, not uploaded from local.

## 3. Monitor and download anything you need

```bash
kgr jobs                           # Locally recorded jobs
kgr sync                           # Refresh all locally tracked job statuses
kgr report --refresh --output ./my-report.json
kgr status JOB_ID                  # Live status for one job
kgr logs JOB_ID                    # Logs (or --follow)
kgr outputs JOB_ID                 # List remote output files
kgr fetch JOB_ID --output ./downloads/JOB_ID
kgr fetch JOB_ID --file-pattern '.*\.json$' --output ./json-only
kgr show JOB_ID                    # Source, metadata, logs and canonical kernel ref
```

`report` includes job statuses (e.g. submitted/running/complete/error), source/revision, owner/email metadata, remote links and output location. The raw API token never appears in reports. `sync` is an **on-demand** refresh, not a background daemon. `active` checks remote kernels for the current authenticated account, including runs not created with `kgr` where visible to the API. `jobs` and `sync` use local SQLite history; another machine has an independent history unless you move that state explicitly.

## 4. Dataset / model discovery

```bash
kgr dataset search iris
kgr dataset inspect uciml/iris
kgr model search mobilenet
kgr model files google/mobilenet-v2/tensorFlow2/130-224-classification/1
```

See [`examples/public-resources`](examples/public-resources/) and [`examples/gpu-smoke-test`](examples/gpu-smoke-test/), or the [native notebook example](examples/notebook-smoke-test/).

## Resource & account policy

Kaggle's Terms of Use do not allow an individual to control multiple active accounts. This CLI supports configuring a token for your own account and explicit credential changes, **not** rotating accounts automatically to bypass concurrent-session or GPU quota limits. When blocked, wait for jobs to finish / quota to refresh or use compute you are authorized to use elsewhere. `EMAIL` is a label in reports only: no mail is sent.

## Limitations

- GPU hour checks require a Kaggle CLI supporting `kaggle quota --format json` and parseable GPU quota data. Update the official Kaggle CLI if unsupported. Unknown data blocks submission.
- Remote preflight uses paginated batch-kernel listings and latest kernel statuses. It cannot necessarily see every interactive session or guarantee that two rapid submissions from independent machines won't race.
- `kgr` does not support Kaggle Benchmarks / frontier-model proxy inference, arbitrary Docker, or automatic multi-account quota evasion.
- Live `kgr run` requires network access, Kaggle credentials and available quota. Offline unit tests do not consume GPU time.

```bash
python -m unittest discover -s tests -v
```

Official docs: [Kaggle kernels](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels.md), [kernel metadata](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels_metadata.md), [Kaggle terms](https://www.kaggle.com/terms).


## Agent-first experiment candidate

For versioned experiments, safe Python/notebook source inspection, atomic local capacity reservations, idempotent submit and scientific result validation, see [agent-first workflow](docs/AGENT_WORKFLOW_P0.md). **Candidate implementation:** review and run the complete offline suite before using real Kaggle jobs. Legacy commands remain available.
