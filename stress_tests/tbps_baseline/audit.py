#!/usr/bin/env python3
"""Independent local audit journal; no Kaggle or runner imports."""
import argparse
import csv
from contextlib import contextmanager, closing
import getpass
import platform
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
STATES = ('todo', 'in_progress', 'done', 'blocked', 'skipped', 'stale')
KINDS = ('decision', 'observation', 'incident', 'doc_read', 'intervention', 'handoff', 'approval_note', 'retry', 'workaround', 'state_drift', 'escalation')
CATEGORIES = ('runner', 'testbed', 'environment', 'documentation', 'authorization', 'science', 'unconfirmed')
VERSION = 2
MARKER = 'audit-version.json'
# Best-effort defense only. Never supply secrets to this utility.
SECRET = re.compile(r'(?i)(["\']?(?:[\w-]*(?:api[_-]?token|api[_-]?key|password|secret|credential)[\w-]*|authorization|access[_-]?token)["\']?\s*[:=]\s*)(?:"[^"\n]*"|\'[^\'\n]*\'|[^\s,;}]+)')
BEARER = re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+')


def redact(value):
    if isinstance(value, dict):
        return {k: ('[REDACTED]' if re.search(r'(?i)token|password|secret|api.?key|credential', k) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(x) for x in value]
    if isinstance(value, str):
        value = SECRET.sub(r'\1[REDACTED]', value)
        return BEARER.sub('Bearer [REDACTED]', value)
    return value


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def steps():
    return json.loads((HERE / 'checklist.json').read_text())


def shared_mkdir(path):
    # Windows Python 3.13+ may apply a protected ACL for mode=0o700.
    # Do not set restrictive private ACLs on operator-shared artifacts.
    # The operator provisions the parent ACL/group; never chmod existing paths.
    if os.name == 'nt':
        path.mkdir(parents=True, exist_ok=True)
    else:
        path.mkdir(mode=0o770, parents=True, exist_ok=True)


def probe(root):
    result = {'path': str(root.absolute()), 'ok': False,
              'operator_access': 'unverified_cross_identity'}
    scratch = None
    created = False
    try:
        shared_mkdir(root)
        result['resolved_path'] = str(root.resolve())
        scratch = root / ('.audit-probe-' + uuid.uuid4().hex)
        scratch.mkdir(mode=0o770 if os.name != "nt" else 0o777)
        created = True
        f = scratch / 'write-test'
        f.write_bytes(b'audit-probe')
        if f.read_bytes() != b'audit-probe':
            raise OSError('Read-back mismatch')
        moved = scratch / 'rename-test'
        f.rename(moved); moved.unlink()
        db = scratch / 'probe.sqlite3'
        with closing(sqlite3.connect(db)) as con:
            with con:
                con.execute('CREATE TABLE probe (x INTEGER)')
                con.execute('INSERT INTO probe VALUES (1)')
            if con.execute('SELECT x FROM probe').fetchone()[0] != 1:
                raise sqlite3.DatabaseError('SQLite read-back mismatch')
        db.unlink(); scratch.rmdir(); scratch = None
        result['ok'] = True
    except (OSError, sqlite3.Error) as exc:
        result.update(error_type=type(exc).__name__, errno=getattr(exc, 'errno', None),
                      action='Ask operator to provision this exact shared directory for both identities; do not change credentials or global ACLs')
    finally:
        if scratch is not None and created and scratch.exists():
            # Only clean up files created by this invocation; never existing evidence.
            try:
                for f in scratch.iterdir():
                    if f.is_file(): f.unlink()
                scratch.rmdir()
            except OSError:
                result.update(ok=False, cleanup_failed=True)
    return result


def ensure_v2(root):
    if root.name.casefold() == 'runtime' or root.resolve().name.casefold() == 'runtime' or root.resolve().is_relative_to((HERE/'runtime').resolve()):
        raise ValueError('V1 runtime is historical evidence; use runtime_v2 or a new operator-provisioned directory')
    marker = root / MARKER
    if marker.exists():
        if json.loads(marker.read_text(encoding='utf-8')) != {'version': VERSION, 'case_id': 'tbps-baseline-3gpu', 'previous': 'V1'}:
            raise ValueError('Audit version mismatch; preserve existing evidence')
        return
    if any((root / name).exists() for name in ('audit.sqlite3', 'events.jsonl', 'checklist.csv', 'audit-summary.md', 'handoff.md')):
        raise ValueError('Unversioned existing audit evidence; choose a fresh V2 directory')
    verdict = probe(root)
    if not verdict['ok']:
        raise OSError('AUDIT_DIRECTORY_INACCESSIBLE: ' + json.dumps(verdict))
    try:
        with marker.open('x', encoding='utf-8') as f:
            json.dump({'version': VERSION, 'case_id': 'tbps-baseline-3gpu', 'previous': 'V1'}, f)
    except FileExistsError:
        if json.loads(marker.read_text(encoding='utf-8')) != {'version': VERSION, 'case_id': 'tbps-baseline-3gpu', 'previous': 'V1'}:
            raise ValueError('Audit version mismatch')


@contextmanager
def connect(root, *, readonly=False):
    if not readonly:
        ensure_v2(root)
    db = root / 'audit.sqlite3'
    uri = db.resolve().as_uri() + '?mode=ro' if readonly else str(db)
    with closing(sqlite3.connect(uri, timeout=30, uri=readonly)) as con:
        con.row_factory = sqlite3.Row
        with con:
            if not readonly:
                con.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, session TEXT NOT NULL, objective TEXT, kind TEXT NOT NULL, step TEXT, payload TEXT NOT NULL)')
            yield con


