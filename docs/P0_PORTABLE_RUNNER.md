# Kaggle Runner — P0 Portable CLI

This Python CLI adds a cross-platform workflow **without replacing the legacy PowerShell scripts**.
All command paths are relative to the current terminal or supplied by the user. **There are no hardcoded developer drive paths**.

## Install (Python 3.11+ / conda or venv)

From this repository's root directory:

```bash
# Activate your preferred environment first (e.g. conda activate linux-python-3-11)
python -m pip install -e '.[kaggle]'
kgr doctor
# Kaggle auth is interactive, performed separately using Kaggle's official CLI
python -m kaggle auth login
kgr doctor --online
```

Or avoid installation and use `python -m kaggle_runner` from the repository root.

**Runtime state:** override with `KGR_STATE_DIR` or `--state-dir`; default is
`$XDG_STATE_HOME/kaggle-runner` (Linux/WSL, fallback `~/.local/state/kaggle-runner`)
or `%LOCALAPPDATA%/kaggle-runner` (Windows). Never write jobs, outputs or credentials into the repo.

**Optional config:** copy `examples/config.toml` into
`~/.config/kaggle-runner/config.toml` (Linux/WSL) or
`%APPDATA%/kaggle-runner/config.toml` (Windows), or use `--config` with a path.
`--owner` overrides `[runner].owner`. Profiles are optional; `--profile primary` selects
credentials from `KGR_PROFILES_DIR/primary` (default `~/.kaggle-profiles/primary`)
using `KAGGLE_CONFIG_DIR` only in that subprocess. Without `--profile`, the CLI
honors your existing Kaggle authentication.

## Run a local Python repository

```bash
kgr run --source ./my-project --entry train.py \
  --owner YOUR_KAGGLE_USERNAME --gpu t4x2 --dry-run
# Review generated manifest + staged run.py before network submission:
kgr run --source ./my-project --entry train.py \
  --owner YOUR_KAGGLE_USERNAME --gpu t4x2
kgr jobs
kgr status JOB_ID
kgr logs JOB_ID --follow
kgr fetch JOB_ID --output ./downloads/JOB_ID
```

The runner creates a new, uniquely named **private Kaggle script kernel** per job.
It embeds a compressed source ZIP into one Python file; this avoids import errors
from Kaggle `kernels push` uploading only the configured `code_file`.
The default source ZIP limit is 3 MiB (tunable to 25 MiB), with a 2 MiB maximum
per source file. Large datasets or model weights are **not source files**.
Use Kaggle datasets/model resources for them. The runner skips common secrets,
virtualenvs, `.git/`, `data/`, checkpoints and symlinks. Inspect the generated
`manifest.json` and `run.py` before submitting confidential repositories.

`train.py` runs with its project directory as CWD, and these environment variables:

- `KGR_PROJECT_ROOT`: `/kaggle/working/kgr_source`
- `KGR_OUTPUT_DIR`: `/kaggle/working`
- `KGR_DATASET_ROOT`: `/kaggle/input`
- `KGR_DATASET_REFS`: JSON array of attached dataset references
- `KGR_DATASET_PATHS`: JSON dictionary from each dataset ref to its mounted path

Inside the script, write outputs to `os.environ["KGR_OUTPUT_DIR"]` to ensure they
appear in the kernel's downloaded output. The runner also writes
`kgr_run_result.json` with success/error info. `--install-requirements` requires
`--enable-internet`; it installs `requirements.txt` at kernel startup. This is a
practical P0 bootstrap, not a full Conda environment replication or container image.

For a public GitHub repo:

```bash
kgr run --source https://github.com/OWNER/REPO --ref main \
  --entry scripts/train.py --owner YOUR_KAGGLE_USERNAME --gpu t4x2
```

## Dataset manager (local, public, accessible private)

```bash
kgr dataset inspect ./local-dataset
kgr dataset inspect OWNER/PUBLIC_OR_PRIVATE_DATASET
kgr dataset upload --path ./local-dataset --ref YOUR_KAGGLE_USERNAME/my-data \
  --title "My private research dataset" # local packaging preview only
kgr dataset upload --path ./local-dataset --ref YOUR_KAGGLE_USERNAME/my-data \
  --title "My private research dataset" --confirm-upload
kgr dataset download --ref OWNER/EXISTING_DATASET --output ./dataset-download
kgr run --source ./my-project --entry eval.py --owner YOUR_KAGGLE_USERNAME \
  --dataset OWNER/EXISTING_DATASET --dry-run
```

`dataset upload` creates a **private** Kaggle dataset by default; public uploads
are intentionally not exposed in P0. Explicit `--confirm-upload` is required.
Files and their relative paths are bundled into `payload.zip`, preserving nested
structure; Kaggle mounts it under `/kaggle/input/DATASET_SLUG/payload.zip`.
Pass `--unpack-datasets` to `kgr run` to automatically unpack
managed `payload.zip` archives into `/kaggle/working/kgr_datasets/SLUG`;
`KGR_DATASET_PATHS` then points to the unpacked folders. Otherwise, unzip it
from your script if necessary. The dataset-upload manifest stays local.
For an existing owned dataset, use `--version-note "new data" --confirm-upload`;
this creates a new dataset version and changes remote content.

**Data protection:** Only upload data you have rights to send to Kaggle. Private
Kaggle datasets are accessible only to accounts with permission; the CLI never
circumvents those permissions. Existing Kaggle data is attached by reference,
not copied to local unless you explicitly download it.

## Job history

Job records live in SQLite under the state directory, separate from the repo:

```bash
kgr jobs
kgr show JOB_ID
kgr status JOB_ID
kgr logs JOB_ID
kgr fetch JOB_ID --force
```

Each record tracks job ID, private kernel reference, source, Git SHA when available,
dataset refs, status, staging directory and output directory. `fetch` generates a
SHA-256 output manifest. Status is looked up live via Kaggle CLI when requested.
The CLI avoids reusing kernel slugs to prevent accidentally overwriting an earlier run.

## P0 boundaries / verification

- Script kernels only (Python entrypoints). Not arbitrary Docker execution.
- CPU or T4 ×2 only, subject to **current** Kaggle availability and quota.
- Source ZIP bootstrap deliberately limited in size; dataset files must go through
  Kaggle Dataset API, and models via Kaggle Model or an accessible dataset.
- `jobs` reports local ledger; historical jobs created outside CLI aren't imported.
- Kaggle `status`, `logs`, and `output` without an explicit kernel version refer
  to the latest version. This P0 creates unique kernel slugs to avoid ambiguity.
- Kaggle notebook input and Kaggle Benchmarks/Model Proxy are **separate APIs**.
  This CLI does not implement chat or frontier-model benchmarking.
- Unit tests and dry-run packaging can execute offline; remote submit/download
  require working auth and network, and must be validated from the user's machine.

Official references:
- https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels.md
- https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels_metadata.md
- https://github.com/Kaggle/kaggle-cli/blob/main/docs/datasets.md
- https://github.com/Kaggle/kaggle-cli/blob/main/docs/datasets_metadata.md
