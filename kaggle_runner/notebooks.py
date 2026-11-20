"""Native notebook staging using the auditable common source contract."""
from __future__ import annotations

from pathlib import Path

from .source_contract import stage_agent_notebook


def stage_notebook(source: Path, target: Path, datasets: list[str], models: list[str], *,
                   root: Path | None = None, entry: str | None = None,
                   install_requirements: bool = False, experiment_id: str = "",
                   attempt_id: str = "", parameters: dict | None = None,
                   seed: int | None = None,
                   expected_result: str = "result.json", max_bytes: int = 3 * 1024 * 1024,
                   include: list[str] | None = None,
                   exclude: list[str] | None = None) -> list[dict]:
    root = (root or source.parent).resolve()
    entry = entry or source.name
    return stage_agent_notebook(source, target, root=root, entry=entry,
                                datasets=datasets, models=models,
                                install_requirements=install_requirements,
                                experiment_id=experiment_id, attempt_id=attempt_id,
                                parameters=parameters, seed=seed, expected_result=expected_result,
                                max_bytes=max_bytes, include=include, exclude=exclude)
