# Kaggle Runner (`kgr`)

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

## 1. Check before running

```bash
kgr quota                         # Remaining GPU hours
kgr active                        # Running/queued Kaggle batch kernels
kgr check --gpu t4x2 --required-gpu-hours 1.5
```

`kgr run` performs the same preflight automatically. The default local safety ceiling is **2 active batch kernels** and the minimum expected GPU budget is **0.25 hours** (adjust using `--required-gpu-hours`). A job is **blocked** if the running count reaches the ceiling, if GPU quota is insufficient, or if the API response cannot be verified. CPU jobs check active batch kernels, but not GPU hours.

**Important:** Kaggle can enforce different resource limits, reserve GPU time, and run interactive sessions that the kernels listing does not enumerate. These checks are conservative estimates, **not** a guarantee of availability or a claim that Kaggle globally allows exactly two sessions. A GPU allocation still depends on Kaggle.

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