def startup(root, session, objective, runner_root):
    """Capture the initial observable state, before any runner invocation."""
    runner_root = runner_root.resolve()
    agents = runner_root / 'AGENTS.md'
    agent_hash = sha(agents)
    attempts = []
    def git(*args):
        base = ['git', '-C', str(runner_root)]
        for prefix in (base, ['git', '-c', f'safe.directory={runner_root.as_posix()}', '-C', str(runner_root)]):
            try:
                p = subprocess.run(prefix + list(args), capture_output=True, text=True, errors='replace', timeout=15)
                attempts.append({'argv': prefix+list(args), 'exit_code': p.returncode, 'stderr': redact(p.stderr)})
                if p.returncode == 0: return p.stdout.strip()
                if 'dubious ownership' not in p.stderr.lower(): break
            except (OSError, subprocess.TimeoutExpired) as exc:
                attempts.append({'argv': prefix+list(args), 'error_type': type(exc).__name__}); break
        return None
    revision = git('rev-parse', 'HEAD')
    status = git('status', '--porcelain', '--untracked-files=no')
    branch = git('rev-parse', '--abbrev-ref', 'HEAD')
    # Never claim that an agent actually read the file: compare the tool history.
    return add(root, session, 'session_start', objective=objective,
               case_sha256=sha(HERE/'case.json'), checklist_sha256=sha(HERE/'checklist.json'),
               testbed_version=VERSION, agents_sha256=agent_hash, revision=revision,
               branch=branch, tracked_worktree_status=status, git_attempts=attempts,
               identity=getpass.getuser(), os=platform.platform(), python=sys.executable,
               repo_root=str(runner_root), audit_root=str(root.resolve()),
               startup_evidence_complete=revision is not None and status is not None and branch is not None)


def require_session(root, session):
    if not any(e['kind']=='session_start' and e['session']==session and e['payload'].get('testbed_version')==VERSION for e in read_events(root)):
        raise ValueError('Initialize this V2 session immediately after reading root AGENTS.md, before runner commands')


def add(root, session, kind, step=None, objective=None, **payload):
    if step and step not in {s['id'] for s in steps()}:
        raise ValueError('Unknown checklist step: ' + step)
    if not session or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', session):
        raise ValueError('Use a simple nonempty session identifier')
    with connect(root) as con:
        if kind == 'session_start':
            con.execute('BEGIN IMMEDIATE')
            prior = con.execute("SELECT seq FROM events WHERE kind='session_start' AND session=?", (session,)).fetchone()
            if prior is not None: return prior[0]
        cur = con.execute('INSERT INTO events(ts,session,objective,kind,step,payload) VALUES(?,?,?,?,?,?)', (now(), session, objective, kind, step, json.dumps(redact(payload), ensure_ascii=False, allow_nan=False)))
        return cur.lastrowid


def read_events(root):
    if not (root / 'audit.sqlite3').exists():
        return []
    with connect(root, readonly=True) as con:
        return [dict(row) | {'payload': json.loads(row['payload'])} for row in con.execute('SELECT * FROM events ORDER BY seq')]


def atomic_text(path, body):
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('x', encoding='utf-8', newline='') as stream:
            stream.write(body)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists(): temp.unlink()


