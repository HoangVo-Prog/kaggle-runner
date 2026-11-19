# Test: one GPU job + one CPU job = two tracked slots

The **offline integration test** is the safest way to verify runner logic without
using Kaggle GPU quota:

```bash
python -m unittest discover -s tests -p 'test_slot_scenarios.py' -v
```

It simulates real CLI invocation and a Kaggle API, checks the SQLite ledger,
and asserts that the third attempt never invokes `kernels push`. It also checks
that completing one run frees a slot, as well as the CLI's compact output.

## Optional real Kaggle test (consumes GPU time)

Only do this if you want to exercise the live service. The `hold.py` script
sleeps for 180 seconds after Kaggle starts the job; Kaggle startup and queuing
times are additional. Stop if Kaggle itself rejects either of the first two
jobs. Each successful submit is a new **private** Kaggle notebook.

```bash
# Check local state and quota first.
kgr check --gpu t4x2 --required-gpu-hours 0.25

# Run these without --wait, so you can submit the next job.
kgr run ./examples/slot-limit-test/hold.py --gpu t4x2 --arg=180
kgr run ./examples/slot-limit-test/hold.py --gpu cpu --arg=180

# While both jobs are still running/queued:
kgr active
kgr check --gpu cpu

# MUST reject before submitting a third kernel:
kgr run ./examples/slot-limit-test/hold.py --gpu cpu --arg=30

# Show local ledger; should list only the two submitted jobs.
kgr jobs
```

Expected in the blocked case:

```text
BLOCKED - cannot submit
Batch jobs (runner jobs): 2/2 active
Reason:    Concurrent batch jobs 2/2
```

Once either remote run is COMPLETE, `kgr check --gpu cpu` can pass again.
This confirms **runner-tracked batch runs** only. A notebook started elsewhere,
interactive sessions, or a simultaneous push from a second machine is not
reliably covered by the local preflight. The Kaggle service remains the final
authority on whether a session can be created.

For debugging, append `--json` to any command. For a broad but potentially slow
remote history scan, use `kgr active --remote`.
