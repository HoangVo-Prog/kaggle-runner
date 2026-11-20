# Experiment authoring (candidate v1.0)

An `ExperimentSpec` is an immutable unit of work. Every intentionally new execution **must** use a distinct `attempt_id` even if the same experiment, seed and code are reused. Never retry a `submit` command to recover an ambiguous push; use `experiment resume`, `reconcile`, `experiment show` and the stored `kernel_ref`.

Required top-level fields: `schema_version="1.0"`, `experiment_id`, `attempt_id`, `project`, `objective`, `source`, `parameters`, nonnegative integer `seed`, `protocol.id`, `protocol.dataset_split`, `authorization.allowed_profiles`, nonempty `expected_outputs` and `primary_metric` with name, unit and direction. Optional: Kaggle datasets, pinned models, `resources`, `result_file` and budgets. See `schemas/experiment_spec.schema.json` and example specs.

`source.root` is resolved relative to the location of the JSON specification. `source.sha256` and `source.revision` may pin source state. `source.include` / `source.exclude` are project-relative globs; security-deny lists are never overridden. `parameters` must be JSON-serializable and contain no credentials; use Kaggle's standard account token configuration outside the spec.

Recommended workflow:

```bash
kgr package inspect ./experiment --entry train.py --json
kgr experiment validate ./experiment/experiment.json --json
kgr experiment submit ./experiment/experiment.json --account alice --approve --json
kgr experiment show cpu-smoke --json
kgr reconcile --experiment-id cpu-smoke --json
kgr experiment resume cpu-smoke --wait --max-wait-minutes 45 --json
kgr results validate JOB_ID --json
kgr results aggregate --experiment-id cpu-smoke --json
kgr report experiment cpu-smoke --format md
```

The `--approve` switch is required only when the spec's authorization explicitly sets `requires_submit_approval=true`. `--account auto` considers only authorized profiles declared by the spec. Local reservations are atomic only for processes sharing the same SQLite file; Kaggle itself remains authoritative.

When a source hash mismatch occurs, do not edit an existing attempt and reuse its ID. Freeze the intended source and create a new attempt. When `unknown_remote` occurs, stop submissions and request remote verification using the original account token. Never manually release an uncertain lease based only on expiry.
