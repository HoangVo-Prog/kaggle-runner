# TBPS baseline stress test V3 — preparation only

Same research task and 25 checklist IDs as V1/V2. The independent agent must discover the existing objective `tbps-baseline-3gpu` without being given the state root. Do not read historical runtime artifacts or operator-only materials. Preserve `runtime/` and `runtime_v2/`; V3 defaults to `runtime_v3/` (or an explicitly provisioned absolute shared directory). No remote API, GPU allocation, approval or training.

## Minimal startup (from Runner checkout root)

Read root `AGENTS.md` **first**. Initialize a fresh audit before Runner commands, using any existing Python 3.9+ interpreter (the Runner requires Python 3.11+). In PowerShell the same commands work with `python` replaced by the verified executable; if it is a quoted absolute path use PowerShell's `&` call operator. Do not quote a directory in place of `audit.py`.

```bash
python stress_tests/tbps_baseline/audit.py init --session v3-prep-01 --objective tbps-baseline-3gpu
python stress_tests/tbps_baseline/audit.py kgr --step A02 -- --help
python stress_tests/tbps_baseline/audit.py discover
```

Resolve the correct Runner interpreter following AGENTS.md. If different from audit Python, pass `--python ABSOLUTE_VERIFIED_PYTHON` to `discover` and to stateless `kgr` calls before binding. No installation or environment recreation. Each command after `init` infers the session/objective **only when exactly one initialized session exists**. After a restart initialize a new session, and pass `--session NEW_ID` to all subsequent commands. Keep the same `--root` (or `KGR_AUDIT_ROOT`) if overriding the default.

`discover` invokes `kgr setup discover --objective-id ... --remember --json`, logs candidate evidence and binds only a unique verified existing objective. It probes an existing local bootstrap pointer, configured paths, env and bounded user-home/platform hints, not other users' profiles or the whole disk. The process needs authorized filesystem access; it cannot bypass a Windows sandbox ACL. Discovery only reads the ledger and optionally writes a secret-free local `.kgr-bootstrap.json` pointer, never a new ledger. A matching objective and stable file identity are evidence, not remote authorization. An inaccessible or ambiguous candidate, corrupt/moved pointer or replaced ledger fails closed. A previously confirmed path can be passed to `discover --state-dir ABSOLUTE_PATH` only when it is actually confirmed; the legacy `bind-state` path-only command does not establish a verified binding.

If discovery succeeds:

```bash
python stress_tests/tbps_baseline/audit.py kgr --step A03 -- setup preflight --json
python stress_tests/tbps_baseline/audit.py kgr --step A03 -- setup status --json
python stress_tests/tbps_baseline/audit.py kgr --step A04 -- work show tbps-baseline-3gpu --json
```

The shorthand injects the verified absolute state path before the Runner subcommand, and the expected state and audit directories for preflight. It checks the ledger identity before stateful calls. If preflight fails, do not continue stateful operations. Reuse the Setup Checkpoint; offline verification only for missing/stale checks after successful preflight. A known objective must not be recreated. For linked attempts requiring remote reconciliation, record the blocker; remote reconciliation is NOT authorized during preparation.

## Independent local preparation

Even if discovery, preflight or objective recovery fails, continue offline source inspection, hashing, local derived-copy adaptation, scientific result-contract review, fixtures and incomplete draft specs. The following needs no state binding:

```bash
python stress_tests/tbps_baseline/audit.py kgr --step B04 -- package inspect workspaces/tbps/irra/notebooks/kaggle_baseline.ipynb --json
python stress_tests/tbps_baseline/audit.py kgr --step C03 -- experiment validate PATH_TO_COMPLETE_SPEC --json
```

A structurally valid package is not execution readiness. Do not fabricate budget, account authorization, dataset versions, metric values or other missing inputs. An incomplete draft is non-executable, even if the scientific parts are locally verified. Preserve the original notebook and training protocol. If the ledger is accessible, persist real structured blockers via `work handoff OBJECTIVE_ID --file HANDOFF.json --expected-revision CURRENT --json` and re-read objective. Otherwise write redacted local handoff with evidence and next action; do not claim persistence to Runner. See TASK.md for full constraints.

## Audit and operator requests

Use `audit.py event --kind decision --step A03 --summary ... --reason ... --evidence ...` before actions and observation/incident after. Use `audit.py run --step ID -- COMMAND ARG ...` for non-Runner commands, or log equivalent manual evidence. Use `audit.py check --step ID --status done --evidence ...` only on actual evidence. For questions to operator, log `--kind operator_request --request-class necessary_intervention|avoidable_intervention|discovery_exhausted|premature_intervention` with the attempted discovery and precise blocker. Do not count a question as an actual operator intervention unless one occurred. Record concise operational rationales, not private chain-of-thought. No raw credentials, environment dumps, secrets, or unrestricted output. UTF-8 is forced for Python subprocesses; arbitrary external programs may emit other encodings. The audit is defense in depth, not a sandbox.

```bash
python stress_tests/tbps_baseline/audit.py report
```

Operator must independently verify access to redacted runtime artifacts. Do not grant blanket permissions or modify private state/credentials. Preserve V1/V2 and the V3 journal; never merge or pre-mark evidence. Review the same 15 preparation/9 live/1 review step IDs, with explicit criteria changes documented in REGRESSION.md. Stage B requires separate explicit authorization, never self-approve.

## Regression verification (operator or patch-development session only)

```bash
python -m unittest discover -s tests -p 'test_state_discovery*.py' -v
python -m unittest discover -s stress_tests/tbps_baseline -p 'test_audit.py' -v
python -m unittest discover -s tests -v
```

These use isolated fixtures. Test Windows native separately; Linux success cannot establish cross-identity Windows accessibility. Do not run V3 as part of patch validation.
