# Operator-only V2 regression matrix

Run in isolated temporary directories on Linux and native Windows using the actual Runner interpreter. No real job state or credentials are needed by the tests. A fixture pass does not establish cross-identity ACL behavior or remote readiness.

| ID | Test and expected evidence | Failure class | Verification |
|---|---|---|---|
| REG-01 | audit test_probe_permission_and_cleanup: scratch create/read/write/rename/delete, no leftovers; denied fixture yields failure | testbed/environment | automated + native platform |
| REG-02 | Human reads agent-created V2 marker and exports and writes/deletes own disposable file; agent reads human sentinel | environment | operator required, not_run |
| REG-03 | test_connection_lifecycle_rollback_and_unlink: connection closed after exception, transaction rollback, immediate unlink | testbed | automated + native Windows |
| REG-04 | test_init_resume, test_startup_and_export: distinct sessions, unchanged history, JSONL equality | testbed | automated |
| REG-05 | test_explicit_state_precedence_across_environments: explicit path overrides env/defaults | runner | automated |
| REG-06 | state binding test + preflight drift fixture: reject different absolute paths without creating ledger | runner/environment | automated + operator |
| REG-07 | test_explicit_work_report_and_draft_handoff: actual report path inside selected state | runner/environment | automated + native Windows |
| REG-08 | same test: no redirect state dir created, objective still readable | runner | automated |
| REG-09 | exact_directory_probe_and_cleanup: missing/denied path diagnostic, no fallback or ledger creation | runner/environment | automated |
| REG-10 | existing objective fixture; work resume reads same ID and revision | runner/agent | automated + operator confirms real existing ID |
| REG-11 | work handoff fixture: structured blocker/next action persisted and recovered while draft | runner | automated |
| REG-12 | notebook and script inspection fixture: structural ok, external indicators, readiness not_verified | runner | automated |
| REG-13 | startup_and_export records hash/revision/identity; compare actual first-read ordering with tool history | agent/testbed | automated evidence + operator review |
| REG-14 | test_redaction: known synthetic token absent in journal; manually avoid all real secrets | testbed/agent | automated + operator review |
| REG-15 | live_blocked and state_binding_and_live_blocked: no command launch for submit/execute/online/approval flag; live steps todo | testbed/authorization | automated + tool history |
| REG-16 | test_history_protected: existing V1/unversioned root rejected and bytes unchanged; verify archive separately | testbed | automated + operator |

Run `python -m unittest discover -s stress_tests/tbps_baseline -p 'test_audit.py' -v`, `python -m unittest discover -s tests -p 'test_local_hardening.py' -v`, then `python -m unittest discover -s tests -v`. Review platform-specific results and preserve redacted evidence. No V2 evaluation score is populated by these fixture tests.
