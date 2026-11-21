"""`nicks` command-line interface (usable standalone or from the Claude Code or Codex skills)."""
from __future__ import annotations

import argparse
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__
from .config import Settings, load_settings, opencode_bin, state_dir, set_config
from .db import DB
from .registry import DiscoveryError, Route, discover
from .review import JobLocked, ReviewExecutor, build_prompt, create_job, job_dir, new_job_id
from .router import Router
from .runner import FaultPlan, OpenCodeRunner
from .evil import EvilUnavailable, VerdictError
from .neutral import NeutralUnavailable
from .nickquisition import NickquisitionError
from .dev import DevError
from .providers import ProviderConfigError
from .sandbox import SandboxUnavailable, self_test
from .snapshot import NothingToReview, SnapshotError, capture, make_writable

EXIT_OK, EXIT_ERR, EXIT_INSUFFICIENT, EXIT_NONE, EXIT_INTERRUPTED = 0, 1, 3, 4, 130
EXIT_CONTINUE, EXIT_BLOCKED = 5, 6        # nicks dev execute: paused (run again) / needs human attention
EXIT_AWAITING = 7                         # implementation complete; The Nickquisition blocked by the billing gate


def open_db() -> DB:
    return DB(state_dir() / "nicks.db")


def _fmt_time(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "-"


def _sigterm(_signo, _frame):
    raise KeyboardInterrupt


def _settings(args) -> Settings:
    return load_settings({
        "nicks": getattr(args, "nicks", None),
        "min_nicks": getattr(args, "min_nicks", None),
        "job_timeout": getattr(args, "timeout", None),
        "attempt_timeout": getattr(args, "attempt_timeout", None),
        "allow_local": False if getattr(args, "no_local", False) else None,
        "subscription_profile": getattr(args, "subscriptions", None),
        "restricted": (getattr(args, "mode", None) == "restricted") if getattr(args, "mode", None) else None,
    })


def _restore_policy(args, settings, saved):
    from .policy import restore
    if saved:
        if (getattr(args, "subscriptions", None) is not None and
                args.subscriptions != saved.get("subscription_profile")) or (
                getattr(args, "mode", None) is not None and
                (args.mode == "restricted") != saved.get("restricted")):
            raise ValueError("job execution policy is pinned; create a new job to change subscriptions or mode")
    return restore(settings, saved)


def _routes(settings, db, only: list[str] | None) -> list[Route]:
    routes = discover(settings, db)
    if only:
        by_id = {r.route: r for r in routes}
        for rid in only:
            if rid not in by_id:
                raise DiscoveryError(f"route {rid!r} was not discovered (see `nicks models --all`)")
            if not by_id[rid].eligible:
                raise DiscoveryError(f"route {rid!r} is not eligible for free use: {by_id[rid].reason}")
        routes = [by_id[r] for r in only]
    return routes


def make_runner(settings: Settings, **kw) -> OpenCodeRunner:
    from .providers import provider_env
    return OpenCodeRunner(steps=kw.pop("steps", settings.agent_steps), sandbox=settings.sandbox,
                          sandbox_extra_ro=settings.sandbox_extra_ro, sandbox_extra_rw=settings.sandbox_extra_rw,
                          provider_env={} if settings.restricted else provider_env(settings), **kw)


def cmd_providers(args) -> int:
    from . import providers
    if args.action == "add":
        e = providers.add(args.provider, policy=args.policy, key_file=args.key_file, key_env=args.key_env,
                          env=args.env or None, models=args.models.split(",") if args.models else None,
                          anonymous=args.anonymous, endpoint=args.endpoint, quota_pool=args.quota_pool,
                          quota_scope=args.quota_scope, entitlements_file=args.entitlements_file)
        credential = "anonymous" if e.get("anonymous") else "file " + e["key_file"] if e.get("key_file") else "env " + e.get("key_env", "")
        print(f"registered {args.provider}: policy={e['policy']} env={e['env']} key={credential}"
              + (f" models={e['models']}" if e.get("models") else ""))
        print("Next: nicks providers discover   (catalog eligibility is not live inference verification)")
    elif args.action == "discover":
        settings = load_settings()
        db = open_db()
        routes = discover(settings, db)
        records = {r['provider']: json.loads(r['detail']) for r in db.all('SELECT * FROM provider_discovery')}
        print(json.dumps({"providers": records, "routes": [r.row() for r in routes]}, indent=2))
    elif args.action == "probe":
        if not args.route or len(args.route) > 8:
            raise ValueError("providers probe requires 1–8 explicit --route values; it never probes the entire catalog")
        from .capacity import probe_routes
        print(json.dumps(probe_routes(load_settings(), open_db(), args.route, args.probe_timeout), indent=2))
    elif args.action == "remove":
        print("removed" if providers.remove(args.provider) else "not registered")
    else:
        settings = load_settings()
        keys = providers.provider_env(settings)
        for pid, cfg in (settings.free_providers or {}).items():
            print(f"{pid:<14} policy={cfg.get('policy')} env={cfg.get('env')} key={'anonymous' if cfg.get('anonymous') else 'available' if pid in keys else 'MISSING (SETUP_REQUIRED)'}"
                  + (f" models={cfg['models']}" if cfg.get("models") else ""))
        if not settings.free_providers:
            print("no external free providers registered (see `nicks providers add --help`)")
    return EXIT_OK


def cmd_config(args) -> int:
    if args.action == "set":
        try:
            set_config(args.key, args.value)
        except ValueError as exc:
            print(f"nicks: {exc}", file=sys.stderr)
            return EXIT_ERR
        return EXIT_OK
    from .policy import allows
    from .policy import resolve
    s = resolve(load_settings())
    authorized = ", ".join(p for p in ("claude", "codex") if allows(s, p)) or "none"
    print(f"host: {s.host}\nsubscriptions: {s.subscription_profile}\nauthorized: {authorized}\nmode: {'restricted' if s.restricted else 'hybrid'}")
    return EXIT_OK


def _print_result(res: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(res, indent=2, default=str))
    else:
        print(f"Job {res['job_id']}: {res['status']} - {res['usable']}/{res['requested']} usable Nick reviews "
              f"from {res['distinct_families']} model families ({res['attempts']} attempts, "
              f"{res['failed_attempts']} failed, {res['injected_faults']} injected). "
              f"Snapshot integrity: {res['snapshot_integrity']}.")
        if res.get("unfilled"):
            print("Unfilled slots: " + ", ".join(f"{k} ({v})" for k, v in sorted(res["unfilled"].items())))
        print(f"PACKET: {res['packet']}")
    return {"complete": EXIT_OK, "degraded": EXIT_OK, "insufficient": EXIT_INSUFFICIENT,
            "exhausted": EXIT_NONE, "interrupted": EXIT_INTERRUPTED}.get(res["status"], EXIT_ERR)


# --------------------------------------------------------------------------- commands

def cmd_init(args) -> int:
    """Explicit startup; ordinary discovery runs the same automatic registration and bounded checks."""
    from .onboarding import summary, print_summary
    from .subscription import subscription_routes
    settings = _settings(args)
    db = open_db()
    routes = discover(settings, db)
    problems = {}
    if settings.hybrid_council or settings.restricted:
        subs, problems = subscription_routes(settings)
        routes += subs
    report = summary(settings, db, routes)
    if not report["can_operate"]:
        report["missing_capabilities"] += [f"{role}: {why}" for role, why in problems.items()
                                           if why != "excluded by subscription profile"]
    if settings.sandbox:
        good, detail = self_test("/bin/sh", state_dir())
        if not good:
            report["can_operate"] = False
            report["missing_capabilities"].append("worker sandbox unavailable: " + detail)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_summary(report)
    return EXIT_OK if report["can_operate"] else EXIT_NONE


def _council(settings, db, routes_filter, log=True) -> tuple[list, object]:
    """(routes, runner) for a Council: free routes (+ optional filter) plus subscription workers when hybrid."""
    from .subscription import CouncilRunner, subscription_routes
    try:
        routes = [] if settings.restricted else _routes(settings, db, routes_filter)
    except DiscoveryError:
        if routes_filter or not settings.hybrid_council:
            raise
        routes = []  # OpenCode is optional when subscription capacity is eligible
    if settings.hybrid_council or settings.restricted:
        subs, problems = subscription_routes(settings)
        routes += subs
        for role, why in problems.items():
            print(f"[nicks] subscription worker {role} unavailable: {why}", file=sys.stderr)
    return routes, CouncilRunner(make_runner(settings), settings)


def _composition(settings, n: int | None) -> list[str]:
    from .policy import resolve
    resolve(settings)
    if not settings.hybrid_council:
        if settings.restricted:
            raise ValueError("Restricted cannot use --no-subscription")
        return ["free"] * (n or settings.nicks)
    from .policy import composition
    comp = composition(settings, settings.council_composition)
    if n:
        if n < 1:
            raise ValueError("number of Council workers must be positive")
        comp = (comp + ["subscription" if settings.restricted else "free"] * n)[:n]
    return comp


def _dev_council(settings) -> tuple[list[str], list, object]:
    """Default development review: three blind subscription reviewers (nicks/policy.py dev_review_composition),
    drawn from the existing subscription availability detection. No free routes are discovered or offered."""
    from .policy import dev_review_composition, resolve
    from .subscription import CouncilRunner, role_of, subscription_routes
    resolve(settings)
    subs, problems = subscription_routes(settings)
    available = {role_of(r, settings): r for r in subs}
    try:
        kinds = dev_review_composition(available)
    except ValueError as exc:
        why = "; ".join(f"{k}: {v}" for k, v in problems.items())
        raise ValueError(str(exc) + (f" ({why})" if why else "")) from exc
    return kinds, [available[k] for k in dict.fromkeys(kinds)], CouncilRunner(make_runner(settings), settings)


def cmd_review(args, settings=None, metadata=None, development=False) -> int:
    settings = settings or _settings(args)
    if args.no_subscription:
        settings.hybrid_council = False
    from .policy import default_dev_review
    strict = development and default_dev_review(settings) and not (args.nicks or args.route)
    if strict:
        kinds, routes, runner = _dev_council(settings)
    else:
        kinds = _composition(settings, args.nicks)
    settings.nicks = len(kinds)
    if strict:
        settings.min_nicks = settings.nicks       # every required review must complete
    if settings.min_nicks > settings.nicks:
        settings.min_nicks = settings.nicks
    db = open_db()
    if not strict:
        routes, runner = _council(settings, db, args.route)   # fails fast if the sandbox is required but unavailable
    job_id = new_job_id()
    jd = job_dir(job_id)
    try:
        snap = capture(args.repo, jd, base=args.base, head=args.head, uncommitted=args.uncommitted,
                       diff_file=args.diff_file, merge_base=not args.no_merge_base, max_diff_bytes=settings.max_diff_bytes)
    except BaseException:
        if jd.exists():
            make_writable(jd)
            shutil.rmtree(jd, ignore_errors=True)
        raise
    for warn in snap["warnings"]:
        print(f"[nicks] warning: {warn}", file=sys.stderr)
    print(f"[nicks] review target: {snap['files_changed']} files, +{snap['insertions']} -{snap['deletions']} "
          f"({snap['base_ref']}@{(snap['base_sha'] or '')[:10]} .. {snap['head_ref']})", file=sys.stderr)
    prompt = build_prompt(snap, Path(snap["diff_path"]).read_text(encoding="utf-8"), args.brief,
                          inline_limit=settings.council_inline_bytes, context_bytes=settings.review_context_bytes,
                          context_file_bytes=settings.review_context_file_bytes)
    # The optional tool-less context index can exceed Codex's input limit on
    # large repositories. Native Codex reviewers already have the entire frozen
    # snapshot: use that existing path, without truncating the diff or brief.
    from .evidence_delivery import MAX_REQUEST
    if (len(prompt.encode('utf-8')) > MAX_REQUEST and settings.restricted and routes
            and all(r.kind == 'codex' and r.tools for r in routes)):
        (jd / 'original-inline-prompt.txt').write_text(prompt, encoding='utf-8')
        prompt = build_prompt(snap, Path(snap['diff_path']).read_text(encoding='utf-8'), args.brief,
                              inline_limit=0)
        print('[nicks] oversized inline context: native reviewers use the complete read-only snapshot; '
              'original inline prompt preserved', file=sys.stderr)
    # Admission covers the complete serialized request, including indexes and
    # the brief. A snapshot fallback can still be too large (e.g. a huge diff).
    if any(r.kind == 'codex' for r in routes):
        from .evidence_delivery import budget
        (jd / 'admission-prompt.txt').write_text(prompt, encoding='utf-8')
        budget(prompt)  # fail before scheduling any inference; never truncate
    print(f"[nicks] Council composition: {', '.join(kinds)}", file=sys.stderr)
    create_job(db, snap, prompt, settings, {**(metadata or {}), "brief": args.brief, "routes_filter": args.route or None,
                                            "slot_kinds": kinds, "hybrid": settings.hybrid_council,
                                            **({"strict_composition": True} if strict else {})}, job_id=job_id)
    if metadata and metadata.get('diagnostic'):
        from .diagnostic import bind_job
        bind_job(db, metadata['diagnostic'], job_id)
    ex = ReviewExecutor(db, settings, routes, runner)
    return _print_result(ex.run(job_id), args.json)


def _resolve_job(db, ref: str | None) -> dict:
    job = db.find_job(ref) if ref else db.latest_job()
    if not job:
        raise LookupError(f"no job matching {ref!r}" if ref else "no jobs yet")
    return job


def cmd_resume(args) -> int:
    settings = _settings(args)
    db = open_db()
    job = _resolve_job(db, args.job)
    _restore_policy(args, settings, job["params"].get("execution_policy"))
    settings.hybrid_council = bool(job["params"].get("hybrid")) and settings.hybrid_council
    if job["params"].get("strict_composition"):   # default development review: its own subscription routes only
        from .subscription import CouncilRunner, subscription_routes
        routes, runner = subscription_routes(settings)[0], CouncilRunner(make_runner(settings), settings)
    else:
        routes, runner = _council(settings, db, job["params"].get("routes_filter"))
    settings.min_nicks = job["params"].get("min_nicks", settings.min_nicks)
    ex = ReviewExecutor(db, settings, routes, runner)
    return _print_result(ex.run(job["id"]), args.json)


def cmd_status(args) -> int:
    db = open_db()
    if not args.job:
        jobs = db.list_jobs(args.limit)
        if args.json:
            print(json.dumps(jobs, indent=2, default=str))
            return EXIT_OK
        if not jobs:
            print("No jobs yet. Run `nicks review`.")
        for j in jobs:
            s = json.loads(j["summary"]) if j["summary"] else {}
            print(f"{j['id']:<26} {j['status']:<13} {s.get('usable', '-')}/{s.get('requested', '-'):<3} "
                  f"{_fmt_time(j['created_at'])}  {j['repo']}")
        return EXIT_OK
    job = _resolve_job(db, args.job)
    attempts = db.attempts(job["id"])
    if args.json:
        print(json.dumps({"job": job, "slots": db.slots(job["id"]), "attempts": attempts}, indent=2, default=str))
        return EXIT_OK
    s = job["summary"] or {}
    print(f"Job {job['id']}  status={job['status']}  usable={s.get('usable', '-')}/{s.get('requested', '-')}  "
          f"repo={job['repo']}")
    ep = job["params"].get("execution_policy") or {}
    print(f"  policy: subscriptions={ep.get('subscription_profile', 'unknown')} mode={'restricted' if ep.get('restricted') else 'hybrid'}")
    for sl in db.slots(job["id"]):
        print(f"  slot {chr(65 + sl['slot'])}: {sl['status']}{' - ' + sl['note'] if sl['note'] else ''}")
    for a in attempts:
        dur = f"{a['finished_at'] - a['started_at']:.0f}s" if a["finished_at"] else "running"
        print(f"  {a['id']:<36} {a['route']:<40} {a['status']:<11} {a['error_class'] or '':<14} {dur:>7}"
              f"{'  INJECTED' if a['injected'] else ''}")
    if s.get("packet"):
        print(f"PACKET: {s['packet']}")
    return EXIT_OK


def cmd_packet(args) -> int:
    db = open_db()
    job = _resolve_job(db, args.job)
    path = job_dir(job["id"]) / "packet.md"
    if args.rebuild or not path.exists():
        from .packet import write_packet
        path = write_packet(db, job["id"], load_settings())
    if args.print:
        sys.stdout.write(path.read_text(encoding="utf-8"))
    else:
        print(f"PACKET: {path}")
    return EXIT_OK


def cmd_report(args) -> int:
    """Markdown report of a development plan or review job, from recorded data only (nicks/report.py)."""
    from . import report
    db = open_db()
    ref = args.job or ((db.latest_dev_plan() or {}).get("id") or (db.latest_job() or {}).get("id"))
    if not ref:
        raise LookupError("no development plans or review jobs yet")
    path = report.write(db, ref)
    if args.print:
        sys.stdout.write(path.read_text(encoding="utf-8"))
    else:
        print(f"REPORT: {path}")
    return EXIT_OK


def cmd_stats(args) -> int:
    """Cross-run statistics over development runs, from recorded data only (nicks/stats.py)."""
    from . import stats
    homes = args.home or [str(state_dir())]
    summary = stats.compute(homes, include_unconcluded=args.all)
    print(stats.to_json(summary) if args.json else stats.render_text(summary))
    return EXIT_OK


def _codex_runner(settings: Settings):
    from .evil import CodexRunner
    return CodexRunner(sandbox=settings.sandbox, model=settings.evil_model or None,
                       sandbox_extra_ro=settings.sandbox_extra_ro, sandbox_extra_rw=settings.sandbox_extra_rw)


def cmd_evil(args) -> int:
    from . import evil
    settings = _settings(args)
    if args.timeout is not None: settings.evil_timeout = args.timeout
    db = open_db()
    job = _resolve_job(db, args.job)
    from .policy import RoleRunner, restore
    _restore_policy(args, settings, job["params"].get("execution_policy"))
    runner = RoleRunner(db, settings, "evil")
    print(f"[nicks] Evil Nick {args.stage} on job {job['id']} via {runner.version()} ...", file=sys.stderr, flush=True)
    if args.stage == "attack":
        r = evil.attack(db, job["id"], runner, settings.evil_timeout, settings)
    else:
        r = evil.challenge(db, job["id"], runner, settings.evil_timeout)
    resp = Path(r["dir"]) / "response.txt"
    for n in r.get("notes") or []:
        print(f"[nicks] note: {n}", file=sys.stderr)
    if args.json:
        print(json.dumps(r, indent=2, default=str))
    else:
        print(f"Evil Nick {args.stage}: {r['status']}"
              + (f" ({r['error_class']}: {r['error']})" if r["status"] != "ok" else
                 f", {r['response_bytes']} bytes, {r.get('commands', 0)} shell commands in the snapshot"))
        print(f"EVIL_RESPONSE: {resp}")
        if args.stage == "attack":
            print(f"PACKET: {job_dir(job['id']) / 'packet.md'}")
    return EXIT_OK if r["status"] == "ok" else EXIT_INSUFFICIENT


def _neutral_runners(settings: Settings) -> dict:
    from .neutral import ClaudeRunner
    kw = dict(sandbox_extra_ro=settings.sandbox_extra_ro, sandbox_extra_rw=settings.sandbox_extra_rw)
    from .evil import CodexRunner
    from .policy import allows
    runners = {}
    if allows(settings, "claude"):
        runners["anthropic"] = ClaudeRunner(sandbox=settings.sandbox, **kw)
    if allows(settings, "codex"):
        runners["openai"] = CodexRunner(sandbox=settings.sandbox, **kw)
    return runners


def cmd_neutral(args) -> int:
    from . import neutral
    settings = _settings(args)
    if getattr(args, "timeout", None) is not None: settings.neutral_timeout = args.timeout
    db = open_db()
    if args.action == "models":
        statuses = neutral.provider_statuses(db, settings)
        for prov, st in statuses.items():
            print(f"{prov:<10} {'eligible' if st.available else 'UNAVAILABLE'} - {st.reason}")
            for m in st.models:
                print(f"    {m}")
            for m, why in st.skipped.items():
                print(f"    {m}  (skipped: {why})")
        if args.probe:
            print("\nProbing allowlisted models with a one-line prompt (subscription usage)...")
            for prov, m, ok, detail in neutral.probe(db, settings, _neutral_runners(settings)):
                print(f"  [{'ok' if ok else 'fail'}] {prov}/{m}: {detail}")
        return EXIT_OK
    job = _resolve_job(db, args.job)
    if args.action == "status":
        for sel in db.neutral_selections(job["id"]):
            print(f"draw #{sel['id']} {sel['reason']:<15} {sel['provider']}/{sel['model']}  run={sel['run_id']}"
                  + (f"  replaced {sel['replaces_run']} ({sel['failure']})" if sel["replaces_run"] else "")
                  + (f"  [{sel['notes']}]" if sel["notes"] else ""))
        for r in neutral.neutral_runs(db, job["id"]):
            print(f"run {r['id']}: {r['status']} actual model={r['model']} {r['error_class'] or ''}")
        return EXIT_OK
    from .policy import restore
    _restore_policy(args, settings, job["params"].get("execution_policy"))
    r = neutral.review(db, job["id"], settings, _neutral_runners(settings), good_model=args.good_model,
                       force_provider=args.provider, log=lambda m: print(f"[nicks] neutral: {m}", file=sys.stderr, flush=True))
    sel = r["selection"]
    if args.json:
        print(json.dumps(r, indent=2, default=str))
    else:
        how = "reused the recorded review (Neutral Nick never re-reviews a job)" if r["reused"] else r["status"]
        print(f"Neutral Nick: {how}; provider {sel['provider']}, requested {sel['model']}, actual model {r['model']}"
              + (f" ({r['error_class']}: {r['error']})" if r["status"] != "ok" else ""))
        if sel["notes"]:
            print(f"Note: {sel['notes']}")
        print(f"NEUTRAL_RESPONSE: {Path(r['dir']) / 'response.txt'}")
        print(f"PACKET: {job_dir(job['id']) / 'packet.md'}")
    return EXIT_OK if r["status"] == "ok" else EXIT_INSUFFICIENT


def cmd_nickquisition(args) -> int:
    from . import nickquisition as nq
    settings = _settings(args)
    db = open_db()
    job = _resolve_job(db, args.job)
    from .policy import restore, RoleRunner
    if args.action in ('open', 'run', 'convene'):
        nq.require_council(db, job['id'], args.allow_incomplete_council)
    _restore_policy(args, settings, job["params"].get("execution_policy"))
    runners = {"evil": RoleRunner(db, settings, "evil"), "good": RoleRunner(db, settings, "good"),
               **_neutral_runners(settings)}
    log = lambda m: print(f"[nicks] nickquisition: {m}", file=sys.stderr, flush=True)
    if args.action == "scale":
        from .judicial_evidence import scale_round
        rnd = scale_round(db, job['id'], args.round)
        print(f"SCALED ROUND: {rnd['round']}  ARTIFACT: {rnd['artifact']}")
        print('Original round, results and investigations preserved; next: nickquisition run '+job['id'])
        return EXIT_OK
    if args.action == "open":
        r = nq.open_round(db, job["id"], test_cmd=args.test_cmd, new_round=args.new_round,
                          gate_timeout=settings.nq_gate_timeout, settings=settings, good_model=args.model,
                          runners=runners, log=log)
        gates = json.loads(r["gates"] or "{}")
        print(f"Nickquisition round {r['round']} {'(already open)' if r['reused'] else 'opened'} for job {job['id']}")
        print(f"Objective gate: {gates.get('status')}: {gates.get('summary')}")
        print(f"ARTIFACT: {nq.short(r['artifact'])}")
        print(f"EVIDENCE: {r['evidence_path']}")
        return EXIT_OK
    if args.action == "submit":
        text = sys.stdin.read() if args.file in (None, "-") else Path(args.file).read_text(encoding="utf-8")
        b = nq.submit_good(db, job["id"], vote=args.vote, artifact=args.artifact or "", justification=args.justification or "",
                           deliberation=text, model=args.model)
        print(f"Good Nick's ballot recorded for round {b['round']}: {b['vote']} (hash {b['record_sha256'][:12]}). "
              f"Next: nicks nickquisition convene {job['id']}")
        return EXIT_OK
    from . import nq_live
    shown_to = sys.stderr if args.json else sys.stdout      # keep --json output machine-readable
    if args.action == "run":
        # Gate passed above: the Council's reviews and reconciliation are complete. No ballot is shown yet.
        cur = nq.current_round(db, job["id"])
        n = (cur or {}).get("round") or 1
        if cur and db.one("SELECT 1 FROM nq_verdicts WHERE job_id=? AND round=?", (job["id"], n)):
            n += 1                                   # a judged round is closed; judging opens the next one
        print(nq_live.render_announcement(db, job["id"], n, colour=nq_live.use_colour(shown_to)), file=shown_to,
              flush=True)
        v = nq.run_independent(db, job["id"], settings, runners, test_cmd=args.test_cmd, log=log)
    elif args.action == "convene":
        v = nq.convene(db, job["id"], runners,
                       timeout=settings.nq_timeout, retry_failed=args.retry_failed, log=log, settings=settings)
    elif args.action == "show":
        rnd = nq._round(db, job["id"], args.round)
        for m, b in nq.ballots(db, job["id"], rnd["round"]).items():
            print(f"{nq.NAMES[m]:<13} {('INVALID' if not b['valid'] else b['vote']):<8} model={b['model']} "
                  f"source={b['source']} hash={b['record_sha256'][:12]}")
            print(f"    justification: {b['justification']}")
            print(f"    deliberation:  {b['deliberation']}")
        print(f"EVIDENCE: {rnd['evidence_path']}")
        return EXIT_OK
    else:   # verdict: recomputed from the database and raw outputs, no model involved
        v = nq.verify(db, job["id"], args.round)
        rnd = nq._round(db, job["id"], args.round)
        latest = db.one('SELECT report_path FROM nq_verdicts WHERE job_id=? AND round=? ORDER BY id DESC LIMIT 1', (job['id'], rnd['round']))
        v["report"] = latest['report_path'] if latest else str(Path(rnd["evidence_path"]).parent / "verdict.md")
    nq_live.print_panel(db, job["id"], v.get("round"), stream=shown_to)   # only once the verdict is recorded
    if args.json:
        print(json.dumps(v, indent=2, default=str))
    else:
        print(f"THE NICKQUISITION VERDICT: {v['verdict']} ({v['tally']})  job {job['id']} round {v['round']}")
        for m in nq.MEMBERS:
            vote = v["votes"].get(m) or ("INVALID: " + v["invalid"][m] if m in v["invalid"] else "MISSING")
            print(f"  {nq.NAMES[m]:<13} {vote}{'  (dissent)' if m in v['dissent'] else ''}")
        for blk in v["blockers"]:
            print(f"  BLOCKER: {blk}")
        print(f"  integrity: {v['integrity']}" + (f" - {'; '.join(v['integrity_problems'])}" if v["integrity_problems"] else ""))
        print(f"REPORT: {v.get('report')}")
    return {"APPROVED": EXIT_OK, "REJECTED": 2, "BLOCKED": 2, "INCOMPLETE": EXIT_INSUFFICIENT}.get(v["verdict"], EXIT_ERR)


def cmd_verdict(args) -> int:
    from . import consensus, evil
    db = open_db()
    job = _resolve_job(db, args.job)
    text = sys.stdin.read() if args.file in (None, "-") else Path(args.file).read_bytes().decode("utf-8")
    if args.decision is None:
        args.decision = consensus.record_response(job_dir(job['id']) / 'reconciliation', text)['decision']
    row = evil.record_decision(db, job["id"], args.stage, args.decision, text, args.unresolved, args.skip_challenge)
    print(f"Recorded Good Nick's {args.stage} decision #{row['id']}: {args.decision} -> {row['path']}")
    if args.stage == "provisional":
        print(f"Next: nicks evil challenge {job['id']}")
    else:
        print(f"FINAL_RECORD: {row['record']}")
        if args.print:
            sys.stdout.write(Path(row["record"]).read_text(encoding="utf-8"))
    return EXIT_OK


def _health_label(router: Router, r: Route) -> str:
    blocked, _, why = router.blocked(r)
    return why if blocked else "ok"


def cmd_models(args) -> int:
    settings = _settings(args)
    db = open_db()
    if settings.restricted:
        routes = []
    elif args.cached:
        routes = [Route.from_row(r) for r in db.routes()]
    else:
        try:
            routes = discover(settings, db)
        except DiscoveryError as exc:
            if not settings.hybrid_council:
                raise
            print(f"[nicks] optional free discovery unavailable: {exc}", file=sys.stderr)
            routes = []
    from .subscription import subscription_routes
    if settings.hybrid_council or settings.restricted:
        subs, problems = subscription_routes(settings)
        routes += subs
    else:
        problems = {}
    if not args.json:
        if settings.auto_onboarding:
            from .onboarding import summary, print_summary
            print_summary(summary(settings, db, routes))
        print(f"host={settings.host}; subscriptions={settings.subscription_profile}; mode={'restricted' if settings.restricted else 'hybrid'}")
        for role, why in problems.items(): print(f"excluded {role}: {why}")
    router = Router(db, routes, allow_local=settings.allow_local)
    rows = []
    for r in routes:
        h = db.health(f"route:{r.route}")
        label = "-" if not r.eligible else ("disabled (--no-local)" if r.local and not settings.allow_local
                                            else _health_label(router, r))
        rows.append({**r.row(), "health": label,
                     "successes": h.get("successes") or 0, "failures": h.get("failures") or 0,
                     "latency_avg": h.get("latency_avg")})
    if args.json:
        print(json.dumps(rows, indent=2))
        return EXIT_OK
    elig = [x for x in rows if x["eligible"]]
    print(f"{len(routes)} routes discovered; {len(elig)} eligible routes.\n")
    print(f"{'ROUTE':<44} {'BASIS':<20} {'FAMILY':<14} {'CTX':>8} {'TOOLS':<5} {'OK/FAIL':<8} HEALTH")
    for x in sorted(elig, key=lambda x: (x["local"], x["route"])):
        print(f"{x['route']:<44} {x['basis']:<20} {(x['family'] or '?')[:14]:<14} {x['context']:>8} "
              f"{'yes' if x['tools'] else 'no':<5} {str(x['successes']) + '/' + str(x['failures']):<8} {x['health']}")
    inel = [x for x in rows if not x["eligible"]]
    if inel:
        if args.all:
            print("\nIneligible routes:")
            for x in inel:
                print(f"  {x['route']:<44} {x['reason']}")
        else:
            print(f"\n{len(inel)} ineligible route(s) hidden (use --all to see reasons).")
    return EXIT_OK


def cmd_doctor(args) -> int:
    db = open_db()
    if args.clear:
        n = db.reset_health(None if args.clear == "all" else args.clear)
        print(f"Cleared {n} health record(s).")
        return EXIT_OK
    from .policy import resolve
    from .neutral import provider_statuses
    from .subscription import subscription_routes
    settings = resolve(_settings(args))
    print(f"[info] host={settings.host}; subscriptions={settings.subscription_profile}; mode={'restricted' if settings.restricted else 'hybrid'}; authorized={settings.authorized_subscriptions}")
    ok = True

    def check(name, good, detail):
        nonlocal ok
        ok = ok and good
        print(f"[{'ok' if good else 'FAIL'}] {name}: {detail}")

    check("git", shutil.which("git") is not None, shutil.which("git") or "not found")
    check("state", True, f"{state_dir()} (db {db.path})")
    if settings.sandbox:
        state_dir().mkdir(parents=True, exist_ok=True)
        good, detail = self_test("/bin/sh", state_dir())
        check("sandbox", good, detail)
    else:
        print("[WARN] sandbox: disabled by existing configuration")
    for prov, st in provider_statuses(db, settings).items():
        print(f"[{'ok' if st.available else 'WARN'}] subscription roles/{prov}: {st.reason}"
              + (f"; models: {', '.join(st.models)}" if st.models else ""))
        for model, why in st.skipped.items():
            print(f"[info] excluded {prov}/{model}: {why}")
    routes = []
    if not settings.restricted:
        try:
            routes = discover(settings, db)
        except DiscoveryError as exc:
            print(f"[WARN] optional free discovery unavailable: {exc}")
    if settings.hybrid_council or settings.restricted:
        subs, problems = subscription_routes(settings)
        routes += subs
        for role, why in problems.items():
            print(f"[info] subscription route {role} excluded: {why}")
    elig = [r for r in routes if r.eligible and (settings.allow_local or not r.local)]
    from .onboarding import summary, print_summary
    report = summary(settings, db, routes)
    print_summary(report)
    check("discovery", report["can_operate"], f"{len(routes)} routes, {len(elig)} eligible under active policy")
    for r in routes:
        if (args.route and r.route in args.route) or r.kind in ("codex", "claude"):
            print(f"[{'ok' if r in elig else 'info'}] route {r.route}: {r.basis}; {r.reason}")
    print("[info] model details: nicks models (--all includes ineligible routes)")
    fams = sorted({r.diversity_key for r in elig})
    print(f"[info] diversity: {len(fams)} model families: {', '.join(fams)}; repeated executions are not distinct families")
    router = Router(db, routes, allow_local=settings.allow_local)
    for h in db.all_health():
        if h.get("quarantined"):
            print(f"[WARN] quarantine {h['scope']}: {h['reason']}")
        elif (h.get("cooldown_until") or 0) > time.time():
            print(f"[info] cooldown {h['scope']} until {_fmt_time(h['cooldown_until'])}: {h['reason']}")

    if args.probe:
        targets = [r for r in elig if not args.route or r.route in args.route]

        def skip(r):
            blocked, until, _ = router.blocked(r)
            return blocked and (until == float("inf") or not args.include_blocked)  # never probe quarantined

        skipped = [r for r in targets if skip(r)]
        for r in skipped:
            print(f"  [skip] {r.route:<44} {router.blocked(r)[2]}")
        targets = [r for r in targets if r not in skipped]
        print(f"\nProbing {len(targets)} route(s) with a one-line prompt (real inference, active policy)...")
        from .subscription import CouncilRunner
        runner = CouncilRunner(make_runner(settings, steps=2, faults=FaultPlan(None)), settings)
        tmp = Path(tempfile.mkdtemp(prefix="nicks-probe-"))

        def probe(r: Route):
            from . import telemetry
            with telemetry.labels(db=db, job_kind="probe", phase="probe"):   # temp dir: no journal to infer
                res = runner.run(r, "Reply with exactly the word PONG and nothing else.", tmp,
                                 tmp / r.route.replace("/", "__"), 240 if r.local else 90, title="nicks probe")
            if res.ok:
                router.record_success(r, res.duration)
                return r, True, f"{res.duration:.1f}s {res.text.strip()[:30]!r}"
            action = router.record_failure(r, res.error_class or "transient", res.error or "", res.retry_after)
            return r, False, f"{res.error_class}: {(res.error or '')[:120]} -> {action}"

        cloud = [r for r in targets if not r.local]
        local = [r for r in targets if r.local]
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(probe, cloud))
        results += [probe(r) for r in local]  # local inference is serial
        good = 0
        for r, success, detail in results:
            good += success
            print(f"  [{'ok' if success else 'fail'}] {r.route:<44} {detail}")
        shutil.rmtree(tmp, ignore_errors=True)
        check("probe", good > 0 and good >= min(settings.min_nicks, len(results)), f"{good}/{len(results)} routes answered")
    return EXIT_OK if ok else EXIT_ERR


