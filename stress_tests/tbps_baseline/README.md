# TBPS baseline stress test V2 — preparation only

`case.json` is a test case, not authorization or an executable ExperimentSpec. Use the existing `workspaces/tbps` checkout and existing objective `tbps-baseline-3gpu` if found in the operator-confirmed state. Never create a replacement ledger/objective to hide a permission failure. `runtime/` is immutable historical V1 evidence; V2 uses `runtime_v2/` (or an explicitly provisioned absolute directory outside V1). Do not read operator-only material during an independent test.

## Startup and audit

Read root `AGENTS.md` first. Immediately initialize the audit from repository root, before Runner commands. Use any existing Python 3.9+ interpreter for the stdlib-only audit tool; separately resolve the Runner interpreter following AGENTS.md. `--root` goes before the audit subcommand; `KGR_AUDIT_ROOT` is an alternative. Examples below use the default V2 directory and an operator-confirmed absolute `STATE` path; replace placeholders with actual values. On Windows use the native equivalent of your chosen interpreter and path quoting.

```bash
python3 stress_tests/tbps_baseline/audit.py init --session v2-prep-01 --objective tbps-baseline-3gpu
python3 stress_tests/tbps_baseline/audit.py run --session v2-prep-01 --step A02 -- python -m kaggle_runner --help
python3 stress_tests/tbps_baseline/audit.py bind-state --session v2-prep-01 --state-dir "$STATE" --evidence 'operator-confirmed existing path'
python3 stress_tests/tbps_baseline/audit.py run --session v2-prep-01 --step A03 -- python -m kaggle_runner --state-dir "$STATE" setup preflight --expected-state-dir "$STATE" --audit-dir "$AUDIT" --json
python3 stress_tests/tbps_baseline/audit.py run --session v2-prep-01 --step A03 -- python -m kaggle_runner --state-dir "$STATE" setup status --json
python3 stress_tests/tbps_baseline/audit.py run --session v2-prep-01 --step A04 -- python -m kaggle_runner --state-dir "$STATE" work list --active --json
python3 stress_tests/tbps_baseline/audit.py report
```

`init` probes create/read/write/rename/delete and scratch SQLite before creating a journal; it records AGENTS hash, Git revision, branch, tracked status, identity hint and interpreter. New sessions require new IDs; the journal and state binding persist. Keep one `--root` for all audit invocations. If Git rejects dubious ownership, startup tries only a per-command `-c safe.directory=<exact repository>`; never change global trust. Inspect startup evidence and tool history; logging a hash does not prove the agent read the instructions. If the exact state is inaccessible or the objective is unexpectedly absent, stop and ask the operator to confirm the path. Do not run offline setup verification until local directory preflight succeeds; do not re-install an existing environment.

`event` accepts decision/observation/incident/doc_read/intervention/handoff/approval_note/retry/workaround/state_drift/escalation; use `--category runner|testbed|environment|documentation|authorization|science|unconfirmed`. Add a decision before acting, and an observation or incident after. `check --step A01 --status done --evidence ...` requires actual evidence; live steps must stay todo. `run` logs completed and failed commands, elapsed time and redacted output; unknown Runner commands and obvious remote actions are refused. `--live` cannot authorize V2. This is defense in depth, not a sandbox or general command classifier. Never log credentials, tokens, secret-bearing arguments, unrestricted output, shell environment dumps, or hidden reasoning. Manual commands must have equivalent observable evidence. `report` exports an atomic-per-file snapshot from SQLite. Do not run parallel report exporters; the journal remains authoritative.

## Safe local preparation

Use `package inspect` before any remote authorization. Its legacy `safe_to_submit` field means structural packaging only: check `readiness` and unresolved dynamic dependencies. Local hashing, source adaptation in a separate copy, result writer fixtures, artifact/metric contract review and draft preparation do not require an authorized account. Do not modify the source notebook or change scientific protocol. Explicitly label incomplete drafts non-executable; `experiment validate` still enforces complete schema and authorization fields. If a persisted plan is not possible, write a redacted JSON handoff and use `work handoff OBJECTIVE_ID --file HANDOFF.json --expected-revision CURRENT --json` with the bound `--state-dir`. Supported fields are `facts`, `missing_inputs`, `pending_approvals`, `completed_steps`, `artifacts` (lists of strings), `blockers` (array of objects with `code`, `category`, `summary`, `evidence` list of strings), and `next_action` (nonempty string). No fake approvals; this changes metadata of an existing draft only. Read `work show` or `work resume` afterward and verify the revision and blocker fields.

## Directory accessibility

Private state, credentials and shared redacted audit artifacts have different access policies. Do not grant access to credentials to fix audit. The operator must provision the exact audit directory with appropriate inherited ACLs/group access for both identities. Windows Python does not request mode `0700` for shared audit dirs; Linux requests `0770`, subject to umask and ACL inheritance. Existing permissions are not changed. Run `audit.py --root <absolute-audit-dir> preflight` only against the new directory. It checks current process access, not human access. `setup preflight` probes the selected existing state and optional existing audit directory with isolated scratch files; it does not create a missing ledger. Operator must independently confirm the generated marker, journal and exports are accessible from the other identity (see operator instructions). If not, preserve evidence and stop. No blanket ACLs or privilege escalation.

## Review

The original 15 preparation, nine live and one review step IDs are retained for comparison; strengthened criteria must be noted. Operator-only baseline and regression review are separate from the independent agent. `python -m unittest discover -s stress_tests/tbps_baseline -p 'test_audit.py' -v` and `python -m unittest discover -s tests -p 'test_local_hardening.py' -v` use isolated fixtures only. Stage B requires a new explicitly approved protocol and is not performed in V2 preparation.
