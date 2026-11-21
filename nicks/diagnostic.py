"""Failed candidates are review evidence, never canonical implementations.

Uses the ordinary Council snapshot/review engine and bounded plan amendments.
Call preparation and correction under the existing plan lock.
"""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess

from . import consensus, dev


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding='utf-8')
    tmp.replace(path)


def read(path):
    path = Path(path)
    record = json.loads(path.read_text())
    for name, digest in record['evidence'].items():
        if dev._sha((path.parent / name).read_bytes()) != digest:
            raise dev.DevError('diagnostic evidence changed: ' + name)
    if record.get('assessment'):
        saved = record['assessment']
        if dev._sha(Path(saved['response']).read_bytes()) != saved['sha256']:
            raise dev.DevError('diagnostic assessment changed after freeze')
    return record


def prepare(db, settings, plan, task_id):
    task = next((t for t in db.dev_tasks(plan['id']) if t['task_id'] == task_id), None)
    if not task or task['status'] not in dev.REPLACEABLE:
        raise dev.DevError('diagnostic review requires an unfinished inspectable task')
    attempts = [a for a in db.dev_attempts(plan['id'], task_id) if a['engine'] != 'integration']
    if not attempts or attempts[-1]['status'] != 'verify-failed':
        raise dev.DevError('diagnostic review requires an actual verification-failed candidate, not missing inference or a scope violation')
    attempt = attempts[-1]
    source = Path(attempt['dir']) / 'failed.patch'
    raw = source.read_bytes()
    if not raw.strip():
        raise dev.DevError('failed candidate has no inspectable patch')
    root = Path(plan['dir']) / 'diagnostics' / attempt['id']
    path = root / 'record.json'
    if path.exists():
        record = read(path)
        if record['patch_sha256'] != dev._sha(raw):
            raise dev.DevError('original failed patch changed after diagnostic freeze')
        return record
    # Incomplete preparation may be rebuilt; it has made no inference or parent mutation.
    if root.exists():
        from .snapshot import make_writable
        make_writable(root)
        shutil.rmtree(root)
    root.mkdir(parents=True)
    gd, wt = root / 'git', root / 'worktree'
    dev._clone(str(Path(attempt['dir']) / 'worktree'), gd, wt)
    start = attempt['detail']['start']
    dev.g(gd, wt, 'checkout', '--quiet', '--detach', start)
    applied = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', f'--git-dir={gd}', f'--work-tree={wt}',
                              'apply', '--index', '-'], input=raw, capture_output=True, cwd=wt, env=dev._git_env())
    if applied.returncode:
        raise dev.DevError('failed patch cannot be reconstructed: ' + applied.stderr.decode(errors='replace')[:400])
    changes = dev.collect_changes(gd, wt, start, task['spec']['scope'], settings, dev._secrets(settings))
    if changes['violations']:
        raise dev.DevError('diagnostic candidate violates scope: ' + '; '.join(changes['violations']))
    tree = dev.g(gd, None, 'write-tree').strip()
    commit = dev.g(gd, None, 'commit-tree', tree, '-p', start, '-m', 'UNVERIFIED diagnostic evidence; do not integrate').strip()
    dev.g(gd, wt, 'reset', '--quiet', '--hard', commit)
    (root / 'failed.patch').write_bytes(raw)
    logs = []
    for log in sorted(Path(attempt['dir']).glob('round-*/verify-*.log')):
        target = root / 'failures' / log.parent.name / log.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(log.read_bytes())
        logs.append(str(target.relative_to(root)))
    if not logs:
        raise dev.DevError('exact verification logs are unavailable; no diagnostic review')
    record = {'status': 'unverified', 'plan': plan['id'], 'task': task_id, 'attempt': attempt['id'],
              'record': str(path), 'gitdir': str(gd), 'worktree': str(wt), 'base': plan['base_sha'],
              'start': start, 'head': commit, 'tree': tree, 'patch_sha256': dev._sha(raw),
              'spec_sha256': dev._sha(json.dumps(task['spec'], sort_keys=True)),
              'logs': logs, 'verification': [r['verification'] for r in attempt['detail']['rounds'] if 'verification' in r],
              'evidence': {name: dev._sha((root / name).read_bytes()) for name in ['failed.patch', *logs]}}
    _write(path, record)
    dev._event(plan, 'diagnostic-candidate', task=task_id, attempt=attempt['id'], record=str(path), status='unverified')
    return record


def brief(plan, task, record):
    root = Path(record['record']).parent
    return ('UNVERIFIED DIAGNOSTIC CANDIDATE. Required verification failed. This is an isolated proposed change, '
            'not integrated work. Investigate concrete failures and other defects; preserve dissent and propose '
            'bounded corrections. Passing other tests or reviewer approval cannot override the failed checks. '
            'Nickquisition is downstream of a corrected, verified implementation.\nOriginal request:\n' + plan['request']
            + '\nOriginal story:\n' + json.dumps(task['spec']) + '\nExact engine verification logs:\n'
            + '\n\n'.join((root / name).read_text() for name in record['logs']))


