# Fast resource preflight (since 82e145a)

The old `kgr check` scanned up to 500 historical notebooks, and treated the
persisted `CANCEL_ACKNOWLEDGED` state as an occupied batch slot. Kaggle can show
**zero Active Events** while old notebooks still return that state. The Kaggle
CLI does not document an authoritative free-slot count at account level.

## Commands

```bash
kgr quota                                 # Only one quota API query; no notebooks
kgr active                                # Only runner-tracked jobs; refresh relevant statuses
kgr check --gpu t4x2 --required-gpu-hours 2  # Tracked jobs + account GPU quota
kgr run ./experiment.ipynb --gpu t4x2     # Same quick preflight before submit

# Deliberate, slower diagnostics that include jobs created outside this runner:
kgr active --remote
kgr check --remote --gpu t4x2 --required-gpu-hours 2
kgr active --remote --strict --max-seconds 120
```

`CANCEL_ACKNOWLEDGED` is **cancelling / ambiguous**, not `RUNNING` and not a
proven free slot. It appears separately under `cancelling` with a warning, and
no longer automatically makes the reported running count reach 2/2.
`CANCEL_REQUESTED` is still provisionally counted as in flight.

**Limitations:** The fast preflight checks only jobs in the local SQLite
history. It cannot detect notebooks started in the browser, notebooks managed
by a different machine, or interactive sessions. Its `has_slot` and `ok` fields
are *local admission heuristics*, never a guarantee; `slot_verified` is always
false. Kaggle decides whether it can accept the submitted job. `--remote` is a
best-effort history scan, **not** a platform-wide slot reservation or an
Active Events API. GPU quota checking remains strict; unknown or insufficient
quota blocks GPU submission.

If you have zero Active Events and receive a `CANCEL_ACKNOWLEDGED` status for
old notebooks, this version will not mistakenly count those as two running
batch jobs. If Kaggle rejects a submit due to actual concurrency, the runner
reports the error instead of retrying with another account.
