# CLI reference (current implementation)

**Source:** `kaggle_runner/cli.py` at commit `cff2a661a3e6357febd9c574158fd58d020781ac`. This page documents commands actually present. Not a specification for planned future commands.

## Global options

```bash
kgr [--config FILE] [--env-file FILE] [--state-dir DIR] [--profile NAME] [--json] COMMAND ...
```

- `--env-file`: team/default secrets `.env`; default project root (unless `KGR_ENV_FILE` set).
- `--state-dir`: isolate a local job ledger and outputs; default from `KGR_STATE_DIR` or platform state dir.
- `--config`: optional legacy `config.toml` for runtime settings; **not** for team credentials.
- `--profile`: legacy Kaggle CLI config-directory profile; different from `run --account` team profile. Do not mix it with a token read from `.env`.
- `--json`: structured output; works before or after most (including nested) commands. Use for agents. Some commands (especially streaming logs) have special output behavior.

## Commands and expected side effects

| Command | Purpose | Remote calls? | Mutates local state? |
| --- | --- | --- | --- |
| `kgr auth init [--owner U --email E]` | Set legacy/default token in `.env` (prompted) | No | Yes, `.env` |
| `kgr auth show` | Show redacted legacy account metadata | No | No |
| `kgr team add ALIAS --username U [--email E] [--replace]` | Add/replace team profile (token prompt) | No | Yes, `.env` |
| `kgr team list` | List metadata only | No | No |
| `kgr team diagnose` | Show chosen `.env` and fields present | No | No |
| `kgr team migrate [--from-dir DIR]` | Import old per-profile files | No | Yes, shared `.env` |
| `kgr team check --gpu {cpu,t4x2} --required-gpu-hours H` | Check eligible team profiles | Yes (quota/status) | May refresh local job metadata |
| `kgr quota` | Default account GPU quota | Yes | No |
| `kgr active [--remote]` | Default-token lookup of locally tracked jobs (currently may include team jobs in mixed ledger), or remote history | Yes | May refresh local job state |
| `kgr check --gpu {cpu,t4x2} [--remote]` | Default-account preflight (mixed-ledger caveat) | Yes | May refresh local job state |
| `kgr doctor [--online]` | Python/Kaggle CLI and optional auth check | Optional | No |
| `kgr run FILE [OPTIONS]` | Stage and optionally submit | Yes unless `--dry-run` | Yes; creates job record |
| `kgr jobs [--limit N]` | List local ledger | No | No |
| `kgr show JOB_ID` | Show job + local manifest | No | No |
| `kgr status JOB_ID` | Refresh one remote job | Yes | Yes, status |
| `kgr logs JOB_ID [--follow]` | Get logs | Yes | Stores last-message excerpt unless follow |
| `kgr outputs JOB_ID` | List remote output filenames | Yes | No |
| `kgr fetch JOB_ID [--file-pattern REGEX] [--output DIR] [--force]` | Download artifacts | Yes | Yes, outputs + hash manifest |
| `kgr sync [--limit N]` | Refresh locally tracked jobs and summarize | Yes | Yes, status |
| `kgr report [--refresh] [--limit N] [--output FILE]` | Summary and account metadata | Only with `--refresh` | Optional output file/status |
| `kgr repair JOB_ID --owner U` | Correct old Kaggle reference locally | No validation | Yes; dangerous |
| `kgr archive JOB_ID --confirm-inactive` | Exclude verified inactive job from local slot accounting | No | Yes; does **not** cancel remote |
| `kgr dataset search QUERY` | Search Kaggle datasets | Yes | No |
| `kgr dataset inspect SOURCE` | Inspect local folder or remote dataset | Maybe | No |
| `kgr dataset upload --path DIR --ref OWNER/SLUG --title TEXT [--confirm-upload]` | Stage / explicitly upload private dataset | With confirmation | Yes |
| `kgr dataset download --ref OWNER/SLUG --output DIR [--unzip]` | Download dataset | Yes | Yes |
| `kgr model search QUERY` | Search models | Yes | No |
| `kgr model files PINNED_MODEL_REF` | List versioned model files | Yes | No |

## `run` options

```bash
kgr run ./train.py --account alice --gpu t4x2 \
  --required-gpu-hours 2 --max-concurrent 2 --dry-run --json
```

- Source: positional `.py` / `.ipynb`, `--file`, or `--source DIR_OR_GITHUB_URL --entry RELATIVE.py`.
- Account: `--account ALIAS` or `--account auto`; optional `--owner` for legacy default account. `auto` selects among configured team profiles and is incompatible with `--dry-run`.
- Compute: `--gpu cpu|t4x2`; `--required-gpu-hours FLOAT`; `--max-concurrent 1|2` (local safety setting).
- Inputs: repeated `--dataset OWNER/SLUG_OR_URL`, repeated `--model OWNER/MODEL/FRAMEWORK/VARIATION/VERSION`.
- Script-only: repeated `--arg STRING`, `--install-requirements`, `--unpack-datasets`; `--install-requirements` requires `--enable-internet`.
- Other: `--enable-internet`, `--timeout SECONDS` (Kaggle push), `--max-source-mb N`.
- Preflight: `--remote`, `--strict`, `--workers N`, `--max-seconds SEC`, `--quiet`.
- Lifecycle: `--dry-run`, `--wait`, `--fetch` (requires `--wait`), `--output DIR` (requires `--fetch`), `--poll-seconds N`, `--max-wait-minutes N`.

## Agent-friendly usage patterns

```bash
# Resource decision for all configured accounts
kgr team check --gpu t4x2 --required-gpu-hours 2 --json

# Note: --account auto chooses all eligible configured profiles, not a
# caller-provided allowlist. For narrow permission scope, choose explicit alias.
kgr run ./train.py --account alice --gpu t4x2 --json

# Saved Job ID is the stable handle; do NOT reconstruct owner/slug manually.
kgr status JOB_ID --json
kgr logs JOB_ID --json
kgr outputs JOB_ID --json
kgr fetch JOB_ID --output ./results/JOB_ID --json
kgr report --refresh --output ./reports/jobs.json --json
```

## Output and error semantics

- Human-readable default: compact summaries/tables. Agents should use `--json` for success responses.
- `kgr logs --follow` is plain-text streaming; it cannot be combined with `--json`.
- Common exit results are zero for successful commands, `3` for failed eligibility (`check` and `team check`), and `2` for a `RunnerError`. Some commands may return other nonzero values for failed execution or doctor checks; inspect both exit code and response, rather than hardcoding one status code for all errors.
- On exception, `RunnerError` prints `kgr: ...` to stderr instead of a JSON error envelope. This is a **known agent-contract gap**, not a stable JSON error API.
- `report --output report.json` writes full structured JSON even if human-oriented stdout was requested.
- `--dry-run` still creates a local `prepared` record and staging files. It does not submit to Kaggle.

## Mixed-account ledger caveat

`kgr team check` / `run --account ALIAS` scope tracked jobs by account alias. As of the audited commit, `kgr active` / `kgr check` for the legacy/default account are not perfectly isolated in a ledger that also contains team jobs: the `account_profile=None` filter can select all tracked jobs and query them with the default token. Prefer `kgr team check` and explicit `--account` for team work. Fix default-account scoping before autonomous use.

## Do not assume these commands exist yet

`kgr queue`, `kgr cancel`, `kgr retry`, `kgr reconcile`, `kgr experiment`, `kgr metrics`, `kgr results validate`, `kgr agent serve`, `kgr agent plan`, `kgr scheduler` are **PROPOSED** APIs, not implemented in the inspected code. See [roadmap](ROADMAP_AGENT_FIRST.md).
