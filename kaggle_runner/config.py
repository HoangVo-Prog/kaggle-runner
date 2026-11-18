"""Portable runtime paths; no paths tied to this repository or a particular OS."""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path


class RunnerError(Exception):
    """User-facing failure without a traceback."""


def state_directory(override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    if os.getenv("KGR_STATE_DIR"):
        return Path(os.environ["KGR_STATE_DIR"]).expanduser().resolve()
    if os.name == "nt" and os.getenv("LOCALAPPDATA"):
        return (Path(os.environ["LOCALAPPDATA"]) / "kaggle-runner").resolve()
    return (Path(os.getenv("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "kaggle-runner").expanduser().resolve()


def default_config_path() -> Path:
    if os.getenv("KGR_CONFIG"):
        return Path(os.environ["KGR_CONFIG"]).expanduser().resolve()
    if os.name == "nt" and os.getenv("APPDATA"):
        return Path(os.environ["APPDATA"]) / "kaggle-runner" / "config.toml"
    return Path(os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "kaggle-runner" / "config.toml"


def load_config(path: str | None) -> dict:
    p = Path(path).expanduser().resolve() if path else default_config_path()
    if not p.exists():
        if path:
            raise RunnerError(f"Config file not found: {p}")
        return {}
    with p.open("rb") as f:
        value = tomllib.load(f)
    if not isinstance(value, dict):
        raise RunnerError("Config must be a TOML table")
    return value


@dataclass(frozen=True)
class Settings:
    state: Path
    profile: str | None
    config_dir: Path | None
    python: str = sys.executable


def build_settings(*, state_dir: str | None, profile: str | None, config: str | None) -> Settings:
    settings = load_config(config)
    kgr = settings.get("runner", {})
    if not isinstance(kgr, dict):
        raise RunnerError("[runner] must be a TOML table")
    state = state_directory(state_dir or kgr.get("state_dir"))
    name = profile or os.getenv("KGR_PROFILE") or kgr.get("profile")
    if name and ("/" in name or "\\" in name or name in (".", "..")):
        raise RunnerError("Profile must be a simple alias, not a path")
    selected: Path | None = None
    if name:
        profiles_root = Path(os.getenv("KGR_PROFILES_DIR", str(Path.home() / ".kaggle-profiles"))).expanduser()
        selected = (profiles_root / name).resolve()
        if not selected.is_dir():
            raise RunnerError(f"Kaggle profile {name!r} not found in {profiles_root}")
    # If no profile is specified, do not override the Kaggle CLI's existing auth.
    return Settings(state=state, profile=name, config_dir=selected)
