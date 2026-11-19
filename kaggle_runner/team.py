"""Opt-in, locally isolated Kaggle credentials for an authorized team.

Only the API token authenticates. Username is a required notebook owner label;
email is optional metadata. Owner/email are
never passed as authentication credentials.  Profiles are not committed to Git.
"""
from __future__ import annotations

import getpass
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .accounts import read_env
from .config import RunnerError, default_config_path

ALIAS = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,39}$")
OWNER = re.compile(r"^[a-zA-Z0-9_-]+$")


def team_dir() -> Path:
    raw = os.environ.get("KGR_TEAM_DIR")
    return Path(raw).expanduser().resolve() if raw else default_config_path().parent / "team"


@dataclass(frozen=True)
class TeamProfile:
    alias: str
    token: str
    owner: str
    email: str | None = None

    def metadata(self) -> dict:
        return {"account_profile": self.alias, "account_owner": self.owner,
                "account_email": self.email}


def profile_path(alias: str) -> Path:
    if not ALIAS.fullmatch(alias) or alias.lower() == "auto":
        raise RunnerError("Team profile must be a simple alias other than 'auto'")
    return team_dir() / (alias + ".env")


def profiles() -> list[str]:
    root = team_dir()
    if not root.exists():
        return []
    if root.is_symlink():
        raise RunnerError("Team credential directory must not be a symlink")
    return sorted(p.stem for p in root.glob("*.env") if ALIAS.fullmatch(p.stem))


def get_profile(alias: str) -> TeamProfile:
    path = profile_path(alias)
    if not path.is_file():
        raise RunnerError(f"Unknown team profile {alias!r}; use `kgr team list`")
    values = read_env(path)
    token = values.get("KAGGLE_API_TOKEN", "")
    if not token or '\n' in token:
        raise RunnerError(f"Missing API token for team profile {alias!r}")
    owner = values.get("KAGGLE_OWNER") or ""
    if not OWNER.fullmatch(owner):
        raise RunnerError(f"Missing or invalid username for team profile {alias!r}")
    return TeamProfile(alias, token, owner, values.get("EMAIL") or None)


def add_profile(alias: str, *, token: str, owner: str,
                email: str | None = None, replace: bool = False) -> dict:
    path = profile_path(alias)
    if path.is_symlink():
        raise RunnerError("Refusing symlinked credential file")
    if path.exists() and not replace:
        raise RunnerError(f"Profile {alias!r} exists; use --replace to explicitly update its token")
    if not token or any(c in token for c in '\r\n'):
        raise RunnerError("API token must be nonempty and single-line")
    if not owner or not OWNER.fullmatch(owner):
        raise RunnerError("--username is required and must match a Kaggle profile URL username")
    if email and any(c in email for c in '\r\n'):
        raise RunnerError("Email metadata cannot contain newlines")
    # Never count two aliases for one Kaggle username as two independent slots.
    for other in profiles():
        if other == alias:
            continue
        existing = get_profile(other)
        if existing.owner.lower() == owner.lower():
            raise RunnerError(f"Username {owner!r} already belongs to profile {other!r}")
        if existing.token == token:
            raise RunnerError(f"This API token is already associated with profile {other!r}")
    root = team_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise RunnerError("Refusing symlinked credential directory")
    if os.name != "nt":
        root.chmod(0o700)
    fd, tmp = tempfile.mkstemp(prefix=".profile-", dir=root)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(f"KAGGLE_OWNER={owner}\n")
            if email:
                f.write(f"EMAIL={email}\n")
            f.write(f"KAGGLE_API_TOKEN={token}\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {"profile": alias, "owner": owner, "email": email,
            "token_configured": True, "env_file": str(path)}


def interactive_add(alias: str, *, stdin_token: bool = False,
                    owner: str, email: str | None = None,
                    replace: bool = False) -> dict:
    token = sys.stdin.readline().strip() if stdin_token else getpass.getpass("Kaggle API token (hidden): ")
    return add_profile(alias, token=token, owner=owner, email=email, replace=replace)


def public_profiles() -> list[dict]:
    return [get_profile(alias).metadata() | {"token_configured": True} for alias in profiles()]
