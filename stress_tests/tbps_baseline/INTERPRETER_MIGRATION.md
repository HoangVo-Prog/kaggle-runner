# Windows interpreter migration procedure

This procedure appends an operator-authorized Python interpreter transition to
the existing V3 audit journal. It never updates an existing `state_bound`
event and never replaces the Runner ledger. Review the exact interpreter and
ledger paths before running it. Do not run it as part of a diagnostic-only task.

## Identity finding

The Windows implementation now records version 2 identities from
`GetFileInformationByHandleEx(FileIdInfo)`: a fixed-width 64-bit volume serial
and the full 128-bit file ID. This avoids depending on Python's version-specific
`st_dev` representation. POSIX continues to use the existing `device`/`inode`
pair.

Legacy Windows `device`/`inode` records are accepted only when the full legacy
device value equals the native volume serial, the inode equals the native file
ID's low 64 bits, and the file ID's upper 64 bits are zero. A truncated device
value, mismatched full volume serial, malformed identity, unsupported API, or
replacement file is rejected.

## Operator-reviewed steps

Use the canonical Python executable for every command. The `--root` value must
be the already-existing V3 audit directory, not `runtime/` or `runtime_v2/`.
Choose a new session ID. The migration command requires explicit authorization
evidence and a reason, verifies the bound ledger identity twice, verifies the
old executable by metadata only, and verifies the requested target is the
interpreter currently running the audit command.

```powershell
$Python = 'D:\Miniconda\envs\python_311\python.exe'
$Repo = 'D:\Programming\Python\kaggle-runner'
$Audit = '<existing V3 audit directory>'
$Session = '<new unique session id>'
Set-Location -LiteralPath $Repo
& $Python stress_tests\tbps_baseline\audit.py --root $Audit init --session $Session --objective tbps-baseline-3gpu --runner-root $Repo
& $Python stress_tests\tbps_baseline\audit.py --root $Audit migrate-interpreter --session $Session --objective tbps-baseline-3gpu --new-python $Python --authorization-evidence '<operator authorization reference>' --reason '<why this interpreter migration is approved>'
& $Python stress_tests\tbps_baseline\audit.py --root $Audit discover --session $Session --objective tbps-baseline-3gpu --python $Python
```

After verified discovery, run offline preflight through the audit wrapper and
continue stateful reads only if it succeeds:

```powershell
& $Python stress_tests\tbps_baseline\audit.py --root $Audit kgr --session $Session --objective tbps-baseline-3gpu --python $Python --step A03 -- setup preflight --json
```

The wrapper adds the bound state path, expected path, and audit directory. If
preflight reports an access failure, stop and have the operator verify that
exact path and execution identity. Do not change ACLs, reset state, update the
bootstrap pointer by hand, or run live Kaggle operations.
