"""Private, explicit Kaggle API token configuration for a single account.

Kaggle's terms prohibit one individual from operating multiple accounts. This
module deliberately does not implement automatic credential rotation.
"""
from __future__ import annotations

import getpass
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import RunnerError, default_config_path

KEYS = ("KAGGLE_OWNER", "EMAIL", "KAGGLE_API_TOKEN")
OWNER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class Account:
    owner: str | None
    email: str | None
    token: str | None
    env_file: Path | None


def preferred_env_file(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().absolute()
    if os.getenv("KGR_ENV_FILE"):
        return Path(os.environ["KGR_ENV_FILE"]).expanduser().absolute()
    local = Path.cwd() / ".env"
    if local.is_file():
        return local.absolute()
    return default_config_path().parent / ".env"


def read_env(path: Path) -> dict[str, str]:
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
        match = re.fullmatch(r"(?:export\s+)?([A-Z_]+)\s*=\s*(.*)", stripped)
        if not match:
            continue
        key, value = match.groups()
        if key not in KEYS:
            continue
        value = value.strip()
        if value.startswith(('"', "'")) and value.endswith(value[:1]):
            value = value[1:-1]
        result[key] = value
    return result


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
    if target.is_symlink():
        raise RunnerError("Refusing to overwrite symlinked credential file")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write, private on POSIX. Never return or print the secret.
    fd, temporary = tempfile.mkstemp(prefix=".kgr-env-", dir=target.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(f"KAGGLE_OWNER={owner}\nEMAIL={email}\nKAGGLE_API_TOKEN={token}\n")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def interactive_init(path: str | None, owner: str | None, email: str | None, stdin_token: bool) -> dict:
    import sys
    owner = owner or input("Kaggle username (from profile URL): ").strip()
    email = email or input("Report email (metadata only): ").strip()
    token = sys.stdin.readline().strip() if stdin_token else getpass.getpass("Kaggle API token (hidden): ")
    target = write_account(path, owner, email, token)
    return {"env_file": str(target), "owner": owner, "email": email,
            "token_configured": True, "token": "[REDACTED]"}
