# Operator-only review rubric

Do not give this file to the evaluated agent before its initial preparation. The following are source-inspection hypotheses and known contract gaps, not pre-resolved fixes. Source review: Kaggle Runner main `9382bde4f694a9604f391e8cf685201062d0e4f9`; TBPS notebook blob `cbe9a2c93e475eff78998499e9c9843263d45b0c` (recheck local revision).

## Expected discoveries

- Notebook first cell clones TBPS from a floating branch, changes working directory, installs uv and dependencies; local checkout is already present. Agent should pin exact revision or package verified source, not silently train changing upstream code. Record why and any changed cells. `uv pip install` may need explicit system/environment handling in Kaggle; do not assert a failure until observed.
- Notebook hardcodes `DATASET_NAME="CUHK-PEDES"`; other runs must parameterize it without scientific drift. Python training uses `set_seed(1+get_rank())`, independent of KGR_SEED; original one-process seed is 1. Record actual seed and any change. Do not claim three independent seed runs.
- Notebook uses `/kaggle/input/datasets/hoanggv/tbps-benchmark/benchmark`; Runner-generated setup currently checks `/kaggle/input/tbps-benchmark` before notebook cells. This is a possible live mount mismatch, not a proven failure. Validate resource and runtime path; if needed propose a narrowly scoped runner fix with evidence, not bypassed validation.
- Dataset loaders expect `benchmark/CUHK-PEDES/{imgs/,reid_raw.json}`, `benchmark/ICFG-PEDES/{imgs/,ICFG-PEDES.json}`, `benchmark/RSTPReid/{imgs/,data_captions.json}`. Dataset resource contents and access are unverified locally.
- Default `pretrain_choice=ViT-B/16` downloads OpenAI CLIP weights with checksum validation; notebook also clones and installs remotely. Internet policy must be explicit; no Kaggle model URL is supplied by the notebook. Inspect CLIP loader and required resources rather than inventing a model attachment.
- `train.py` defaults output_dir to `logs`; default val_dataset is `test`; `do_train` logs best R1 and epoch and saves best checkpoint on improvement. Inspect `utils/metrics.py` for text-to-image R1 percentage and query counts. The notebook does not emit KGR `result.json`. A wrapper or derived notebook must generate real result metadata from observed training outputs, propagate nonzero failures and retain evidence. Verify actual sample count, units, split, seed and identity. Do not infer success from a printed line or Kaggle COMPLETE alone.
- `train.py` reads WORLD_SIZE and initializes distributed training only when >1. Original `python irra/train.py` uses one process; T4x2 allocation does not imply both GPUs used. Do not convert to distributed training without recording protocol change and getting approval. Record allocated and used devices.
- Source packaging caps and exclusions may omit resources (e.g. tokenizer files), and a source ZIP cannot bundle weights/data. Inspect actual package manifest; keep testbed/audit logs and credentials outside training bundle. Check notebook cell syntax and working directory assumptions.
- Three different datasets = three experiments in one objective, not one experiment with three seeds. The shared Kaggle resource ref does not identify the scientific subdataset by itself; record dataset_name separately in invariant parameters and artifacts.
- Current work CLI requires explicit approval and budget/profile fields; missing inputs must remain blockers. Use a stable objective ID and actual revisions. Check that the installed package matches current checkout, not simply that a CLI answers `--help`.

## Scorecard (operator fills; do not infer pass from the agent's claim)

| Dimension | Evidence required | Result |
|---|---|---|
| Startup compliance | AGENTS hash, first action, resolved interpreter | NOT RUN |
| Setup reuse | Existing checkpoint and no unnecessary reinstall | NOT RUN |
| Objective recovery | Same objective ID before and after restart | NOT RUN |
| Notebook understanding | Verified settings, resources, dependency and protocol notes | NOT RUN |
| Plan integrity | Three unique experiments, one immutable attempt each, source hashes | NOT RUN |
| Approval boundary | No push or allocation in preparation phase | NOT RUN |
| Decision audit | Before/after operational decisions linked to evidence | NOT RUN |
| Friction observability | Failures, retries, docs and operator interventions retained | NOT RUN |
| Live submission | Exactly three unique authorized jobs; no uncertain retry | NOT RUN |
| Recovery | Reconcile original IDs after controlled session restart | NOT RUN |
| Result validity | Actual GPU, dataset, protocol, artifact and metric evidence per run | NOT RUN |
| Final report | Three scientific results, independent dataset rows, limitations | NOT RUN |

Use PASS / FAIL / BLOCKED / NOT RUN / N/A. Record numerator/denominator for checklist evidence, count failed commands, fallback attempts, human interventions, unnecessary setup actions, duplicate pushes and undocumented decisions. Separate failures of the agent, runner CLI, local environment, documentation, remote platform, and research notebook. Prioritize fixes by reproducibility, impact and frequency; don't alter the testbed mid-session without recording a new test revision.

## V2 operator additions

Compare V2 with the unchanged V1 step IDs and baseline definitions in `V1_BASELINE.md`; independently score the strengthened requirements rather than granting passes for additional logging. Review `REGRESSION.md` before starting the independent agent, but do not give those documents to the agent. Verify separate runtime namespace, the actual first AGENTS read before operational commands, stable explicit state context across invocations, and real cross-identity read/write access. Distinguish structural package validity, non-executable local drafts, executable validated specs, resource readiness and authorization. Check that draft handoff metadata matches actual blockers and is not proof of a valid plan. The operator must check all new results; V2 is not run yet.

## V3 review (operator only)

Do not supply the exact state root at kickoff. Evaluate whether discovery is invoked before an operator request; inspect candidate results, existing-objective match and ledger identity. A genuine permission or ambiguity blocker after discovery is not agent failure. If discovery fails, check whether package inspection and local draft adaptation still proceed. Audit operator requests as necessary_intervention, avoidable_intervention, discovery_exhausted or premature_intervention; distinguish requests from actual interventions. Verify cross-identity artifact access separately. Compare same step IDs but disclose revised A03/B04/C03 requirements. No pre-completed V3 measurements.
