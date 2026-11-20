# Source packaging contract (candidate v1.0)

Both `python` and native `notebook` are first-class source kinds. Source selection is rooted at `source.root` **relative to the ExperimentSpec JSON file**, never at the CLI current directory. `source.entrypoint` is a project-relative path with `.py` or `.ipynb` suffix; traversal, missing files and entrypoint symlinks are blocked.

Use `kgr package inspect PROJECT --entry train.py --json` or `kgr package inspect PROJECT/experiment.ipynb --json` before any remote action. Inspect reports exact file paths, byte sizes, SHA256, source revision, archive checksum, code_file, included/excluded rules and attached resource references. The same `source_files` + `pack_source` selector is used for inspection, dry-run metadata and real submission. To specify companion files, use `source.include` and `source.exclude` patterns, or `.kaggle-runnerignore`; security exclusions always take precedence. A missing/excluded entrypoint is a blocking error.

**What is uploaded:** The Kaggle push staging folder contains `kernel-metadata.json` and exactly one executable `code_file` (`run.py` for Python, `run.ipynb` for notebook). Python `run.py` embeds a bounded deterministic ZIP containing the selected `.py` and small config/source assets. Notebook `run.ipynb` preserves original user cell order and source while prepending one deterministic setup cell with an embedded ZIP containing *companion files only* (original `.ipynb` is not duplicated in the companion ZIP). Stale notebook outputs are removed from the staged copy; the original notebook is not modified.

**What stays local:** `.env`, `.env.*`, `kaggle.json`, private keys, symlinks, repo `.git`, virtualenvs, local `data/`, models, checkpoints, caches, output folders and oversized/non-whitelisted assets are not bundled. Store large datasets and model weights as Kaggle Dataset/Model resources, not source. A `.kaggle-runnerignore`/`exclude` rule may exclude additional files. Inspect output before submission; static inspection cannot prove that arbitrary source content contains no hardcoded secrets.

**Kaggle runtime:** Both kinds set `KGR_PROJECT_ROOT=/kaggle/working/kgr_source`, `KGR_OUTPUT_DIR=/kaggle/working`, `KGR_DATASET_PATHS`, `KGR_MODEL_PATHS`, `KGR_DATASET_REFS`, `KGR_MODEL_REFS`, `KGR_EXPERIMENT_ID`, `KGR_ATTEMPT_ID`, `KGR_PARAMETERS_JSON`, `KGR_SOURCE_SHA256` and `KGR_RESULT_FILENAME`. The runner adds the extracted root to Python's module path. Kaggle mounts attached data and model resources read-only under `/kaggle/input`, identified by their canonical refs. Both modes support `requirements.txt` when `install_requirements` and internet are explicitly enabled. Only script mode supports positional `--arg`; notebooks must consume parameters via environment JSON.

**What is downloaded:** `kgr fetch` or `kgr experiment resume` retrieves Kaggle kernel output files to a job-specific directory and hashes every file into `kgr_download_manifest.json`. The scientific result is separate from remote worker status; read [RESULT_CONTRACT_AGENT.md](RESULT_CONTRACT_AGENT.md).

## Four supported layouts

- Single script: `train.py` (entrypoint `train.py`).
- Multi-module Python project: `train.py`, `src/model.py`, `configs/train.yaml`, optional `requirements.txt` (entrypoint `train.py`; include rules must cover companion files).
- Standalone notebook: `experiment.ipynb` (entrypoint `experiment.ipynb`).
- Notebook with local imports: `experiment.ipynb`, `src/model.py`, `requirements.txt` (entrypoint `experiment.ipynb`; companion files zipped before user cells run).

For example, see `examples/agent_python` and `examples/agent_notebook`. Do not pass arbitrary environment secrets through the experiment specification or notebook cells.
