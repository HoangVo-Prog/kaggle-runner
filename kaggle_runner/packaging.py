"""Package small Python repositories into a *single* Kaggle script kernel.

Kaggle kernel push uploads the configured code_file, so referring to local imports
without bundling them is unsafe. We embed a bounded source ZIP in a bootstrap .py.
This is for source code; large data and weights belong in Kaggle datasets/models.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import io
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from .config import RunnerError

# Conservative defaults; opt-in overrides of excluded files are not supported.
EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__",
    "_runner-state", "_runner-state.stage-*", "_runner-state.empty-*",
    ".pytest_cache", ".mypy_cache", ".tox", "node_modules", ".kaggle",
    ".ssh", "data", "datasets", "outputs", "output", "results", "checkpoints",
    "wandb", "build", "dist", ".idea", ".vscode", ".ruff_cache", ".cache",
}
EXCLUDE_FILES = {".env", "kaggle.json", "id_rsa", "id_ed25519", "credentials", "credentials.json", ".netrc",
                 ".kgr-bootstrap.json", ".kgr-bootstrap.json.pre-state-migration",
                 ".kgr-bootstrap.json.state-migration-tmp"}
EXCLUDE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".ckpt", ".pt", ".pth", ".safetensors", ".parquet"}
ALLOWED_SUFFIXES = {".py", ".toml", ".txt", ".yaml", ".yml", ".json", ".cfg", ".ini", ".sh", ".md"}
MAX_FILE_BYTES = 2 * 1024 * 1024


def _kaggle_web_ref(ref: str, resource: str) -> str:
    if not ref.startswith(("https://", "http://")):
        return ref
    parsed = urlsplit(ref)
    if parsed.scheme != "https" or parsed.hostname not in {"www.kaggle.com", "kaggle.com"} or parsed.query or parsed.fragment:
        raise RunnerError(f"Invalid Kaggle {resource} URL")
    parts = parsed.path.strip("/").split("/")
    if not parts or parts[0] != resource:
        raise RunnerError(f"Expected Kaggle /{resource}/... URL")
    return "/".join(parts[1:])


def validate_ref(ref: str, kind: str = "reference") -> str:
    ref = _kaggle_web_ref(ref, "datasets")
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", ref):
        raise RunnerError(f"Invalid {kind}: {ref!r}; expected owner/slug")
    return ref.lower()


def validate_model_ref(ref: str) -> str:
    """A pinned Kaggle Model variation version, not a Model Proxy model ID."""
    ref = _kaggle_web_ref(ref, "models")
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[1-9][0-9]*", ref):
        raise RunnerError(
            f"Invalid model reference: {ref!r}; expected "
            "owner/model/framework/variation/version (e.g. google/mobilenet-v2/tensorFlow2/130-224-classification/1)"
        )
    return ref


def slugify(s: str, maxlen: int = 30) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")
    return (s[:maxlen].rstrip("-") or "job")


def git_revision(path: Path) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=5, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def resolve_source(value: str, workspace: Path, *, ref: str | None = None) -> tuple[Path, str | None]:
    if value.startswith("https://"):
        if not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+(?:\.git)?/?", value):
            raise RunnerError("Only plain https://github.com/owner/repo URLs are supported; no embedded tokens")
        dst = workspace / "checkout"
        command = ["git", "clone", "--depth", "1"]
        if ref:
            command += ["--branch", ref]
        command += [value, str(dst)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        except OSError as exc:
            raise RunnerError(f"Git is required for GitHub sources: {exc}") from exc
        if result.returncode:
            raise RunnerError(f"Git clone failed: {result.stderr.strip()}")
        return dst, git_revision(dst)
    if ref:
        raise RunnerError("--ref applies only to GitHub URL sources; local checkout is used as-is")
    src = Path(value).expanduser().resolve()
    if not src.is_dir():
        raise RunnerError(f"Source directory does not exist: {src}")
    return src, git_revision(src)


def _ignore_patterns(root: Path) -> list[str]:
    p = root / ".kaggle-runnerignore"
    if not p.exists():
        return []
    return [line.strip() for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _ignored(relative: Path, patterns: list[str]) -> bool:
    path = relative.as_posix()
    return any(fnmatch.fnmatch(path, p.rstrip("/")) or
               (p.endswith("/") and path.startswith(p)) for p in patterns)


def _excluded_directory(name: str) -> bool:
    return name in EXCLUDE_DIRS or any(fnmatch.fnmatchcase(name, pattern) for pattern in EXCLUDE_DIRS if "*" in pattern)


def source_files(root: Path, entry: str, *, include: list[str] | None = None,
                 exclude: list[str] | None = None) -> list[Path]:
    entry_path = Path(entry)
    if entry_path.is_absolute() or ".." in entry_path.parts or entry_path.suffix not in {".py", ".ipynb"}:
        raise RunnerError("Entrypoint must be a relative .py/.ipynb file without '..'")
    patterns = [*_ignore_patterns(root), *(exclude or [])]
    files: list[Path] = []
    for current, dirs, names in os.walk(root, followlinks=False):
        base = Path(current)
        relative_dir = base.relative_to(root)
        dirs[:] = sorted(d for d in dirs if not _excluded_directory(d) and not (base / d).is_symlink()
                         and not _ignored(relative_dir / d, patterns))
        for name in sorted(names):
            p = base / name
            if p.is_symlink() or not p.is_file():
                continue
            rel = p.relative_to(root)
            if any(_excluded_directory(part) for part in rel.parts[:-1]):
                continue
            if (name.lower() in EXCLUDE_FILES or name.lower().startswith(".env.")
                or name.lower().endswith(".local.json") or p.suffix.lower() in EXCLUDE_SUFFIXES):
                continue
            if p.suffix.lower() not in ALLOWED_SUFFIXES and name not in {"Dockerfile", "Makefile"} and rel != entry_path:
                continue
            if _ignored(rel, patterns):
                continue
            if include and rel != entry_path and not any(fnmatch.fnmatch(rel.as_posix(), rule) for rule in include):
                continue
            if p.stat().st_size > MAX_FILE_BYTES:
                raise RunnerError(f"Large source file {rel} ({p.stat().st_size} bytes). Use a Kaggle dataset instead")
            files.append(rel)
    if entry_path not in files:
        raise RunnerError(f"Entrypoint {entry!r} excluded or missing; check name and ignore rules")
    return files


def pack_source(root: Path, entry: str, max_bytes: int, *, include: list[str] | None = None,
                exclude: list[str] | None = None) -> tuple[bytes, list[dict]]:
    files = source_files(root, entry, include=include, exclude=exclude)
    if not files:
        raise RunnerError("No source files selected")
    output = io.BytesIO()
    manifest = []
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=8) as bundle:
        for relative in files:
            raw = (root / relative).read_bytes()
            info = zipfile.ZipInfo(relative.as_posix(), (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            bundle.writestr(info, raw)
            manifest.append({"path": relative.as_posix(), "bytes": len(raw),
                             "sha256": hashlib.sha256(raw).hexdigest()})
    data = output.getvalue()
    if len(data) > max_bytes:
        raise RunnerError(f"Source ZIP is {len(data)} bytes, exceeds limit {max_bytes}; move assets into datasets")
    return data, manifest


_BOOTSTRAP = r'''# Generated by kaggle-runner; do not edit directly.
import base64
import io
import json
import os
import pathlib
import runpy
import subprocess
import sys
import traceback
import zipfile
from datetime import datetime, timezone

_PAYLOAD = "__PAYLOAD__"
_OPTIONS = json.loads(base64.b64decode("__OPTIONS__"))
project = pathlib.Path("/kaggle/working/kgr_source")
project.mkdir(parents=True, exist_ok=True)
output = pathlib.Path("/kaggle/working")
result_path = output / "kgr_run_result.json"
os.environ["KGR_EXPERIMENT_ID"] = _OPTIONS.get("experiment_id", "")
os.environ["KGR_ATTEMPT_ID"] = _OPTIONS.get("attempt_id", "")
os.environ["KGR_SEED"] = str(_OPTIONS.get("seed", ""))
os.environ["KGR_PARAMETERS_JSON"] = json.dumps(_OPTIONS.get("parameters", {}), sort_keys=True)
os.environ["KGR_RESULT_FILENAME"] = _OPTIONS.get("expected_result", "result.json")
os.environ["KGR_SOURCE_SHA256"] = _OPTIONS.get("source_sha256", "")
result = {"status": "failed", "entry": _OPTIONS["entry"], "started_at": datetime.now(timezone.utc).isoformat()}
try:
    archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(_PAYLOAD)))
    for item in archive.infolist():
        rel = pathlib.PurePosixPath(item.filename)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("Unsafe ZIP entry")
    archive.extractall(project)
    # Source code is always extracted under Kaggle working; dataset mounts remain read-only.
    os.environ["KGR_PROJECT_ROOT"] = str(project)
    os.environ["KGR_OUTPUT_DIR"] = str(output)
    os.environ["KGR_DATASET_ROOT"] = "/kaggle/input"
    os.environ["KGR_DATASET_REFS"] = json.dumps(_OPTIONS["datasets"])
    dataset_paths = {}
    for dataset_ref in _OPTIONS["datasets"]:
        slug = dataset_ref.split("/")[1]
        mount = pathlib.Path("/kaggle/input") / slug
        if not mount.exists():
            raise FileNotFoundError(f"Attached Kaggle dataset not mounted: {dataset_ref} ({mount})")
        selected = mount
        if _OPTIONS["unpack_datasets"] and (mount / "payload.zip").is_file():
            destination = output / "kgr_datasets" / slug
            destination.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(mount / "payload.zip") as zipped:
                total = sum(item.file_size for item in zipped.infolist())
                if total > 8 * 1024**3:
                    raise ValueError("Dataset archive exceeds 8 GiB uncompressed safety limit")
                for item in zipped.infolist():
                    rel = pathlib.PurePosixPath(item.filename)
                    if rel.is_absolute() or ".." in rel.parts:
                        raise ValueError("Unsafe dataset ZIP path")
                zipped.extractall(destination)
            selected = destination
        dataset_paths[dataset_ref] = str(selected)
    os.environ["KGR_DATASET_PATHS"] = json.dumps(dataset_paths)
    os.environ["KGR_MODEL_REFS"] = json.dumps(_OPTIONS["models"])
    model_paths = {}
    for model_ref in _OPTIONS["models"]:
        _, model_slug, framework, variation, version = model_ref.split("/")
        root = pathlib.Path("/kaggle/input") / model_slug
        # Framework names can differ in capitalization between API and disk.
        mount = root / framework / variation / version
        if not mount.is_dir() and root.is_dir():
            mount = next((p for p in root.glob("*/*/*")
                          if p.is_dir() and p.parent.parent.name.casefold() == framework.casefold()
                          and p.parent.name.casefold() == variation.casefold()
                          and p.name == version), mount)
        if not mount.is_dir():
            raise FileNotFoundError(f"Attached Kaggle model not mounted: {model_ref} (expected {mount})")
        model_paths[model_ref] = str(mount)
    os.environ["KGR_MODEL_PATHS"] = json.dumps(model_paths)
    os.chdir(project)
    sys.path.insert(0, str(project))
    if _OPTIONS["install_requirements"]:
        requirements = project / "requirements.txt"
        if not requirements.is_file():
            raise FileNotFoundError("--install-requirements set but requirements.txt missing")
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(requirements)], check=True)
    entry = (project / _OPTIONS["entry"]).resolve()
    if project not in entry.parents or not entry.is_file():
        raise ValueError("Entrypoint missing or outside project")
    sys.argv = [str(entry), *_OPTIONS["arguments"]]
    try:
        runpy.run_path(str(entry), run_name="__main__")
    except SystemExit as exc:
        # Common Python entrypoints use sys.exit(main()). Exit code 0 is success.
        if exc.code not in (None, 0):
            raise
    result["status"] = "ok"
except BaseException as exc:
    result["error"] = repr(exc)
    result["traceback"] = traceback.format_exc()
    raise
finally:
    result["ended_at"] = datetime.now(timezone.utc).isoformat()
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
'''


def make_bootstrap(bundle: bytes, *, entry: str, arguments: list[str], datasets: list[str],
                   install_requirements: bool, unpack_datasets: bool = False,
                   models: list[str] | None = None, experiment_id: str = "",
                   attempt_id: str = "", parameters: dict | None = None,
                   seed: int | None = None,
                   expected_result: str = "result.json",
                   source_sha256: str = "") -> str:
    options = {"entry": entry, "arguments": arguments, "datasets": datasets, "models": models or [],
               "experiment_id": experiment_id, "attempt_id": attempt_id,
               "seed": seed,
               "parameters": parameters or {}, "expected_result": expected_result,
               "source_sha256": source_sha256,
               "install_requirements": install_requirements, "unpack_datasets": unpack_datasets}
    encoded_options = base64.b64encode(json.dumps(options).encode()).decode("ascii")
    return (_BOOTSTRAP.replace("__PAYLOAD__", base64.b64encode(bundle).decode("ascii"))
            .replace("__OPTIONS__", encoded_options))
