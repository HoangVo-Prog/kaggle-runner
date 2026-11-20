"""Team profiles stored together in the selected private .env file.

Only the API token authenticates; username is required to form Kaggle notebook
references, and email is optional report metadata. No team credentials are
stored in the repository, SQLite, notebook package or report.
"""
from __future__ import annotations

import getpass
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .accounts import preferred_env_file, read_env, read_env_values, update_env_values
from .config import RunnerError, default_config_path

ALIAS = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,39}$")
OWNER = re.compile(r"^[a-zA-Z0-9_-]+$")
ROSTER = "KGR_TEAM_PROFILES"


def _alias(alias: str) -> str:
    if not ALIAS.fullmatch(alias) or alias.lower() == "auto":
        raise RunnerError("Team profile must be a simple alias other than 'auto'")
    return alias.lower()


def _prefix(alias: str) -> str:
    # Existing aliases with dashes remain usable after migration.
    return "KGR_TEAM_" + alias.upper().replace("-", "_") + "_"


def _value(values: dict[str, str], key: str) -> str:
    """Resolve manually edited .env keys without depending on alias case.

    Environment variable keys themselves are case-sensitive, but .env team
    profile names are declared by an alias. Reject contradictory definitions
    rather than silently picking a token from an arbitrary spelling.
    """
    matches = [(name, value) for name, value in values.items()
               if name.casefold() == key.casefold()]
    distinct = {value for _, value in matches if value}
    if len(distinct) > 1:
        names = ", ".join(sorted(name for name, _ in matches))
        raise RunnerError(f"Conflicting definitions of {key} in .env ({names}); "
                          "remove duplicate keys. Secret values are not displayed")
    return next(iter(distinct), "")


def _roster(values: dict[str, str]) -> list[str]:
    raw = _value(values, ROSTER).strip()
    if not raw:
        return []
    names = [_alias(part.strip()) for part in raw.split(",")]
    if len(names) != len(set(names)) or any(not n for n in names):
        raise RunnerError("Duplicate or invalid aliases in KGR_TEAM_PROFILES")
    prefixes = [_prefix(n) for n in names]
    if len(prefixes) != len(set(prefixes)):
        raise RunnerError("Team profile aliases collide as .env variable names (dash vs underscore)")
    return names


@dataclass(frozen=True)
class TeamProfile:
    alias: str
    token: str
    owner: str
    email: str | None = None

    def metadata(self) -> dict:
        return {"account_profile": self.alias, "account_owner": self.owner,
                "account_email": self.email}


def profiles(env_file: str | None = None) -> list[str]:
    return sorted(_roster(read_env_values(preferred_env_file(env_file))))


def get_profile(alias: str, env_file: str | None = None) -> TeamProfile:
    alias = _alias(alias)
    target = preferred_env_file(env_file)
    values = read_env_values(target)
    if alias not in _roster(values):
        raise RunnerError(f"Unknown team profile {alias!r} in {target}; "
                          "use `kgr team diagnose` to inspect the selected .env")
    prefix = _prefix(alias)
    token = _value(values, prefix + "API_TOKEN")
    owner = _value(values, prefix + "USERNAME")
    if not token or "\n" in token:
        raise RunnerError(f"Missing API token for team profile {alias!r} in {target}; "
                          f"expected {prefix}API_TOKEN. Run `kgr team diagnose` "
                          "to check which .env was loaded (tokens stay hidden)")
    if not OWNER.fullmatch(owner):
        raise RunnerError(f"Missing or invalid username for team profile {alias!r} "
                          f"in {target}; expected {prefix}USERNAME")
    return TeamProfile(alias, token, owner, _value(values, prefix + "EMAIL") or None)


def diagnose_profiles(env_file: str | None = None) -> dict:
    """Show the credential file path and key presence, never any API tokens."""
    target = preferred_env_file(env_file)
    values = read_env_values(target)
    records = []
    for alias in _roster(values):
        prefix = _prefix(alias)
        try:
            owner = _value(values, prefix + "USERNAME")
            token_configured = bool(_value(values, prefix + "API_TOKEN"))
            records.append({"profile": alias, "username": owner or None,
                            "token_configured": token_configured,
                            "ready": bool(token_configured and OWNER.fullmatch(owner))})
        except RunnerError as exc:
            records.append({"profile": alias, "username": None,
                            "token_configured": False, "ready": False,
                            "error": str(exc)})
    return {"env_file": str(target), "exists": target.is_file(),
            "source": ("--env-file" if env_file else
                       "KGR_ENV_FILE" if os.getenv("KGR_ENV_FILE") else "project-root"),
            "profiles": records,
            "note": "This only checks .env syntax and key presence, not Kaggle API access"}