def report(root):
    ensure_v2(root)
    events = read_events(root)
    checks = {s['id']: dict(s, status='todo', evidence='', event_id='') for s in steps()}
    for e in events:
        if e['kind'] == 'check':
            checks[e['step']].update(status=e['payload']['status'], evidence=e['payload']['evidence'], event_id=e['seq'])
    ends = [e for e in events if e['kind'] == 'command_end']
    started = {e['payload']['command_id'] for e in events if e['kind'] == 'command_start'}
    ended = {e['payload']['command_id'] for e in ends}
    incidents = [e for e in events if e['kind'] == 'incident']
    stats = {'events': len(events), 'decisions': sum(e['kind']=='decision' for e in events), 'commands_finished': len(ends), 'failed_commands': sum(e['payload']['exit_code'] != 0 for e in ends), 'unfinished_commands': sorted(started-ended), 'command_elapsed_seconds': round(sum(e['payload']['elapsed_seconds'] for e in ends), 3), 'incidents': len(incidents), 'doc_reads': sum(e['kind']=='doc_read' for e in events), 'operator_interventions': sum(e['kind']=='intervention' for e in events), 'sessions': sorted({e['session'] for e in events}), 'objective_ids': sorted({e['objective'] for e in events if e['objective']}), 'prepare_done': sum(s['phase']=='prepare' and s['status']=='done' for s in checks.values()), 'prepare_total': sum(s['phase']=='prepare' for s in checks.values())}
    stats['cli_help_lookups'] = sum(any(flag in e['payload'].get('argv', []) for flag in ('--help','-h')) for e in events if e['kind']=='command_start')
    stats['incident_categories'] = {k: sum(e['payload'].get('category')==k for e in incidents) for k in CATEGORIES}
    stats['retries'] = sum(e['kind']=='retry' for e in events)
    stats['workarounds'] = sum(e['kind']=='workaround' for e in events)
    stats['state_drift_events'] = sum(e['kind']=='state_drift' for e in events)
    stats['escalations'] = sum(e['kind']=='escalation' for e in events)
    stats['operator_access'] = 'unverified_cross_identity'
    stats['prepare_blocked'] = sum(s['phase']=='prepare' and s['status']=='blocked' for s in checks.values())
    stats['prepare_stale'] = sum(s['phase']=='prepare' and s['status']=='stale' for s in checks.values())
    stats['live_done'] = sum(s['phase']=='live' and s['status']=='done' for s in checks.values())
    stats['unclassified_incidents'] = sum(e['payload'].get('category') not in CATEGORIES for e in incidents)
    atomic_text(root/'events.jsonl', ''.join(json.dumps(e, ensure_ascii=False)+'\n' for e in events))
    import io
    f = io.StringIO(newline='')
    writer = csv.DictWriter(f, fieldnames=['id','phase','title','status','evidence','event_id']); writer.writeheader(); writer.writerows(checks.values())
    atomic_text(root/'checklist.csv', f.getvalue())
    lines = ['# Audit snapshot', '', 'Generated: '+now(), '', '```json', json.dumps(stats, indent=2), '```', '', '## Checklist', '']
    for s in checks.values():
        lines.append(f"- [{'x' if s['status']=='done' else ' '}] {s['id']} {s['title']} — {s['status']} | {s['evidence']} | event {s['event_id']}")
    lines.extend(['', '## Incidents', ''])
    lines.extend(f"- #{e['seq']} {e['step'] or '-'}: {e['payload'].get('summary','')} | {e['payload'].get('reason','')} | {e['payload'].get('evidence','')}" for e in incidents)
    lines.extend(['', '## Review notes', 'This is an audit snapshot, not an acceptance decision. Compare with runner ledger and Codex tool history. Command time is not total wall time; parallel commands may overlap. An unfinished command does not establish remote outcome.'])
    atomic_text(root/'audit-summary.md', '\n'.join(lines)+'\n')
    return stats


