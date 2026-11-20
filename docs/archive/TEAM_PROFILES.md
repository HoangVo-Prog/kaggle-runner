# Team Kaggle profiles: one private `.env`

The `.env` file is the **single source of truth** for team API credentials and
account metadata. `config.toml` is **not** used for team accounts. All users must
have authorized the use of their own Kaggle token on this runner machine.

## 1. Configure the team

Use **`<repository-root>/.env`** (beside `pyproject.toml`) as the default
credential file. On Linux/WSL use `chmod 600 .env`. The project-root file
contains only *locally authorized* credentials; **never commit, upload or share it**.
The fields `USERNAME` and `API_TOKEN` are required for each account; `EMAIL` is
optional report metadata and never used for authentication.

```dotenv
# Optional legacy default account (independent of team profiles)
KAGGLE_OWNER=default_user
EMAIL=default@example.org
KAGGLE_API_TOKEN=default-token

# All team accounts live in this one file:
KGR_TEAM_PROFILES=alice,bob
KGR_TEAM_ALICE_USERNAME=alice_kaggle
KGR_TEAM_ALICE_API_TOKEN=alice-token
KGR_TEAM_ALICE_EMAIL=alice@example.org
KGR_TEAM_BOB_USERNAME=bob_kaggle
KGR_TEAM_BOB_API_TOKEN=bob-token
# KGR_TEAM_BOB_EMAIL is optional
```

Values above are **placeholders**, not working tokens. Aliases are case-insensitive
and may use letters, numbers, `_` and `-`. For aliases containing a dash, the
corresponding variable prefix uses `_`; `lab-a` is `KGR_TEAM_LAB_A_`. Do not
use both `lab-a` and `lab_a` (same variable namespace). The comma-separated
`KGR_TEAM_PROFILES` declares the enabled profiles; `team add` maintains it.

Create the repository-root `.env` automatically with the CLI (it uses
private `0600` permissions on POSIX):

```bash
cd /path/to/kaggle-runner
kgr team add alice --username alice_kaggle
kgr team add bob --username bob_kaggle --email bob@example.org
kgr team list
# ls -l .env
```

An editable install (`pip install -e ".[kaggle]"`) continues using the same
repository-root `.env` even when the shell runs from outside this folder.

`kgr team add` prompts for each token without echoing it, updates **the same**
`.env`, preserves unrelated settings and creates/replaces it atomically with
`0600` permissions on POSIX. Use `--replace` to update an existing profile.
Use the global `--env-file PATH` option if you want a one-off file selection:

```bash
kgr --env-file "$HOME/kgr-secrets.env" team list
kgr --env-file "$HOME/kgr-secrets.env" run ./train.py --account alice --gpu cpu
```

Lookup precedence: explicit `--env-file PATH`, then `KGR_ENV_FILE`, then
`<kaggle-runner repository-root>/.env`. The current directory's `.env` from a
different project is **never** used by accident. If installed from a wheel and
running outside the checkout, provide `--env-file` or `KGR_ENV_FILE` because
there is no discoverable project root. The `.env` file is ignored by Git.

For WSL checkouts on `/mnt/c` or `/mnt/d`, DrvFs may not honor `chmod 600`
unless mounted with `metadata`; do not disable the token permission check.
If upgrading from the previous default `~/.config/kaggle-runner/.env`, copy
that file to the repository root yourself (and set secure permissions).

## 2. Migrate existing `team/*.env` files

Earlier runner versions created one file per profile under
`~/.config/kaggle-runner/team/`. Import them explicitly:

```bash
kgr team migrate
# Or for a custom old directory:
kgr team migrate --from-dir "$HOME/old-kgr-team"
kgr team list
```

Migration copies the existing tokens to the selected `.env` once and **does not
delete** old files. If a name or username conflicts with an existing profile,
it stops without changing the destination. After validating the team list and
jobs, remove/secure duplicate old credentials manually. Normal operation no
longer reads the old directory.

## 3. Check, submit, report and download

```bash
kgr team check --gpu t4x2 --required-gpu-hours 2
kgr run ./train.ipynb --account auto --gpu t4x2 --required-gpu-hours 2
kgr run ./train.py --account bob --gpu cpu
kgr jobs
kgr report --refresh
kgr logs JOB_ID
kgr fetch JOB_ID --output ./results/JOB_ID
```

`--account auto` chooses among profiles that pass their own quota/tracked-slot
preflight. It never combines quotas across accounts. Each job stores only
`account_profile`, `account_owner` and `account_email` in SQLite and manifests,
**not the token**. Its status/logs/fetch always use the profile token selected
at submission, even when the `.env` has no legacy default token. If a required
profile is removed, access fails instead of silently using another account.

A profile's slot count is a local best-effort estimate. It cannot see every job
started elsewhere, and Kaggle makes the final placement decision. Report
metadata is bound to the account that launched each job.

## 4. Offline and real multi-account tests

**Offline tests (no Kaggle calls):**

```bash
python -m unittest discover -s tests -p 'test_team_env.py' -v
python -m unittest discover -s tests -p 'test_team_profiles.py' -v
python -m unittest discover -s tests -v
```

**Live integration test (two real Kaggle submissions):**

```bash
python examples/team-env-multi-account/run_live_test.py \
  --accounts alice bob \
  --gpu cpu \
  --confirm-live
```

Requires Kaggle CLI credentials for **both** accounts in `.env`. It submits a
private, CPU-only notebook to each account, polls both, downloads their artifacts
and logs, and checks per-job account metadata. It will consume Kaggle compute
and may leave remote jobs if interrupted. It is intentionally **not run by the
unit test suite**. To submit without waiting, add `--no-wait`. For a GPU smoke
instead, use `--gpu t4x2 --required-gpu-hours 1`; this consumes GPU quota.

The local smoke file is `examples/team-env-multi-account/smoke.py` (standard
library only, writes `team_smoke_result.json`). No notebook package includes
an API token or `.env` content. The integration test needs real Kaggle access,
which cannot be simulated by unit tests.

### Troubleshooting: `Missing API token for team profile`

By default `kgr` reads **`<kaggle-runner-project-root>/.env`**. However,
`KGR_ENV_FILE` or `--env-file PATH` takes priority over that default. To inspect
*which file is being read* without printing secrets:

```bash
kgr team diagnose
kgr --env-file .env team diagnose
```

The diagnostic displays the resolved `.env` path and whether each roster entry
has a username and API token, but **never prints token contents**. If an old
`KGR_ENV_FILE` override points elsewhere, unset it (`unset KGR_ENV_FILE`) or
select the intended file with `--env-file`.

Team variable names are now accepted case-insensitively: both
`KGR_TEAM_TOANLTV_API_TOKEN` and `KGR_TEAM_toanltv_API_TOKEN` are recognized
for the roster alias `toanltv`. Prefer the uppercase spelling for consistency;
if both spellings exist with different values, remove the duplicate—the runner
fails clearly instead of guessing which credential to use.
