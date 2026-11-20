# Agent attempt states (candidate v1.0)

```
reserved -> staging -> submitting -> submitted -> running -> remote_complete
                   \-> unknown_remote ------^            \-> failed/cancelled
remote_complete -> succeeded (scientific validation passed)
remote_complete -> invalid_result (validation failed)
remote_complete -> fetch_failed -> remote_complete (resume download)
```

All state changes are validated against a transition graph and recorded as append-only timestamped events. The existing `jobs` table retains Kaggle-native status (e.g. `complete`) and historical jobs; agent attempts use a separate `agent_attempts` table with `scientific_status`.

`unknown_remote` is deliberate: an interrupted push, API timeout or unexpected response may have completed remotely. The corresponding local capacity lease **must not** be released automatically. A confirmed remote terminal state releases the lease; lease expiry alone only identifies a stale lease for manual review.
