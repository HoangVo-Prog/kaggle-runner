# Testing strategy — correctness before autonomous execution

**Baseline:** commit `cff2a661a3e6357febd9c574158fd58d020781ac`. Existing tests use Python `unittest` and mocks; the repo also has opt-in Kaggle live smoke examples. The docs package does **not** execute remote submissions.

## Local tests (free; no remote API required)

```bash
cd /path/to/kaggle-runner
python -m pip install -e '.[kaggle]'
python -m unittest discover -s tests -v
```

Focus areas currently covered by test file names/source inspection:

| Test file | Intended regression area |
| --- | --- |
| `test_team_env.py`, `test_team_env_casing.py` | Shared `.env` team profiles, alias case/normalization, diagnostics and credential handling. |
| `test_team_profiles.py` | Multi-profile routing, token isolation, report metadata, two-profile execution mock. |
| `test_project_root_env.py` | Root `.env` resolution, precedence, path safety. |
| `test_slot_scenarios.py` | CPU+GPU same-account limit, third submission rejection, release after completion. |
| `test_fast_preflight.py`, `test_active_performance.py` | Local tracking, remote scan and timeout/performance behavior. |
| `test_kaggle_quota.py` | Official Kaggle quota formats and error handling. |
| `test_status_parser.py`, `test_legacy_reference.py` | Worker status states and stale owner ref recovery. |
| `test_gpu_smoke_example.py`, `test_p0.py`, `test_public_resources.py`, `test_workflow.py` | Packaging, code execution bootstrap, resources, smoke/dry-run and full CLI lifecycles. |

The exact number of tests may change; do not hardcode a historical count as evidence of a current branch passing. Run the tests on the exact commit being documented.

## Live smoke (two accounts; side effects)

```bash
python examples/team-env-multi-account/run_live_test.py \
  --accounts alice bob --gpu cpu --confirm-live
```

The script submits one CPU kernel under each profile, polls remote status, fetches artifacts, checks metadata and logs, and verifies a smoke artifact. `--confirm-live` is mandatory. This test consumes Kaggle compute, can encounter live quotas/network failures, and can leave running kernels if interrupted. **It is not included in normal unit-test discovery.**

There is also an opt-in GPU/CPU slot-limit smoke documented in `examples/slot-limit-test/README.md`. It intentionally occupies slots; run it only in an authorized window and inspect Active Events afterward.

## Recommended agent-focused contract tests (NOT ALL IMPLEMENTED)

| Priority | Scenario | Acceptance criterion |
| --- | --- | --- |
| P0 | Two agents concurrently schedule on the same account | Exactly two successful reservations and no third parallel push; no race. |
| P0 | Submit succeeds remotely but local process crashes | Reconciliation recovers the existing remote job; no duplicate submit. |
| P0 | Kaggle quota works but slot info is stale | Runner labels uncertainty; never claims a guaranteed slot. |
| P0 | Rejected/missing/revoked token | Structured denial, no fallback to another user's token, secret not printed. |
| P0 | `status=complete` but no metrics file | Experiment outcome is invalid output, not success. |
| P0 | Wrong result schema/NaN/Inf/mismatched seed or split | Metric validation fails with provenance preserved. |
| P0 | Status request 429/500/transient network failure | Bounded status retry; no duplicate push. |
| P0 | Profile alias typo and duplicated env keys | Error identifies expected key names without secret values. |
| P1 | Agent restart midway through monitoring | Resumes from durable ledger and canonical kernel ref. |
| P1 | Two machines share queue | Central reservation ensures consistent per-account capacity. |
| P1 | Log includes a fake token | All machine and human outputs redact it before persistence. |
| P1 | Several runs with comparable metrics | Stable aggregate with units, split, seed, confidence/statistics policy. |

## Definition of done for a future autonomous release

Before claiming AGENTS can fully manage experiments unattended, the project should demonstrate: (a) idempotent submission after process interruption, (b) atomic admission under concurrent workers, (c) explicit per-owner authorization, (d) versioned result schema validation, (e) consistent machine-readable error envelopes, (f) automatic resume/reconcile, and (g) end-to-end report generated from actual artifact values with traceable job/source metadata.

Tests should run in CI in three tiers: offline unit/contract tests on each push; hermetic integration tests against a fake Kaggle API; optional manual/live integration with two authorized profiles. **Never run live Kaggle submissions on every PR** without explicit approval and resource budgeting.