def add_profile(alias: str, *, token: str, owner: str, email: str | None = None,
                replace: bool = False, env_file: str | None = None) -> dict:
    alias = _alias(alias)
    if not token or any(c in token for c in "\r\n"):
        raise RunnerError("API token must be nonempty and single-line")
    if not owner or not OWNER.fullmatch(owner):
        raise RunnerError("--username is required and must match a Kaggle profile URL username")
    if email and any(c in email for c in "\r\n"):
        raise RunnerError("Email metadata cannot contain newlines")
    target = preferred_env_file(env_file)
    values = read_env_values(target)
    names = _roster(values)
    if alias in names and not replace:
        raise RunnerError(f"Profile {alias!r} exists; use --replace to explicitly update its token")
    if any(_prefix(name) == _prefix(alias) and name != alias for name in names):
        raise RunnerError("Alias conflicts with an existing .env variable prefix")
    for other in names:
        if other == alias:
            continue
        prefix = _prefix(other)
        if _value(values, prefix + "USERNAME").lower() == owner.lower():
            raise RunnerError(f"Username {owner!r} already belongs to profile {other!r}")
        if _value(values, prefix + "API_TOKEN") == token:
            raise RunnerError(f"This API token is already associated with profile {other!r}")
    prefix = _prefix(alias)
    updates: dict[str, str | None] = {
        ROSTER: ",".join(sorted(set(names + [alias]))),
        prefix + "USERNAME": owner,
        prefix + "API_TOKEN": token,
        prefix + "EMAIL": email or None,
    }
    # Canonicalize old lowercase or mixed-case spellings on explicit replacement.
    # Otherwise two keys differing only by case would remain in .env.
    canonical_names = {key.casefold() for key in updates}
    for original in values:
        if original.casefold() in canonical_names and original not in updates:
            updates[original] = None
    update_env_values(target, updates)
    return {"profile": alias, "owner": owner, "email": email,
            "token_configured": True, "env_file": str(target)}


def interactive_add(alias: str, *, stdin_token: bool = False,
                    owner: str, email: str | None = None,
                    replace: bool = False, env_file: str | None = None) -> dict:
    token = sys.stdin.readline().strip() if stdin_token else getpass.getpass("Kaggle API token (hidden): ")
    return add_profile(alias, token=token, owner=owner, email=email, replace=replace, env_file=env_file)


def public_profiles(env_file: str | None = None) -> list[dict]:
    return [get_profile(alias, env_file).metadata() | {"token_configured": True,
            "env_file": str(preferred_env_file(env_file))} for alias in profiles(env_file)]


def migrate_legacy_profiles(*, env_file: str | None = None,
                            from_dir: str | None = None) -> dict:
    """Explicit, non-destructive import of older team/*.env files.

    Never read these files as fallback during normal operations: the shared .env
    must be the sole team-profile source of truth.
    """
    legacy = (Path(from_dir).expanduser() if from_dir else
              Path(os.getenv("KGR_TEAM_DIR", str(default_config_path().parent / "team"))).expanduser())
    if legacy.is_symlink():
        raise RunnerError("Refusing symlinked legacy credential directory")
    legacy = legacy.resolve()
    if not legacy.is_dir():
        raise RunnerError(f"Legacy team profile directory not found: {legacy}")
    paths = sorted(legacy.glob("*.env"))
    if not paths:
        raise RunnerError(f"No legacy team profiles in {legacy}")
    target = preferred_env_file(env_file)
    current = read_env_values(target)
    names = _roster(current)
    updates: dict[str, str | None] = {}
    imported = []
    owners = {_value(current, _prefix(n) + "USERNAME").lower() for n in names}
    tokens = {_value(current, _prefix(n) + "API_TOKEN") for n in names}
    for path in paths:
        alias = _alias(path.stem)
        if alias in names or any(_prefix(n) == _prefix(alias) for n in names):
            raise RunnerError(f"Profile {alias!r} already exists in .env; migration stopped without changes")
        vals = read_env(path)
        owner, token = vals.get("KAGGLE_OWNER", ""), vals.get("KAGGLE_API_TOKEN", "")
        if not OWNER.fullmatch(owner) or not token:
            raise RunnerError(f"Invalid legacy profile {alias!r}; migration stopped")
        if owner.lower() in owners or token in tokens:
            raise RunnerError(f"Duplicate owner or token in legacy profile {alias!r}; migration stopped")
        names.append(alias)
        owners.add(owner.lower())
        tokens.add(token)
        prefix = _prefix(alias)
        updates.update({prefix + "USERNAME": owner, prefix + "API_TOKEN": token,
                        prefix + "EMAIL": vals.get("EMAIL") or None})
        imported.append(alias)
    if len({_prefix(n) for n in names}) != len(names):
        raise RunnerError("Legacy aliases collide as .env keys; migration stopped")
    updates[ROSTER] = ",".join(sorted(names))
    update_env_values(target, updates)
    return {"imported_profiles": imported, "count": len(imported),
            "env_file": str(target), "legacy_files_preserved": True}
