"""Dataset inspection and explicit PRIVATE upload through official Kaggle CLI."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
import zipfile
from pathlib import Path

from .api import KaggleCLI
from .config import RunnerError
from .packaging import EXCLUDE_FILES, EXCLUDE_SUFFIXES, validate_ref


def list_dataset_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        raise RunnerError(f"Dataset directory not found: {folder}")
    selected: list[Path] = []
    forbidden_dirs = {".git", ".kaggle", ".ssh", ".venv", "venv", "__pycache__"}
    for base, dirs, filenames in os.walk(folder, followlinks=False):
        current = Path(base)
        dirs[:] = sorted(d for d in dirs if d not in forbidden_dirs and not d.startswith(".")
                         and not (current / d).is_symlink())
        for filename in sorted(filenames):
            candidate = current / filename
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if (filename.startswith(".") or filename.lower() in EXCLUDE_FILES or filename.lower().startswith(".env.")
                or filename.lower().endswith(".local.json")
                or candidate.suffix.lower() in {".key", ".pem", ".p12", ".pfx"}):
                continue
            selected.append(candidate.relative_to(folder))
    if not selected:
        raise RunnerError("Dataset folder has no safe, non-empty file selection")
    return selected


def inspect_local(folder: Path) -> dict:
    files = list_dataset_files(folder)
    return {"type": "local", "path": str(folder.resolve()), "file_count": len(files),
            "total_bytes": sum((folder / f).stat().st_size for f in files),
            "sample_files": [f.as_posix() for f in files[:20]]}


def prepare_dataset(folder: Path, state: Path, ref: str, title: str, license_name: str,
                    max_bytes: int = 5 * 1024**3) -> tuple[Path, dict]:
    ref = validate_ref(ref, "dataset reference")
    files = list_dataset_files(folder)
    size = sum((folder / f).stat().st_size for f in files)
    if size > max_bytes:
        raise RunnerError(f"Selected data {size} bytes exceeds configured limit {max_bytes}")
    if not (6 <= len(title) <= 50):
        raise RunnerError("Kaggle dataset title must be 6–50 characters")
    staging = state / "dataset-staging" / uuid.uuid4().hex
    staging.mkdir(parents=True, exist_ok=False)
    manifest = {"dataset_ref": ref, "source": str(folder.resolve()), "total_bytes": size, "files": []}
    with zipfile.ZipFile(staging / "payload.zip", "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for rel in files:
            src = folder / rel
            h = hashlib.sha256()
            with src.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(chunk)
            z.write(src, arcname=rel.as_posix())
            manifest["files"].append({"path": rel.as_posix(), "bytes": src.stat().st_size, "sha256": h.hexdigest()})
    (staging / "dataset-metadata.json").write_text(json.dumps({
        "id": ref, "title": title, "licenses": [{"name": license_name}],
        "description": "Private dataset staged by kaggle-runner. Data is in payload.zip; unpack to use.",
    }, indent=2), encoding="utf-8")
    (staging / "source-manifest.local.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    # source-manifest.local.json stays local; don't upload it (it includes absolute paths).
    return staging, manifest


def upload_dataset(cli: KaggleCLI, staging: Path, ref: str, *, version_note: str | None = None) -> str:
    # The Kaggle CLI uploads everything in the staging directory. Place only approved artifacts there.
    local_manifest = staging / "source-manifest.local.json"
    # This manifest carries absolute local paths, so keep it outside the upload directory.
    if local_manifest.exists():
        local_manifest.rename(staging.parent / f"{staging.name}-manifest.local.json")
    if version_note is not None:
        if not version_note.strip():
            raise RunnerError("--version-note must be non-empty")
        args = ["datasets", "version", "-p", str(staging), "-m", version_note, "-t", "-r", "skip"]
    else:
        args = ["datasets", "create", "-p", str(staging), "-t", "-r", "skip"]
    result = cli.call(*args)
    return result.text


def download_dataset(cli: KaggleCLI, ref: str, folder: Path, *, unzip: bool = False) -> str:
    ref = validate_ref(ref, "dataset reference")
    folder.mkdir(parents=True, exist_ok=True)
    args = ["datasets", "download", ref, "-p", str(folder)]
    if unzip:
        args.append("--unzip")
    return cli.call(*args).text
