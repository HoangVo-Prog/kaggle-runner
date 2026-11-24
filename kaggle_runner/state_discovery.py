"""Bounded, offline discovery of existing local state. Never initializes a ledger."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tomllib

from .config import default_config_path, state_directory

CONTEXT_NAME = '.kgr-bootstrap.json'
ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{2,119}\Z')


def absolute(value):
    p = Path(value).expanduser()
    if not p.is_absolute():
        raise ValueError('State discovery paths must be absolute')
    return p.resolve()


def ledger_identity(path: Path, objective_id: str | None = None) -> dict:
    """Read only the schema and requested objective, never credential/job data."""
    db = path / 'jobs.sqlite3'
    try:
        before = db.stat()
        if not db.is_file():
            return {'status': 'unreadable', 'reason': 'ledger_not_regular_file'}
        # A WAL database without an existing shared-memory file may require SQLite
        # to create a sidecar even in mode=ro. Do not do that during discovery.
        if Path(str(db) + '-wal').exists() and not Path(str(db) + '-shm').exists():
            return {'status': 'unreadable', 'reason': 'wal_requires_operator_review'}
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as con:
            con.execute('PRAGMA query_only=ON')
            tables = {x[0] for x in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'jobs' not in tables:
                return {'status': 'unreadable', 'reason': 'not_runner_ledger'}
            if con.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                return {'status': 'unreadable', 'reason': 'integrity_check_failed'}
            row = None
            if objective_id and 'work_objectives' in tables:
                row = con.execute('SELECT objective_id,created_at FROM work_objectives WHERE objective_id=?', (objective_id,)).fetchone()
        after = db.stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            return {'status': 'unreadable', 'reason': 'ledger_changed_during_probe'}
        # Inodes are identity evidence, not a content hash or an authorization.
        if not after.st_ino:
            return {'status': 'unreadable', 'reason': 'stable_file_identity_unavailable'}
        ident = {'device': after.st_dev, 'inode': after.st_ino}
        return {'status': 'match' if row else 'objective_absent' if objective_id else 'ledger_present',
                'identity': ident, 'objective': {'id': row[0], 'created_at': row[1]} if row else None}
    except FileNotFoundError:
        return {'status': 'missing'}
    except (OSError, sqlite3.Error, ValueError) as exc:
        return {'status': 'unreadable', 'error_type': type(exc).__name__}


def discover(*, objective_id=None, state_dir=None, config=None, context_file=None, remember=False):
    """Discover, not resolve defaults for a new ledger. With --remember, write only
    a local pointer after a verified selection. Never change an existing pointer."""
    if objective_id and not ID_RE.fullmatch(objective_id):
        raise ValueError('Invalid objective identifier')
    # Running from a checkout or explicit context is required to persist a pointer.
    # Do not write to site-packages or an arbitrary working directory by default.
    root = Path(__file__).resolve().parent.parent
    if not (root / 'AGENTS.md').is_file():
        root = Path.cwd() if (Path.cwd() / 'AGENTS.md').is_file() else None
    context = absolute(context_file) if context_file else (root / CONTEXT_NAME if root else None)
    problems = []
    candidates = {}
    cached = None

    def add(value, source):
        if not value:
            return
        try:
            path = absolute(value)
            key = os.path.normcase(str(path))
            entry = candidates.setdefault(key, {'path': str(path), 'sources': []})
            if source not in entry['sources']:
                entry['sources'].append(source)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            problems.append({'source': source, 'error_type': type(exc).__name__})

    if context:
        try:
            cached = json.loads(context.read_text(encoding='utf-8'))
            if (not isinstance(cached, dict) or cached.get('schema_version') != 1 or
                    not isinstance(cached.get('identity'), dict) or
                    not isinstance(cached.get('objective'), dict) or
                    not ID_RE.fullmatch(str(cached['objective'].get('id', ''))) or
                    not isinstance(cached.get('state_directory'), str)):
                raise ValueError('Invalid bootstrap context')
            add(cached['state_directory'], 'bootstrap_context')
        except FileNotFoundError:
            cached = None
        except (OSError, ValueError, TypeError) as exc:
            problems.append({'source': 'bootstrap_context', 'error_type': type(exc).__name__})
            cached = None

    # Explicit path is an intentional constraint, not permission to fall back.
    if state_dir:
        add(state_dir, 'explicit')
    else:
        config_paths = [(Path(config).expanduser() if config else default_config_path(), 'configured')]
        homes = [('home', Path.home())]
        for name in ('HOME', 'USERPROFILE'):
            if os.getenv(name):
                homes.append((name, Path(os.environ[name])))
        if os.name == 'nt' and os.getenv('HOMEDRIVE') and os.getenv('HOMEPATH'):
            homes.append(('HOMEDRIVE_HOMEPATH', Path(os.environ['HOMEDRIVE'] + os.environ['HOMEPATH'])))
        if os.name == 'nt':
            # Some packaged Windows apps redirect LOCALAPPDATA but retain the
            # normal login-name hint. Probe only that conventional profile path.
            username = os.getenv('USERNAME', '')
            drive = os.getenv('SystemDrive', '')
            if re.fullmatch(r'[A-Za-z0-9_. -]+', username) and username not in ('.', '..') and re.fullmatch(r'[A-Za-z]:', drive):
                homes.append(('windows_login_hint', Path(drive + '\\Users') / username))
        # No recursive search, no profile enumeration. Only current process home hints.
        for label, home in homes:
            if not home.is_absolute():
                continue
            if os.name == 'nt':
                add(home / 'AppData' / 'Local' / 'kaggle-runner', label + '_local_state')
                if not config and not os.getenv('KGR_CONFIG'):
                    config_paths.append((home / 'AppData' / 'Roaming' / 'kaggle-runner' / 'config.toml', label + '_config'))
            else:
                add(home / '.local' / 'state' / 'kaggle-runner', label + '_local_state')
                if not config and not os.getenv('KGR_CONFIG'):
                    config_paths.append((home / '.config' / 'kaggle-runner' / 'config.toml', label + '_config'))
        for path, label in dict.fromkeys(config_paths):
            try:
                with path.open('rb') as f:
                    cfg = tomllib.load(f)
                section = cfg.get('runner', {})
                if not isinstance(section, dict):
                    raise ValueError('Invalid runner config')
                add(section.get('state_dir'), label + '_runner.state_dir')
            except FileNotFoundError:
                if label == 'configured' and (config or os.getenv('KGR_CONFIG')):
                    problems.append({'source': label, 'error_type': 'configured_file_missing'})
            except (OSError, ValueError, TypeError) as exc:
                problems.append({'source': label, 'error_type': type(exc).__name__})
        add(os.getenv('KGR_STATE_DIR'), 'KGR_STATE_DIR')
        try:
            # state_directory honors KGR_STATE_DIR; include the true platform default too.
            add(state_directory(), 'resolved_default')
            if os.name == 'nt' and os.getenv('LOCALAPPDATA'):
                add(Path(os.environ['LOCALAPPDATA']) / 'kaggle-runner', 'LOCALAPPDATA')
            elif os.getenv('XDG_STATE_HOME'):
                add(Path(os.environ['XDG_STATE_HOME']) / 'kaggle-runner', 'XDG_STATE_HOME')
        except (OSError, ValueError, RuntimeError) as exc:
            problems.append({'source': 'platform_default', 'error_type': type(exc).__name__})

    for entry in candidates.values():
        entry.update(ledger_identity(Path(entry['path']), objective_id))
    matches = [e for e in candidates.values() if e['status'] == 'match']
    unreadable = [e for e in candidates.values() if e['status'] == 'unreadable']
    selected = None
    status = 'objective_required' if not objective_id else 'not_found'
    if cached:
        try:
            pin = absolute(cached['state_directory'])
            check = ledger_identity(pin, cached['objective']['id'])
            pin_ok = (check['status'] == 'match' and check['identity'] == cached['identity'] and
                      check['objective'] == cached['objective'])
        except (OSError, ValueError, KeyError, RuntimeError):
            pin_ok = False
        if not pin_ok:
            problems.append({'source': 'bootstrap_context', 'error_type': 'STATE_IDENTITY_DRIFT'})
        if state_dir and not any('explicit' in e['sources'] and 'bootstrap_context' in e['sources'] for e in candidates.values()):
            problems.append({'source': 'explicit', 'error_type': 'STATE_PATH_DRIFT'})
    if problems or unreadable:
        status = 'blocked'
    elif len(matches) > 1:
        status = 'ambiguous'
    elif len(matches) == 1:
        if cached and 'bootstrap_context' not in matches[0]['sources']:
            status = 'blocked'
            problems.append({'source': 'bootstrap_context', 'error_type': 'STATE_PATH_DRIFT'})
        else:
            status, selected = 'verified', matches[0]
    report = {'schema_version': 1, 'ok': selected is not None, 'status': status,
              'objective_id': objective_id, 'candidates': list(candidates.values()), 'problems': problems,
              'selected': selected, 'context_file': str(context) if context else None,
              'context_status': 'existing' if cached else 'not_written',
              'python_executable': sys.executable, 'runner_module': str(Path(__file__).resolve()),
              'offline': True, 'authorization': 'not_checked', 'write_access': 'not_checked',
              'action': 'Bind selected state, run offline preflight, then resume objective' if selected else
                        'Continue stateless preparation; ask operator only after reviewing candidate evidence. Never initialize a replacement ledger.'}
    if remember and selected and not cached:
        if context is None:
            report['context_status'] = 'unavailable_no_checkout'
        else:
            record = {'schema_version': 1, 'state_directory': selected['path'], 'identity': selected['identity'],
                      'objective': selected['objective'], 'python_executable': sys.executable,
                      'runner_module': report['runner_module']}
            try:
                # Exclusive create; a crash may leave an invalid pointer, which fails closed.
                with context.open('x', encoding='utf-8') as f:
                    json.dump(record, f, indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
                report['context_status'] = 'saved'
            except FileExistsError:
                report.update(ok=False,status='blocked',selected=None,context_status='concurrent_context_created',action='Re-run discovery to verify the newly created context; do not bind yet')
            except OSError as exc:
                report['context_status'] = 'write_failed'
                report['context_error_type'] = type(exc).__name__
    return report
