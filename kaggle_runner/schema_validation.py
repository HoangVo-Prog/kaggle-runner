"""Executable JSON Schema validation of the versioned experiment contracts.

Schemas are shipped as package data, so editable and wheel installs behave alike.
Error summaries never echo source objects, runtime parameters or credential data.
"""
from __future__ import annotations

import json
from importlib import resources

from .config import RunnerError


def validate_contract(document: object, kind: str) -> None:
    if kind not in {"experiment_spec", "experiment_result"}:
        raise ValueError("Unsupported schema kind")
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise RunnerError("MISSING_DEPENDENCY: jsonschema>=4.20 must be installed") from exc
    source = resources.files("kaggle_runner").joinpath("schemas", kind + ".schema.json")
    schema = json.loads(source.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(document),
                    key=lambda error: tuple(str(x) for x in error.absolute_path))
    if errors:
        # Only print schema paths and error types; never echo values from user input.
        err = errors[0]
        path = ".".join(str(p) for p in err.absolute_path) or "<root>"
        raise RunnerError(f"SCHEMA_INVALID: {kind} rejected at {path} ({err.validator})")
