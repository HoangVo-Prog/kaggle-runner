# Runner state-location migration readiness

This is a reviewed Windows operator procedure. The V3 audit command verifies a
prepared copy, appends `state_location_migration`, and advances the bootstrap
pointer atomically. It does not copy the ledger, move the source, change ACLs,
or authorize Runner/Kaggle work. Run it only after separate explicit operator
authorization. Use an existing V3 audit directory and session; never create or
rewrite a historical audit journal.

## Design and invariants

- Quiesce all Runner processes and operators first. Do not migrate while any
  submission, reconciliation, fetch, report, or other state operation is active.
- Use SQLite's online backup API to create `jobs.sqlite3` at the destination.
  Reject source `-wal`/`-shm` sidecars after quiescing; do not copy them as
  ordinary files. Copy every other state artifact with its relative path and
  verify its SHA-256. The audit transition command checks `integrity_check`,
  objective creation time and revision, a deterministic SQLite dump digest,
  side-artifact hashes, and source/destination native file identities.
- Copy rather than move the database. The destination necessarily has a new
  Windows file ID. Keep the source as a rollback archive. After the destination
  copy is verified, rename the source directory to a unique sibling archive and
  create a Windows directory junction at the original path targeting the new
  state directory. This leaves one live ledger; the archive is offline. The
  junction preserves existing absolute `spec_path` references recorded in
  `work_attempt_links` and keeps old-path invocations routed to the new ledger.
- The original audit `state_bound` and all `interpreter_migration` events remain
  byte-for-byte unchanged. A new state-location event records the old/new path
  and native ledger identity. The bootstrap pointer transition is atomic and
  retains its original bytes in `.kgr-bootstrap.json.pre-state-migration`.
- If the audit append succeeds but pointer replacement is interrupted, rerun
  the same audited transition command. It recognizes the already-appended event,
  verifies the destination identity and completes only the pointer update.
- Do not edit rows, revisions, experiment/attempt IDs, immutable specs, or
  historical report content during migration. Absolute source-root paths in
  specs are scientific provenance and are not rewritten. The old-path junction
  also preserves state-relative absolute paths. If any path is unavailable
  through that junction, stop and roll back before resuming Runner operations.

The actual state inspected during preparation had `work_attempt_links` but zero
linked specs. The path column is nevertheless absolute, so the junction remains
required for future and historical state compatibility.

## Operator commands for a separately authorized migration

Set the exact old path from the verified bootstrap pointer and the provisioned
candidate. Select a unique archive name. Set `$Audit` and `$Session` to the
already-existing V3 journal and initialized session. Keep the canonical Python
for every command.

```powershell
$Python = 'D:\Miniconda\envs\python_311\python.exe'
$Repo = 'D:\Programming\Python\kaggle-runner'
$Source = '<exact state_directory from the verified bootstrap pointer>'
$Destination = 'D:\Programming\Python\kaggle-runner\_runner-state'
$Archive = "$Source.pre-state-migration-<unique-id>"
$Audit = '<existing V3 audit directory>'
$Session = '<initialized V3 session id>'
$Context = Join-Path $Repo '.kgr-bootstrap.json'

# Reconfirm identity and stop every Runner process before proceeding.
whoami /user
whoami /groups
Get-ChildItem -Force -LiteralPath $Destination
Get-Acl -LiteralPath $Source
Get-Acl -LiteralPath $Destination
```

After confirming the destination is empty and both identities retain write
access, use a unique staging directory next to the destination. The following
Python block refuses SQLite sidecars, backs up the database consistently, and
copies all other state files without following symlinks:

```powershell
$Stage = "$Destination.stage-<unique-id>"
@'
import shutil, sqlite3, sys
from pathlib import Path
source, stage = map(Path, sys.argv[1:])
stage.mkdir()
db = source / 'jobs.sqlite3'
if not db.is_file() or Path(str(db) + '-wal').exists() or Path(str(db) + '-shm').exists():
    raise SystemExit('STOP: missing ledger or SQLite sidecars remain')
src = sqlite3.connect(db)
dst = sqlite3.connect(stage / 'jobs.sqlite3')
src.backup(dst)
dst.close(); src.close()
for item in source.rglob('*'):
    rel = item.relative_to(source)
    if rel.as_posix() == 'jobs.sqlite3':
        continue
    if item.is_symlink():
        raise SystemExit(f'STOP: symlink requires review: {rel}')
    target = stage / rel
    if item.is_dir(): target.mkdir(parents=True, exist_ok=True)
    elif item.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
'@ | & $Python - $Source $Stage
if ($LASTEXITCODE -ne 0) { throw "State staging failed: $LASTEXITCODE" }
```

Review the staged copy before installation. Rename the empty candidate directory
to a unique placeholder, move the stage into the exact destination path, archive
the source, then create the compatibility junction. If any rename or junction
operation fails, stop and restore the source archive to `$Source`; do not run
Runner commands.

```powershell
$EmptyCandidate = "$Destination.empty-<unique-id>"
if ((Get-ChildItem -Force -LiteralPath $Destination | Measure-Object).Count -ne 0) { throw 'Destination is not empty' }
Move-Item -LiteralPath $Destination -Destination $EmptyCandidate
Move-Item -LiteralPath $Stage -Destination $Destination
Move-Item -LiteralPath $Source -Destination $Archive
New-Item -ItemType Junction -Path $Source -Target $Destination | Out-Null
```

Append the state-location transition only after the junction exists and the
archive still contains the source ledger. This audits the retained source
archive while verifying the old bound path, new destination, and bootstrap
pointer transition:

```powershell
Set-Location -LiteralPath $Repo
& $Python stress_tests\tbps_baseline\audit.py --root $Audit migrate-state-location `
  --session $Session --objective tbps-baseline-3gpu `
  --source-dir $Source --source-data-dir $Archive --destination-dir $Destination `
  --context-file $Context `
  --authorization-evidence '<operator approval reference>' `
  --reason '<reviewed migration rationale>'
if ($LASTEXITCODE -ne 0) { throw "Audited state transition failed: $LASTEXITCODE" }
& $Python -m kaggle_runner --state-dir $Destination setup discover `
  --objective-id tbps-baseline-3gpu --json
```

Require successful, unique discovery; compare the destination identity in the
result with the appended audit transition; then run offline `setup preflight`
under both operator and Codex identities using `--expected-state-dir
$Destination`. Resume other stateful Runner operations only after both checks
pass. Keep the source archive until the operator separately accepts migration
and its rollback window closes.

## Rollback

Quiesce Runner again. Remove the old-path junction, restore the original source
archive to the exact old path, then use a reverse `migrate-state-location`
transition with the current destination as `--source-dir` and the restored old
path as `--destination-dir`. The audit command appends a second transition and
atomically advances the pointer back; it never deletes or rewrites the forward
event. After the reverse transition verifies, move the failed destination to a
unique offline quarantine path. Keep both archives until a separate operator
review closes rollback.

If failure occurs before the audit event is appended, keep Runner quiesced and
restore the original source path from its archive before resuming. If failure
occurs after the event append, do not manually edit the pointer or audit: rerun
the same transition command to finish pointer recovery, or execute the reviewed
reverse procedure. A mismatched identity, logical digest, artifact manifest,
objective timestamp/revision, audit binding, or bootstrap pointer stops the
procedure.

The procedure does not test remote Kaggle state, change ACLs, or make the two
Windows identities share access automatically. Cross-identity scratch probes
and ACL review are operator prerequisites.
