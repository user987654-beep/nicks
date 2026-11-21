"""Optional subscription planner using the shared skill and existing manifest validator.

The interactive host can still supply --file, amend and revise the exact same manifests.
"""
import json
import secrets
from pathlib import Path

from . import dev
from .config import state_dir
from .plancritic import _snapshot
from .policy import RoleRunner, snapshot as policy_snapshot


def generate(db, settings, repo, request, base="HEAD"):
    root = dev._repo_root(repo)
    sha = dev._resolve(root, base)
    out = state_dir() / "planning" / secrets.token_hex(8)
    out.mkdir(parents=True)
    plan = dict(repo=str(root), base_sha=sha)
    tree = _snapshot(plan, out / "snapshot")
    workflow = Path(__file__).with_name("WORKFLOW.md").read_text()
    prompt = ("You are Architect Nick. Inspect the repository in your working directory. Generate a bounded plan "
              "for the request below using the shared planning instructions. Do not implement code or run nicks. "
              "Return ONLY the manifest JSON object, without Markdown or commentary. Verification runs offline "
              "in an export without .git.\n\n" + workflow + "\n\nUSER REQUEST:\n" + request)
    runner = RoleRunner(db, settings, "architect")
    attempts = []
    evidence = dict(attempts=attempts, execution_policy=policy_snapshot(settings), base_sha=sha)
    (out / "record.json").write_text(json.dumps(evidence, indent=2))
    for n in (1, 2):
        dest = out / f"attempt-{n}"
        res = runner.run(prompt, tree, dest, settings.dev_critic_timeout)
        attempts.append(dict(model=res.model, ok=res.ok, error=res.error, dir=str(dest)))
        (out / "record.json").write_text(json.dumps(evidence, indent=2))
        if not res.ok:
            raise dev.PlanError("Architect Nick unavailable: " + str(res.error) + "; evidence: " + str(out))
        # Codex's response transcript includes progress messages as well as the final
        # answer. Parse its dedicated final message, retaining the full transcript as evidence.
        final = dest / "last-message.txt"
        text = (final if final.exists() else dest / "response.txt").read_text().strip()
        if text.startswith("```") and text.endswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            manifest = json.loads(text)
            errors = dev.validate_manifest(manifest, root, sha, settings)
        except (ValueError, TypeError) as exc:
            errors = [str(exc)]
        if not errors:
            evidence = dict(attempts=attempts, execution_policy=policy_snapshot(settings), base_sha=sha)
            (out / "record.json").write_text(json.dumps(evidence, indent=2))
            return manifest, dict(evidence, record=str(out / "record.json"))
        prompt += "\nYour previous manifest:\n" + text + "\nPython validation errors; correct them:\n" + "\n".join(errors)
    raise dev.PlanError("Architect manifest failed validation: " + "; ".join(errors) + "; evidence: " + str(out))


def durable_state():
    """State shared by delivery and its explicitly authorized child cycles."""
    import os
    state = state_dir().resolve()
    temporary = ['/tmp', '/var/tmp', '/run', '/dev/shm']
    temporary += [os.environ[k] for k in ('TMPDIR', 'TEMP', 'TMP') if os.environ.get(k)]
    if any(state == Path(p).resolve() or Path(p).resolve() in state.parents for p in temporary):
        raise dev.PlanError('deliver requires durable NICKS_HOME/XDG_STATE_HOME outside temporary storage; '
                            f'refusing {state} before inference')
    return state