def obvious_live(argv):
    # Defense in depth, NOT a sandbox. Reject known remote Runner operations;
    # unknown Runner commands fail closed. No shell wrappers for remote actions.
    tokens = [str(v).lower() for v in argv]
    joined = ' '.join(tokens)
    if re.search(r'\b(?:git\s+(?:push|clone|fetch|pull)|pip\s+install|uv\s+pip\s+install|curl|wget)\b', joined):
        return True
    index = next((i for i,t in enumerate(tokens) if t == 'kaggle_runner' or re.split(r'[/\\]',t)[-1] in ('kgr','kgr.exe','kaggle','kaggle.exe')), None)
    if index is None:
        return False
    tail = tokens[index+1:]
    if '--help' in tail or '-h' in tail or '--version' in tail:
        return False
    if re.split(r'[/\\]',tokens[index])[-1] in ('kaggle','kaggle.exe'):
        return True
    # Global options appear before commands; do not interpret their values as commands.
    while tail and tail[0].startswith('--'):
        option = tail.pop(0)
        if option in ('--state-dir','--env-file','--profile','--config') and tail:
            tail.pop(0)
        elif option != '--json' and '=' not in option:
            return True
    if tail[:2] == ['setup','verify']:
        return '--offline' not in tail or '--online' in tail
    local = {('setup','status'),('setup','preflight'),('work','create'),('work','list'),('work','show'),('work','status'),('work','resume'),('work','plan'),('work','checkpoint'),('work','handoff'),('work','report'),('work','pause'),('package','inspect'),('experiment','validate'),('experiment','show'),('results','validate'),('results','aggregate'),('team','diagnose'),('team','list')}
    return tuple(tail[:2]) not in local


