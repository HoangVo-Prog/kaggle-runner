"""Private, explicit Kaggle API token configuration for a single account.

Kaggle's terms prohibit one individual from operating multiple accounts. This
module deliberately does not implement automatic credential rotation.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import RunnerError

KEYS = ("KAGGLE_OWNER", "EMAIL", "KAGGLE_API_TOKEN")
OWNER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class Account:
    owner: str | None
    email: str | None
    token: str | None
    env_file: Path | None


def _looks_like_project_root(path: Path) -> bool:
    """Identify the kaggle-runner checkout rather than an unrelated .env."""
    pyproject = path / "pyproject.toml"
    if not pyproject.is_file() or not (path / "kaggle_runner" / "accounts.py").is_file():
        return False
    # pyproject.toml is small. Verify the project name to avoid accidentally
    # reading credentials belonging to a different Python project.
    import tomllib
    try:
        return tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {}).get("name") == "kaggle-runner"
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return False


def _project_root() -> Path:
    """Resolve checkout root from CWD or source of an editable installation.

    An installed wheel outside the checkout cannot reliably know where the
    user's source tree lives. In that case ask for an explicit --env-file path
    instead of accidentally creating credentials under site-packages or $HOME.
    """
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if _looks_like_project_root(candidate):
            return candidate
    source_root = Path(__file__).resolve().parent.parent
    if _looks_like_project_root(source_root):
        return source_root
    raise RunnerError(
        "Cannot locate the kaggle-runner project root. Run from the repository "
        "directory, install with `pip install -e .`, or use --env-file PATH / KGR_ENV_FILE."
    )


def preferred_env_file(explicit: str | None = None) -> Path:
    """The project's root .env is the only implicit credential location.

    Order: --env-file > KGR_ENV_FILE > <kaggle-runner repo>/.env.
    Never silently fall back to ~/.config or a random current-directory .env.
    """
    if explicit:
        return Path(explicit).expanduser().absolute()
    if os.getenv("KGR_ENV_FILE"):
        return Path(os.environ["KGR_ENV_FILE"]).expanduser().absolute()
    return _project_root() / ".env"


def read_env_values(path: Path) -> dict[str, str]:
    """Read a private .env, including namespaced team profile settings."""
    if path.is_symlink():
        raise RunnerError(f"Refusing a symlink for credential file: {path}")
    if not path.is_file():
        return {}
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise RunnerError(f"Credential file permissions too open: {path}; run chmod 600 '{path}'")
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", stripped)
        if not match:
            continue
        key, value = match.groups()
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except (ValueError, TypeError) as exc:
                raise RunnerError(f"Invalid quoted value for {key} in {path}") from exc
            if not isinstance(value, str):
                raise RunnerError(f"Expected text for {key} in {path}")
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        result[key] = value
    return result


def read_env(path: Path) -> dict[str, str]:
    return {key: val for key, val in read_env_values(path).items() if key in KEYS}


def update_env_values(target: Path, updates: dict[str, str | None]) -> Path:
    """Atomically edit selected .env values without deleting unrelated settings."""
    if target.is_symlink():
        raise RunnerError(f"Refusing symlinked credential file: {target}")
    read_env_values(target)  # enforce private permissions before reading/writing
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in updates):
        raise RunnerError("Invalid .env variable name")
    if any(value is not None and ('\r' in value or '\n' in value)
           for value in updates.values()):
        raise RunnerError(".env values must be single-line")
    content = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    old: list[str] = []
    for line in content:
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in updates:
            continue
        old.append(line)
    if old and old[-1].strip():
        old.append("")
    for key, val in updates.items():
        if val is None:
            continue
        # Keep common tokens/usernames easy to edit manually; quote other values.
        encoded = val if re.fullmatch(r"[a-zA-Z0-9_@.,:/+=-]*", val) else json.dumps(val, ensure_ascii=False)
        old.append(f"{key}={encoded}")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".kgr-env-", dir=target.parent)
    try:
        os.fchmod(fd, 0o600) if hasattr(os, "fchmod") else None
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(old).rstrip("\n") + "\n")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def load_account(path: str | None = None) -> Account:
    env_file = preferred_env_file(path)
    values = read_env(env_file)
    # Explicit env vars override the file, which enables ephemeral CI tokens.
    values.update({k: v for k in KEYS if (v := os.getenv(k)) is not None})
    owner = values.get("KAGGLE_OWNER") or None
    token = values.get("KAGGLE_API_TOKEN") or None
    email = values.get("EMAIL") or None
    if owner and not OWNER_PATTERN.fullmatch(owner):
        raise RunnerError("KAGGLE_OWNER must be your Kaggle profile URL username, not the display name")
    return Account(owner=owner, email=email, token=token, env_file=env_file if env_file.is_file() else None)


def write_account(path: str | None, owner: str, email: str, token: str) -> Path:
    if not OWNER_PATTERN.fullmatch(owner):
        raise RunnerError("KAGGLE_OWNER must be a Kaggle username without spaces")
    if not email or any(c in email for c in "\r\n") or "@" not in email:
        raise RunnerError("EMAIL must be a valid-looking email address")
    if not token or any(c in token for c in "\r\n"):
        raise RunnerError("KAGGLE_API_TOKEN cannot be empty or multiline")
    target = preferred_env_file(path)
    return update_env_values(target, {"KAGGLE_OWNER": owner,
                                      "EMAIL": email, "KAGGLE_API_TOKEN": token})


def interactive_init(path: str | None, owner: str | None, email: str | None, stdin_token: bool) -> dict:
    import sys
    owner = owner or input("Kaggle username (from profile URL): ").strip()
    email = email or input("Report email (metadata only): ").strip()
    token = sys.stdin.readline().strip() if stdin_token else getpass.getpass("Kaggle API token (hidden): ")
    target = write_account(path, owner, email, token)
    return {"env_file": str(target), "owner": owner, "email": email,
            "token_configured": True, "token": "[REDACTED]"}
