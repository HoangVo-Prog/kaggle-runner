# Recovery, idempotency, and safe retries

1. Inspect `kgr experiment show EXPERIMENT_ID --json` and the corresponding Kaggle kernel reference.
2. Run `kgr reconcile --experiment-id EXPERIMENT_ID --json` using the original profile's credentials. Reconciliation performs remote status reads but never pushes, deletes or cancels a kernel.
3. For a confirmed completed kernel use `kgr experiment resume EXPERIMENT_ID --json` (or `--wait` with a bounded deadline) to fetch files and validate the scientific result.
4. If remote status is missing, forbidden, unknown or inaccessible, retain `unknown_remote` and request operator intervention. A 404 after timeout is not proof a push never happened.
5. For a *deliberate* new attempt, change `attempt_id` and validate a new immutable spec. Never do this silently in a retry loop.

Status/quota and output read failures use bounded retry with exponential backoff and jitter. Pushes are not automatically retried after ambiguous errors. Slot leases are stored in SQLite and renewed during reconciliation; expiry alone cannot free an uncertain remote submission. Lost credentials must be restored for the **same** profile/owner, never replaced by another team member's credentials.
