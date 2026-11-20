# GPU smoke test

This is the only example maintained in `kaggle-runner`: submit one private Kaggle T4 ×2 experiment, inspect CUDA/PyTorch, and download its artifacts. It does not require a dataset or Kaggle Benchmarks API.

## Setup

From the repository root, with Python 3.11+:

```bash
python -m pip install -e '.[kaggle]'
python -m kaggle auth login
kgr doctor --online
```

On a headless WSL/SSH machine, try `python -m kaggle auth login --no-launch-browser`. Credentials remain local.

## Run

```bash
# No API or GPU use: just stage the script
python examples/gpu-smoke-test/run_example.py --owner YOUR_KAGGLE_USERNAME --dry-run

# Submit, poll every 20 seconds, then download results
python examples/gpu-smoke-test/run_example.py --owner YOUR_KAGGLE_USERNAME
```

The `--owner` value must match the username in your `https://www.kaggle.com/USERNAME` profile URL. You may omit it if `[runner].owner` is configured in your user-local `config.toml` (see the root README).

Optional example arguments: `--interval-seconds 10`, `--max-wait-minutes 30`, `--output ./my-results`. The default download destination is `./gpu-smoke-results/JOB_ID/`, relative to the calling directory.

Use the CLI manually if you prefer:

```bash
kgr run --source ./examples/gpu-smoke-test --entry gpu_probe.py --owner YOUR_KAGGLE_USERNAME --gpu t4x2
kgr jobs
kgr status JOB_ID
kgr logs JOB_ID
kgr fetch JOB_ID --output ./gpu-smoke-results/JOB_ID
```

The GPU probe writes `gpu_smoke_result.json`, `gpu_devices.csv`, and `gpu_smoke.log` under `KGR_OUTPUT_DIR`. The runner also creates `kgr_run_result.json` and a download checksum manifest. Check the JSON `status`, `cuda_available`, `cuda_device_count`, and the actual `devices[].name` values; merely requesting T4 ×2 is not proof of GPU allocation.

If monitoring reports `kernels.get` permission denied, inspect the Kaggle submit response URL. For example, when the URL points to `/code/hoanggvo/SLUG`, a status request for `hoang_vo/SLUG` targets a **different kernel**. Always use the actual owner/slug shown by Kaggle. Check `python -m kaggle kernels list --mine` before resubmitting.

This example does not install dependencies inside the Kaggle kernel, does not send credentials, and does not start a long-running API server. It is a short, auditable GPU test.
