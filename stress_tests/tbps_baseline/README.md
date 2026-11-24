# TBPS baseline agent stress test (prepare-only by default)

This additive testbed changes no runner implementation and does not submit or train anything when installed. Run from the Kaggle Runner repository root. The workload is the existing `workspaces/tbps` checkout. A future live test requires explicit operator authorization, budget, accounts and approved plan. `case.json` is a **test case**, not an executable ExperimentSpec or approval. Its null/empty fields are intentional. Keep `OPERATOR_REVIEW.md` out of the initial agent context to avoid priming the test.

## Start

Give Codex `TASK.md` (via the short prompt in the delivery message). Its first action must be reading root `AGENTS.md`. Let it discover operational commands and record friction. Do not run a setup script that pre-resolves the environment before the test; that would hide the exact failure under study.

The independent audit logger uses Python stdlib only, not the kgr environment, so logging can start even when kgr is unavailable. Choose any existing Python 3.9+ interpreter for the logger. On POSIX/WSL:

```bash
python3 stress_tests/tbps_baseline/audit.py init --session initial-prep
python3 stress_tests/tbps_baseline/audit.py event --session initial-prep --kind decision --step A02 --summary 'Resolve existing runner interpreter' --reason 'kgr not found in this shell' --evidence 'Previous command exit 127'
python3 stress_tests/tbps_baseline/audit.py run --session initial-prep --step A02 -- python -m kaggle_runner --help
python3 stress_tests/tbps_baseline/audit.py check --session initial-prep --step A02 --status blocked --evidence 'Interpreter probe failed; command event ID ...'
python3 stress_tests/tbps_baseline/audit.py report
```

Replace the examples with actual observations. `init` is idempotent, creates private local runtime files and does not change existing checkpoints. `event` supports decision, observation, incident, doc_read, intervention, handoff, approval_note. An approval note is documentation only and does NOT grant permission to execute. `check` accepts todo/in_progress/done/blocked/skipped/stale. Evidence is mandatory for done/blocked/skipped/stale. Every event has UTC timestamp, sequence ID, session, step, type and optional objective ID. `run` records a command start/end, elapsed seconds, exit status and redacted stdout/stderr files; the command return code is propagated. It is for approved safe commands only. It is not a sandbox, security boundary or authorization engine. Never pass tokens, `env`, `printenv`, credential-file reads, or unrestricted private output through it. Avoid shell wrappers for remote mutations; the logger rejects obvious live runner actions in preparation mode but cannot enforce all possible external effects. Use native Codex tool history privately as supplementary audit evidence if available; do not export hidden reasoning or raw secrets.

The logger uses a SQLite journal with transactional event append and exports JSONL/CSV/Markdown on `report`. Logs stay in `runtime/` (gitignored; not part of the workload package). Use unique session IDs on restart; the same audit journal and Work Objective should be reused. `report` does not mark checklist entries passed by itself. Evidence may be a command-event ID, file path/hash, work/experiment event ID, or a brief redacted observation. Record command decisions before use and outcomes after. Manual actions must receive equivalent audit events. The first AGENTS read predates logger initialization, so record its actual action/time immediately after initialization. The logger does not record actions executed outside it automatically; audit completeness requires agent compliance and operator comparison with Codex history.

## Review and test design

Phase 1: preparation only; stop at the approval boundary even if all prerequisites pass. Phase 2: after separate authorization, one full run, controlled handoff/restart, then remaining two; never intentionally kill or retry an ambiguous live submission. Phase 3: compare audit journal and runner ledger, grade with the operator rubric, extract improvements for skills/setup/CLI/docs. A successful research score does not erase orchestration friction. A blocked run is not a passed run. No live steps are prechecked.

Use `python3 -m unittest discover -s stress_tests/tbps_baseline -p 'test_audit.py' -v` to test the audit utility offline (operator can run before handing over the task). This does not run the workload or kgr tests.
