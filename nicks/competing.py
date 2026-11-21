"""Optional one-story proposal comparison, using the existing development and consensus engines.

Proposal journals are private Development Engine plans, not new workers or providers.
Only the selected verified tree can enter the parent integration queue. Original
proposals and sessions remain immutable; a repair gets a separate engine journal.

The competitor engine is Codex Luna when the Codex subscription is authorized, otherwise Claude Haiku.
Luna implements with tools and the challenge rounds resume its own sessions. Haiku implements through
tool-less JSON proposals applied by Python, and its comparison, challenge and reconciliation calls are
tool-less too: the packet is inlined and each earlier turn is replayed verbatim instead of resumed.
The engine is frozen in the competition record; resume never switches it.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import re
import secrets
import time

from . import consensus, dev
from .config import Settings
from .db import DB
from .evil import CodexRunner, parse_codex_events
from .snapshot import tree_digest
from .testdesign import _export, contract_context

MODEL = 'gpt-6-luna'          # the Codex competitor
CODEX_ENGINE = {'provider': 'codex', 'model': MODEL}
INLINE_BYTES, INLINE_FILE_BYTES = 240_000, 40_000   # tool-less Claude packet material


def select_engine(settings):
    """The competitor for a new competition: Codex Luna if authorized, else Claude Haiku."""
    from .policy import allows
    if allows(settings, 'codex'):
        return dict(CODEX_ENGINE)
    if allows(settings, 'claude'):
        spec = (settings.council_subscription or {}).get('haiku') or {}
        model = spec['model'] if spec.get('provider') == 'claude' else 'claude-haiku-5-5'
        return {'provider': 'claude', 'model': model}
    raise dev.PlanError('competing implementations require an authorized Codex or Claude subscription')


def engine_of(record):
    """Records written before Claude competitors existed are Codex Luna competitions."""
    return record.get('engine') or dict(CODEX_ENGINE)


def same_model(actual, expected):
    """Claude Code may report a dated snapshot id (claude-haiku-5-5-20261001) for the requested alias."""
    return bool(actual) and (actual == expected or actual.startswith(expected + '-'))


def _label(engine):
    return 'Luna' if engine['provider'] == 'codex' else 'Haiku'


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding='utf-8')
    tmp.replace(path)


def _read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _remaining(record, cap):
    left = record['deadline'] - time.time()
    if left <= 0:
        raise dev.DevError('competing implementation budget exhausted; no inference repeated')
    return min(cap, left)


def _settings(settings, engine=None):
    from .policy import allows
    engine = engine or select_engine(settings)
    if not allows(settings, engine['provider']):
        raise dev.PlanError(f"competing implementations require the authorized {engine['provider']} subscription "
                            'frozen in this competition')
    s = Settings(**deepcopy(asdict(settings)))
    s.restricted, s.allow_local = True, False
    if engine['provider'] == 'codex':
        s.subscription_profile, s.authorized_subscriptions, s.dev_workers = 'codex-only', ['codex'], ['luna']
        s.council_subscription = {'luna': {'provider': 'codex', 'model': engine['model'],
                                          'args': ['-c', 'model_reasoning_effort="medium"']}}
    else:
        s.subscription_profile, s.authorized_subscriptions, s.dev_workers = 'claude-only', ['claude'], ['haiku']
        s.council_subscription = {'haiku': {'provider': 'claude', 'model': engine['model']}}
    # These proposal plans inherit the parent's already completed planning gate.
    # They do not invoke additional planners or broaden the selected task.
    s.dev_plan_critic = s.dev_adversarial_tests = False
    s.dev_max_attempts = s.dev_max_impl_failures = 1
    s.dev_max_repairs = 0
    s.dev_max_integration_retries = 0
    return s


def _proposal(parent, task, settings, root, record, name, repo, base, correction=''):
    out = root / name
    out.mkdir(exist_ok=True)
    result_path = out / 'result.json'
    if result_path.exists():
        return _read(result_path)
    db = DB(out / 'engine.db')
    try:
        child = db.latest_dev_plan()
        if child is None:
            manifest = deepcopy(parent['manifest'])
            spec = deepcopy(task['spec'])
            spec['depends_on'] = []  # prerequisites are in the identical frozen baseline
            spec['context'] = spec.get('context', '') + contract_context(parent)
            if correction:
                spec['context'] += '\nBounded correction to this same story:\n' + correction
            manifest['tasks'] = [spec]
            manifest['stories'] = [s for s in manifest['stories'] if s['id'] == spec['story']]
            child = dev.create_plan(db, settings, str(repo), parent['request'], manifest, base=base, directory=out)
            _write(out / 'inherited-planning.json', {'parent': parent['id'], 'baseline': base,
                   'original_spec_sha256': record['spec_sha256'], 'parent_manifest_sha256': record['manifest_sha256']})
        attempts = db.dev_attempts(child['id'])
        state = db.dev_tasks(child['id'])[0]
        if not attempts:
            settings.dev_worker_timeout = _remaining(record, settings.dev_worker_timeout)
            pool = dev.WorkerPool(db, settings, dev._log)
            pool.session_store = out / 'codex-sessions'
            dev.run_task(db, settings, child['id'], state['task_id'], pool=pool)
            state = db.dev_tasks(child['id'])[0]
            attempts = db.dev_attempts(child['id'])
        elif state['status'] in ('implemented', 'integrating'):
            with dev.plan_lock(child):
                dev.recover(db, child['id'])
                dev.integrate_pending(db, settings, db.dev_plan(child['id']))
            state = db.dev_tasks(child['id'])[0]
        # An interrupted inference is preserved, not automatically repeated on resume.
        attempts = db.dev_attempts(child['id'])
        workers = [a for a in attempts if a['engine'] != 'integration']
        last = workers[-1] if workers else None
        rounds = ((last or {}).get('detail') or {}).get('rounds', [])
        response = Path(rounds[-1]['response']) if rounds else None
        events = response.parent / 'events.jsonl' if response else None
        engine = engine_of(record)
        # Only Codex sessions are resumable; a Claude proposal is replayed from its preserved response.
        thread = (parse_codex_events(events)['thread_id'] if engine['provider'] == 'codex' and events
                  and events.exists() else None)
        verified = state['status'] == 'verified' and same_model(state['result'].get('model'), engine['model'])
        result = {'status': 'verified' if verified else 'failed', 'plan': child['id'], 'dir': str(out),
                  'task': state, 'thread': thread, 'session_store': str(out / 'codex-sessions'),
                  'response': str(response) if response else None, 'attempts': workers,
                  'integration_attempts': [a for a in attempts if a['engine'] == 'integration'],
                  'reason': state.get('reason'), 'baseline': base}
        if verified:
            r = state['result']
            result.update(patch_sha256=dev._sha(Path(r['patch']).read_bytes()), tree=dev.g(Path(r['gitdir']), None,
                          'rev-parse', r['commit'] + '^{tree}').strip())
        _write(result_path, result)
        return result
    finally:
        db.close()


def _call(root, record, name, prompt, tree, settings, *, thread=None, store=None, extra_ro=(), runner=None):
    out = root / name
    result = out / 'result.json'
    if result.exists():
        r = _read(result)
    else:
        if (out / 'started.json').exists():
            raise dev.DevError(f'{name}: interrupted inference; raw artifacts retained, no automatic replay')
        _write(out / 'started.json', {'at': time.time(), 'prompt_sha256': dev._sha(prompt)})
        spec = engine_of(record)
        if runner is not None:
            engine = runner
        elif spec['provider'] == 'codex':
            engine = CodexRunner(model=spec['model'], sandbox=True,
                  extra_args=['-c', 'model_reasoning_effort="medium"'],
                  sandbox_extra_ro=[*settings.sandbox_extra_ro, *extra_ro],
                  sandbox_extra_rw=settings.sandbox_extra_rw)
        else:
            engine = ClaudeCompetitor(spec['model'], settings, material=[tree, *extra_ro])
        t = time.time()
        res = engine.run(prompt, tree, out, _remaining(record, settings.attempt_timeout),
                         resume_thread=thread, session_store=store)
        raw = out / 'last-message.txt'
        if not raw.exists():
            raw = out / 'response.txt'
        r = {**asdict(res), 'seconds': time.time() - t, 'response': str(raw),
             'sha256': dev._sha(raw.read_bytes()) if raw.exists() else None}
        _write(result, r)
    if not r['ok'] or not same_model(r['model'], engine_of(record)['model']):
        raise dev.DevError(f"{name}: missing eligible {_label(engine_of(record))} response: {r.get('error')}")
    raw = Path(r['response']).read_bytes()
    if dev._sha(raw) != r['sha256']:
        raise dev.DevError(f'{name}: response changed after completion')
    return raw.decode('utf-8'), r


def _changed_paths(patch_text):
    out = set()
    for m in re.finditer(r'^diff --git a/(\S+) b/(\S+)$', patch_text, re.M):
        out.update(m.groups())
    return out


def inline_material(dirs, total=INLINE_BYTES, per_file=INLINE_FILE_BYTES):
    """Stage material for a tool-less call: (prompt text, notes).

    A directory holding index.json or packet.md is a packet: its own files, patches and evidence logs are
    included, and its other subdirectories are exported trees. Any other directory is an exported tree or
    worktree. Exported trees contribute their changed files, a file listing, and then unchanged files once
    (verification runs over the whole tree) while the budget lasts. Secret-looking paths are skipped and
    secret values redacted. Files are bounded; omissions are named (unchanged ones counted) so the reviewer
    can say what it could not verify.
    """
    from .snapshot import is_secret_path, redact
    dirs = list(dict.fromkeys(Path(d) for d in dirs if Path(d).is_dir()))
    packets = [d for d in dirs if (d / 'index.json').is_file() or (d / 'packet.md').is_file()]
    exports = [d for d in dirs if d not in packets]
    changed = set()
    for d in packets:
        for patch in d.glob('*.patch'):
            changed |= _changed_paths(patch.read_text(encoding='utf-8', errors='replace'))
    first, trees, logs = [], [], []
    for d in packets:
        for f in sorted(d.iterdir()):
            if f.is_file():
                first.append((d.name + '/' + f.name, f))
            elif f.is_dir() and f.name == 'evidence':
                logs += [(d.name + '/' + str(x.relative_to(d)), x) for x in sorted(f.rglob('*')) if x.is_file()]
            elif f.is_dir():
                exports.append(f)
    listings, unchanged, shared = [], [], set()
    # Unchanged files are identical in every export: include them once, from the baseline when present
    # (verification runs over the whole tree, e.g. unchanged test modules; found live).
    exports.sort(key=lambda d: d.name != 'baseline')
    for d in exports:
        label = d.parent.name + '/' + d.name if d.parent in packets else d.name
        base = d.resolve()
        for c in sorted(changed):
            f = d / c
            # Never follow a patch path or symlinked parent out of the exported tree.
            if (f.is_file() and not f.is_symlink() and f.resolve().is_relative_to(base) and '..' not in Path(c).parts
                    and not is_secret_path(c)):
                trees.append((label + '/' + c, f))
        names = []
        for f in sorted(d.rglob('*')):
            rel = f.relative_to(d).as_posix()
            if (any(x in ('.git', '__pycache__') for x in f.relative_to(d).parts) or f.is_symlink() or not f.is_file()
                    or not f.resolve().is_relative_to(base) or is_secret_path(rel)):
                continue
            names.append(rel)
            if rel not in changed and rel not in shared:
                shared.add(rel)
                unchanged.append((label + '/' + rel, f))
        listings.append(f'{label}: ' + ', '.join(names[:500]) + (f' ... ({len(names)} files)' if len(names) > 500 else ''))
    first.sort(key=lambda x: (not x[0].endswith('packet.md'), not x[0].endswith('index.json'), x[0]))
    parts, omitted, used = [], [], 0
    if listings:
        parts.append('<<<TREE LISTINGS (files in each exported tree)>>>\n' + '\n'.join(listings) + '\n<<<END TREE LISTINGS>>>')
    skipped = 0
    for label, path, context in ([(*x, False) for x in first + trees + logs] + [(*x, True) for x in unchanged]):
        if context and used + min(path.stat().st_size, per_file) > total:
            skipped += 1              # unchanged context: counted, not listed one by one, never read
            continue
        raw = path.read_bytes()
        if b'\0' in raw or used + min(len(raw), per_file) > total:
            if context:
                skipped += 1
            else:
                omitted.append(label + (' (binary)' if b'\0' in raw else ''))
            continue
        text = redact(raw[:per_file].decode('utf-8', errors='replace'))[0]
        used += len(text)
        cut = f' TRUNCATED to {per_file} of {len(raw)} bytes' if len(raw) > per_file else ''
        parts.append(f'<<<FILE {label}{cut}>>>\n{text}\n<<<END FILE {label}>>>')
    head = ('\n\nINLINED STAGE MATERIAL. You have no file tools: every packet, patch, tree and log path named '
            'above is provided below where it fits: changed files of every exported tree, then unchanged files once '
            '(identical in every tree) after the logs.\n')
    notes = []
    if skipped:
        head += f'{skipped} unchanged file(s) listed in TREE LISTINGS were not inlined for size or type.\n'
        notes.append(f'{skipped} unchanged file(s) not inlined')
    if omitted:
        head += 'Omitted for size or type (say what you could not verify): ' + ', '.join(omitted) + '\n'
        notes.append(f'{len(omitted)} packet file(s) not inlined: ' + ', '.join(omitted))
    return head + '\n'.join(parts) + '\n', notes


class ClaudeCompetitor:
    """Tool-less Claude Code comparison, challenge, reconciliation or ratification call.

    The hardened ClaudeRunner mounts nothing and exposes no tools, so the stage material is inlined
    (inline_material). Resume arguments are ignored: the prompts already replay earlier turns verbatim.
    """

    def __init__(self, model, settings, material=()):
        self.model, self.s, self.material = model, settings, [Path(m) for m in material]

    def run(self, prompt, tree, out, timeout, resume_thread=None, session_store=None, **_):
        from .evil import EvilResult
        from .neutral import ClaudeRunner, NeutralUnavailable
        text, notes = inline_material(self.material)
        try:
            res = ClaudeRunner(sandbox=True, sandbox_extra_ro=self.s.sandbox_extra_ro,
                               sandbox_extra_rw=self.s.sandbox_extra_rw).run(self.model, prompt + text, Path(tree), out, timeout)
        except NeutralUnavailable as exc:
            return EvilResult(ok=False, error_class='auth', error=str(exc), model=None, notes=notes)
        res.notes = list(res.notes) + notes
        return res


def _verification_evidence(root, proposal, destination, label):
    """Publish original engine checks with revision identity and readable full logs.

    Keep the private originals intact. Public copies remove private attempt paths,
    not outcomes, and are immutable once written. Never substitute worker prose
    for absent verification evidence or assign an old failure to the current tree.
    """
    attempts = proposal['attempts'] + proposal.get('integration_attempts', [])
    private = {str(root), *(a['dir'] for a in attempts)}
    if proposal.get('dir'):
        private.add(proposal['dir'])
        if proposal.get('plan'):
            private.add(str(Path(proposal['dir']) / proposal['plan']))
    private = sorted(private, key=len, reverse=True)

    def public(text):
        for path in private:
            text = text.replace(path, '<private>')
        return text

    def verification(value, name, revision):
        result = deepcopy(value)
        result['revision'] = revision  # None means an uncommitted/failed attempt, not the current revision.
        for i, run in enumerate(result.get('runs', []), 1):
            original = Path(run['log']) if run.get('log') else None
            if original is None or not original.is_file():
                raise dev.DevError(f'{label}: missing verification log for {name} check {i}')
            relative = Path('evidence') / label / f'{name}-check-{i}.log'
            path = destination / relative
            raw = public(original.read_text(encoding='utf-8')).encode('utf-8')
            if path.exists() and path.read_bytes() != raw:
                raise dev.DevError(f'{label}: verification evidence changed after freeze: {relative}')
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            run.update(log=str(relative), log_sha256=dev._sha(raw))
        return json.loads(public(json.dumps(result)))

    result = proposal['task'].get('result') or {}
    info = {'status': proposal['status'], 'baseline': proposal['baseline'],
            'revision': result.get('commit'), 'tree': proposal.get('tree'),
            'evidence_root': str(destination), 'verification': [], 'integration_verification': [],
            'integration': []}
    for i, attempt in enumerate(proposal['attempts'], 1):
        for j, round_ in enumerate(attempt.get('detail', {}).get('rounds', [])):
            if 'verification' in round_:
                v = round_['verification']
                info['verification'].append(verification(v, f'worker-{i}-round-{j}',
                    attempt.get('commit_sha') if v['passed'] else None))
    for i, attempt in enumerate(proposal.get('integration_attempts', []), 1):
        detail = attempt.get('detail') or {}
        info['integration'].append({'status': attempt['status'], 'revision': attempt.get('commit_sha'),
            'mode': detail.get('mode'), 'gate': detail.get('gate', []),
            'own_verified': detail.get('own_verified', []), 'built_on': detail.get('built_on')})
        if 'verification' in detail:
            info['integration_verification'].append(verification(detail['verification'], f'integration-{i}',
                attempt.get('commit_sha')))
    if proposal['status'] == 'verified' and not any(
            v['passed'] and v.get('runs') and all(r['exit_code'] == 0 for r in v['runs'])
            and v['revision'] == info['revision'] for v in info['verification']):
        raise dev.DevError(f'{label}: missing worker verification for the current revision')
    return json.loads(public(json.dumps(info))) | {'evidence_root': str(destination)}


def _packet(parent, root, record, proposals):
    target = root / 'comparison'
    if (target / 'index.json').exists():
        if tree_digest(target) != record.get('packet_digest'):
            raise dev.DevError('comparison evidence changed after freeze')
        return target
    target.mkdir(exist_ok=True)
    _, base_tree = dev.state_paths(parent)
    _export(str(base_tree), record['baseline'], target / 'baseline')
    info = {}
    for label, name in record['labels'].items():
        p = proposals[name]
        info[label] = _verification_evidence(root, p, target, label)
        if p['status'] == 'verified':
            r = p['task']['result']
            _export(r['worktree'], r['commit'], target / label)
            # Keep the original format-patch privately. Its mail header exposes
            # plan/attempt IDs and chronology even when the source tree has no .git.
            (target / (label + '.patch')).write_text(dev.g(Path(r['gitdir']), None,
                'diff', '--binary', record['baseline'], r['commit']), encoding='utf-8')
            info[label]['diff_sha256'] = dev._sha((target / (label + '.patch')).read_bytes())
        elif p['attempts']:
            attempt = Path(p['attempts'][-1]['dir'])
            patch = next((attempt / n for n in ('failed.patch', 'rejected.patch', 'partial.patch')
                          if (attempt / n).exists()), None)
            if patch:
                (target / (label + '.patch')).write_bytes(patch.read_bytes())
    _write(target / 'index.json', info)
    record['packet_digest'] = tree_digest(target)
    _write(root / 'record.json', record)
    return target


def _review_packet(root, name, parent, task, record, responses=(), *, repair=None, selected=None):
    """Present actual stage evidence to the existing arbitration protocol.

    Each view is immutable and mounted separately. A challenge view never exposes
    the other implementer's challenge, including through a shared packet directory.
    These contributions are not blind post-implementation reviews or judicial votes.
    """
    info = _read(root / 'comparison/index.json')
    path = root / 'review-packets' / name / 'packet.md'
    labels = [label for label, _ in responses]
    coverage = {
        'stage': name, 'baseline': record['baseline'], 'story': task['spec'],
        'proposals': info,
        'independent_comparison': {'requested': 1, 'supplied': labels.count('comparison-review')},
        'implementer_challenges': {'requested': 2,
                                  'supplied_in_this_view': sum(label in ('challenge-P', 'challenge-Q') for label in labels)},
        'model': engine_of(record)['model'], 'initial_agents': 3, 'model_families': 1,
        'sessions': ('implementer challenges resume their own sessions' if engine_of(record)['provider'] == 'codex'
                     else 'stateless: each earlier turn is replayed verbatim in the prompt'),
        'ordinary_blind_review_slots': {'requested': 0, 'supplied': 0},
        'judiciary': 'Not run: Good, Evil and Neutral judicial investigations and votes occur only after selection and integration.',
        'correction_budget': {'limit': 1, 'used': int(repair is not None), 'remaining': int(repair is None)},
    }
    if repair is not None:
        if selected not in info or repair['baseline'] != info[selected].get('revision'):
            raise dev.DevError('repair evidence does not descend from the selected proposal')
        evidence = _verification_evidence(root, repair, path.parent, 'repair')
        r = repair['task']['result']
        delta = dev.g(Path(r['gitdir']), None, 'diff', '--binary', repair['baseline'], r['commit'])
        diff = path.parent / 'repair.patch'
        if diff.exists() and diff.read_text(encoding='utf-8') != delta:
            raise dev.DevError('repair diff changed after freeze')
        if not diff.exists():
            diff.parent.mkdir(parents=True, exist_ok=True)
            diff.write_text(delta, encoding='utf-8')
        coverage['repair'] = dict(evidence, selected_proposal=selected, diff='repair.patch',
                                 diff_sha256=dev._sha(delta),
                                 snapshot_sha256=tree_digest(root / 'repair-review-tree'))
    text = ('# Competing implementation evidence packet\n\n'
            'This is the proposal-comparison stage of the existing Council, before Nickquisition. '
            'P.patch and Q.patch are the two review targets; there is no single diff.patch yet. '
            'Initial comparison has no prior review responses by design. Challenge views contain only the '
            'independent comparison; the other challenge is intentionally withheld. Reconciliation receives '
            'the comparison and both completed challenges. Count these roles separately, not as three blind reviews. '
            'Pending downstream judicial work is not missing Council coverage. Actual missing required contributions '
            'or unresolved consequential findings must still prevent approval. '
            'Verification is revision-bound: original P/Q results do not verify a later repair. '
            'Read full logs relative to each evidence_root; output_tail is only an excerpt. '
            'Integration may explicitly reuse worker checks on the exact same tree; an absent separate integration '
            'run is not an invented pass. Use the repair revision and delta for ratification, preserve earlier '
            'failures as historical evidence, and distinguish contract-backed blockers from optional improvements.\n\n'
            'Original request:\n' + parent['request'] + '\n\nManifest, coverage and permitted verification:\n'
            + json.dumps(coverage, indent=2, sort_keys=True) + '\n')
    for label, raw in responses:
        text += (f"\n<<<NICK {label} BEGIN model={engine_of(record)['model']} sha256={dev._sha(raw)}>>>\n"
                 f'{raw}\n<<<NICK {label} END>>>\n')
    if path.exists():
        if path.read_text(encoding='utf-8') != text:
            raise dev.DevError('review packet changed after freeze: ' + name)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    return path


def _selection(text):
    decision = consensus.parse_response(text)['decision']
    labels = []
    for line in text.splitlines():
        control = re.sub(r'^#{1,6} ', '', line.strip())
        if control.startswith('**') and control.endswith('**'):
            control = control[2:-2]
        if control.startswith('Selected proposal:'):
            match = re.fullmatch(r'Selected proposal:\s*([PQ])', control)
            if not match:
                raise dev.DevError('qualified or unsupported proposal selection')
            labels.append(match[1])
    for start in re.finditer(r'^\{', text, re.M):
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start.start():])
        except ValueError:
            continue
        if isinstance(value, dict) and 'selected' in value:
            labels.append(value['selected'])
    if labels and (any(label not in ('P', 'Q') for label in labels) or len(set(labels)) != 1):
        raise dev.DevError('conflicting or unsupported proposal selection')
    if decision != 'needs-investigation' and not labels:
        raise dev.DevError('selection requires one explicit Selected proposal: P or Q; no inferred winner')
    return decision, labels[0] if labels else None


def run(db, settings, plan_id, task_id=None, max_seconds=None, *, reviewer=None):
    """Two independent native proposals, comparison, two blind-to-each-other challenges,
    existing arbitration, at most one verified repair and ratification, one integration.
    """
    parent = db.dev_plan(plan_id)
    from .policy import restore
    restore(settings, parent.get('detail', {}).get('execution_policy'))
    root = Path(parent['dir']) / 'competing'
    existing = _read(root / 'record.json') if (root / 'record.json').exists() else None
    engine = engine_of(existing) if existing else select_engine(settings)
    s = _settings(settings, engine)
    with dev.plan_lock(parent):
        if why := dev.critic_pending(parent) or dev.debate_pending_(parent) or dev.adversarial_pending(parent):
            raise dev.DevError(why)
        tasks = db.dev_tasks(parent['id'])
        task = next((t for t in tasks if t['task_id'] == task_id), None) if task_id else next(iter(dev.ready_tasks(db, parent['id'])), None)
        if (root / 'record.json').exists():
            record = _read(root / 'record.json')
            if task_id and task_id != record['task']:
                raise dev.PlanError('V1 permits only one competing story per plan')
            task = next(t for t in tasks if t['task_id'] == record['task'])
        else:
            if not task or task not in dev.ready_tasks(db, parent['id']):
                raise dev.PlanError('select one ready bounded task')
            if task['spec'].get('microtask') or sum(t['spec']['story'] == task['spec']['story'] for t in tasks) != 1:
                raise dev.PlanError('V1 requires one normal-sized task for the selected story')
            _, _, base = dev.ensure_state(db, parent)
            names = ['proposal-1', 'proposal-2']
            secrets.SystemRandom().shuffle(names)
            budget = settings.dev_execute_seconds if max_seconds is None else max_seconds
            if budget <= 0:
                raise dev.PlanError('competing implementation budget must be positive')
            record = {'status': 'running', 'task': task['task_id'], 'baseline': base, 'engine': engine,
                      'spec_sha256': dev._sha(json.dumps(task['spec'], sort_keys=True)),
                      'manifest_sha256': parent['manifest_sha256'], 'labels': dict(zip(('P', 'Q'), names)),
                      'started': time.time(), 'deadline': time.time() + budget}
            _write(root / 'record.json', record)
        if (record['spec_sha256'] != dev._sha(json.dumps(task['spec'], sort_keys=True))
                or record['manifest_sha256'] != parent['manifest_sha256']):
            raise dev.DevError('the frozen competing story changed')
        if record['status'] in ('integrated', 'incomplete'):
            return record
        db.set_dev_plan(parent['id'], detail=dict(parent.get('detail') or {}, competition={'status': 'running', 'record': str(root / 'record.json')}))
        if task['status'] == 'pending':
            db.set_dev_task(parent['id'], task['task_id'], 'competing')
        try:
            if 'selected_result' not in record:
                _, base_tree, head = dev.ensure_state(db, db.dev_plan(parent['id']))
                if head != record['baseline']:
                    raise dev.DevError('parent baseline moved during competing development')
                proposals = {name: _proposal(parent, task, deepcopy(s), root, record, name, base_tree, head)
                             for name in ('proposal-1', 'proposal-2')}
                record['proposals'] = proposals
                _write(root / 'record.json', record)
                if not any(p['status'] == 'verified' for p in proposals.values()):
                    raise dev.DevError('no independently verified proposal; no selection or integration')
                packet = _packet(parent, root, record, proposals)
                protocol = Path(__file__).with_name('WORKFLOW.md').read_text().split('## Arbitration protocol', 1)[1]
                protocol = protocol.split('## Final output format', 1)[0]
                common = ('Apply the existing Council arbitration protocol below. Preserve minority findings and unresolved dissent. '
                          'Passing local verification is evidence, not proof of correctness or superiority. '
                          'Do not prefer a proposal merely because it differs or has more tests. Inspect both anonymized '
                          'proposals P and Q, their patches and index.json. No hidden evaluator results are available. '
                          'Do not edit code.\nOriginal request:\n' + parent['request'] + '\nFrozen story:\n' +
                          json.dumps(task['spec']) + '\n' + protocol)
                view = _review_packet(root, 'comparison-review', parent, task, record)
                comparison, comp = _call(root, record, 'comparison-review', common + '\nPACKET: ' + str(view)
                                         + '\nGive substantive findings for both proposals; no selection yet.',
                                         packet, s, store=root / 'comparison-sessions', extra_ro=[str(view.parent)], runner=reviewer)
                responses = [('comparison-review', comparison)]
                challenges = []
                for label, name in record['labels'].items():
                    p = proposals[name]
                    if not p['response'] or (engine['provider'] == 'codex' and not p['thread']):
                        raise dev.DevError(f'{label}: implementing session missing; reconciliation is incomplete')
                    wt = Path(p['response']).parent.parent / 'worktree'
                    replay = ''
                    if engine['provider'] != 'codex':
                        original = Path(p['response'])
                        if not original.is_file():
                            raise dev.DevError(f'{label}: implementing response missing; reconciliation is incomplete')
                        replay = ('Your own original implementation response for proposal ' + label +
                                  ', replayed verbatim (no session is resumed):\n' + original.read_text(encoding='utf-8') + '\n')
                    prompt = (replay + f'You implemented proposal {label}. This is your ONE bounded challenge round. '
                              'Review the competing proposal and the independent comparison. Defend or correct your reasoning; '
                              'identify specific evidence and unresolved findings. Do not edit code or invent agreement. '
                              'You cannot see the other implementer\'s challenge. Both proposals and verification are at '
                              + str(packet) + '\n' + common + '\nIndependent comparison, verbatim:\n' + comparison)
                    view = _review_packet(root, 'challenge-' + label, parent, task, record, responses[:1])
                    challenge, _ = _call(root, record, 'challenge-' + label, prompt + '\nPACKET: ' + str(view), wt, s,
                                         thread=p['thread'], store=Path(p['session_store']),
                                         extra_ro=[str(packet), str(view.parent)], runner=reviewer)
                    responses.append(('challenge-' + label, challenge))
                    challenges.append(f'Implementer {label}, verbatim:\n{challenge}')
                prompt = common + '\nIndependent comparison, verbatim:\n' + comparison + '\n' + '\n\n'.join(challenges)
                prompt += ('\nResolve findings on evidence, preserving dissent. Use the existing decision values approve, '
                           'request-changes, needs-investigation. State an explicit Decision: <value> and, for approval '
                           'or a repair, Selected proposal: P or Q on its own line. Approve only a verified proposal with '
                           'no unresolved consequential findings. Request-changes permits ONE bounded repair of the selected '
                           'proposal; specify concrete corrections. Do not manufacture consensus if neither is defensible.')
                view = _review_packet(root, 'reconciliation', parent, task, record, responses)
                answer, _ = _call(root, record, 'reconciliation', prompt + '\nPACKET: ' + str(view), packet, s,
                                  thread=comp['thread_id'], store=root / 'comparison-sessions',
                                  extra_ro=[str(view.parent)], runner=reviewer)
                consensus.record_response(root / 'decisions', answer)
                decision, selected = _selection(answer)
                if decision == 'needs-investigation':
                    raise dev.DevError('consensus needs investigation; dissent preserved, no winner')
                chosen = proposals[record['labels'][selected]]
                if chosen['status'] != 'verified':
                    raise dev.DevError('a failed proposal cannot be selected')
                if decision == 'request-changes':
                    r = chosen['task']['result']
                    chosen = _proposal(parent, task, deepcopy(s), root, record, 'repair', Path(r['worktree']), r['commit'], answer)
                    record['repair'] = chosen
                    if chosen['status'] != 'verified':
                        raise dev.DevError('the single bounded repair failed verification')
                    r = chosen['task']['result']
                    repair_tree = root / 'repair-review-tree'
                    if not repair_tree.exists():
                        _export(r['worktree'], r['commit'], repair_tree)
                    view = _review_packet(root, 'repair-ratification', parent, task, record,
                                          responses + [('reconciliation', answer), ('repair', Path(chosen['response']).read_text())],
                                          repair=chosen, selected=selected)
                    final, _ = _call(root, record, 'repair-ratification', prompt + '\nPACKET: ' + str(view)
                                     + '\nOne verified repair of selected proposal '
                                     + selected + ' is at ' + str(repair_tree) + '. Recheck all original findings and the corrected code. Approve only '
                                     'if resolved; no further repairs or reconciliation rounds are permitted.\nRepair response:\n' +
                                     Path(chosen['response']).read_text(), packet, s, thread=comp['thread_id'],
                                     store=root / 'comparison-sessions', extra_ro=[str(repair_tree), str(view.parent)], runner=reviewer)
                    consensus.record_response(root / 'decisions', final)
                    if consensus.parse_response(final)['decision'] != 'approve':
                        raise dev.DevError('repair not approved; consensus incomplete')
                r = deepcopy(chosen['task']['result'])
                # A repair can have multiple ancestors. Publish exactly its verified
                # tree as one proposal commit on the common baseline, not a patch of only the repair.
                gd = Path(r['gitdir'])
                tree = dev.g(gd, None, 'rev-parse', r['commit'] + '^{tree}').strip()
                commit = dev.g(gd, None, 'commit-tree', tree, '-p', record['baseline'],
                               '-m', 'Council selected verified competing implementation').strip()
                r.update(commit=commit, start=record['baseline'], competition_source_commit=chosen['task']['result']['commit'])
                record.update(selected=selected, selected_result=r, status='selected')
                _write(root / 'record.json', record)  # durable choice before entering the existing queue
            dev.recover(db, parent['id'])
            current = next(t for t in db.dev_tasks(parent['id']) if t['task_id'] == task['task_id'])
            if current['status'] != 'verified':
                db.set_dev_task(parent['id'], task['task_id'], 'implemented', result=record['selected_result'])
                current = next(t for t in db.dev_tasks(parent['id']) if t['task_id'] == task['task_id'])
                _remaining(record, 1)
                result = dev.integrate_task(db, s, db.dev_plan(parent['id']), current)
                if result != 'integrated':
                    raise dev.DevError('selected proposal did not pass the existing integration gate: ' + result)
            elif current['result']['commit'] != record['selected_result']['commit']:
                raise dev.DevError('parent integrated an unexpected result')
            record.update(status='integrated', finished=time.time())
        except (dev.DevError, ValueError) as exc:
            record.update(status='incomplete', reason=str(exc), finished=time.time())
        _write(root / 'record.json', record)
        detail = db.dev_plan(parent['id']).get('detail') or {}
        db.set_dev_plan(parent['id'], detail=dict(detail, competition={'status': record['status'], 'record': str(root / 'record.json')}))
        dev._event(parent, 'competing', status=record['status'], selected=record.get('selected'), reason=record.get('reason'))
        dev.write_plan_md(db, parent['id'])
        return record


def judgment_job(db, settings, plan):
    """Freeze the selected target for the unchanged judiciary, with separately labelled
    original comparison contributions. These are not three blind post-implementation reviews.
    """
    from .review import build_prompt, create_job, job_dir, new_job_id
    from .snapshot import capture
    from .packet import write_packet
    integrated = dev.integrate(db, plan)
    prior = db.get_job(plan['review_job']) if plan.get('review_job') else None
    if prior and prior['snapshot']['head_commit'] == integrated['head']:
        return prior['id']
    history = None
    if prior:
        verdict = db.one('SELECT * FROM nq_verdicts WHERE job_id=? ORDER BY id DESC LIMIT 1', (prior['id'],))
        if verdict:
            from .nickquisition import verify
            checked = verify(db, prior['id'], verdict['round'])
            if checked['integrity'] != 'ok' or checked['delivery_integrity'] != 'ok':
                raise dev.DevError('prior judicial evidence failed integrity verification')
            report = Path(verdict['report_path']).read_text()
            history = {'job': prior['id'], 'head': prior['snapshot']['head_commit'],
                       'report': report, 'sha256': dev._sha(report)}
    meta = (plan.get('detail') or {}).get('competition') or {}
    if meta.get('status') != 'integrated':
        raise dev.DevError('competing consensus has not integrated a verified selection')
    root = Path(meta['record']).parent
    record = _read(root / 'record.json')
    spec, who = engine_of(record), _label(engine_of(record))
    contributions = []
    for name in ('comparison-review', 'challenge-P', 'challenge-Q', 'reconciliation', 'repair-ratification'):
        if not (root / name / 'result.json').exists():
            continue
        result = _read(root / name / 'result.json')
        raw = Path(result['response']).read_bytes()
        if not result['ok'] or not same_model(result['model'], spec['model']) or dev._sha(raw) != result['sha256']:
            raise dev.DevError('competing contribution integrity failure: ' + name)
        contributions.append((name, raw, result))
    if len(contributions) < 4:
        raise dev.DevError('missing comparison, challenges or reconciliation')
    job = new_job_id()
    snap = capture(integrated['worktree'], job_dir(job), base=integrated['base'], head=integrated['head'], merge_base=False)
    sources = []
    for name, raw, result in contributions:
        dest = job_dir(job) / 'competing' / (name + '.txt')
        dest.parent.mkdir(exist_ok=True)
        dest.write_bytes(raw)
        sources.append({'label': name, 'path': str(dest), 'sha256': result['sha256'],
                        'route': spec['provider'] + '/' + spec['model'], 'family': spec['model'],
                        'actual_model': result['model'], 'basis': 'comparison/reconciliation; not blind independent reviews'})
    brief = dev.review_brief(db, plan, integrated['tasks'])
    if prior:
        brief += ('\nThe comparison contributions below concern the original selected implementation at '
                  + record['selected_result']['commit'] + '. Additional verified correction tasks are now included. '
                  'Those historical contributions do not constitute review or approval of the correction.')
    s = deepcopy(settings)
    s.nicks = s.min_nicks = 0  # no additional review inference; comparison is the treatment
    create_job(db, snap, build_prompt(snap, Path(snap['diff_path']).read_text(), brief), s,
               {'brief': brief, 'competing_contributions': sources, 'prior_judgment': history,
                'competing_summary': f'Two independent {who} proposals; one independent comparison; two implementing '
                f'{who} challenge turns; reconciler selection. Three initial agents, one model family; dissent retained.'
                + (f' One additional {who} repair execution.' if record.get('repair') else '')}, job_id=job)
    db.update_job(job, status='complete', summary={'usable': 0, 'requested': 0,
                  'snapshot_integrity': 'verified-unchanged', 'competing_agents': 3, 'competing_contributions': len(sources)})
    write_packet(db, job, s)
    db.set_dev_plan(plan['id'], review_job=job, status='in-review')
    return job


def contributions(job):
    out = []
    for item in job['params'].get('competing_contributions') or []:
        raw = Path(item['path']).read_bytes()
        if dev._sha(raw) != item['sha256']:
            raise dev.DevError('competing Council contribution changed after freeze')
        out.append((item['label'], item, raw.decode('utf-8')))
    return out
