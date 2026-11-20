# Native notebook experiments

Notebook submissions create `run.ipynb` while preserving all original user source cells and their ordering. A generated setup cell precedes user cells; it materializes bounded companion source files into `/kaggle/working/kgr_source`, sets the runtime environment, adds that directory to `sys.path` and optionally installs `requirements.txt` before any user cell executes. Source notebook is not overwritten or converted into a Python script.

Validation checks nbformat 4, Python kernelspec, cell structure and duplicate IDs. Embedded attachments and suspicious embedded credentials are blocked. The staged notebook clears prior execution counts and outputs. A large notebook or unsupported local resource is rejected before push. A notebook with `from src.model import ...` needs `src/model.py` explicitly selected for upload; use `kgr package inspect` to verify.

Arguments to notebooks are **not** Python `sys.argv`. Use `json.loads(os.environ['KGR_PARAMETERS_JSON'])`; write your scientific result into `os.environ['KGR_OUTPUT_DIR']`. Dataset and model refs appear in the same environment variables as script execution.