def run_command(root, args):
    argv = args.argv[:]
    if argv and argv[0] == '--': argv.pop(0)
    if not argv: raise ValueError('Missing command after --')
    if obvious_live(argv):
        seq = add(root,args.session,'incident',args.step,args.objective,summary='Rejected obvious live action in preparation mode',category='authorization',reason='Explicit approval required',evidence='No command executed',command=argv)
        print(f'Blocked live action; audit event {seq}', file=sys.stderr)
        return 3
    if args.live:
        raise ValueError('V2 preparation audit cannot authorize live actions; Stage B requires a separate reviewed protocol')
    runner_index = next((i for i,t in enumerate(argv) if t.lower() == 'kaggle_runner' or re.split(r'[/\\]',t.lower())[-1] in ('kgr','kgr.exe')), None)
    if runner_index is not None and not any(t in ('--help','-h','--version') for t in argv[runner_index+1:]):
        tail = argv[runner_index+1:]
        given = next((t.split('=',1)[1] for t in tail if t.startswith('--state-dir=')), None)
        if '--state-dir' in tail:
            i=tail.index('--state-dir'); given=tail[i+1] if i+1<len(tail) else None
        bound = [e['payload']['state_dir'] for e in read_events(root) if e['kind']=='state_bound']
        if not bound or not given or not Path(given).is_absolute() or os.path.normcase(str(Path(given).resolve())) != os.path.normcase(bound[-1]):
            add(root,args.session,'incident',args.step,args.objective,summary='State binding missing or mismatched',category='environment',reason='Explicit operator-confirmed --state-dir required',evidence='No command executed',command=argv)
            print('STATE_PATH_DRIFT: bind-state and use explicit --state-dir',file=sys.stderr); return 3
    cid = uuid.uuid4().hex
    start = time.monotonic()
    add(root,args.session,'command_start',args.step,args.objective,command_id=cid,argv=argv,cwd=str(Path.cwd()),approval_reference=args.approval_reference)
    outdir = root/'commands'; shared_mkdir(outdir)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, errors='replace', timeout=args.timeout)
        code, out, err = proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        code, out, err = 127, '', 'Executable not found'
    except subprocess.TimeoutExpired as exc:
        code = 124
        out = exc.stdout.decode(errors='replace') if isinstance(exc.stdout, bytes) else exc.stdout or ''
        err = exc.stderr.decode(errors='replace') if isinstance(exc.stderr, bytes) else exc.stderr or ''
        err += '\nCommand timed out; reconcile any uncertain side effects before retry.'
    except OSError as exc:
        code, out, err = 126, '', type(exc).__name__ + ': ' + str(exc)
    for name, data in [('stdout',out),('stderr',err)]:
        p = outdir/f'{cid}.{name}.txt'; p.write_text(redact(data),encoding='utf-8')
    seq = add(root,args.session,'command_end',args.step,args.objective,command_id=cid,exit_code=code,elapsed_seconds=round(time.monotonic()-start,3),stdout=str(outdir/f'{cid}.stdout.txt'),stderr=str(outdir/f'{cid}.stderr.txt'))
    print(redact(out),end=''); print(redact(err),end='',file=sys.stderr)
    print(f'\n[audit event {seq}; command {cid}]',file=sys.stderr)
    return code if code>=0 else 128-code


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path(os.environ.get('KGR_AUDIT_ROOT', HERE/'runtime_v2')),help='Operator-provisioned shared V2 audit directory')
    sub=p.add_subparsers(dest='action',required=True)
    for name in ('init','event','check','run'):
        q=sub.add_parser(name); q.add_argument('--session',required=True); q.add_argument('--objective'); q.add_argument('--step')
        if name=='init':
            q.add_argument('--runner-root',type=Path,default=HERE.parents[1])
        if name=='event':
            q.add_argument('--kind',choices=KINDS,required=True); q.add_argument('--summary',required=True); q.add_argument('--reason',default=''); q.add_argument('--evidence',default=''); q.add_argument('--alternatives',default=''); q.add_argument('--expected',default=''); q.add_argument('--category',choices=CATEGORIES,default='unconfirmed')
        if name=='check':
            q.add_argument('--status',choices=STATES,required=True); q.add_argument('--evidence',default='')
        if name=='run':
            q.add_argument('--timeout',type=float,default=None); q.add_argument('--live',action='store_true'); q.add_argument('--approval-reference'); q.add_argument('argv',nargs=argparse.REMAINDER)
    b=sub.add_parser('bind-state', help='Pin operator-confirmed state directory for this audit')
    b.add_argument('--session', required=True); b.add_argument('--state-dir', type=Path, required=True); b.add_argument('--evidence', required=True); b.add_argument('--objective')
    sub.add_parser('report')
    sub.add_parser('preflight')
    a=p.parse_args(argv)
    try:
        if a.action=='preflight':
            if a.root.name.casefold() == 'runtime' or a.root.resolve().name.casefold() == 'runtime' or a.root.resolve().is_relative_to((HERE/'runtime').resolve()) or ((a.root/'audit.sqlite3').exists() and not (a.root/MARKER).exists()): raise ValueError('Historical/unversioned directory must not be probed')
            result=probe(a.root); print(json.dumps(result,indent=2)); return 0 if result['ok'] else 2
        if a.action=='init':
            ensure_v2(a.root)
            # Journal is reused; every new session gets one explicit start event.
            events=read_events(a.root)
            if not any(e['kind']=='session_start' and e['session']==a.session for e in events):
                print(startup(a.root,a.session,a.objective,a.runner_root))
        elif a.action=='bind-state':
            require_session(a.root,a.session)
            path = str(a.state_dir.resolve())
            if not a.state_dir.is_absolute() or not a.evidence.strip(): raise ValueError('Absolute state directory and operator evidence required')
            with connect(a.root) as con:
                con.execute('BEGIN IMMEDIATE')
                existing = [json.loads(row[0])['state_dir'] for row in con.execute("SELECT payload FROM events WHERE kind='state_bound'")]
                if existing and any(os.path.normcase(x)!=os.path.normcase(path) for x in existing):
                    raise ValueError('STATE_PATH_DRIFT: original binding differs; preserve evidence and ask operator')
                if not existing:
                    con.execute('INSERT INTO events(ts,session,objective,kind,step,payload) VALUES(?,?,?,?,?,?)', (now(),a.session,a.objective,'state_bound','A03',json.dumps(redact({'state_dir':path,'evidence':a.evidence}))))
            print(json.dumps({'state_dir':path,'ok':True}))
        elif a.action=='event':
            require_session(a.root,a.session)
            print(add(a.root,a.session,a.kind,a.step,a.objective,summary=a.summary,reason=a.reason,evidence=a.evidence,alternatives=a.alternatives,expected=a.expected,category=a.category))
        elif a.action=='check':
            require_session(a.root,a.session)
            if not a.step: raise ValueError('--step is required')
            if a.step.startswith('D') and a.status != 'todo': raise ValueError('Live checklist remains todo in V2 preparation mode')
            if a.status in ('done','blocked','skipped','stale') and not a.evidence.strip(): raise ValueError('Evidence is required for this status')
            print(add(a.root,a.session,'check',a.step,a.objective,status=a.status,evidence=a.evidence))
        elif a.action=='run':
            require_session(a.root,a.session)
            return run_command(a.root,a)
        else: print(json.dumps(report(a.root),indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(redact(str(exc)),file=sys.stderr); return 2


if __name__=='__main__':
    raise SystemExit(main())
