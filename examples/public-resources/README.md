# Public Kaggle Dataset + Model smoke test

This example checks that **both types of Kaggle resources are attached to your remote script**. It reads the `Iris.csv` public dataset and inspects files from a pinned public MobileNetV2 model variation. It **does not run MobileNet inference on Iris data**; they are intentionally independent inputs to test the resource mounting API. It uses no extra dependencies or GPU hours.

Public resources:

- Dataset: <https://www.kaggle.com/datasets/uciml/iris> (`uciml/iris`)
- Model: <https://www.kaggle.com/models/google/mobilenet-v2/tensorFlow2/130-224-classification/1> (`google/mobilenet-v2/tensorFlow2/130-224-classification/1`)

## Run (copy and paste)

Install and log in **once**, from the repository root:

```bash
python -m pip install -e '.[kaggle]'
python -m kaggle auth login
kgr doctor --online
```

Set your correct Kaggle *profile URL* username once in your **user** config (not in this repository):

```toml
# ~/.config/kaggle-runner/config.toml on Linux/WSL
[runner]
owner = "YOUR_KAGGLE_USERNAME"
```

Or append `--owner YOUR_KAGGLE_USERNAME` to the run command below.

**Step 1: Check metadata offline (does not submit):**

```bash
kgr run --source ./examples/public-resources --entry inspect_resources.py \
  --dataset uciml/iris \
  --model google/mobilenet-v2/tensorFlow2/130-224-classification/1 \
  --gpu cpu --dry-run
```

**Step 2: Submit, monitor and download automatically:**

```bash
kgr run --source ./examples/public-resources --entry inspect_resources.py \
  --dataset uciml/iris \
  --model google/mobilenet-v2/tensorFlow2/130-224-classification/1 \
  --gpu cpu --wait --fetch --output './results/public-resources/{job_id}'
```

**Step 3: Read the results:**

```bash
python -m json.tool ./results/public-resources/JOB_ID/public_resources_report.json
```

Replace `JOB_ID` with the ID printed when the run is submitted. Each run gets a separate output directory.

The runner also saves `iris_species_counts.csv`, `kgr_run_result.json`, and `kgr_download_manifest.json`.

If a run is interrupted, find its `job_id` from the initial output or `kgr jobs`, then use:

```bash
kgr status JOB_ID
kgr logs JOB_ID
kgr fetch JOB_ID --output ./results/public-resources --force
```

## Use attached resources in your own Python code

```python
import json
import os
from pathlib import Path

datasets = json.loads(os.environ["KGR_DATASET_PATHS"])
models = json.loads(os.environ["KGR_MODEL_PATHS"])

csv_path = Path(datasets["uciml/iris"]) / "Iris.csv"
model_dir = Path(models["google/mobilenet-v2/tensorFlow2/130-224-classification/1"])
output_dir = Path(os.environ["KGR_OUTPUT_DIR"])
```

The model directory holds artifacts (weights/SavedModel etc.), **not an automatically executing model**. Loading and inference require the corresponding ML framework and the model's documented preprocessing/inference code. Browse the model page for its usage details. A Kaggle Model here is **not** a Kaggle Benchmarks frontier-model API endpoint.

The version suffix `/1` is pinned for reproducibility. If that public resource changes or is no longer accessible, `kgr model files REF` and the model page will help choose a new version.
