"""Minimal subprocess adapter over Kaggle's official, documented CLI."""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass

from .config import RunnerError, Settings


@dataclass(frozen=True)
class Result:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def text(self) -> str:
        return "\n".join(x for x in (self.stdout, self.stderr) if x).strip()


class KaggleCLI:
    def __init__(self, settings: Settings, *, token: str | None = None):
        self.settings = settings
        self.token = token

    def call(self, *args: str, check: bool = True, stream: bool = False,
             timeout_seconds: float | None = None) -> Result:
        command = [sys.executable, "-m", "kaggle", *args]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        if self.settings.config_dir:
            env["KAGGLE_CONFIG_DIR"] = str(self.settings.config_dir)
        if self.token:
            # Isolate token authentication from legacy username/key credentials.
            env.pop("KAGGLE_USERNAME", None)
            env.pop("KAGGLE_KEY", None)
            env["KAGGLE_API_TOKEN"] = self.token
        try:
            if stream:
                result = subprocess.run(command, env=env, check=False, text=True, encoding="utf-8",
                                        errors="replace", timeout=timeout_seconds)
                response = Result(command, result.returncode, "", "")
            else:
                result = subprocess.run(command, env=env, check=False, capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=timeout_seconds)
                response = Result(command, result.returncode, result.stdout, result.stderr)
        except subprocess.TimeoutExpired as exc:
            raise RunnerError(f"Kaggle CLI request timed out after {timeout_seconds:g}s "
                              f"({' '.join(args[:3])})") from exc
        except FileNotFoundError as exc:
            raise RunnerError(f"Python/Kaggle CLI unavailable: {exc}") from exc
        if self.token:
            response = Result(response.args, response.returncode,
                              response.stdout.replace(self.token, "[REDACTED]"),
                              response.stderr.replace(self.token, "[REDACTED]"))
        if check and response.returncode != 0:
            raise RunnerError(f"Kaggle CLI exited with {response.returncode}: {response.text or 'see terminal output'}")
        return response