def bind_job(db, metadata, job_id):
    path = Path(metadata['record'])
    record = read(path)
    if record.get('job') and record['job'] != job_id:
        raise dev.DevError('diagnostic review already has a persistent job')
    record['job'] = job_id
    _write(path, record)  # before review inference, allowing ordinary resume
    plan = db.dev_plan(record['plan'])
    detail = dict(plan.get('detail') or {})
    diagnostics = dict(detail.get('diagnostics') or {})
    diagnostics[job_id] = str(path)
    detail['diagnostics'] = diagnostics
    db.set_dev_plan(plan['id'], detail=detail)


def correct(db, settings, plan_id, task_id, job_id, fragment, assessment):
    """Turn an explicit correction decision into one existing bounded replacement task."""
    plan = db.dev_plan(plan_id)
    job = db.get_job(job_id)
    meta = job['params'].get('diagnostic') or {}
    if meta.get('plan') != plan['id'] or meta.get('task') != task_id:
        raise dev.DevError('diagnostic review does not belong to this failed task')
    record = read(meta['record'])
    if job['snapshot']['head_commit'] != record['head']:
        raise dev.DevError('diagnostic review targets a different candidate')
    tasks = db.dev_tasks(plan['id'])
    prior = [t['task_id'] for t in tasks if t['spec'].get('diagnostic_origin') == job_id]
    if prior:
        if record.get('assessment', {}).get('sha256') != dev._sha(assessment):
            raise dev.DevError('correction already exists for a different assessment')
        record['correction'] = prior
        _write(Path(record['record']), record)
        return prior  # atomic amendment journal wins after an interruption
    parsed = consensus.record_response(Path(meta['record']).parent / 'reconciliation', assessment)
    if parsed['decision'] != 'request-changes':
        raise dev.DevError('unverified candidate needs an explicit request-changes decision; it cannot be approved or silently repaired')
    usable = [a for a in db.attempts(job_id) if a['status'] == 'ok']
    requested = job['params']['nicks']
    if requested < 2 or len(usable) < requested:
        raise dev.DevError('diagnostic correction requires all requested independent Council reviews')
    task = next(t for t in tasks if t['task_id'] == task_id)
    if dev._sha(json.dumps(task['spec'], sort_keys=True)) != record['spec_sha256']:
        raise dev.DevError('diagnostic story changed after review')
    replacement = deepcopy(fragment)
    if len(replacement.get('tasks') or []) != 1:
        raise dev.DevError('diagnostic correction requires one bounded replacement story task')
    new, old = replacement['tasks'][0], task['spec']
    if (new.get('story') != old['story'] or set(new.get('scope') or []) != set(old['scope'])
            or set(new.get('depends_on') or []) != set(old['depends_on'])
            or not set(old['verify']).issubset(new.get('verify') or [])
            or not set(old['acceptance']).issubset(new.get('acceptance') or [])
            or not set(old['out_of_scope']).issubset(new.get('out_of_scope') or [])):
        raise dev.DevError('diagnostic correction must preserve scope, dependencies, acceptance, verification and exclusions')
    from .evil import council_responses
    originals = '\n\n'.join(f'Nick {label}, verbatim:\n{text}' for label, _, text in council_responses(db, job_id))
    new['diagnostic_origin'] = job_id
    new['context'] = (new.get('context', '') + '\nUNVERIFIED failed proposal for correction, not an accepted baseline:\n'
                      + (Path(record['record']).parent / 'failed.patch').read_text()
                      + '\n' + brief(plan, task, record) + '\nIndependent reviews:\n' + originals
                      + '\nReconciliation:\n' + assessment
                      + '\nCorrect the implementation; do not weaken required behavior or remove failing tests merely to pass.')
    record['assessment'] = parsed
    _write(Path(record['record']), record)  # evidence precedes the atomic task replacement
    new_ids = dev.amend_plan(db, settings, plan['id'], task_id, replacement, reason=assessment)
    record['correction'] = new_ids
    _write(Path(record['record']), record)
    return new_ids


def history(db, plan):
    """Carry actual diagnostic findings into the final review and judiciary."""
    from .evil import council_responses
    out = []
    for job_id, path in (plan.get('detail', {}).get('diagnostics') or {}).items():
        record = read(path)
        out.append(f'Historical UNVERIFIED candidate for {record["task"]}; original failed patch SHA256 {record["patch_sha256"]}. '
                   'These are past failures, not current verification results.')
        out += [(Path(path).parent / name).read_text() for name in record['logs']]
        out += [f'Diagnostic Nick {label}, verbatim:\n{text}' for label, _, text in council_responses(db, job_id)]
        if record.get('assessment'):
            out.append(Path(record['assessment']['response']).read_text())
    return '\n\n'.join(out)
