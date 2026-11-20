# Documentation map

These documents are the supported navigation path. Implementation and tests define behavior where wording differs.

| Task | Canonical guide |
| --- | --- |
| Install and run an offline example | [QUICKSTART.md](QUICKSTART.md) |
| Submit, recover, monitor and report experiments | [AGENT_WORKFLOW.md](AGENT_WORKFLOW.md) |
| Understand modules, lifecycle and persistence | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Find command flags and JSON behavior | [CLI_REFERENCE.md](CLI_REFERENCE.md) |
| Read spec, result and packaging schemas | [CONTRACTS.md](CONTRACTS.md) |
| Configure credentials and handle incidents | [OPERATIONS.md](OPERATIONS.md) |

The root [AGENTS.md](../AGENTS.md) is the concise entry point for coding agents. Older P0, roadmap and legacy audits are historical snapshots; see [archive](archive/README.md). They are not current command references.

## Status labels

- **Implemented:** available in the checked-in code.
- **Offline tested:** covered by tests that use local state or fakes; this does not prove Kaggle behavior.
- **Unverified:** depends on Kaggle or another external service and has not been verified in this task.
- **Planned:** not implemented.
