# AGENTS.md — Kaggle Runner operational contract

**Scope:** candidate agent-first CLI added to baseline `main` at `c778a14b`. This contract distinguishes working candidate capabilities from unresolved P0 acceptance; it is not a claim of full production or live Kaggle verification.

## Mandatory safety rules

1. Read the chosen ExperimentSpec and confirm research objective, data sharing policy, resource cost and **explicit** `authorization.allowed_profiles`. `--account auto` must not expand authorization.
2. Keep all tokens in private project-root `.env` / the existing Kaggle credential configuration. Do not read, echo, commit, include in notebook output, embed in specs or attach to remote kernels. Never copy tokens into reports.
3. Use `kgr experiment ... --json` and check both the exit code and `ok`; a successful push is **not** a scientifically successful experiment.
4. Inspect upload contents *before* push using `kgr package inspect` and `kgr experiment validate`. Verify file names, SHA256, entrypoint, companion modules, runtime parameters, explicit dataset/model refs and expected outputs. Reject unsafe source trees.
5. Use only `kgr experiment submit` for agent-managed work. The legacy `kgr run` command is preserved but not yet fully bound to the new transactional lease contract; do not use it concurrently with unattended workers.
6. After `submit`, persist `job_id`, `experiment_id`, `attempt_id`, account alias and remote kernel ref from the JSON response. Never infer remote success from the local job ID alone.
7. If a push fails ambiguously, do **not** push again. Execute `kgr reconcile --experiment-id ID --json` under the original account and request operator review if remote existence cannot be proven.
8. Use `kgr experiment resume ID --json` or bounded `--wait`, not a new submission. Fetch artifacts with the original account token and compare hashes.
9. Require a versioned `result.json`, validate it with `kgr results validate JOB_ID --json`, then aggregate only compatible attempts with `kgr results aggregate`.
10. Generate report and content-addressed evidence using `kgr report experiment ID --format md`. Preserve provenance, excluded runs, uncertainty and limitations. Do not invent metrics.
11. Never automatically rotate accounts to evade Kaggle limits, archive history, cancel remote kernels, upload private datasets, release unknown_remote leases or rerun failed code without explicit approval.
12. Existing team `.env` diagnostics (`kgr team diagnose/list/check`) remain available, but local reservations cover **only processes sharing the same jobs.sqlite3**. Platform capacity remains Kaggle's authority.

## Recommended deterministic command sequence

```bash
kgr team diagnose
kgr team list --json
kgr package inspect ./experiment --entry train.py --json
kgr experiment validate ./experiment/experiment.json --json
kgr experiment submit ./experiment/experiment.json --account alice --approve --json
kgr experiment show EXPERIMENT_ID --json
kgr reconcile --experiment-id EXPERIMENT_ID --json
kgr experiment resume EXPERIMENT_ID --wait --max-wait-minutes 45 --json
kgr results validate JOB_ID --json
kgr results aggregate --experiment-id EXPERIMENT_ID --json
kgr report experiment EXPERIMENT_ID --format md
```

Use `kgr package inspect ./experiment.ipynb --json` for native notebook entrypoints. Notebook code reads `KGR_PARAMETERS_JSON`, and both formats write standardized scientific output to `KGR_OUTPUT_DIR`. For new runs use distinct `attempt_id` values rather than trying to clear old records.

## Stop and request operator intervention

Missing/invalid credentials, unauthorized profiles, quota/slot blocking, source-hash drift, suspected secrets in source, ambiguous remote submission, inaccessible remote kernel, schema mismatch, missing or inconsistent artifacts, destructive actions, compute exceeding approved budgets or scientifically invalid metrics. Do not silently downgrade any such failure to success.

## Verification before completion claim

```bash
python -m unittest discover -s tests -v
python -m unittest discover -s tests -p 'test_agent_first_p0.py' -v
python -m unittest discover -s tests -p 'test_agent_workflow_p0.py' -v
```

Current code and limitations: [agent workflow](docs/AGENT_WORKFLOW_P0.md), [source packaging](docs/SOURCE_PACKAGING.md), [result contract](docs/RESULT_CONTRACT_AGENT.md), [recovery](docs/RECOVERY.md). Live tests require separate explicit approval and are not part of the offline test suite.
