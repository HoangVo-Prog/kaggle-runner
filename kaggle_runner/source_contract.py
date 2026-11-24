"""Offline, auditable source contract shared by inspection and native notebook staging.

No Kaggle credentials, network calls, mutable current-directory assumptions, or
implicit inclusion of binary training assets. Python entrypoints use the existing
packaging.pack_source implementation. Notebook companions use the same selector.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import zipfile

from .config import RunnerError
from .packaging import (pack_source, source_files, git_revision, EXCLUDE_DIRS,
                        EXCLUDE_FILES, EXCLUDE_SUFFIXES)

VERSION = "1.0"
MAX_NOTEBOOK_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_BYTES = 3 * 1024 * 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(obj: object) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def resolve_source(path: str | Path, root: str | Path | None = None,
                   entry: str | None = None) -> tuple[Path, str, str]:
    """Return (resolved_root, project-relative_entry, kind), rejecting traversal."""
    target = Path(path).expanduser()
    if target.is_file():
        relative = entry or target.name
        base = Path(root).expanduser() if root else target.parent
        candidate = base / relative if entry else target
    elif target.is_dir():
        if not entry:
            raise RunnerError("Source directory requires an explicit --entry")
        base = Path(root).expanduser() if root else target
        relative = entry
        candidate = base / entry
    else:
        raise RunnerError(f"Missing source: {target}")
    base = base.resolve()
    path_rel = Path(relative)
    if path_rel.is_absolute() or ".." in path_rel.parts or not relative or path_rel.suffix.lower() not in {".py", ".ipynb"}:
        raise RunnerError("Entrypoint must be a project-relative .py or .ipynb without '..'")
    if candidate.is_symlink():
        raise RunnerError("Source entrypoint symlinks are forbidden")
    resolved = candidate.resolve()
    if not resolved.is_file() or not resolved.is_relative_to(base):
        raise RunnerError("Entrypoint missing or outside source root")
    return base, path_rel.as_posix(), "notebook" if path_rel.suffix.lower() == ".ipynb" else "python"


def read_notebook(source: Path) -> dict:
    if source.is_symlink() or source.stat().st_size > MAX_NOTEBOOK_BYTES:
        raise RunnerError("Unsafe or excessively large notebook")
    try:
        nb = json.loads(source.read_text(encoding="utf-8"),
                        parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
    except (ValueError, UnicodeError, OSError) as exc:
        raise RunnerError("Invalid notebook JSON") from exc
    if not isinstance(nb, dict) or nb.get("nbformat") != 4 or not isinstance(nb.get("cells"), list):
        raise RunnerError("Notebook must be nbformat 4 and contain a cells list")
    meta = nb.get("metadata", {})
    if not isinstance(meta, dict):
        raise RunnerError("Notebook metadata must be an object")
    ks = meta.get("kernelspec", {"language": "python"})
    if not isinstance(ks, dict) or str(ks.get("language", "python")).casefold() != "python":
        raise RunnerError("Only Python-kernel notebooks are supported")
    def _credential_metadata(value: object) -> bool:
        if isinstance(value, dict):
            return any(bool(re.search(r"(?i)(?:password|secret|api.?token|private.?key|credential)", str(k)))
                       or _credential_metadata(v) for k, v in value.items())
        if isinstance(value, list):
            return any(_credential_metadata(v) for v in value)
        return False

    if _credential_metadata(meta):
        raise RunnerError("Notebook metadata contains credential-like fields")
    seen_ids: set[str] = set()
    for i, cell in enumerate(nb["cells"]):
        if not isinstance(cell, dict) or cell.get("cell_type") not in {"code", "markdown", "raw"}:
            raise RunnerError(f"Invalid notebook cell {i}")
        if not isinstance(cell.get("metadata", {}), dict):
            raise RunnerError(f"Invalid metadata in notebook cell {i}")
        code = cell.get("source", "")
        if not (isinstance(code, str) or isinstance(code, list) and all(isinstance(x, str) for x in code)):
            raise RunnerError(f"Invalid source in notebook cell {i}")
        cell_id = cell.get("id")
        if cell_id is not None:
            if not isinstance(cell_id, str) or not 1 <= len(cell_id) <= 64 or cell_id in seen_ids:
                raise RunnerError("Invalid or duplicate notebook cell IDs")
            seen_ids.add(cell_id)
        if cell.get("attachments"):
            raise RunnerError("Notebook embeds attachments; move them to a Kaggle dataset")
        if _credential_metadata(cell.get("metadata", {})):
            raise RunnerError("Notebook cell metadata contains credential-like fields")
        if cell["cell_type"] == "code":
            if not isinstance(cell.get("outputs", []), list):
                raise RunnerError(f"Invalid outputs in notebook cell {i}")
            # Executable code must be screened conservatively for pasted credentials.
            body = "".join(code) if isinstance(code, list) else code
            if re.search(r"(?i)(?:KAGGLE_API_TOKEN|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----)\s*[=:]\s*['\"]?\S{10}", body):
                raise RunnerError("Notebook contains apparent embedded credentials")
    return nb


def file_manifest(root: Path, entry: str, *, include: list[str] | None = None,
                  exclude: list[str] | None = None) -> list[dict]:
    selected = source_files(root, entry, include=include, exclude=exclude)
    out = []
    for rel in selected:
        path = root / rel
        data = path.read_bytes()
        out.append({"path": rel.as_posix(), "bytes": len(data), "sha256": digest(data)})
    return out


def excluded_paths(root: Path, included: list[dict]) -> list[dict]:
    """Audit exclusion decisions without reading excluded data/credentials.

    Ignored directory subtrees appear as one directory entry. The full scan is
    bounded: unusually large project trees should be narrowed with source roots.
    """
    included_set = {item["path"] for item in included}
    removed: list[dict] = []
    scanned = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        parent = Path(current)
        for name in sorted(list(dirs)):
            scanned += 1
            directory = parent / name
            relative = directory.relative_to(root).as_posix()
            if name in EXCLUDE_DIRS or directory.is_symlink():
                removed.append({"path": relative + "/", "reason": "excluded_directory_or_symlink"})
                dirs.remove(name)
        for name in sorted(names):
            scanned += 1
            candidate = parent / name
            relative = candidate.relative_to(root).as_posix()
            if relative not in included_set:
                reason = "excluded_by_rules_or_type"
                if candidate.is_symlink():
                    reason = "symlink"
                elif name.lower() in EXCLUDE_FILES or name.lower().startswith(".env.") or name.lower().endswith(".local.json"):
                    reason = "credential_filename"
                elif candidate.suffix.lower() in EXCLUDE_SUFFIXES:
                    reason = "binary_or_weight_extension"
                removed.append({"path": relative, "reason": reason})
        if scanned > 10000:
            raise RunnerError("Too many files to audit safely; use a smaller project root")
    return sorted(removed, key=lambda row: row["path"])


def source_hash(files: list[dict]) -> str:
    """Canonical source identity independent of ZIP timestamp or local absolute path."""
    return digest(canonical(files))


def inspect_source(path: str | Path, *, root: str | Path | None = None,
                   entry: str | None = None, max_mb: int = 3,
                   dataset_refs: list[str] | None = None,
                   model_refs: list[str] | None = None,
                   include: list[str] | None = None, exclude: list[str] | None = None) -> dict:
    base, rel, kind = resolve_source(path, root, entry)
    if not 1 <= max_mb <= 25:
        raise RunnerError("max_mb must be between 1 and 25")
    # Structural package validity does not establish scientific or execution readiness.
    if kind == "notebook":
        nb = read_notebook(base / rel)
        code = "\n".join("".join(c.get("source", "")) for c in nb["cells"] if c["cell_type"] == "code")
    else:
        code = (base / rel).read_text(encoding="utf-8", errors="replace")
    bundle, files = pack_source(base, rel, max_mb * 1024 * 1024,
                                include=include, exclude=exclude)
    notebook_comps = [x["path"] for x in files if x["path"] != rel] if kind == "notebook" else []
    excluded = excluded_paths(base, files)
    indicators = []
    for name, pattern in (("external_source", r"(?i)\bgit\s+clone\b"),
                          ("runtime_install", r"(?i)\b(?:pip|conda|uv)\s+(?:pip\s+)?install\b"),
                          ("dataset_path", r"/kaggle/input(?:/|\b)"),
                          ("network_resource", r"(?i)https?://")):
        if re.search(pattern, code): indicators.append(name)
    if "KGR_RESULT_FILENAME" not in code and "result.json" not in code:
        indicators.append("result_writer_not_observed")
    return {
        "schema_version": VERSION, "ok": True, "safe_to_submit": True,
        "safe_to_submit_scope": "structural_package_only_legacy_field",
        "readiness": {"structural_package": "verified", "reproducibility": "not_verified",
                      "experiment": "not_verified", "execution_authorization": "not_checked",
                      "resource_access": "not_checked", "static_indicators": indicators,
                      "dynamic_dependencies": "unresolved_by_static_inspection"},
        "root": str(base), "entrypoint": rel, "source_kind": kind,
        "source_revision": git_revision(base), "source_sha256": source_hash(files),
        "source_zip_sha256": digest(bundle), "source_zip_bytes": len(bundle),
        "files": files, "include_rules": include or [], "exclude_rules": exclude or [],
        "excluded": excluded,
        "excluded_policy": "secure defaults + .kaggle-runnerignore + explicit exclude_rules",
        "required_dependencies": ["requirements.txt"] if any(f["path"] == "requirements.txt" for f in files) else [],
        "notebook_companions": notebook_comps,
        "dataset_refs": dataset_refs or [], "model_refs": model_refs or [],
        "kaggle_kernel_type": "notebook" if kind == "notebook" else "script",
        "kaggle_code_file": "run.ipynb" if kind == "notebook" else "run.py",
        "kaggle_metadata": {"code_file": "run.ipynb" if kind == "notebook" else "run.py",
                            "kernel_type": "notebook" if kind == "notebook" else "script",
                            "language": "python", "is_private": "true",
                            "dataset_sources": dataset_refs or [],
                            "model_sources": model_refs or []},
        "warnings": (["Notebook code must read KGR_PARAMETERS_JSON; --arg is script-only"] if kind == "notebook" else []),
        "errors": [],
    }


def _make_zip(items: list[tuple[str, bytes]]) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=8) as z:
        for path, data in sorted(items):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
    return target.getvalue()


def stage_agent_notebook(source: Path, target: Path, *, root: Path, entry: str,
                         datasets: list[str], models: list[str], install_requirements: bool = False,
                         experiment_id: str = "", attempt_id: str = "", parameters: dict | None = None,
                         seed: int | None = None,
                         expected_result: str = "result.json", max_bytes: int = MAX_BUNDLE_BYTES,
                         include: list[str] | None = None,
                         exclude: list[str] | None = None) -> list[dict]:
    """Preserves user cell order/content, prepends deterministic setup and bundles safe companions."""
    source = source.resolve()
    root = root.resolve()
    if not source.is_relative_to(root):
        raise RunnerError("Notebook must be inside project root")
    original = read_notebook(source)
    files = file_manifest(root, entry, include=include, exclude=exclude)
    companions = [(f["path"], (root / f["path"]).read_bytes()) for f in files if f["path"] != entry]
    bundle = _make_zip(companions)
    if len(bundle) > max_bytes:
        raise RunnerError("Notebook companion source exceeds upload size limit")
    params = canonical(parameters or {}).decode("utf-8")
    source_digest = source_hash(files)
    setup_id = "kgr-" + digest((source_digest + experiment_id + attempt_id).encode())[:16]
    existing = {c.get("id") for c in original["cells"]}
    while setup_id in existing:
        setup_id = "kgr-" + digest(setup_id.encode())[:16]
    setup = f'''# Kaggle Runner generated setup; user notebook remains unchanged.
import base64, io, json, os, pathlib, sys, subprocess, zipfile
_kgr_project = pathlib.Path('/kaggle/working/kgr_source')
_kgr_project.mkdir(parents=True, exist_ok=True)
_kgr_zip = zipfile.ZipFile(io.BytesIO(base64.b64decode({base64.b64encode(bundle).decode()!r})))
for _item in _kgr_zip.infolist():
    _rel = pathlib.PurePosixPath(_item.filename)
    if _rel.is_absolute() or '..' in _rel.parts or _item.is_dir():
        raise ValueError('Unsafe companion archive member')
_kgr_zip.extractall(_kgr_project)
sys.path.insert(0, str(_kgr_project))
os.chdir(_kgr_project)
os.environ['KGR_PROJECT_ROOT'] = str(_kgr_project)
os.environ['KGR_OUTPUT_DIR'] = '/kaggle/working'
os.environ['KGR_DATASET_ROOT'] = '/kaggle/input'
os.environ['KGR_EXPERIMENT_ID'] = {experiment_id!r}
os.environ['KGR_ATTEMPT_ID'] = {attempt_id!r}
os.environ['KGR_SEED'] = {str(seed if seed is not None else '')!r}
os.environ['KGR_PARAMETERS_JSON'] = {params!r}
os.environ['KGR_RESULT_FILENAME'] = {expected_result!r}
os.environ['KGR_SOURCE_SHA256'] = {source_digest!r}
_kgr_ds = {datasets!r}
_kgr_models = {models!r}
os.environ['KGR_DATASET_REFS'] = json.dumps(_kgr_ds)
os.environ['KGR_MODEL_REFS'] = json.dumps(_kgr_models)
_kgr_ds_paths = {{}}
for _ref in _kgr_ds:
    _mount = pathlib.Path('/kaggle/input') / _ref.split('/')[1]
    if not _mount.is_dir():
        raise FileNotFoundError(f'Dataset mount not found: {{_ref}}')
    _kgr_ds_paths[_ref] = str(_mount)
os.environ['KGR_DATASET_PATHS'] = json.dumps(_kgr_ds_paths)
_kgr_model_paths = {{}}
for _ref in _kgr_models:
    _, _slug, _framework, _variation, _version = _ref.split('/')
    _root = pathlib.Path('/kaggle/input') / _slug
    _found = next((_p for _p in _root.glob('*/*/*') if _p.is_dir() and
                   _p.parent.parent.name.casefold() == _framework.casefold() and
                   _p.parent.name.casefold() == _variation.casefold() and
                   _p.name == _version), None)
    if _found is None:
        raise FileNotFoundError(f'Model mount not found: {{_ref}}')
    _kgr_model_paths[_ref] = str(_found)
os.environ['KGR_MODEL_PATHS'] = json.dumps(_kgr_model_paths)
if {install_requirements!r}:
    _requirements = _kgr_project / 'requirements.txt'
    if not _requirements.is_file():
        raise FileNotFoundError('requirements.txt missing')
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', str(_requirements)], check=True)
'''
    cells = original["cells"]
    for cell in cells:
        if cell["cell_type"] == "code":
            cell["outputs"] = []  # Prevent leaking stale outputs; do not mutate input file.
            cell["execution_count"] = None
    cells.insert(0, {"cell_type": "code", "execution_count": None, "metadata": {},
                     "outputs": [], "source": setup, "id": setup_id})
    original.setdefault("metadata", {}).setdefault(
        "kernelspec", {"display_name": "Python 3", "language": "python", "name": "python3"})
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(original, ensure_ascii=False, indent=1), encoding="utf-8")
    return files
