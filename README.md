# kaggle-runner

A small, portable Python CLI for **running experiments on Kaggle Notebooks** from a local machine. It prepares code, submits private CPU/GPU kernels through Kaggle's official CLI, tracks runs, and downloads their results.

This project is **not** a Kaggle Benchmarks/Model Proxy client, a training framework, or a GPU server. The local machine controls jobs; execution happens on Kaggle.

## Install and authenticate

Requires Python 3.11+, Git (for GitHub sources), and a Kaggle account with Notebook access.

```bash
python -m pip install -e '.[kaggle]'
python -m kaggle auth login
kgr doctor --online
```

For headless terminals, the Kaggle CLI also offers `python -m kaggle auth login --no-launch-browser`. Authentication stays on your machine; never commit tokens or Kaggle credentials to this repository.

## Run an experiment

```bash
# Package and inspect locally (does not contact Kaggle or consume GPU quota)
kgr run --source ./my-project --entry train.py --owner YOUR_KAGGLE_USERNAME --gpu t4x2 --dry-run

# Submit a new private Kaggle kernel
kgr run --source ./my-project --entry train.py --owner YOUR_KAGGLE_USERNAME --gpu t4x2

# Use the job_id printed by the submit command
kgr jobs
kgr status JOB_ID
kgr logs JOB_ID
kgr fetch JOB_ID --output ./results/JOB_ID
```

`--owner` is your Kaggle **profile URL username**, not your display name. You can set it once in the user-local config instead of passing it each time:

```toml
# Linux/WSL: ~/.config/kaggle-runner/config.toml
# Windows: %APPDATA%/kaggle-runner/config.toml
[runner]
owner = "YOUR_KAGGLE_USERNAME"
```

The CLI uses `XDG_CONFIG_HOME` / `XDG_STATE_HOME` on Linux/WSL, and the platform app-data folders on Windows. Override with `KGR_CONFIG` and `KGR_STATE_DIR` or `--config` and `--state-dir`. No machine-specific path is required in the repo.

Use `--gpu cpu` for CPU runs, or `--gpu t4x2` to request Kaggle's `NvidiaTeslaT4` shape; actual availability depends on your account and Kaggle quota. An entrypoint is a Python file relative to the source directory.

A GitHub repository can also be the source:

```bash
kgr run --source https://github.com/OWNER/REPO --ref main --entry scripts/train.py --gpu t4x2
```

The runner packages a small Python source tree into a single Kaggle script kernel. Large datasets, weights and checkpoints should be attached as Kaggle resources, not bundled in the source. Common secrets, local environments and large files are excluded; review the staged manifest for sensitive code before submitting.

## Datasets as experiment inputs

```bash
kgr dataset inspect ./my-data
kgr dataset inspect OWNER/DATASET
kgr dataset upload --path ./my-data --ref YOUR_KAGGLE_USERNAME/my-data --title 'My data'
# Preview first; the next command explicitly uploads a PRIVATE dataset:
kgr dataset upload --path ./my-data --ref YOUR_KAGGLE_USERNAME/my-data --title 'My data' --confirm-upload

kgr run --source ./my-project --entry train.py --dataset OWNER/DATASET --gpu t4x2
```

For the runner's managed dataset ZIP, add `--unpack-datasets` to `kgr run`. Inside Kaggle, `KGR_DATASET_PATHS` provides attached dataset locations and `KGR_OUTPUT_DIR` points to the download-ready output directory. Only upload datasets you are permitted to share with Kaggle.

## Verify the GPU workflow

See [`examples/gpu-smoke-test/`](examples/gpu-smoke-test/) for a minimal PyTorch CUDA test that submits a kernel, polls status, and fetches JSON/CSV/log outputs.

```bash
python examples/gpu-smoke-test/run_example.py --owner YOUR_KAGGLE_USERNAME --dry-run
python examples/gpu-smoke-test/run_example.py --owner YOUR_KAGGLE_USERNAME
```

## Development and boundaries

```bash
python -m unittest discover -s tests -v
```

- The CLI runs Python **script kernels**, not arbitrary containers or Kaggle Benchmarks tasks.
- Each run creates a unique, private kernel; the local SQLite job ledger lives outside the repository.
- `kgr jobs` lists locally recorded jobs; `kgr status` queries Kaggle for the selected job.
- Submissions and downloads require valid credentials and network access. Offline tests do not certify a live Kaggle GPU allocation.
- `kgr --help` shows all commands and flags.

Kaggle CLI reference: https://github.com/Kaggle/kaggle-cli
