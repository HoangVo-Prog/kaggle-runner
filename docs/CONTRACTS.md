# Experiment and runtime contracts

The canonical JSON Schemas live in `kaggle_runner/schemas/` and are included as package data in wheels. The same files are used by editable and wheel installations:

- [ExperimentSpec 1.0](../kaggle_runner/schemas/experiment_spec.schema.json)
- [ExperimentResult 1.0](../kaggle_runner/schemas/experiment_result.schema.json)

## ExperimentSpec

Required values include version, stable `experiment_id` and `attempt_id`, project, objective, source (`kind`, project-relative `root` and `entrypoint`), object parameters, nonnegative seed, protocol ID/split, explicit nonempty `authorization.allowed_profiles`, nonempty `expected_outputs` and a primary metric definition. Resources, dataset/model refs, a source hash/revision, approval requirement, result filename and budgets may further constrain the run. Validation also rejects unsafe paths, duplicate refs/outputs, credentials in parameter names and source hash drift.

The schema does not authorize a profile by itself; the submission layer enforces the allowlist. A changed immutable attempt must use a new attempt ID.

## Result and scientific validation

`result.json` must be an object with `schema_version="1.0"`, matching experiment and attempt IDs, `status="succeeded"`, matching seed/protocol/source SHA256/dataset split, finite typed metrics including the primary metric, positive `samples_evaluated`, nonnegative finite runtime and artifact declarations. Empty objects, arrays, `null`, malformed JSON, missing metrics and identity mismatches are invalid.

Remote `COMPLETE` only establishes worker completion. It does not establish scientific success. The fetched result and expected artifact files must validate before an attempt can be marked scientifically succeeded or included in aggregation.

## Packaging and runtime

`kgr package inspect` and submit use the same deterministic source selector and exclusion rules. Python source is embedded in a bounded archive; notebooks preserve the original cell sequence in the staged copy and may include selected companion files. The original notebook is not modified. Review the package report before submitting.

Both runtime modes receive `KGR_PARAMETERS_JSON`, `KGR_SEED`, `KGR_EXPERIMENT_ID`, `KGR_ATTEMPT_ID`, `KGR_SOURCE_SHA256` and `KGR_OUTPUT_DIR`. `KGR_SEED` is the top-level per-attempt ExperimentSpec seed; it stays separate from invariant hyperparameters in `KGR_PARAMETERS_JSON`, and the validated result must report the same seed. Write declared output files in `KGR_OUTPUT_DIR`. Explicit dataset and model refs are mounted by Kaggle; local private data is not uploaded automatically. Installing requirements requires internet access in the spec.

Schemas and validator are implemented and offline tested. Actual Kaggle mount paths, package install behavior and output fetches are unverified until a separately authorized live test.
