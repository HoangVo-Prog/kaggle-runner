# Agent task — TBPS baseline workflow stress test

You are the Codex orchestration agent. Your **first operational action must be reading the repository-root AGENTS.md in full** (before invoking kgr, inspecting the notebook, or modifying files). Record its SHA256 and the checkout revision in the audit log immediately afterward. Follow its environment resolution and checkpoint rules. Treat AGENTS.md as the primary operating guide; do not open archived docs indiscriminately.

Read this task, `case.json`, `checklist.json` and the audit usage in `README.md`. Do not read `OPERATOR_REVIEW.md` until the evaluation is finished; it is the operator's independent review rubric, not an agent solution guide.

## User objective

Prepare three GPU runs of the existing TBPS notebook in `workspaces/tbps/irra/notebooks/kaggle_baseline.ipynb`: CUHK-PEDES, ICFG-PEDES and RSTPReid, exactly one run per dataset, on T4x2. The Kaggle dataset URL is in the notebook. Keep the original training protocol and 60 epochs, batch size 64, ITC loss and image augmentation. Do not reduce workload, change the model, change the evaluation split or silently change the seed. Inspect the actual notebook and repository rather than assuming its CLI works as-is. If adaptations are needed, preserve the original and record a diff and justification. Do not invent a Kaggle model reference.

## This session: preparation only

No Kaggle kernel push, work execute, experiment submit, legacy run (including dry-run), remote mutation, work approve, GPU allocation or training. No credential edits, new environment installation, destructive cleanup, account rotation, git push or merge. Prefer local checks; online checks require separate operator permission. Do not self-authorize a live phase or assume authorization from a configured account. Read-only local verification, packaging inspection, locally creating/reusing a Work Objective and validating a draft plan are permitted. If authorization/budget is missing, record it as a blocker; do not invent values or use placeholders as approved inputs. The testbed case intentionally leaves these unset.

## Observability contract

Initialize one persistent audit session under the testbed runtime directory, assign a session ID, and retain the same objective ID across restarts. Record every material operational decision BEFORE acting, including evidence, alternatives considered (briefly), selected action, and expected outcome. Record observed outcome AFTER acting. Record all failures, retries, doc lookups, timeouts, fallback choices, requests for human intervention and changes to assumptions. These are short operational summaries, not private chain-of-thought. Use `audit.py run` for safe commands where possible, or record equivalent command/exit/duration/output evidence manually. Do not log secrets, raw credential files, full environment dumps or unredacted private output. Link every checklist completion to actual evidence. Do not mark failed/unverified/skipped work as done. Do not hide failed commands or rewrite previous events. Track docs read and why.

Resolve and reuse the existing interpreter and state directory; inspect setup and work checkpoints. Look for a matching existing objective before creating another. Use one objective and three distinct experiment IDs. Read actual CLI help/docs as needed rather than guessing command flags. Make a bounded plan with explicit approval, immutable snapshots and separate scientific reporting per dataset. Verify both exit codes and JSON semantics. If a blocker is encountered, record the precise symptom, evidence, category, attempted resolution and whether operator help is required. Avoid speculative changes to the Kaggle Runner engine itself: list suggested improvements separately.

## Preparation deliverables

- Audit session ID, objective ID (or precise blocker preventing creation), runner/TBPS revisions, selected interpreter and checkpoint status.
- Three-run matrix with actual observed inputs, source/resource identity, expected outputs, seed/protocol, metric and estimates (unknowns marked explicitly).
- Minimal proposed adaptation diff and package/spec validation evidence, or an explicit blocker for each missing item. Do not modify the original notebook.
- Checklist status for every preparation step; live steps remain todo.
- Friction report: observed errors, resolution attempts, time, docs consulted, operator questions, suspected missing skill/setup support and priority.
- Handoff: exact next safe action and approvals still required.

Stop before approval or submission. The purpose is to observe real agent difficulties; do not invent success or quietly repair the engine. The operator will separately authorize any future pilot and remaining runs.

## Future live session (not authorized now)

Only after explicit operator approval of the exact plan, allowed account, accelerator, estimated and maximum compute budget, and internet policy: begin with one of the three required full runs; it counts as run 1, not a fourth pilot. Use the agent-first lifecycle. Confirm completion and actual valid artifacts before advancing. Between runs, safely end a Codex session and resume with the same objective and authoritative job state. Do not deliberately inject faults into a live push. Never resubmit an uncertain push. After the first run passes and the operator permits the remainder, finish the other two within the approved concurrency and budget. Report observed GPU availability and actual utilization separately. Do not aggregate R1 across different datasets as if they were independent seeds of one experiment. Mark completion only after all three valid results and reviewed acceptance evidence.