# --------------------------------------------------------------------------- parser

def cmd_billing(args) -> int:
    """Pre-inference subscription gate and credit usage, read live from the accounts (no inference)."""
    from . import billing
    out = {}
    for prov in ("anthropic", "openai"):
        e = billing.check(prov, refresh=True)
        out[prov] = {"eligible": e.eligible, "reasons": e.reasons, "facts": e.facts, "source": e.source}
        if not args.json:
            print(f"{prov:<10} {'ELIGIBLE' if e.eligible else 'BLOCKED'}  " + "; ".join(e.reasons))
            for k, v in e.facts.items():
                print(f"           {k} = {v}")
    if not args.json:
        print("credit-only models (not included in the subscription; permanently excluded): " + ", ".join(
            billing.CREDIT_BILLED_MODELS + tuple(billing.credit_billed_anthropic_models())))
        print("policy: subscription logins only (never API keys); the subscriptions' extra usage / credits are "
              "authorized, so runs MAY incur incremental charges up to the organization's limits")
    else:
        print(json.dumps(out, indent=2))
    return EXIT_OK if all(v["eligible"] for v in out.values()) else EXIT_INSUFFICIENT


def cmd_dev(args) -> int:
    """Development Engine: Architect Nick plans (`plan`/`amend`), the Council implements (`run`), the Nickquisition judges
    (`review`, then `nickquisition` as usual)."""
    from . import dev
    db = open_db()
    settings = _settings(args)
    if args.action == 'rework':
        from .rework import deliver
        if not args.plan or not args.from_job:
            raise ValueError('usage: nicks dev rework PLAN --from REJECTED_JOB [--max-seconds N]')
        result = deliver(db, settings, args.plan, args.from_job, 3600 if args.max_seconds is None else args.max_seconds)
        print(json.dumps(result, indent=2))
        return EXIT_OK if result['complete_workflow'] else EXIT_INSUFFICIENT
    if args.action == 'deliver':
        from .architect import deliver
        request = Path(args.request_file).read_text(encoding='utf-8') if args.request_file else (args.request or '')
        result = deliver(db, settings, args.repo, request, args.max_seconds)
        print(json.dumps(result, indent=2))
        return EXIT_OK if result['complete_workflow'] else EXIT_INSUFFICIENT
    if args.action == "plan":
        request = Path(args.request_file).read_text(encoding="utf-8") if args.request_file else (args.request or "")
        if not request.strip():
            raise ValueError("give the user's feature request verbatim with --request or --request-file")
        if bool(args.file) == bool(args.architect):
            raise ValueError("give --file <manifest.json> or --architect to generate the same manifest")
        planning = None
        if args.architect:
            from .architect import generate
            manifest, planning = generate(db, settings, args.repo, request, args.base or "HEAD")
        else:
            manifest = json.loads(Path(args.file).read_text(encoding="utf-8"))
        plan = dev.create_plan(db, settings, args.repo, request, manifest, base=args.base or "HEAD",
                               debate=bool(args.debate))
        if planning:
            db.set_dev_plan(plan["id"], detail=dict(plan.get("detail") or {}, architect=planning))
        for w in plan["warnings"]:
            print(f"[nicks dev] warning: {w}", file=sys.stderr)
        ready = [t["task_id"] for t in dev.ready_tasks(db, plan["id"])]
        print(f"PLAN: {plan['id']}  ({len(plan['manifest']['tasks'])} task(s); ready: {', '.join(ready) or 'none'})")
        print(f"PLAN_MD: {Path(plan['dir']) / 'plan.md'}")
        if args.debate:
            print(f"Next: the design debate with Evil Nick: `nicks dev debate {plan['id']} start`")
        return EXIT_OK
    if args.action == "amend":
        if not (args.plan and args.file and (bool(args.replace) != bool(args.add))):
            raise ValueError("usage: nicks dev amend <plan> (--replace <task> | --add) --reason \"...\" --file <tasks.json>")
        if args.diagnostic_job:
            from .diagnostic import correct
            plan = db.dev_plan(args.plan)
            if not args.replace or not args.response_file:
                raise ValueError('diagnostic correction needs --replace and --response-file')
            from .policy import restore
            restore(settings, (plan.get('detail') or {}).get('execution_policy'))
            with dev.plan_lock(plan):
                new = correct(db, settings, plan['id'], args.replace, args.diagnostic_job,
                              json.loads(Path(args.file).read_text()), Path(args.response_file).read_text())
        else:
            new = dev.amend_plan(db, settings, db.dev_plan(args.plan)["id"], args.replace,
                                 json.loads(Path(args.file).read_text(encoding="utf-8")), reason=args.reason or "")
        print(f"task {args.replace} superseded by: {', '.join(new)}" if args.replace else f"added: {', '.join(new)}")
        print(dev.status_report(db, args.plan))
        return EXIT_OK
    plan = db.dev_plan(args.plan) if args.plan else db.latest_dev_plan()
    if not plan:
        raise LookupError(f"no development plan matching {args.plan!r}" if args.plan else "no development plans yet")
    from .policy import restore
    _restore_policy(args, settings, (plan.get("detail") or {}).get("execution_policy"))
    if args.action == 'compete':
        from .competing import run
        result = run(db, settings, plan['id'], args.task, args.max_seconds)
        print(json.dumps(result, indent=2) if args.json else
              f"COMPETING: {result['status']} selected={result.get('selected')} reason={result.get('reason')}")
        return EXIT_OK if result['status'] == 'integrated' else EXIT_INSUFFICIENT
    if args.action == "run":
        res = dev.run_task(db, settings, plan["id"], args.task)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"TASK: {res.get('task', '-')}  STATUS: {res['status']}  OUTCOME: {res.get('outcome', res['status'])}")
            if res.get("result_md"):
                print(f"RESULT: {res['result_md']}")
            print("PLAN STATUS: " + ", ".join(f"{k}={v}" for k, v in res["tasks"].items()))
        return {"verified": EXIT_OK, "needs-decomposition": EXIT_INSUFFICIENT,
                "blocked": EXIT_INSUFFICIENT}.get(res["status"], EXIT_NONE)
    if args.action == "execute":
        res = dev.execute_plan(db, settings, plan["id"], args.max_seconds)
        print(json.dumps(res, indent=2) if args.json else dev.status_report(db, plan["id"]))
        if res["stop"] == "paused" or (res["status"] in ("executing", "planned") and res["stop"] is None and res["pending"]):
            print("EXECUTE: paused (time budget); run `nicks dev execute` again to continue", file=sys.stderr)
            return EXIT_CONTINUE
        return {"implemented": EXIT_OK, "needs-decomposition": EXIT_INSUFFICIENT, "blocked": EXIT_BLOCKED}.get(
            res["status"], EXIT_NONE)
    if args.action == "integrate":
        v = dev.verify_integration(db, settings, plan)
        for r in v["runs"]:
            print(f"[nicks dev] integration `{r['command']}` -> exit {r['exit_code']} ({r['seconds']}s)", file=sys.stderr)
        if not v["passed"]:
            print(dev.status_report(db, plan["id"]))
            print("INTEGRATION: FAILED (the cumulative change does not pass); add a fix task with `nicks dev amend --add`")
            return EXIT_INSUFFICIENT
        print(f"INTEGRATION: passed on cumulative {v['head'][:12]}; starting the final Council review", file=sys.stderr)
        if (plan.get('detail') or {}).get('competition', {}).get('status') == 'integrated':
            from .competing import judgment_job
            job = db.get_job(judgment_job(db, settings, db.dev_plan(plan['id'])))
            rc = EXIT_OK
        else:
            rc = _dev_review(db, dev, db.dev_plan(plan["id"]), None, args.json)   # with the integration results
            job = db.latest_job()
        db.set_dev_plan(plan["id"], review_job=job["id"], status="in-review")
        dev.write_plan_md(db, plan["id"])
        print(f"FINAL_REVIEW_JOB: {job['id']}")
        blockers = dev.judgment_blockers(settings)
        if blockers:
            dev.mark_awaiting_judgment(db, db.dev_plan(plan["id"]), blockers)
            print("AWAITING_JUDGMENT: the implementation is complete, integrated and preserved; The Nickquisition "
                  "cannot convene under the billing gate: " + "; ".join(blockers))
            return EXIT_AWAITING
        print(f"Now The Nickquisition: verdict --stage provisional, nickquisition open/submit/convene, then "
              f"`nicks dev conclude {plan['id']}`")
        return rc
    if args.action == "conclude":
        r = dev.conclude(db, plan["id"])
        print(dev.status_report(db, plan["id"]))
        if r["status"] == "awaiting-judgment":
            print("AWAITING_JUDGMENT: no verdict yet; The Nickquisition is blocked by the billing gate")
            return EXIT_AWAITING
        print(f"VERDICT: {r['verdict']} ({r['tally']})  PLAN: {r['status']}  REPORT: {r['report']}")
        return EXIT_OK if r["status"] == "complete" else EXIT_INSUFFICIENT
    if args.action == "status":
        print(dev.status_report(db, plan["id"]))
        return EXIT_OK
    if args.action == "testdesign":
        rec = dev.design_tests(db, settings, plan["id"])
        for g in rec["generations"]:
            print(f"[nicks dev] Test Designer pass {g['pass']}: {'ok' if g['ok'] else 'failed: ' + str(g['error_class'])} "
                  f"(model {g['model']}, {g['seconds']}s, {len(g['files'])} file(s))", file=sys.stderr)
        if rec["status"] == "unavailable":
            print(f"TESTDESIGN: UNAVAILABLE ({rec.get('reason')}); recorded, no adversarial coverage is claimed")
            return EXIT_OK
        v = rec["validation"]
        print(f"TESTDESIGN: {v['counts']}  record {Path(plan['dir']) / 'adversarial' / 'record.json'}")
        for tid, t in sorted(v["tests"].items()):
            print(f"  {t['status']:<9} {t['category']:<8} {t['basis']:<17} {tid}  ({t['why'][:110]})")
        print(f"Next: resolve uncertain tests against the request, then `nicks dev testfreeze {plan['id']} "
              "[--resolve ID=accepted|uncertain|rejected:reason ...] --response ...`")
        return EXIT_OK
    if args.action == "testfreeze":
        res = {}
        for item in args.resolve or []:
            tid, _, rest = item.partition("=")
            status, _, reason = rest.partition(":")
            res[tid] = (status, reason)
        response = Path(args.response_file).read_text(encoding="utf-8") if args.response_file else (args.response or "")
        fz = dev.freeze_tests(db, settings, plan["id"], res, response)
        print(f"FROZEN: {fz['counts']}  contract files {len(fz['contract_files'])}, holdout files "
              f"{len(fz['holdout_files'])}  manifest sha256 {fz['manifest_sha256'][:16]}")
        return EXIT_OK
    if args.action == "critique":
        rec = dev.critique_plan(db, settings, plan["id"])
        for a in rec["attempts"]:
            print(f"[nicks dev] critic {a['role']} ({a['model'] or a['requested_model']}): "
                  f"{'ok' if a['ok'] else 'failed: ' + str(a['error_class'])} ({a['seconds']}s)", file=sys.stderr)
        if rec["status"] != "done":
            print("CRITIQUE: UNAVAILABLE: no eligible Plan Critic model could run (see above)")
            return EXIT_NONE
        print(f"CRITIQUE: {rec['path']}  (by {rec['critic']['model'] or rec['critic']['requested_model']})")
        print(f"Next: read it, then `nicks dev revise {plan['id']} --file <revised.json> --response ...` "
              "(or --keep --response ...)")
        return EXIT_OK
    if args.action == "revise":
        response = Path(args.response_file).read_text(encoding="utf-8") if args.response_file else (args.response or "")
        manifest = None if args.keep else json.loads(Path(args.file).read_text(encoding="utf-8")) if args.file else None
        if manifest is None and not args.keep:
            raise ValueError("revise needs --file <revised manifest> or --keep")
        rev = dev.revise_plan(db, settings, plan["id"], manifest, response)
        print(f"REVISION: {'kept unchanged' if rev['kept'] else 'revised manifest registered'}; "
              f"response {rev['response']}")
        print(f"PLAN_MD: {Path(plan['dir']) / 'plan.md'}")
        return EXIT_OK
    if args.action == "preflight":
        from .preflight import summary
        # Preflight creates/repairs the cumulative clone too. Use the same
        # cross-process lock as execution, not only ensure_state's thread lock.
        with dev.plan_lock(plan):
            plan = db.dev_plan(plan['id'])
            _, _, head = dev.ensure_state(db, plan)
            bad = 0
            for t in db.dev_tasks(plan["id"]):
                if t["status"] not in ("pending", "needs-decomposition") or (args.task and t["task_id"] != args.task):
                    continue
                pf = dev.run_preflight(db, settings, plan, t, head)
                bad += pf["status"] != "ok"
                deps = [d for d in t["spec"]["depends_on"] if d]
                print(f"{t['task_id']}: {summary(pf)}" + (f"  [checked on {head[:10]}; prerequisites {', '.join(deps)} not yet "
                                                           "implemented, so 'fails on baseline' is expected]" if deps else ""))
                for r in pf["commands"]:
                    print(f"    {r['verdict']:<19} {r['command'][:110]}")
                    for w in r["broken"] + r["warnings"]:
                        print(f"        - {w}")
        return EXIT_INSUFFICIENT if bad else EXIT_OK
    if args.action == "show":
        if args.task:
            t = next((t for t in db.dev_tasks(plan["id"]) if t["task_id"] == args.task), None)
            if not t:
                raise LookupError(f"no task {args.task!r} in plan {plan['id']}")
            path = dev.write_result(db, plan, t)
        else:
            path = dev.write_plan_md(db, plan["id"])
        print(path.read_text(encoding="utf-8"))
        return EXIT_OK
    if args.action == "review":
        return _dev_review(db, dev, plan, args.task, args.json, diagnostic=args.diagnostic)
    if args.action == "debate":
        if (args.task or 'status') == 'status':
            return _dev_debate(db, settings, plan, args)
        with dev.plan_lock(plan):
            return _dev_debate(db, settings, db.dev_plan(plan['id']), args)
    raise ValueError(f"unknown dev action {args.action}")


