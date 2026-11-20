# Quickstart — human and agent operators

**Applies to inspected commit:** `cff2a661a3e6357febd9c574158fd58d020781ac`.

## 1. Environment

Python >= 3.11 and a Kaggle account with authorized API credentials are required. The package depends on the official Kaggle CLI for remote actions.

```bash
cd /path/to/kaggle-runner
python -m pip install -e '.[kaggle]'
kgr --version
kgr doctor
```

`pip install -e` keeps source changes live. For non-editable wheel installations outside the checkout, provide `--env-file` / `KGR_ENV_FILE` explicitly.

## 2. Create a single, private team `.env`

File location: **`<kaggle-runner-project-root>/.env`**. Do not commit or share it. Prefer adding profiles with the CLI to avoid case, underscore or alias typos:

```bash
kgr team add alice --username alice_kaggle
kgr team add bob --username bob_kaggle --email bob@example.com
kgr team diagnose
kgr team list
```

The CLI asks for each token without echoing it. You can also edit the file directly:

```dotenv
KGR_TEAM_PROFILES=alice,bob
KGR_TEAM_ALICE_USERNAME=alice_kaggle
KGR_TEAM_ALICE_API_TOKEN=REPLACE_WITH_ALICE_TOKEN
KGR_TEAM_BOB_USERNAME=bob_kaggle
KGR_TEAM_BOB_API_TOKEN=REPLACE_WITH_BOB_TOKEN
KGR_TEAM_BOB_EMAIL=bob@example.com
```

Aliases in `KGR_TEAM_PROFILES` must match variable prefixes after uppercase normalization. For `hggggv`, the keys begin `KGR_TEAM_HGGGGV_`, **not** `KGR_TEAM_HGGGV_`. Prefer generating keys using `kgr team add` or `kgr team diagnose` to avoid manual typos. `KGR_ENV_FILE` and global `--env-file` override the default project root path.

```bash
# POSIX private permission (WSL DrvFs may require a metadata mount)
chmod 600 .env
kgr team diagnose
```

Do not use `cat .env` in agent logs or send tokens to a chatbot. The single-account `KAGGLE_OWNER` / `KAGGLE_API_TOKEN` fields are optional if you only use `--account` team profiles.

## 3. Verify resources

```bash
kgr team check --gpu t4x2 --required-gpu-hours 2
kgr team check --gpu t4x2 --required-gpu-hours 2 --json
```

For team operations, use `team check`. `kgr quota`, `kgr active`, and `kgr check` without team selection use the legacy/default token, **not** each team token. **Current caveat:** in a mixed-account SQLite ledger, default `active/check` may mistakenly probe team jobs with that token. Use `team check` or explicit `run --account ALIAS` until scoped default preflight is fixed.

The local concurrency check is shared by CPU and GPU for each account. It does not see every remote/out-of-band session. A READY result is permission to *attempt* a submit, not a GPU reservation.

## 4. Safest first submission

Use an explicit profile and a CPU smoke first. A dry run does not call Kaggle:

```bash
kgr run ./examples/team-env-multi-account/smoke.py \
  --account alice --gpu cpu --dry-run --json
```

Inspect the emitted `kernel_ref`, `staging_dir`, `kernel_type`, input refs and manifest. Real submit:

```bash
kgr run ./examples/team-env-multi-account/smoke.py \
  --account alice --gpu cpu --json
```

Capture the returned `job_id` (not the preview slug). To choose among **authorized** profiles automatically:

```bash
kgr run ./examples/team-env-multi-account/smoke.py \
  --account auto --gpu cpu --json
```

`--account auto` is online-only and **cannot** be combined with `--dry-run`.

## 5. Monitor and fetch

```bash
kgr jobs
kgr status JOB_ID --json
kgr logs JOB_ID --json
kgr outputs JOB_ID --json
kgr fetch JOB_ID --output ./results/JOB_ID --json
kgr report --refresh --output ./reports/jobs.json --json
```

For foreground waiting and output retrieval in one command:

```bash
kgr run ./train.py --account alice --gpu t4x2 \
  --required-gpu-hours 2 --wait --fetch \
  --output './results/{job_id}' --poll-seconds 20 --max-wait-minutes 45
```

`--max-wait-minutes` limits the **local wait**; it does not cancel the remote job. If monitoring times out, preserve the returned Job ID and resume with `status` instead of re-submitting.

## 6. Submit your own experiment

**Python `.py`** (entrypoint source is staged into a Kaggle script):

```bash
kgr run ./train.py --account alice --gpu t4x2 \
  --dataset uciml/iris \
  --arg=--epochs --arg=3 \
  --required-gpu-hours 1
```

**Native `.ipynb`** (cells preserved, inputs mounted via setup cell):

```bash
kgr run ./experiment.ipynb --account alice --gpu t4x2
```

A notebook must be self-contained: `--arg`, `--install-requirements`, and `--unpack-datasets` are **not supported** in `.ipynb` mode. In script mode `--install-requirements` requires `--enable-internet`.

**Advanced source directory / GitHub source:**

```bash
kgr run --source ./research-repo --entry scripts/train.py \
  --account alice --gpu t4x2 --enable-internet --install-requirements
```

Avoid submitting unreviewed source or dependencies under an authorized teammate's account.

## 7. Run tests

```bash
# Offline, no Kaggle compute use
python -m unittest discover -s tests -v

# Optional LIVE test: creates two real CPU jobs on separate profiles
python examples/team-env-multi-account/run_live_test.py \
  --accounts alice bob --gpu cpu --confirm-live
```

Do not run live tests without explicit permission. See [testing](TESTING.md) and [security](SECURITY.md).
