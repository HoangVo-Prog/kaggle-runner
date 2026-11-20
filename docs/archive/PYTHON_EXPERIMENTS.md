# Python experiments

Script submissions package a deterministic, size-limited source ZIP inside `run.py`, which is the Kaggle `code_file` for `kernel_type=script`. Source ZIP members retain project-relative paths, and the bootstrap creates `/kaggle/working/kgr_source` before invoking the entrypoint with `runpy.run_path`. `sys.path` includes the project root. Python packages in `src/` may require a proper package layout (`__init__.py`) or explicit import structure.

Use `kgr package inspect ./experiments/example --entry train.py --json` to check every file/hash; keep `requirements.txt` in the bundle if using `install_requirements`. Dependency installation requires `enable_internet=true` in the resources section; install is executed before user entrypoint. For standard `kgr run`, pass `--arg` repeatedly; for ExperimentSpecs, read structured parameters via `json.loads(os.environ['KGR_PARAMETERS_JSON'])`. The source fingerprint is available as `KGR_SOURCE_SHA256`.

Write `result.json` into `KGR_OUTPUT_DIR` and include experiment/attempt ID, seed, protocol, source hash, metric value/unit/direction, sample count and runtime. Remote `COMPLETE` by itself does not validate this output.