def _dev_review(db, dev, plan: dict, task: str | None, as_json: bool, diagnostic=False) -> int:
    if diagnostic:
        from . import diagnostic as diag
        from .policy import restore
        settings = restore(load_settings(), (plan.get('detail') or {}).get('execution_policy'))
        with dev.plan_lock(plan):
            record = diag.prepare(db, settings, plan, task)
            if record.get('job'):
                diag.bind_job(db, record, record['job'])
                return cmd_resume(build_parser().parse_args(['resume', record['job']]))
            spec = next(t for t in db.dev_tasks(plan['id']) if t['task_id'] == task)
            argv = ['review', '--repo', record['worktree'], '--base', record['base'], '--head', record['head'],
                    '--no-merge-base', '--brief', diag.brief(plan, spec, record)] + (['--json'] if as_json else [])
            return cmd_review(build_parser().parse_args(argv), settings=settings, development=True,
                              metadata={'diagnostic': {k: record[k] for k in ('plan', 'task', 'record', 'status')}})
    # Always the accumulated change against the plan's ORIGINAL base: a task with everything verified before it,
    # or (no task) the plan's cumulative verified state.
    integ = dev.integrate(db, plan, task)
    brief = dev.review_brief(db, plan, integ["tasks"])
    argv = ["review", "--repo", integ["worktree"], "--base", integ["base"], "--head", integ["head"],
            "--no-merge-base", "--brief", brief] + (["--json"] if as_json else [])
    print(f"[nicks dev] Council review of the accumulated change {integ['base'][:12]}..{integ['head'][:12]} "
          f"(tasks {', '.join(integ['tasks'])}); then run The Nickquisition on the printed job with --test-cmd",
          file=sys.stderr)
    from .policy import restore
    settings = restore(load_settings(), (plan.get("detail") or {}).get("execution_policy"))
    return cmd_review(build_parser().parse_args(argv), settings=settings, development=True,
                      metadata={'development_verification': dev.review_verification(db, plan, integ['head']),
                                **({'rework': plan['detail']['rework']} if plan['detail'].get('rework') else {})})


