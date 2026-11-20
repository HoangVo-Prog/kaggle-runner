# Kaggle Runner — documentation index

**Documentation baseline:** `HoangVo-Prog/kaggle-runner`, branch `cleanup/runner-only`, commit `cff2a661a3e6357febd9c574158fd58d020781ac`, inspected on 2026-11-20.

## What this repository is

`kgr` is a Python CLI over Kaggle's official CLI. It packages `.py` / `.ipynb` experiments, submits kernels, checks team-account quotas and locally tracked concurrency, tracks jobs in SQLite, and retrieves remote statuses, logs and artifacts. It is **not yet** a complete autonomous experimentation service. Agents can drive the existing CLI, but must provide their own orchestration and report validation until the missing pieces in the roadmap are built.

## Start here

| Reader / task | Document |
| --- | --- |
| New team member | [Quickstart](QUICKSTART.md) |
| Coding agent or agent operator | [Root AGENTS.md](../AGENTS.md) + [Agent workflow](AGENT_WORKFLOW.md) |
| Complete command inventory | [CLI reference](CLI_REFERENCE.md) |
| Data flow / modules / persisted state | [Architecture](ARCHITECTURE.md) |
| Machine-readable metrics and report design | [Result contracts](RESULT_CONTRACT.md) |
| Credentials, authorization, safe operations | [Security and access](SECURITY.md) |
| Debugging, incident response, recovery | [Operations runbook](OPERATIONS.md) |
| Test coverage and end-to-end gates | [Testing](TESTING.md) |
| Features required for truly autonomous agents | [Agent-first roadmap](ROADMAP_AGENT_FIRST.md) |

## Existing documentation

- [Team profiles](TEAM_PROFILES.md): `.env`, profile metadata, multi-account smoke test, diagnostics.
- [Preflight](PREFLIGHT.md): quick vs remote status checks and their limitations.
- [CLI output](CLI_OUTPUT.md): compact text vs `--json`, local slot policy.
- [Example: CPU/GPU slot test](../examples/slot-limit-test/README.md).
- [Example: multi-account live smoke](../examples/team-env-multi-account/run_live_test.py).

## Source of truth and drift warnings

Treat the **implementation and tests** as authoritative for supported CLI flags, and these new docs as an audited map of that implementation at the pinned commit. The historical root `README.md` contains superseded statements (e.g. default `.env` under home, `kgr active` doing full remote scan by default, a single-account-only model). The current source instead resolves `.env` at the project root, uses `kgr active` for locally tracked jobs by default, and supports team profiles in one `.env`. **Known implementation caveat:** default-account `kgr active` / `kgr check` may attempt to inspect team-profile jobs in a shared ledger using the default token; see architecture and roadmap. A README refresh is recommended; do not silently adopt obsolete instructions.

**Labels:**

- **IMPLEMENTED**: present in code at the pinned commit.
- **PARTIAL**: present but insufficient for unattended / multi-agent correctness.
- **PROPOSED**: design recommendation; no corresponding command exists yet.

## External references

- Kaggle official [CLI commands](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels.md) and [repository](https://github.com/Kaggle/kaggle-cli).
- Kaggle [API documentation](https://www.kaggle.com/docs/api).
- Kaggle [Terms of Use](https://www.kaggle.com/terms).

Do not confuse `kaggle quota` (compute GPU/TPU budget) with `kaggle benchmarks quota` (a separate model proxy quota). This repo is for notebook execution, not the Kaggle Benchmarks product.
