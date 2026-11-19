# Team API profiles

`kgr` can manage **explicitly authorized** Kaggle API tokens for different team members. Each profile is a local credential file; **never commit or share it**. Everyone should authorize the machine where their token will be used, and follow Kaggle's account policies.

## Add an account

```bash
kgr team add alice --username alice-kaggle --email alice@lab.example
kgr team add bob --username bob-kaggle
```

The prompt reads the API token with input hidden. Both `--username` and the token are **required**; `--email` is optional and report-only. The username creates the `owner/slug` notebook reference and labels the report; the API token is the **only credential** passed to Kaggle. A mismatched username/token will be rejected by Kaggle.

For CI, read from standard input instead of a command argument (avoid shell history):

```bash
# Example: supply the token through a secret provider, not a literal shell value.
secret_provider_command | kgr team add alice --username alice-kaggle --token-stdin
```

Profiles live under `~/.config/kaggle-runner/team/*.env` on Linux/WSL by default, with restrictive file permissions (`0600`, directory `0700`). `KGR_TEAM_DIR` optionally overrides that location. **Keep the credential directory outside Git.** `kgr team add` rejects replacing an existing profile without `--replace`, as well as duplicate usernames under different aliases.

## Check and schedule jobs

```bash
kgr team list
kgr team check --gpu t4x2 --required-gpu-hours 2

# Choose a particular member's credentials
kgr run ./train.py --gpu t4x2 --account alice

# Automatically select a locally authorized profile with capacity and enough GPU hours
kgr run ./train.ipynb --gpu t4x2 --required-gpu-hours 2 --account auto \
  --dataset uciml/iris \
  --model google/mobilenet-v2/tensorFlow2/130-224-classification/1
```

`--account auto` checks each profile's **runner-tracked** concurrent CPU + GPU jobs and its independent GPU quota. Selection prioritizes more free tracked slots, then greater remaining GPU quota, then alphabetical profile name. If none is eligible, no notebook is submitted. `--account auto` needs live preflight and is incompatible with `--dry-run`; for dry runs select `--account alice` explicitly.

**Important limitation:** This is a best-effort local decision, not a Kaggle slot reservation. Jobs launched outside this runner or from another machine may not be visible in the local SQLite ledger. Kaggle makes the final submission decision. All tokens are local; the repository never synchronizes credentials or job history automatically across machines. For full diagnosis of a single account, use the existing `kgr active --remote` with that account's credentials.

## Track results and ownership

```bash
kgr jobs
kgr report --refresh
kgr report --refresh --json --output report.json
kgr status JOB_ID
kgr logs JOB_ID
kgr logs JOB_ID --json
kgr outputs JOB_ID
kgr fetch JOB_ID --output ./results/JOB_ID
```

Each submitted job records `account_profile`, `account_owner` and `account_email` in the SQLite record and manifest (no token). `status`, `logs`, `outputs`, `fetch`, and `report --refresh` look up the stored profile and **use only its matching token**; they fail rather than silently switching to the default token when a profile is missing. Historical jobs without `account_profile` preserve the old single-account behavior. `kgr team check --json` and `kgr report --json` provide machine-readable results.

The report is a point-in-time snapshot: `--refresh` queries individual job statuses, and `kgr team check` queries local activity plus remote GPU quota. Neither command is a background service.

## Tested scenarios (offline mocks)

`python -m unittest discover -s tests -p test_team_profiles.py -v` covers required username/token, secret-file permissions, independent quotas, mixed CPU+GPU slot limits, automatic authorized profile selection, correct token selection for status/logs/fetch/reports, no fallback after a profile is removed, and no token disclosure in reports. Actual Kaggle GPU allocations require a live authorized test and consume quota.