def _dev_debate(db, settings, plan: dict, args) -> int:
    """`nicks dev debate <plan> start|turn|status|resume|hash|escalate` (Architect Nick drives its own turns)."""
    from . import debate as dbt
    step = args.task or "status"
    d = dbt.Debate(db, settings, plan["id"], log=lambda m: print(f"[nicks dev] {m}", file=sys.stderr, flush=True))
    try:
        if step == "start":
            d.start()
        elif step == "turn":
            if not args.file:
                raise ValueError("`nicks dev debate <plan> turn --file <message.md>`")
            d.architect_turn(Path(args.file).read_text(encoding="utf-8"))
        elif step == "resume":
            d.resume()
        elif step == "hash":
            if not args.file:
                raise ValueError("`nicks dev debate <plan> hash --file <manifest.json>`")
            chk = d.check_manifest(json.loads(Path(args.file).read_text(encoding="utf-8")))
            print(f"HASH: {chk['sha256']}  (valid manifest; preflight: "
                  + ", ".join(f"{k} {v['summary']}" for k, v in chk["preflight"].items()) + ")")
            return EXIT_OK
        elif step == "probe":
            if not args.file:
                raise ValueError("`nicks dev debate <plan> probe --file <command.sh>`")
            r = d.probe(Path(args.file).read_text(encoding="utf-8"))
            print(f"PROBE exit {r['exit_code']} ({r['seconds']}s, base {r['base']}):\n{r['output']}")
            return EXIT_OK
        elif step == "escalate":
            if not (args.decision and (args.reason or "").strip()):
                raise ValueError("`nicks dev debate <plan> escalate --decision proceed|stop --reason \"<the user's words>\"`")
            d.escalate(args.decision, args.reason)
        elif step != "status":
            raise ValueError(f"unknown debate step {step!r}: start|turn|status|resume|hash|probe|escalate")
    except dbt.DebateError as exc:
        print(f"DEBATE REJECTED (no state change): {exc}")
        return EXIT_INSUFFICIENT
    state, evs = d.state()
    if state is None:
        print(f"DEBATE: not started (`nicks dev debate {plan['id']} start`)")
        return EXIT_OK
    # Everything since Architect Nick's last accepted turn, verbatim.
    last_arch = max((i for i, e in enumerate(evs) if e["kind"] == "turn" and e["actor"] == "architect"), default=-1)
    turn_no = 0
    for i, e in enumerate(evs):
        turn_no += e["kind"] == "turn"
        if i <= last_arch:
            continue
        if e["kind"] == "turn" and e["actor"] == "evil":
            msg = (d.root / e["data"]["files"][0]["path"]).read_text(encoding="utf-8")
            print(f"===== EVIL NICK (turn {turn_no}) =====")
            print(msg.strip())
            for r in (e["data"]["results"].get("evidence_all") or {}).values():
                print(f"----- orchestrator evidence run on base {r['base']}: `{r['command']}` -> exit {r['exit_code']}")
                print(r["output"][-1500:])
        elif e["kind"] in ("interrupted", "end", "escalation"):
            print(f"===== {e['kind'].upper()}: {json.dumps(e['data'], ensure_ascii=False)[:800]}")
    print("=====")
    print(dbt.state_text(state, "architect"))
    print(f"TRANSCRIPT: {d.root / 'transcript.md'}")
    if state["ended"]:
        print(f"DEBATE OUTCOME: {state['ended']['outcome']}")
        if state["ended"]["outcome"] == "CONSENSUS":
            return EXIT_OK
        print("No consensus: this needs the USER's decision. Report both positions; never proceed on your own.")
        return EXIT_BLOCKED
    if evs[-1]["kind"] == "interrupted":
        print(f"DEBATE INTERRUPTED: `nicks dev debate {plan['id']} resume` once the cause is fixed")
        return EXIT_NONE
    print(dbt.ARCHITECT_GUIDE.format(plan=plan["id"]))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nicks", description="Council of Nicks: blind multi-model code review on free models.")
    p.add_argument("--version", action="version", version=f"nicks {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="automatically discover free/local capacity and connected subscriptions")
    init.add_argument("--json", action="store_true", help="machine-readable startup summary")
    init.add_argument("--subscriptions", choices=["auto", "claude-only", "codex-only", "both"])
    init.add_argument("--mode", choices=["hybrid", "restricted"])
    init.set_defaults(func=cmd_init)

    cf = sub.add_parser("config", help="show or set persistent policy defaults")
    cfs = cf.add_subparsers(dest="action", required=True)
    cshow = cfs.add_parser("show"); cshow.set_defaults(func=cmd_config)
    cset = cfs.add_parser("set"); cset.add_argument("key", choices=["subscriptions", "mode"]); cset.add_argument("value"); cset.set_defaults(func=cmd_config)

    r = sub.add_parser("review", help="run independent blind reviews of a diff and write an arbiter packet")
    r.add_argument("base_pos", nargs="?", metavar="BASE", help="base ref (same as --base)")
    r.add_argument("--base", help="base ref; default: origin/HEAD, origin/main, main, ... (merge-base with head)")
    r.add_argument("--head", help="head ref (default HEAD)")
    r.add_argument("--uncommitted", action="store_true", help="review the working tree (incl. untracked files) against base (default HEAD)")
    r.add_argument("--diff-file", help="review a supplied patch file ('-' for stdin)")
    r.add_argument("--no-merge-base", action="store_true", help="diff directly against base instead of the merge-base")
    r.add_argument("--repo", default=".", help="repository path (default: current directory)")
    r.add_argument("-n", "--nicks", type=int, help="number of Council workers (default: the composition, 1 Luna + "
                                                   "1 Haiku + 3 free; free-only default 3)")
    r.add_argument("--min-nicks", type=int, help="minimum usable reviews for a non-degraded result (default 2)")
    r.add_argument("--brief", help="architect's scope/acceptance criteria passed identically to every Nick")
    r.add_argument("--route", action="append", help="restrict the pool to these eligible routes (repeatable)")
    r.add_argument("--no-local", action="store_true", help="do not use local (loopback) inference routes")
    r.add_argument("--no-subscription", action="store_true",
                   help="free-only Council (no Luna/Haiku subscription workers or overflow)")
    r.add_argument("--subscriptions", choices=["auto", "claude-only", "codex-only", "both"])
    r.add_argument("--mode", choices=["hybrid", "restricted"])
    r.add_argument("--timeout", type=float, help="job wall-clock limit in seconds (default 540)")
    r.add_argument("--attempt-timeout", type=float, help="per-attempt limit in seconds (default 420)")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_review)

    s = sub.add_parser("resume", help="continue an interrupted/degraded job (fills unfilled slots blind)")
    s.add_argument("job", nargs="?", help="job id or prefix (default: latest)")
    s.add_argument("--no-local", action="store_true")
    s.add_argument("--timeout", type=float)
    s.add_argument("--attempt-timeout", type=float)
    s.add_argument("--subscriptions", choices=["auto", "claude-only", "codex-only", "both"])
    s.add_argument("--mode", choices=["hybrid", "restricted"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_resume)

    st = sub.add_parser("status", help="list jobs, or show one job's slots and attempts")
    st.add_argument("job", nargs="?")
    st.add_argument("--limit", type=int, default=20)
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)

    rp = sub.add_parser("report", help="write a Markdown report of a development plan or review job (models, "
                                       "attempts, token usage, Council reviews, The Nickquisition); no model is called")
    rp.add_argument("job", nargs="?", help="plan id (p...) or review job id (r...), or a unique prefix; default: latest")
    rp.add_argument("--print", action="store_true", help="also print the report")
    rp.set_defaults(func=cmd_report)

    sx = sub.add_parser("stats", help="statistics across development runs (tokens, time, retries, tests, verdicts, "
                                      "model comparisons); read-only, no model is called")
    sx.add_argument("--home", action="append", metavar="DIR",
                    help="state directory or journal to include (repeatable; nested `dev deliver` engines are found "
                         "automatically); default: $NICKS_HOME")
    sx.add_argument("--all", action="store_true", help="also include runs that have not concluded")
    sx.add_argument("--json", action="store_true", help="machine-readable output")
    sx.set_defaults(func=cmd_stats)

    pk = sub.add_parser("packet", help="print the path (or content) of a job's arbiter packet")
    pk.add_argument("job", nargs="?")
    pk.add_argument("--print", action="store_true", help="print the packet content")
    pk.add_argument("--rebuild", action="store_true")
    pk.set_defaults(func=cmd_packet)

    m = sub.add_parser("models", help="discover routes and show free eligibility and health")
    m.add_argument("--all", action="store_true", help="also list ineligible routes with reasons")
    m.add_argument("--cached", action="store_true", help="use the last discovery instead of querying OpenCode")
    m.add_argument("--no-local", action="store_true")
    m.add_argument("--subscriptions", choices=["auto", "claude-only", "codex-only", "both"]); m.add_argument("--mode", choices=["hybrid", "restricted"])
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=cmd_models)

    ev = sub.add_parser("evil", help="Evil Nick: independent blind 'attack' of a job, or 'challenge' of Good Nick's "
                                     "provisional decision")
    ev.add_argument("stage", choices=["attack", "challenge"])
    ev.add_argument("job", nargs="?", help="job id or prefix (default: latest)")
    ev.add_argument("--timeout", type=float, help="seconds (default 480)")
    ev.add_argument("--subscriptions", choices=["auto", "claude-only", "codex-only", "both"]); ev.add_argument("--mode", choices=["hybrid", "restricted"])
    ev.add_argument("--json", action="store_true")
    ev.set_defaults(func=cmd_evil)

    nn = sub.add_parser("neutral", help="Neutral Nick: independent blind review by a randomly drawn subscription model")
    nn.add_argument("action", choices=["review", "status", "models"])
    nn.add_argument("job", nargs="?", help="job id or prefix (default: latest)")
    nn.add_argument("--good-model", help="Good Nick's own model id, so the draw can prefer a different model")
    nn.add_argument("--provider", choices=["anthropic", "openai"],
                    help="operator override of the provider draw (recorded as operator-forced)")
    nn.add_argument("--probe", action="store_true", help="models: send a one-line prompt to each allowlisted model")
    nn.add_argument("--timeout", type=float, help="seconds (default 480)")
    nn.add_argument("--subscriptions", choices=["auto", "claude-only", "codex-only", "both"]); nn.add_argument("--mode", choices=["hybrid", "restricted"])
    nn.add_argument("--json", action="store_true")
    nn.set_defaults(func=cmd_neutral)

    pv = sub.add_parser("providers", help="register external free providers (OpenRouter, Groq, Gemini, ...) for the Council")
    pv.add_argument("action", choices=["list", "add", "remove", "discover", "probe"])
    pv.add_argument("provider", nargs="?", help="OpenCode/models.dev provider id, e.g. openrouter, groq, google")
    pv.add_argument("--policy", choices=["zero-priced", "free-tier-account", "free-quota-only"], default="zero-priced",
                    help="zero-priced: only explicitly zero-priced models; free-tier-account: you declare the account "
                         "uses a documented free account with no billing; free-quota-only: dated quota evidence "
                         "and a provider-side hard billing stop")
    pv.add_argument("--key-file", help="file holding the API key (must be chmod 600)")
    pv.add_argument("--key-env", help="or: name of an environment variable holding the key")
    pv.add_argument("--env", action="append", help="provider key variable name(s); default: looked up on models.dev")
    pv.add_argument("--models", help="comma-separated allowlist of model ids")
    pv.add_argument("--anonymous", action="store_true", help="Kilo free gateway without an account/key")
    pv.add_argument("--endpoint", help="OpenAI-compatible https base URL; known providers have presets")
    pv.add_argument("--quota-pool", help="non-secret shared account/gateway quota identity (same allowance = same ID)")
    pv.add_argument("--quota-scope", choices=["provider", "account", "model", "gateway", "trial"],
                    help="model appends each model ID to the pool; other scopes share it")
    pv.add_argument("--entitlements-file", help="short-lived JSON pricing/quota evidence with server-side billing protection")
    pv.add_argument("--route", action="append", help="probe: explicit route, repeatable (max 8)")
    pv.add_argument("--probe-timeout", type=float, default=60, help="probe: per-call limit, 1–90 seconds")
    pv.set_defaults(func=cmd_providers)

    nq = sub.add_parser("nickquisition", help="The Nickquisition: open a round, Good Nick's ballot, convene Evil and "
                                              "Neutral Nick, inspect, and the authoritative verdict")
    nq.add_argument("action", choices=["run", "open", "scale", "submit", "convene", "show", "verdict"],
                    help="open runs any missing Phase A work of Evil and Neutral Nick itself")
    nq.add_argument("job", nargs="?", help="job id or prefix (default: latest)")
    nq.add_argument("--test-cmd", help="open: objective test command, run on base and reviewed revision in a jail")
    nq.add_argument("--new-round", action="store_true", help="open: start an explicit new round")
    nq.add_argument("--vote", choices=["APPROVE", "REJECT"], help="submit: Good Nick's own vote")
    nq.add_argument("--artifact", help="submit: the ARTIFACT id printed by `open` (binds the vote to the revision)")
    nq.add_argument("--justification", help="submit: written justification of the vote")
    nq.add_argument("--model", help="open/submit: Good Nick's own model id (Neutral prefers a different one; "
                                     "recorded as self-declared on the ballot)")
    nq.add_argument("--file", help="submit: deliberation text file ('-' or omitted: stdin)")
    nq.add_argument("--retry-failed", action="store_true", help="convene: re-run member executions that failed")
    nq.add_argument('--allow-incomplete-council', metavar='REASON',
                    help='explicit, recorded exception to required review/reconciliation ordering (never fabricates coverage)')
    nq.add_argument("--round", type=int, help="show/verdict: round number (default: latest)")
    nq.add_argument("--json", action="store_true")
    nq.add_argument("--subscriptions", choices=["auto", "claude-only", "codex-only", "both"]); nq.add_argument("--mode", choices=["hybrid", "restricted"])
    nq.set_defaults(func=cmd_nickquisition)

    vd = sub.add_parser("verdict", help="record Good Nick's provisional or final decision (text on stdin or --file)")
    vd.add_argument("job", nargs="?")
    vd.add_argument("--stage", required=True, choices=["provisional", "final"])
    vd.add_argument("--decision", choices=["approve", "request-changes", "needs-investigation"],
                    help="explicit decision; if omitted, decode the assessment's explicit JSON/Markdown/plain-text decision")
    vd.add_argument("--file", help="arbitration text file ('-' or omitted: stdin)")
    vd.add_argument("--unresolved", type=int, help="final: number of Evil Nick objections still unresolved")
    vd.add_argument("--skip-challenge", metavar="REASON", help="final without an Evil Nick challenge: why")
    vd.add_argument("--print", action="store_true", help="final: print the decision record")
    vd.set_defaults(func=cmd_verdict)

    bl = sub.add_parser("billing", help="pre-inference billing gate: can subscription-backed Nicks run without any "
                                        "possible charge beyond the plan? (read live from the accounts)")
    bl.add_argument("--json", action="store_true")
    bl.set_defaults(func=cmd_billing)

    dv = sub.add_parser("dev", help="Development Engine: Architect Nick's plan -> one Council worker implements a task in a "
                                    "jailed worktree -> engine verification -> Nickquisition review")
    dv.add_argument("action", choices=["plan", "execute", "status", "amend", "integrate", "conclude", "run", "show",
                                       "review", "preflight", "critique", "revise", "testdesign", "testfreeze",
                                       "debate", "compete", "deliver", "rework"],
                    help="plan: register Architect Nick's manifest; execute: run ready tasks in dependency order (resumable); "
                         "status: plan/task table; amend: Architect Nick's decomposition; integrate: integration verification "
                         "+ final Council review; conclude: record the Nickquisition verdict; run/show/review: single task; "
                         "preflight: validate tasks' verification commands; critique: independent Plan Critic; revise: "
                         "Architect Nick's one revision after the critique; deliver: unattended full Council workflow; "
                         "rework: authorize one focused child of a concluded rejection (PLAN --from JOB), "
                         "then inspect nickquisition show/verdict in the returned engine_state")
    dv.add_argument("plan", nargs="?", help="plan id or prefix (default: latest)")
    dv.add_argument("task", nargs="?", help="task id (run: default = first ready task)")
    dv.add_argument("--repo", default=".", help="plan: repository path")
    dv.add_argument("--base", help="plan: base ref (default HEAD)")
    dv.add_argument("--architect", action="store_true", help="generate a manifest with an eligible Architect Nick")
    dv.add_argument("--request", help="plan: the user's feature request, verbatim")
    dv.add_argument("--request-file", help="plan: file holding the request")
    dv.add_argument("--file", help="plan: manifest JSON; amend: replacement tasks JSON ({\"tasks\": [...]})")
    dv.add_argument("--replace", help="amend: the task to decompose")
    dv.add_argument("--keep", action="store_true", help="revise: keep the plan unchanged after the critique")
    dv.add_argument("--debate", action="store_true", help="plan: experimental Stake-Based Design Debate with Evil Nick "
                    "instead of the Plan Critic. Then: nicks dev debate <plan> start|turn --file M|status|resume|"
                    "hash --file MANIFEST|escalate --decision proceed|stop --reason R")
    dv.add_argument("--decision", choices=["proceed", "stop"], help="debate escalate: the USER's decision")
    dv.add_argument("--resolve", action="append", help="testfreeze: ID=accepted|uncertain|rejected:reason (repeatable)")
    dv.add_argument("--response", help="revise: how each critique point was addressed (or why rejected)")
    dv.add_argument("--response-file", help="revise: file holding the response")
    dv.add_argument('--diagnostic', action='store_true', help='review: inspect a failed candidate without integrating it')
    dv.add_argument('--diagnostic-job', help='amend: use this diagnostic Council job and --response-file assessment')
    dv.add_argument("--add", action="store_true", help="amend: add new tasks (e.g. an integration fix)")
    dv.add_argument("--reason", help="amend: why the task is split / added (required, recorded)")
    dv.add_argument("--from", dest="from_job", help="rework: exact concluded rejected review job")
    dv.add_argument("--max-seconds", type=float, help="execute: time budget for this call (default dev_execute_seconds)")
    dv.add_argument("--subscriptions", choices=["auto", "claude-only", "codex-only", "both"]); dv.add_argument("--mode", choices=["hybrid", "restricted"])
    dv.add_argument("--json", action="store_true")
    dv.set_defaults(func=cmd_dev)

    d = sub.add_parser("doctor", help="check the environment; --probe sends a tiny prompt to each eligible route")
    d.add_argument("--probe", action="store_true")
    d.add_argument("--route", action="append", help="limit --probe to these routes")
    d.add_argument("--include-blocked", action="store_true", help="also probe routes that are cooling down (quarantined providers are never probed)")
    d.add_argument("--no-local", action="store_true")
    d.add_argument("--subscriptions", choices=["auto", "claude-only", "codex-only", "both"]); d.add_argument("--mode", choices=["hybrid", "restricted"])
    d.add_argument("--clear", metavar="SCOPE", help="clear health/cooldown/quarantine for SCOPE (e.g. provider:opencode) or 'all'")
    d.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args((sys.argv[1:] if argv is None else argv) or ["init"])
    if getattr(args, "base_pos", None):
        if args.base and args.base != args.base_pos:
            print("nicks: give the base either positionally or with --base, not both", file=sys.stderr)
            return EXIT_ERR
        args.base = args.base_pos
    signal.signal(signal.SIGTERM, _sigterm)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("[nicks] interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except NothingToReview as exc:
        print(f"nicks: {exc}", file=sys.stderr)
        return EXIT_ERR
    except (SnapshotError, DiscoveryError, JobLocked, LookupError, ValueError, SandboxUnavailable) as exc:
        print(f"nicks: error: {exc}", file=sys.stderr)
        return EXIT_ERR
    except (VerdictError, EvilUnavailable, NeutralUnavailable, NickquisitionError, ProviderConfigError, DevError) as exc:
        print(f"nicks: error: {exc}", file=sys.stderr)
        return EXIT_ERR
