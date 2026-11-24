# Operator-only historical baseline — do not show the evaluation agent

Source: original operator archive `runtime(1).zip`; retained separately, not included in patch. Archive verification during patch development: SQLite integrity `ok`; 87 journal rows match 87 JSONL records. Metrics reflect audited commands, not all tool calls or total elapsed wall time. Post-session cross-identity access incident is separate from the eight in-session incidents. V2 has not been executed.

| Metric | V1 | V2 |
|---|---:|---|
| Audit events | 87 | not_run |
| Incidents | 8 | not_run |
| Decisions | 11 | not_run |
| Completed audited commands | 11 | not_run |
| Failed audited commands | 3 | not_run |
| Audited CLI help lookups | 6 | not_run |
| Audited command elapsed seconds | 53.396 | not_run |
| Operator interventions | 1 | not_run |
| Preparation done / blocked / stale | 8 / 6 / 1 of 15 | not_run |
| Live done | 0/9 | not_run |
| Kaggle submissions | 0 | not_run |
| Immutable package prepared | No | not_run |
| Validated experiment specs | 0 | not_run |
| Persisted executable plan | No | not_run |
| SQLite integrity and JSONL agreement | Passed | not_run |
| Cross-identity operator artifact access | Failed after session | not_run |

Keep original A01–C05, D01–D09 and E01 IDs; use one-to-one comparison but disclose strengthened requirements in A01/A03/A04/B04/C03/C04/C05. Compare common step outcomes with their original evidence. Count failed completed commands using nonzero exit codes, not incident count; count help lookups among audited command starts. Count retries/workarounds/state drift/escalation separately in V2; V1 did not collect comparable structured counters. Categorize incidents after reading evidence as runner/testbed/environment/documentation/authorization/science/unconfirmed; separate expected gates from avoidable friction. Do not count a blocked scientific dependency as a bug or infer better performance from a changed denominator. Compare comparable audit coverage and independent operator access. Do not publish conclusions before V2 evidence exists.
