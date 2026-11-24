"""Handlers for package, experiment, reconciliation, results and reports."""
from __future__ import annotations

from .. import agent_workflow as agent
from ..config import RunnerError, Settings
from ..source_contract import inspect_source


def dispatch_agent_command(args, settings: Settings | None, emit, *, json_output: bool) -> int | None:
    """Handle an agent-first command, returning None when it is not in this group."""
    if args.command not in {"package", "experiment", "reconcile", "results"} and not (
            args.command == "report" and args.report_target):
        return None
    if settings is None and not (args.command == "package" or (args.command == "experiment" and args.experiment_command == "validate")):
        raise RunnerError("Stateful agent command requires resolved settings")
    if args.command == "package":
        emit(inspect_source(args.source, root=args.root, entry=args.entry,
                            max_mb=args.max_source_mb, include=args.include, exclude=args.exclude))
        return 0
    if args.command == "experiment":
        if args.experiment_command == "validate":
            emit(agent.validate(args.spec_file))
            return 0
        if args.experiment_command == "submit":
            emit(agent.submit(args.spec_file, settings=settings, env_file=args.env_file,
                              account=args.account, approve=args.approve))
            return 0
        if args.experiment_command == "show":
            emit(agent.show(settings, args.experiment_id))
            return 0
        outcome = agent.resume(settings, args.experiment_id, env_file=args.env_file,
                               wait=args.wait, poll_seconds=args.poll_seconds,
                               max_wait_minutes=args.max_wait_minutes)
        emit(outcome)
        return 0 if outcome["ok"] else 3
    if args.command == "reconcile":
        outcome = agent.reconcile(settings, args.env_file, args.experiment_id)
        emit(outcome)
        return 0 if outcome["ok"] else 3
    if args.command == "results":
        outcome = (agent.validate_result(settings, args.job_id) if args.results_command == "validate"
                   else agent.aggregate(settings, args.experiment_id))
        emit(outcome)
        return 0 if outcome["ok"] else 3
    if args.command == "report":
        if not args.report_experiment_id:
            raise RunnerError("report experiment requires EXPERIMENT_ID")
        outcome = agent.report(settings, args.report_experiment_id, format=args.format)
        evidence = agent.evidence_bundle(settings, args.report_experiment_id)
        if args.format == "md" and not json_output:
            print(outcome + "\n## Evidence bundle\n"
                  + f"- SHA256: `{evidence['sha256']}`\n"
                  + f"- Path: `{evidence['bundle_path']}`\n")
        elif isinstance(outcome, dict):
            outcome["evidence"] = evidence
            emit(outcome)
        else:
            emit({"report": outcome, "evidence": evidence})
        return 0
    return None
