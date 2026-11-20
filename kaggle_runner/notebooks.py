"""Build native Kaggle notebook kernels without converting cells to Python scripts."""
from __future__ import annotations

import json
from pathlib import Path

from .config import RunnerError


def stage_notebook(source: Path, target: Path, datasets: list[str], models: list[str]) -> list[dict]:
    if source.is_symlink() or not source.is_file() or source.suffix.lower() != ".ipynb":
        raise RunnerError("Notebook entry must be an existing, non-symlink .ipynb file")
    if source.stat().st_size > 25 * 1024 * 1024:
        raise RunnerError("Notebook exceeds 25 MiB; move large data to Kaggle Datasets")
    try:
        nb = json.loads(source.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise RunnerError(f"Invalid notebook JSON: {source}") from exc
    if not isinstance(nb, dict) or int(nb.get("nbformat", 0)) != 4 or not isinstance(nb.get("cells"), list):
        raise RunnerError("Notebook must be nbformat 4 with a list of cells")
    # Credential-free environment setup. Nothing from local .env is uploaded.
    script = (
        "import os, json, pathlib\n"
        "os.environ['KGR_OUTPUT_DIR'] = '/kaggle/working'\n"
        "os.environ['KGR_PROJECT_ROOT'] = '/kaggle/working'\n"
        "os.environ['KGR_DATASET_ROOT'] = '/kaggle/input'\n"
        f"_kgr_ds = {datasets!r}\n"
        f"_kgr_models = {models!r}\n"
        "os.environ['KGR_DATASET_REFS'] = json.dumps(_kgr_ds)\n"
        "os.environ['KGR_MODEL_REFS'] = json.dumps(_kgr_models)\n"
        "_ds_paths = {}\n"
        "for ref in _kgr_ds:\n"
        "    mount = pathlib.Path('/kaggle/input') / ref.split('/')[1]\n"
        "    if not mount.is_dir(): raise FileNotFoundError(f'Dataset mount not found: {ref}')\n"
        "    _ds_paths[ref] = str(mount)\n"
        "os.environ['KGR_DATASET_PATHS'] = json.dumps(_ds_paths)\n"
        "_model_paths = {}\n"
        "for ref in _kgr_models:\n"
        "    _, slug, framework, variation, version = ref.split('/')\n"
        "    root = pathlib.Path('/kaggle/input') / slug\n"
        "    found = next((p for p in root.glob('*/*/*') if p.is_dir() "
        "and p.parent.parent.name.casefold() == framework.casefold() "
        "and p.parent.name.casefold() == variation.casefold() and p.name == version), None)\n"
        "    if found is None: raise FileNotFoundError(f'Model mount not found: {ref}')\n"
        "    _model_paths[ref] = str(found)\n"
        "os.environ['KGR_MODEL_PATHS'] = json.dumps(_model_paths)\n"
    )
    cell = {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [],
            "source": script, "id": "kgr-setup"}
    nb["cells"].insert(0, cell)
    metadata = nb.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise RunnerError("Notebook metadata must be an object")
    metadata.setdefault("kernelspec", {"display_name": "Python 3", "language": "python", "name": "python3"})
    target.write_text(json.dumps(nb, ensure_ascii=False, indent=1), encoding="utf-8")
    return [{"path": source.name, "bytes": source.stat().st_size, "type": "notebook"}]
