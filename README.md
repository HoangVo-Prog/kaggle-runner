# kaggle-runner

**Run Python experiments on Kaggle CPU/GPU from your own terminal.** This small CLI packages source code, attaches Kaggle Datasets and Models, submits private notebook jobs, watches status and downloads outputs. It does **not** provide frontier-model inference / Kaggle Benchmarks or a permanent GPU server.

## 1. Install and sign in

Python **3.11+** and an authenticated Kaggle account are required; Git is needed only for GitHub-source jobs.

```bash
python -m pip install -e '.[kaggle]'
python -m kaggle auth login
kgr doctor --online
```

On a headless terminal use `python -m kaggle auth login --no-launch-browser`. Authentication lives on your machine; never commit tokens.

Set your **Kaggle profile URL username** once in the machine's config (this is not your display name):

```toml
# Linux / WSL: ~/.config/kaggle-runner/config.toml
# Windows: %APPDATA%/kaggle-runner/config.toml
[runner]
owner = "YOUR_KAGGLE_USERNAME"
```

Alternatively, add `--owner YOUR_KAGGLE_USERNAME` to each `kgr run` command. For a different config path use `KGR_CONFIG` or `kgr --config /path/to/config.toml run ...`.

## 2. Try a public Dataset + Model

Both resources below are public. Kaggle attaches them **remotely**; they are **not downloaded to your local machine**.

```bash
# Find public resources and inspect the exact model version
kgr dataset search iris
kgr model search mobilenet
kgr model files google/mobilenet-v2/tensorFlow2/130-224-classification/1

# Prepare source and kernel metadata without uploading anything
kgr run --source ./examples/public-resources --entry inspect_resources.py \
  --dataset uciml/iris \
  --model google/mobilenet-v2/tensorFlow2/130-224-classification/1 \
  --gpu cpu --dry-run

# Submit, track, and download results in one command
kgr run --source ./examples/public-resources --entry inspect_resources.py \
  --dataset uciml/iris \
  --model google/mobilenet-v2/tensorFlow2/130-224-classification/1 \
  --gpu cpu --wait --fetch --output './results/public-resources/{job_id}'

# Open the result
python -m json.tool ./results/public-resources/JOB_ID/public_resources_report.json
```

See [the example guide](examples/public-resources/README.md) for the expected files and the Python code that reads resource paths. This smoke test reads Iris and inspects MobileNet files; **it does not claim that MobileNet is a classifier for Iris**.

## 3. Run your own project

```bash
kgr run --source ./my-project --entry train.py --gpu t4x2 \
  --dataset OWNER/DATASET \
  --model OWNER/MODEL/FRAMEWORK/VARIATION/VERSION \
  --wait --fetch --output './results/my-project/{job_id}'
```

Add `--dataset` or `--model` multiple times to attach multiple resources. Models **must have a numeric version**: this prevents silently switching weights between runs. `--gpu cpu` requires no GPU; `--gpu t4x2` requests Kaggle T4 ×2, subject to quota and availability. All kernels are private by default.

The input Python file gets paths through environment variables:

```python
import json, os
from pathlib import Path

datasets = json.loads(os.environ["KGR_DATASET_PATHS"])
models = json.loads(os.environ["KGR_MODEL_PATHS"])

some_dataset = Path(datasets["OWNER/DATASET"])
some_model = Path(models["OWNER/MODEL/FRAMEWORK/VARIATION/VERSION"])
output_dir = Path(os.environ["KGR_OUTPUT_DIR"])
```

`some_model` is an **artifact directory**, not an API endpoint. To run inference, your script must load the files with the matching framework and preprocessing code. The attached model's published usage instructions are the source of truth.

GitHub sources work too:

```bash
kgr run --source https://github.com/OWNER/REPO --ref main \
  --entry scripts/train.py --gpu cpu --dataset OWNER/DATASET
```

The runner bundles a small Python source tree into one Kaggle script kernel; large data, weights, checkpoints, credentials and virtual environments are not bundled. Attach large inputs via Kaggle resources. For project-specific dependencies, add `requirements.txt` and use `--install-requirements --enable-internet` when acceptable.

## 4. Manage jobs and outputs

With `--output './results/name/{job_id}'`, every run receives its own folder. Copy the actual `job_id` from the first submit response for inspecting a run.

If you don't use `--wait --fetch`, copy `job_id` from the initial response and use:

```bash
kgr jobs
kgr status JOB_ID
kgr logs JOB_ID
kgr fetch JOB_ID --output ./results/JOB_ID
kgr show JOB_ID
```

With `--wait --fetch`, the CLI prints live polling status and downloads on completion (including error runs when Kaggle has output). If monitoring times out, the **remote job is not cancelled**; resume with `kgr status JOB_ID`. Outputs are stored outside the repository by default; use `--output` to choose a download directory. Use `--dry-run` for offline packaging tests.

A GPU-specific end-to-end test is in [examples/gpu-smoke-test](examples/gpu-smoke-test/README.md).

## More commands

```bash
kgr dataset inspect OWNER/DATASET
kgr dataset download --ref OWNER/DATASET --output ./downloaded
kgr dataset upload --path ./my-data --ref OWNER/SLUG --title "My private dataset" --confirm-upload
kgr model files OWNER/MODEL/FRAMEWORK/VARIATION/VERSION
kgr run --help
python -m unittest discover -s tests -v
```

Dataset uploads are **private** and require explicit `--confirm-upload`. Local job records are in an OS-specific state directory (`KGR_STATE_DIR` to override), not checked into Git. This CLI invokes Kaggle's official CLI underneath. Runs are limited by Kaggle's notebook policies and compute quota.

Official formats: [Kaggle kernel metadata](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels_metadata.md) · [Kaggle Model version commands](https://github.com/Kaggle/kaggle-cli/blob/main/docs/model_variations_versions.md).
