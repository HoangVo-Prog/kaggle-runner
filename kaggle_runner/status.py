"""Shared parser for the status printed by the official Kaggle CLI.

Important: a kernel slug may itself contain words such as 'running' or 'error'.
Never infer the worker state by searching arbitrary words in an entire line.
"""
from __future__ import annotations

import json
import re

# States documented by Kaggle's GetKernelSessionStatus endpoint, plus
# backwards-compatible CLI spellings observed in older tools.
_KNOWN = {
    "queued", "running", "complete", "error", "cancel_requested",
    "cancel_acknowledged", "new_script", "pending", "initializing",
    "starting", "cancelled", "failed",
}
_ALIASES = {
    "canceled": "cancelled", "cancellation_requested": "cancel_requested",
    "cancellation_acknowledged": "cancel_acknowledged",
}
_ENUM = re.compile(r"\bKernelWorkerStatus\.([A-Za-z][A-Za-z_0-9]*)\b", re.IGNORECASE)
_LABELED = re.compile(
    r"\b(?:has\s+status|(?:worker\s+)?status\s*[:=])\s*[\"']?([A-Za-z][A-Za-z_0-9]*)\b",
    re.IGNORECASE,
)


def _normalize(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    state = value.strip().strip('"\'').lower()
    if state.startswith("kernelworkerstatus."):
        state = state.split(".", 1)[1]
    state = _ALIASES.get(state, state)
    return state if state in _KNOWN else "unknown"


def parse_kernel_status(raw: str) -> str:
    """Parse a Kaggle worker state without matching accidental slug words.

    Returns ``unknown`` for any unrecognized response so preflight can fail
    closed, rather than assume an unknown state doesn't consume a session.
    """
    if not isinstance(raw, str) or not raw.strip():
        return "unknown"
    value = raw.strip()
    # Accept explicit machine-readable status from API clients (if present).
    if value.startswith("{"):
        try:
            doc = json.loads(value)
        except json.JSONDecodeError:
            doc = None
        if isinstance(doc, dict):
            for key in ("status", "workerStatus", "worker_status", "runStatus", "run_status"):
                state = doc.get(key)
                if isinstance(state, dict):
                    state = state.get("name") or state.get("status")
                parsed = _normalize(state)
                if parsed != "unknown":
                    return parsed
    # Kaggle CLI's standard output:
    # user/slug has status "KernelWorkerStatus.NEW_SCRIPT"
    match = _ENUM.search(value)
    if match:
        return _normalize(match.group(1))
    match = _LABELED.search(value)
    if match:
        return _normalize(match.group(1))
    # Direct tokens are useful for older and mock clients. Reject prose.
    return _normalize(value)