def deliver(db, settings, repo, request, max_seconds=None, experiment_note=None):
    """Reserve one durable host per repository/baseline/request; never replay it implicitly.

    `experiment_note` is for benchmark configurations only (bench/v2): an explicit, recorded workflow instruction
    appended to the host prompt. Without it the host prompt and identity are exactly the production ones."""
    import hashlib
    import os

    if not request.strip():
        raise dev.PlanError('deliver requires --request or --request-file')
    if max_seconds is not None and not 0 < max_seconds <= 7200:
        raise dev.PlanError('deliver --max-seconds must be positive and at most 7200')
    state = durable_state()
    root = dev._repo_root(repo)
    identity = {'repository': str(root), 'baseline': dev._resolve(root, 'HEAD'), 'request': request}
    if experiment_note:
        identity['experiment_note'] = experiment_note
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
    out = state / 'hosts' / key
    out.mkdir(parents=True, exist_ok=True)
    with dev.plan_lock({'id': 'delivery ' + key, 'dir': str(out)}):
        # Older launchers used random host directories. Do not turn an upgrade
        # into an implicit replay of an existing delivery of the same request.
        for old in (state / 'hosts').iterdir():
            if old == out or not (old / 'launch.json').is_file() or not (old / 'request.md').is_file():
                continue
            if (old / 'request.md').read_text() != request:
                continue
            launch = json.loads((old / 'launch.json').read_text())
            if launch.get('repository') == str(root) and launch.get('baseline') == identity['baseline']:
                raise dev.PlanError(f'existing delivery at {old}; no duplicate host inference. '
                                    f'Inspect/resume with NICKS_HOME={old / "engine"}')
        reservation = out / 'started.json'
        if reservation.exists():
            if json.loads(reservation.read_text()) != identity:
                raise dev.PlanError('delivery reservation identity mismatch; original evidence retained')
            if (out / 'result.json').exists():
                return json.loads((out / 'result.json').read_text())
            raise dev.PlanError(f'delivery already started; no duplicate host inference. Original attempts: {out}. '
                                f'Inspect/resume existing dev, review and judicial jobs with NICKS_HOME={out / "engine"}; '
                                'an interrupted model call is not a completed decision')
        extra = {'experiment_note': experiment_note} if experiment_note else {}   # production call unchanged
        return _deliver(db, settings, root, request, max_seconds, out, identity, **extra)




# Built-in Claude Code tools for an unattended Claude host. No web, MCP, subagents or skills: the host operates
# the Council through `nicks` and reads/writes notes; the bubblewrap jail is the write boundary.
CLAUDE_HOST_TOOLS = 'Bash,Read,Write,Edit,Glob,Grep'


def host_choice(db, settings):
    """(host, model): the subscription CLI that hosts delivery and its strong Architect model.

    `host` / NICKS_HOST only orders the authorized choices; it never authorizes a provider. A Claude Code
    user with a Codex-only profile therefore still gets a Codex host and needs no Claude inference, and a
    Claude-only profile gets a Claude host from either CLI.
    """
    from .policy import candidates
    pool = candidates(db, settings, 'architect')
    strong = {'codex': ('openai', settings.neutral_openai_models),
              'claude': ('anthropic', settings.neutral_anthropic_models)}
    for host in (('claude', 'codex') if settings.host == 'claude' else ('codex', 'claude')):
        provider, models = strong[host]
        eligible = [m for p, m in pool if p == provider and m in models]
        if eligible:
            return host, eligible[0]
    raise dev.PlanError('no eligible strong Codex or Claude Architect for native skill delivery')


def _installed_skill(host):
    """(installed skill directory, its path relative to $HOME inside the jail)."""
    import os
    if host == 'codex':
        return Path.home() / '.agents/skills/nicks', Path('.agents/skills/nicks')
    base = os.environ.get('CLAUDE_SKILLS_DIR')
    return (Path(base) if base else Path.home() / '.claude/skills') / 'nicks', Path('.claude/skills/nicks')


def _secret_strings(raw: bytes) -> list[str]:
    """Every 24+ character string in a JSON login file (tokens, ids); used only for scrubbing outputs."""
    try:
        stack = [json.loads(raw)]
    except (ValueError, TypeError):
        return []
    out = []
    while stack:
        cur = stack.pop()
        for v in (cur.values() if isinstance(cur, dict) else cur if isinstance(cur, list) else []):
            if isinstance(v, str) and len(v) >= 24:
                out.append(v)
            elif isinstance(v, (dict, list)):
                stack.append(v)
    return out


