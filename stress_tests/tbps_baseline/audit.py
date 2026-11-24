#!/usr/bin/env python3
"""Independent local audit journal; no Kaggle or runner imports."""
import argparse
import csv
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
KINDS = ('decision', 'observation', 'incident', 'doc_read', 'intervention', 'handoff', 'approval_note')
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


def connect(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = root / 'audit.sqlite3'
    con = sqlite3.connect(db, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, session TEXT NOT NULL, objective TEXT, kind TEXT NOT NULL, step TEXT, payload TEXT NOT NULL)')
    con.commit()
    return con


def add(root, session, kind, step=None, objective=None, **payload):
    if step and step not in {s['id'] for s in steps()}:
        raise ValueError('Unknown checklist step: ' + step)
    if not session or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', session):
        raise ValueError('Use a simple nonempty session identifier')
    with connect(root) as con:
        cur = con.execute('INSERT INTO events(ts,session,objective,kind,step,payload) VALUES(?,?,?,?,?,?)', (now(), session, objective, kind, step, json.dumps(redact(payload), ensure_ascii=False, allow_nan=False)))
        return cur.lastrowid


def read_events(root):
    if not (root / 'audit.sqlite3').exists():
        return []
    with connect(root) as con:
        return [dict(row) | {'payload': json.loads(row['payload'])} for row in con.execute('SELECT * FROM events ORDER BY seq')]


def report(root):
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
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root/'events.jsonl').write_text(''.join(json.dumps(e, ensure_ascii=False)+'\n' for e in events), encoding='utf-8')
    with (root/'checklist.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['id','phase','title','status','evidence','event_id']); writer.writeheader(); writer.writerows(checks.values())
    lines = ['# Audit snapshot', '', 'Generated: '+now(), '', '```json', json.dumps(stats, indent=2), '```', '', '## Checklist', '']
    for s in checks.values():
        lines.append(f"- [{'x' if s['status']=='done' else ' '}] {s['id']} {s['title']} — {s['status']} | {s['evidence']} | event {s['event_id']}")
    lines.extend(['', '## Incidents', ''])
    lines.extend(f"- #{e['seq']} {e['step'] or '-'}: {e['payload'].get('summary','')} | {e['payload'].get('reason','')} | {e['payload'].get('evidence','')}" for e in incidents)
    lines.extend(['', '## Review notes', 'This is an audit snapshot, not an acceptance decision. Compare with runner ledger and Codex tool history. Command time is not total wall time; parallel commands may overlap. An unfinished command does not establish remote outcome.'])
    (root/'audit-summary.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    return stats


def obvious_live(argv):
    # Defense in depth: catch common direct invocations. Not an authorization sandbox.
    joined = ' '.join(argv).lower()
    return bool(re.search(r'\b(?:experiment\s+submit|work\s+(?:execute|approve)|kernels\s+(?:push|delete|cancel)|kgr\s+run|kaggle_runner\s+run|git\s+push)\b', joined))


def run_command(root, args):
    argv = args.argv[:]
    if argv and argv[0] == '--': argv.pop(0)
    if not argv: raise ValueError('Missing command after --')
    if obvious_live(argv) and not args.live:
        seq = add(root,args.session,'incident',args.step,args.objective,summary='Rejected obvious live action in preparation mode',reason='Explicit approval required',evidence='No command executed',command=argv)
        print(f'Blocked live action; audit event {seq}', file=sys.stderr)
        return 3
    if args.live and not args.approval_reference:
        raise ValueError('--live requires a reference to separate operator approval; it does not grant approval')
    cid = uuid.uuid4().hex
    start = time.monotonic()
    add(root,args.session,'command_start',args.step,args.objective,command_id=cid,argv=argv,cwd=str(Path.cwd()),approval_reference=args.approval_reference)
    outdir = root/'commands'; outdir.mkdir(mode=0o700, exist_ok=True)
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
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=HERE/'runtime',help='Private audit journal directory')
    sub=p.add_subparsers(dest='action',required=True)
    for name in ('init','event','check','run'):
        q=sub.add_parser(name); q.add_argument('--session',required=True); q.add_argument('--objective'); q.add_argument('--step')
        if name=='event':
            q.add_argument('--kind',choices=KINDS,required=True); q.add_argument('--summary',required=True); q.add_argument('--reason',default=''); q.add_argument('--evidence',default=''); q.add_argument('--alternatives',default=''); q.add_argument('--expected',default='')
        if name=='check':
            q.add_argument('--status',choices=STATES,required=True); q.add_argument('--evidence',default='')
        if name=='run':
            q.add_argument('--timeout',type=float,default=None); q.add_argument('--live',action='store_true'); q.add_argument('--approval-reference'); q.add_argument('argv',nargs=argparse.REMAINDER)
    sub.add_parser('report')
    a=p.parse_args(argv)
    try:
        if a.action=='init':
            # Journal is reused; every new session gets one explicit start event.
            events=read_events(a.root)
            if not any(e['kind']=='session_start' and e['session']==a.session for e in events):
                print(add(a.root,a.session,'session_start',objective=a.objective,case_sha256=sha(HERE/'case.json'),checklist_sha256=sha(HERE/'checklist.json')))
        elif a.action=='event':
            print(add(a.root,a.session,a.kind,a.step,a.objective,summary=a.summary,reason=a.reason,evidence=a.evidence,alternatives=a.alternatives,expected=a.expected))
        elif a.action=='check':
            if not a.step: raise ValueError('--step is required')
            if a.status in ('done','blocked','skipped','stale') and not a.evidence.strip(): raise ValueError('Evidence is required for this status')
            print(add(a.root,a.session,'check',a.step,a.objective,status=a.status,evidence=a.evidence))
        elif a.action=='run': return run_command(a.root,a)
        else: print(json.dumps(report(a.root),indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(redact(str(exc)),file=sys.stderr); return 2


if __name__=='__main__':
    raise SystemExit(main())
