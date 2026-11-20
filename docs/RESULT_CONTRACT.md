# Experiment result contracts and reporting

This page separates **what `kgr` currently saves** from the **additional schema an autonomous experiment agent needs**.

## Current: job and download metadata (IMPLEMENTED)

`kgr run` stages `jobs/<job_id>/manifest.json` containing, among others, `job_id`, `kernel_ref`, source/revision, entry, GPU request, dataset/model refs, file/source hash, CLI args, and account profile/owner/email. SQLite stores job status and account metadata. `kgr fetch` writes `kgr_download_manifest.json` with downloaded file names, sizes and SHA256 digests. These are provenance/inventory records, **not** a standardized metrics table.

Current `kgr report` includes status counts and per-account job summaries. It does **not** compute accuracy, loss, F1, runtime comparisons or scientific conclusions from arbitrary outputs. Any agent claiming otherwise is over-interpreting the CLI.

## Proposed: `experiment_spec.json` (NOT IMPLEMENTED)

Future agents should submit an immutable experiment definition (validate against versioned JSON Schema). Example with only synthetic data:

```json
{
  "schema_version": "1.0",
  "experiment_id": "smoke-2026-11-20-001",
  "project": "example-classifier",
  "objective": "Confirm data loading and train a tiny baseline",
  "source": {
    "git_url": "https://github.com/example-lab/example-classifier",
    "git_commit": "REPLACE_WITH_FULL_COMMIT_SHA",
    "entrypoint": "train.py",
    "source_sha256": null
  },
  "inputs": {
    "datasets": ["example/dataset-name"],
    "models": [],
    "split": "validation"
  },
  "parameters": {"epochs": 3, "seed": 42},
  "compute": {
    "gpu": "t4x2",
    "required_gpu_hours": 2.0,
    "max_runtime_minutes": 120,
    "max_retries": 0
  },
  "authorization": {
    "allowed_profiles": ["alice"],
    "allow_internet": false,
    "requires_submit_approval": true
  },
  "expected_outputs": ["experiment_result.json"],
  "primary_metric": {"name": "accuracy", "direction": "maximize"}
}
```

The `authorization` block is **not** a credential. It is a policy/allowlist to be enforced by a future scheduler. Do not assume passing it to the current CLI has any effect. If the source is Git, pin a commit, not only a moving branch; for datasets, capture version/hash whenever the Kaggle API exposes one.

## Proposed: `experiment_result.json` (NOT IMPLEMENTED as a runner-wide schema)

An experiment's code should write a **single, validated JSON document** to the Kaggle working/output directory; this file is then retrieved with `kgr fetch`. Example:

```json
{
  "schema_version": "1.0",
  "experiment_id": "smoke-2026-11-20-001",
  "run_id": "sha256-or-uuid-from-orchestrator",
  "status": "succeeded",
  "seed": 42,
  "dataset": {"ref": "example/dataset-name", "split": "validation"},
  "metrics": {
    "accuracy": {"value": 0.8125, "unit": "fraction", "higher_is_better": true},
    "loss": {"value": 0.487, "unit": "nats", "higher_is_better": false}
  },
  "samples_evaluated": 160,
  "runtime_seconds": 121.3,
  "artifacts": ["predictions.jsonl"],
  "warnings": []
}
```

Recommended validation invariants:

- Exact `schema_version`, required keys, finite numeric metric values (reject NaN/Inf), metric units and direction.
- `experiment_id` and `run_id` must match the submission plan / ledger; do not trust an arbitrary downloaded JSON claiming a different job identity.
- Dataset version/split, seed and evaluation protocol must match before comparing runs.
- Artifact paths must be relative to an isolated download folder; reject `..`, absolute paths and symlinks on extraction.
- Require `status=succeeded` **and** Kaggle terminal state `complete` to label a run scientifically successful.
- Record SHA256 checksum and source file for every reported metric.

## How experiment code can emit metrics today

Your own script can write a project-specific JSON output without waiting for future runner code:

```python
import json
from pathlib import Path

out = Path('/kaggle/working/experiment_result.json')
result = {
    'schema_version': '1.0',
    'experiment_id': 'REPLACE_WITH_EXPERIMENT_ID',
    'run_id': 'REPLACE_WITH_RUN_ID',
    'status': 'succeeded',
    'seed': 42,
    'dataset': {'ref': 'example/dataset-name', 'split': 'validation'},
    'metrics': {'accuracy': {'value': 0.81, 'unit': 'fraction', 'higher_is_better': True}},
    'samples_evaluated': 100,
    'runtime_seconds': 12.0,
    'artifacts': [],
    'warnings': [],
}
out.write_text(json.dumps(result, indent=2), encoding='utf-8')
```

This sample only produces a file; **the current `kgr` does not automatically validate or aggregate it**. The agent must explicitly fetch and parse it.

## Proposed: final agent report

A reliable report should have these sections:

1. **Question / objective:** what was tested, compared, and why.
2. **Run inventory:** every attempted/blocked/failed/completed job with account alias, `job_id`, Kaggle URL and timestamp.
3. **Reproducibility:** source commit/hash, entrypoint, input dataset/model version, seed, parameters, machine type, code/dependency environment.
4. **Resource utilization:** requested vs observed GPU mode, quota snapshot, per-profile slot policy, runtime and exceptions. Do not claim exact GPU hours used unless measured.
5. **Validated metrics:** per-run values, units, metric definition, dataset split and artifact hash.
6. **Aggregate analysis:** mean/std/CI only with justified comparable repeats; never average across different protocols or incomplete runs.
7. **Failure analysis:** not-submitted vs submission error vs kernel failure vs invalid output vs unknown remote status.
8. **Conclusions and limitations:** evidence-backed interpretation, remaining uncertainty, explicit next actions.

A report may include **both** infrastructure status (`kgr report`) and experiment metrics (separately parsed outputs), but must not conflate them.

## Proposed machine-facing interfaces

```bash
# THESE COMMANDS DO NOT EXIST IN THE CURRENT REPOSITORY:
kgr experiment validate experiment_spec.json --json
kgr results validate JOB_ID --schema experiment-result-v1 --json
kgr results aggregate --experiment-id smoke-2026-11-20-001 --json
kgr report experiment smoke-2026-11-20-001 --format md
```

Implement these only after versioned JSON schemas and validation tests exist. Until then, build a separate agent-side adapter that drives current `kgr` commands and validates known experiment-specific result files.