def _logins(settings, host, home):
    """Subscription logins copied into the host's private home for its authenticated CLI children.

    The host's own login is required. The other provider's login is copied only when the policy authorizes it
    and it is a valid subscription login, so a both-provider delivery keeps its Luna/Haiku workers and judges.
    Returns (logins, env, read-only mounts); files are written only when the launch is dispatched.
    """
    import os
    from .evil import EvilUnavailable, check_subscription_auth, codex_bin, codex_home, login_secrets
    from .neutral import NeutralUnavailable, check_claude_subscription, claude_bin, claude_credentials
    from .policy import allows
    logins, env, ro = [], {}, []
    for provider in (host, 'claude' if host == 'codex' else 'codex'):
        check = check_subscription_auth if provider == 'codex' else check_claude_subscription
        binary = codex_bin() if provider == 'codex' else claude_bin()
        if provider == host:
            check()
        else:
            if not allows(settings, provider) or not Path(binary).exists():
                continue
            try:
                check()
            except (EvilUnavailable, NeutralUnavailable):
                continue
        binary = os.path.realpath(binary)
        label = ('Codex' if provider == 'codex' else 'Claude Code') + (' host' if provider == host else '')
        if provider == 'codex':
            private = home / '.nicks-codex'
            private.mkdir(parents=True, mode=0o700, exist_ok=True)
            real = codex_home() / 'auth.json'
            original = real.read_bytes()
            catalog = codex_home() / 'models_cache.json'
            extra = [(catalog, private / catalog.name)] if catalog.is_file() else []
            logins.append(dict(label=label, real=real, jailed=private / 'auth.json', original=original,
                               secrets=login_secrets(original), extra=extra, rollouts=private))
            env.update(CODEX_HOME=str(Path.home() / '.nicks-codex'), NICKS_CODEX_BIN=binary)
            ro.append(Path(binary).parent.parent)
        else:
            (home / '.claude').mkdir(parents=True, mode=0o700, exist_ok=True)
            real = claude_credentials()
            original = real.read_bytes()
            logins.append(dict(label=label, real=real, jailed=home / '.claude/.credentials.json', original=original,
                               secrets=_secret_strings(original), extra=[], rollouts=None))
            env.update(NICKS_CLAUDE_BIN=binary)
            ro.append(Path(binary))
    return logins, env, ro


def _host_command(host, binary, model, work, out):
    if host == 'codex':
        return [binary, '-a', 'never', 'exec', '--json', '--ignore-user-config', '--ignore-rules', '--skip-git-repo-check',
                '--sandbox', 'workspace-write', '--add-dir', str(out),
                '-c', 'sandbox_workspace_write.network_access=true', '-c', 'agents.enabled=false',
                '-c', 'web_search="disabled"', '-m', model, '-C', str(work),
                '-o', str(out / 'last-message.txt'), '-']
    # Headless Claude Code: prompt on stdin, full stream-json transcript as evidence. Only built-in file and
    # shell tools; no MCP servers, user/project settings, skills, slash commands or web tools. Permission
    # prompts are bypassed because nobody can answer them; the bubblewrap jail enforces the write boundary.
    return [binary, '-p', '--model', model, '--add-dir', str(out),
            '--tools', CLAUDE_HOST_TOOLS, '--allowedTools', CLAUDE_HOST_TOOLS,
            '--permission-mode', 'bypassPermissions', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--setting-sources', '', '--disable-slash-commands', '--no-session-persistence',
            '--output-format', 'stream-json', '--verbose']


def claude_host_usage(events: Path) -> dict:
    """Actual models, tokens and billing evidence from a Claude host's stream-json transcript."""
    from .billing import model_allowed
    from .neutral import parse_claude_stream
    parsed = parse_claude_stream(events) if events.exists() else {'init': {}, 'result': {}, 'overage': False}
    init, result = parsed['init'], parsed['result']
    usage = result.get('usage') or {}
    models = list((result.get('modelUsage') or {}).keys()) or ([init['model']] if init.get('model') else [])
    violations = []
    if init.get('apiKeySource') not in (None, 'none'):
        violations.append(f"Claude Code authenticated with {init.get('apiKeySource')!r}, not the subscription")
    for m in models:
        ok, why = model_allowed('anthropic', m)
        if not ok:
            violations.append(f'Claude Code served {m}: {why}')
    return {'models': models, 'session_id': init.get('session_id'), 'tools': init.get('tools'),
            'mcp_servers': init.get('mcp_servers'),
            'tokens_in': int(usage.get('input_tokens') or 0) + int(usage.get('cache_read_input_tokens') or 0),
            'tokens_out': int(usage.get('output_tokens') or 0), 'turns': result.get('num_turns'),
            'is_error': bool(result.get('is_error')) or (bool(result) and result.get('subtype') != 'success'),
            'extra_usage': bool(parsed['overage']), 'billing_violations': violations}


