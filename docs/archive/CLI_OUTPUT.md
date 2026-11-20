# kgr command output

The CLI prints short, human-readable summaries by default. Raw diagnostic
JSON is only printed when `--json` is specified. It works before or after
commands, including nested ones:

```bash
kgr check --gpu t4x2 --required-gpu-hours 2
kgr quota
kgr active
kgr jobs
kgr report --refresh

kgr check --gpu t4x2 --required-gpu-hours 2 --json
kgr --json jobs
kgr auth show --json
```

`kgr check` gives a short READY/BLOCKED decision and reasons. `kgr active`
shows up to 10 active jobs without dumping missing historical sessions.
`kgr jobs` and `kgr report` show concise tables. `kgr run` prints the job ID,
canonical Kaggle URL and a next command; it never includes the API token.

`--json` retains the existing structured payload, including extra diagnostics
and resource links, for Python scripts and CI. The GPU smoke example explicitly
requests it because it consumes machine-readable output.

`kgr logs` remains raw text on stdout. `kgr report --output FILE.json`
continues writing the full JSON file even in normal human-output mode.

## Concurrency semantics

The default fast preflight checks **only jobs created by this runner** with the
same local state directory (which should be dedicated to one Kaggle account).
The `KAGGLE_OWNER` metadata label is not used as a filter because Kaggle can
return a different canonical account name during submit. The two-job cap is shared by CPU and GPU:
one CPU job and one GPU job count as two slots. The third submission is blocked
if both still report running, queued, or pending. `NEW_SCRIPT` observed within
two minutes after push is temporarily treated as pending to protect against
Kaggle session-state propagation delays.

`CANCEL_ACKNOWLEDGED` is shown separately and not automatically counted active.
Completed jobs release their slot. For older notebooks or jobs created outside
this runner, use the optional `--remote` scan; it is not an authoritative list
of interactive sessions. The server ultimately determines capacity, and this
local check is not a cross-machine atomic reservation.

## Historical jobs and incorrect Kaggle owner

Some old jobs were submitted with an incorrect owner alias, such as
`hoang_vo/slug` rather than the canonical `hoanggvo/slug`. Fast preflight
tries an alternative ref from account metadata or a stored Kaggle submit URL,
and **persists it only after a valid Kaggle status response**.

An inaccessible old record is shown under `needs attention`; it does not
make `kgr active` crash, but blocks another submit until resolved.

```bash
kgr active
kgr repair JOB_ID --owner hoanggvo
kgr status JOB_ID
# If and ONLY if Kaggle Active Events confirms there is no live session:
kgr archive JOB_ID --confirm-inactive
kgr check --gpu t4x2 --required-gpu-hours 2
```

Archiving keeps history and files intact; it never cancels a remote kernel.
Correcting a ref does not validate remote access by itself. New jobs remain
strictly tracked, preventing a third CPU/GPU submission under the two-job cap.
