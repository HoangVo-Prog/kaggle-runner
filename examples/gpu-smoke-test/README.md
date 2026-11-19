# Kaggle GPU smoke test (T4 ×2)

This is a **portable end-to-end example** for `kaggle-runner` P0. It submits a private
Kaggle script kernel, checks CUDA/GPU availability, does a few small PyTorch matrix
multiplications on each available GPU, and produces three downloadable files:

- `gpu_smoke_result.json` — machine / CUDA / GPU names, timing and status
- `gpu_devices.csv` — per-GPU summary
- `gpu_smoke.log` — human-readable messages

`kaggle-runner` also generates `kgr_run_result.json`, and `kgr fetch` writes
`kgr_download_manifest.json` with SHA-256 checksums.

## 1. Install (WSL, Linux, macOS, Windows)

In the root of **your own checkout**, using any Python 3.11+ environment:

```bash
python -m pip install -e '.[kaggle]'
python -m kaggle --version
kgr doctor
```

For Conda, activate your preferred environment before installation. Nothing in
this example assumes a particular Conda environment name or a drive/path.

## 2. Authenticate Kaggle (choose ONE method)

**Recommended: interactive OAuth** (run in the same WSL/Linux/Windows environment
where you installed the CLI):

```bash
python -m kaggle auth login
# For a headless WSL/SSH terminal:
python -m kaggle auth login --no-launch-browser
```

**API token**, for headless sessions or CI (do not paste it into repo/config/git):

```bash
# Bash: read token without echoing or writing it to shell history
read -rsp "Kaggle API token: " KAGGLE_API_TOKEN; echo
export KAGGLE_API_TOKEN
```

Create a token at https://www.kaggle.com/settings/api . The CLI also supports
legacy `~/.kaggle/kaggle.json` credentials, but new setups should prefer OAuth
or `KAGGLE_API_TOKEN`. On Windows PowerShell, a session-only alternative is:

```powershell
$env:KAGGLE_API_TOKEN = Read-Host "Kaggle API token"
```

**Check access** (this should show `kaggle_auth_ok: true`):

```bash
kgr doctor --online
python -m kaggle kernels list --mine
```

Do NOT upload `kaggle.json`, API tokens, or OAuth credentials to Kaggle notebooks.
The credentials are needed **locally** to submit/monitor/fetch your private runs.

## 3. Dry-run (no API request, no GPU quota)

From the repository root:

```bash
kgr run \
  --source ./examples/gpu-smoke-test \
  --entry gpu_probe.py \
  --owner YOUR_KAGGLE_USERNAME \
  --gpu t4x2 \
  --dry-run
```

The output gives a `job_id` and staging directory. The generated private kernel
metadata uses `"machine_shape": "NvidiaTeslaT4"` (Kaggle T4 ×2). Review staged
`kernel-metadata.json` and `run.py` if desired. This is only a packaging check.

## 4A. One-command submit → wait → download

From **any directory** (adjust only the path to this example script):

```bash
python examples/gpu-smoke-test/run_example.py --owner YOUR_KAGGLE_USERNAME
```

The script verifies auth, submits once, prints job ID, kernel ref and Kaggle submit response, polls
Kaggle status every 20 seconds (up to 45 minutes), and after completion downloads
results under `./gpu-smoke-results/JOB_ID/` by default. If the job times out, it
**does not cancel the remote run**. Re-check later via the manual commands below.

`--owner` is the **Kaggle profile URL username**, not the display name. It must
match the authenticated account allowed to create/read the notebook. If you
configure `[runner].owner` in your user-local `config.toml`, omit `--owner`:

```toml
[runner]
owner = "YOUR_KAGGLE_PROFILE_USERNAME"
```

```bash
python examples/gpu-smoke-test/run_example.py
```

Optional flags:

```bash
python examples/gpu-smoke-test/run_example.py --owner YOUR_KAGGLE_USERNAME \
  --output ./downloads/my-gpu-check --interval-seconds 10 --max-wait-minutes 30
```

You can test packaging of the wrapper itself without credentials:

```bash
python examples/gpu-smoke-test/run_example.py \
  --owner YOUR_KAGGLE_USERNAME --dry-run
```

## 4B. Manual submit / monitor / fetch

From the repository root:

```bash
kgr run --source ./examples/gpu-smoke-test --entry gpu_probe.py \
  --owner YOUR_KAGGLE_USERNAME --gpu t4x2
```

Copy `job_id` from the JSON response, then:

```bash
kgr jobs
kgr status JOB_ID
kgr logs JOB_ID --follow        # Optional live Kaggle logs; Ctrl+C to stop watching
kgr fetch JOB_ID --output ./gpu-smoke-results/JOB_ID
```

Note: `kgr logs --follow` depends on the installed Kaggle CLI version. If it is
unsupported, use `kgr status` and inspect the downloaded `gpu_smoke.log` afterward.

## 5. Inspect result

```bash
python -m json.tool ./gpu-smoke-results/JOB_ID/gpu_smoke_result.json
```

Successful results contain `"status": "ok"`, `"cuda_available": true` and
`"cuda_device_count": 2` when Kaggle actually allocates both T4s. The example
runs on whatever GPUs are available; **do not infer two T4s without checking
`devices[].name`**. Kaggle allocation depends on account eligibility and quota.

If it fails, `gpu_smoke_result.json` (when saved) and `kgr_run_result.json` include
diagnostics. Use `kgr status JOB_ID` and the Kaggle kernel page. PyTorch is
available in standard Kaggle GPU notebook images; this test uses no pip install
and no internet. It does not use any external dataset or store credentials.

The output folder is caller-controlled: `KGR_OUTPUT_DIR` under Kaggle and
`--output` for local download. No machine-specific absolute paths are embedded.

## Troubleshooting: `kernels.get` permission denied

If submit prints a local Job ID but `kgr status` fails with `Permission 'kernels.get' was denied`,
this **does not prove the GPU kernel was created or ran**. In particular, a valid
looking `--owner` can still be a different account from the one authenticated by
Kaggle CLI. Check in order:

```bash
python -m kaggle kernels list --mine
python -m kaggle kernels status USERNAME/EXACT-KERNEL-SLUG
kgr show LOCAL_JOB_ID
```

Copy the username from `https://www.kaggle.com/USERNAME`, not the display name;
verify which account the Kaggle CLI is actually using (including credentials
from `KAGGLE_API_TOKEN`, `KAGGLE_USERNAME`/`KAGGLE_KEY`, and OAuth). Do not
publish credentials or paste `kaggle auth print-access-token` output into logs.
If the remote notebook is not listed, inspect the **Kaggle submit response**
before assuming a GPU allocation issue. Re-submit with the correct account
only after checking what happened; each submission may create a new notebook.