def experiment_text(note: str | None) -> str:
    """Host-prompt text for a benchmark configuration's experiment note; empty (production prompt) without one."""
    if not note:
        return ''
    return ('EXPERIMENTAL BENCHMARK CONFIGURATION (this run only; where it conflicts with the workflow instructions '
            'above, it overrides them): ' + note.strip() + ' ')


def _deliver(db, settings, repo, request, max_seconds, out, identity, experiment_note=None):
    """Launch the existing host skill once; the host uses ordinary engine commands.

    This is a native CLI entry point, not a second workflow/state machine. Workers,
    reconciliation and judges remain owned by the existing engine and skill. The host
    is Codex CLI or Claude Code (`host_choice`); both run the same skill text and engine.
    """
    import os
    import shutil
    import subprocess
    import threading
    import time
    from dataclasses import asdict
    from .billing import require
    from .db import DB
    from .evil import codex_bin, scrub_login_secrets, write_back_credentials, rollout_model, parse_codex_events
    from .neutral import claude_bin
    from .runner import base_env, run_supervised, kill_process_group
    from .evidence_delivery import save
    from .sandbox import wrap

    if not request.strip():
        raise dev.PlanError('deliver requires --request or --request-file')
    seconds = max_seconds if max_seconds is not None else 7200
    if not 0 < seconds <= 7200:
        raise dev.PlanError('deliver --max-seconds must be positive and at most 7200')
    if not settings.sandbox:
        raise dev.PlanError('deliver requires the existing sandbox')
    from .policy import resolve
    resolve(settings)   # pin auto authorization once: the host, its logins and its journal share one policy
    root = dev._repo_root(repo)
    engine = Path(__file__).resolve().parent.parent
    host, model = host_choice(db, settings)
    skill, skill_alias = _installed_skill(host)
    if not (skill / 'SKILL.md').is_file() or not (skill / 'WORKFLOW.md').is_file():
        raise dev.PlanError(f'install the {"Codex" if host == "codex" else "Claude Code"} nicks skill with '
                            'install.sh before deliver')
    home, journal = out / 'home', out / 'engine'
    work = out / 'workspace'
    require('openai' if host == 'codex' else 'anthropic', model)   # billing gate before any private state
    logins, login_env, login_ro = _logins(settings, host, home)
    journal.mkdir(exist_ok=True)
    work.mkdir(exist_ok=True)
    # Mount the checkout at its canonical location and preserve the installed
    # alias as a symlink. Binding the alias as a directory rebases ../nicks in
    # WORKFLOW.md onto itself and creates the observed loop.
    installed = home / skill_alias
    installed.parent.mkdir(parents=True, exist_ok=True)
    if not installed.is_symlink():
        installed.symlink_to(skill.resolve(), target_is_directory=True)
    (journal / 'config.json').write_text(json.dumps(asdict(settings), indent=2))
    (out / 'request.md').write_text(request)
    binary = os.path.realpath(codex_bin() if host == 'codex' else claude_bin())
    cli = engine / 'bin/nicks'
    env = base_env()
    env.update(login_env)
    env.update(NICKS_HOME=str(journal), NICKS_HOST=host, NICKS_DELIVERY_HOST='1', PWD=str(work),
               XDG_RUNTIME_DIR='/tmp/nicks-host-runtime', PYTHONDONTWRITEBYTECODE='1')
    if host == 'claude':
        # Council commands are bounded for a 10-minute shell call (dev_execute_seconds); Claude Code's
        # default Bash timeout is 2 minutes. No self-update inside the jail.
        env.update(BASH_DEFAULT_TIMEOUT_MS='600000', BASH_MAX_TIMEOUT_MS='600000', DISABLE_AUTOUPDATER='1')
    deadline = time.time() + seconds
    host_rules = ('' if host == 'codex' else
                  'You run headless in Claude Code with no user to answer questions. Run every command in the '
                  'foreground (never in the background) with a Bash timeout of 600000 ms. Council commands '
                  'are bounded to fit that limit; when dev execute pauses on its time budget, call it again. ')
    # Name the skill by its path INSIDE the jail: the private home's alias. CLAUDE_SKILLS_DIR may point
    # outside $HOME, where nothing is mounted (found live).
    prompt = (f'Use the installed nicks skill at {Path.home() / skill_alias / "SKILL.md"} to deliver the request below. '
              f'You are the existing Architect/arbiter host ({model}); use {cli} for every Council operation. '
              f'The repository is {root}; NICKS_HOME={journal}. Write manifests and arbitration notes under {out}. '
              'The source repository is read-only. Only Council engine workers may implement or correct it in their isolated worktrees. '
              'Do not edit engine code, journals, receipts, ballots, private clones or configuration. '
              'Do not call dev deliver recursively, start benchmark controllers, use external agents, or merge/push. '
              + host_rules +
              'Use debate mode and its real BET/CALL/FOLD protocol, normal stories and default review-based consensus. '
              'Run state-changing plan commands sequentially. Require every configured blind review. '
              'Read and arbitrate all responses, record your provisional assessment with verdict --stage provisional, '
              'then use nickquisition run for three independent judges, and dev conclude. '
              'Never use --allow-incomplete-council or manual ballots. Do not manually retry judicial protocol failures; '
              'the engine owns the bounded automatic acknowledgment retry. '
              'You may use at most one consensus correction and one judicial correction through existing amend/execute paths, '
              'with fresh review and judging for a changed head; never repair after the independent final evaluation. '
              'A legitimate REJECT is a completed outcome; missing stages or ballots are incomplete. '
              f'Absolute wall deadline (Unix seconds): {deadline:.3f}. Do not renew it. '
              'Stop safely on exhausted limits or unavailable infrastructure, preserving every original. '
              + experiment_text(experiment_note)
              + 'Return the actual final plan/job/commit/verdict and any blockers.\n\n'
              + (skill / 'SKILL.md').read_text() + '\n\n' + (skill / 'WORKFLOW.md').read_text()
              + '\n\nORIGINAL USER REQUEST:\n' + request)
    from .evidence_delivery import budget
    budget(prompt)
    (out / 'prompt.txt').write_text(prompt)
    cmd = _host_command(host, binary, model, work, out)
    # Same private-/tmp launch used by every hardened worker. The host remains
    # an operator (it must invoke authenticated CLI children); worker credential
    # denial and offline project verification are unchanged.
    ro = [*login_ro, engine / 'nicks', engine / 'bin', engine / 'skill', skill.resolve(), root]
    if settings.rework_context:
        ro.append(Path(settings.rework_context['path']))
    cmd = wrap(cmd, snapshot=work, home=home, binary=binary, extra_ro=ro, extra_rw=[out], writable=True)
    launch = {'command': cmd, 'host': host, 'model': model, 'started_at': time.time(), 'deadline': deadline,
              'repository': str(root), 'baseline': dev._resolve(root, 'HEAD'), 'skill': str(skill),
              'engine_state': str(journal), 'operator_interventions': 0,
              'logins': [x['label'] for x in logins]}
    # Preflight failures may be retried before any inference. Once dispatch is
    # reserved, even an uncertain interruption must not launch another host.
    save(out / 'started.json', identity)
    with (out / 'started.json').open('rb') as handle:
        os.fsync(handle.fileno())
    save(out / 'launch.json', launch)
    print(f'HOST: {out}\nENGINE_STATE: {journal}', flush=True)
    processes, process_lock = set(), threading.Lock()
    notes = []

    def rollouts():
        return sorted(p for x in logins if x['rollouts'] for p in x['rollouts'].glob('sessions/**/*.jsonl'))

    try:
        for login in logins:
            for src, dst in login['extra']:
                shutil.copyfile(src, dst)
            login['jailed'].write_bytes(login['original'])
            login['jailed'].chmod(0o600)
        from . import telemetry
        from .nq_live import Watcher                   # the Nickquisition in this terminal as soon as it rules
        watcher = Watcher(journal / 'nicks.db', out / 'nickquisition-terminal.json').start()
        with telemetry.labels(db=db, job_kind='delivery', phase='delivery', role='architect'), \
                telemetry.invocation(host, 'openai' if host == 'codex' else 'anthropic', model, out) as inv:
            rc, timed_out, interrupted, _ = run_supervised(cmd, work, env, prompt, out / 'events.jsonl',
                    out / 'stderr.log', max(0, deadline-time.time()), None, processes, process_lock)
            launch.update(exit_code=rc, timed_out=timed_out, interrupted=interrupted,
                          seconds=time.time()-launch['started_at'])
            if host == 'codex':
                launch['actual_model'] = rollout_model(rollouts())
                host_failed = rc != 0 or bool(parse_codex_events(out / 'events.jsonl')['errors'])
            else:
                usage = claude_host_usage(out / 'events.jsonl')
                launch.update(actual_model=','.join(usage['models']) or None, host_error=usage['is_error'],
                              billing_violations=usage['billing_violations'], extra_usage=usage['extra_usage'])
                host_failed = rc != 0 or usage['is_error'] or bool(usage['billing_violations'])
            inv.finish(ok=not host_failed, error_class='host-error' if host_failed else None,
                       model_actual=launch.get('actual_model'))
        watcher.stop()
    except BaseException as exc:
        with process_lock:
            children = list(processes)
        for child in children:
            kill_process_group(child)
        launch.update(interrupted=True, exception=type(exc).__name__, seconds=time.time()-launch['started_at'])
        raise
    finally:
        try:
            save(out / 'launch.json', launch)
            # Retain partial transcripts even when SIGTERM/KeyboardInterrupt
            # takes the exceptional path. SIGKILL leaves original files in
            # durable storage; a subsequent delivery never overwrites them.
            # A Claude host's transcript is events.jsonl itself.
            if host == 'codex':
                (out / 'rollout.jsonl').write_bytes(b''.join(p.read_bytes() for p in rollouts()))
        finally:
            secret_values = []
            for login in logins:
                secret_values += login['secrets']
                if login['jailed'].exists():
                    secret_values += _secret_strings(login['jailed'].read_bytes())
            try:
                for login in logins:
                    notes += write_back_credentials(login['original'], login['jailed'], login['real'], login['label'])
            finally:
                shutil.rmtree(home, ignore_errors=True)
                scrub_login_secrets([p for p in out.glob('*') if p.is_file()], secret_values)
    if host == 'codex':
        usage = parse_codex_events(out / 'events.jsonl')
    else:
        if usage['extra_usage']:
            notes.append("billed to the subscription's EXTRA USAGE credits (Claude Code reported isUsingOverage=true)")
        notes += usage['billing_violations']
    result = {'host': str(out), 'host_cli': host, 'engine_state': str(journal), 'complete_workflow': False,
              'launch': launch, 'credential_notes': notes, 'usage': usage}
    if (journal / 'nicks.db').exists():
        child = DB(journal / 'nicks.db')
        plan = child.latest_dev_plan()
        if plan:
            result.update(plan=plan['id'], status=plan['status'], head=plan['head'], job=plan['review_job'])
            if plan['review_job']:
                check = subprocess.run([str(cli), 'nickquisition', 'verdict', plan['review_job'], '--json'],
                                       env={**os.environ, 'NICKS_HOME': str(journal)}, capture_output=True, text=True)
                (out / 'final-verification.stdout').write_text(check.stdout)
                (out / 'final-verification.stderr').write_text(check.stderr)
                if check.returncode in (0, 2):
                    verdict = json.loads(check.stdout)
                    result['verdict'] = verdict
                    reviewed = child.get_job(plan['review_job'])
                    integration = (plan.get('detail') or {}).get('integration') or {}
                    actual_ballots = child.all('SELECT source FROM nq_ballots WHERE job_id=? AND round=?',
                                               (plan['review_job'], verdict['round']))
                    result['complete_workflow'] = (launch['exit_code'] == 0 and not launch['timed_out'] and not launch['interrupted']
                        and not launch.get('host_error') and not launch.get('billing_violations')
                        and plan['status'] in ('complete', 'rejected', 'blocked')
                        and reviewed['snapshot']['head_commit'] == plan['head']
                        and integration.get('passed') and integration.get('head') == plan['head']
                        and len(actual_ballots) == 3 and all(b['source'] == 'collected-by-orchestrator' for b in actual_ballots)
                        and not (journal / 'jobs' / plan['review_job'] / 'council-exception.json').exists()
                        and verdict['verdict'] in ('APPROVED', 'REJECTED', 'BLOCKED')
                        and verdict['integrity'] == 'ok' and verdict.get('delivery_integrity', 'ok') == 'ok')
        child.conn.close()
    save(out / 'result.json', result)
    return result
