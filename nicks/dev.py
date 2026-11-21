"""Development Engine V1A: Claude plans, the Council implements, the Nickquisition judges.

* **Plan** (Story Architect): Claude inspects the repository and writes a manifest of small user stories and
  worker-sized tasks (goal, context, permitted scope, dependencies, acceptance criteria, verification commands,
  out-of-scope work). `create_plan` validates and persists it; the manifest is for orchestration only, worker
  output is never required to follow a format.
* **Implement**: one ready task at a time. Each attempt gets a disposable private clone of the repository (its
  own git directory) checked out at the plan's base plus the patches of verified dependencies. The worker runs
  in the bubblewrap jail with ONLY that worktree writable; the worktree's `.git` pointer and the private git
  directory are re-mounted read-only, and the user's repository is never mounted at all.
* **Verify**: the engine (never the model) exports exactly the staged change into a fresh tree and runs the
  task's verification commands in a network-less jail. Scope is checked by the engine from git, not trusted
  from the worker.
* **Judge**: a verified task becomes a commit (attributed to the actual model) on `nicks/<plan>/<task>` in the
  private clone, plus a patch file; `nicks dev review` hands it to the existing Council review, after which
  The Nickquisition runs unchanged.

Failure handling: infrastructure failures (rate limits, quota, outages, timeouts, crashes) switch worker and
start from a fresh worktree; failed verification gets a bounded repair round; scope violations are rejected;
repeated implementation failures return the task to Claude for decomposition. Budgets persist across runs, so
an oversized task is never retried indefinitely. Nothing is pushed, nothing touches the user's branches.
"""
from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import state_dir
from .sandbox import SandboxUnavailable, bwrap_path, wrap
from .snapshot import make_writable

INFRA_CLASSES = {"rate_limit", "quota", "auth", "transient", "timeout", "unavailable", "ineligible", "runtime", "proposal_apply",
                 "billing", "billing_gate", "interrupted", "deadline"}
TASK_FIELDS = ("id", "story", "goal", "context_files", "scope", "depends_on", "acceptance", "verify", "out_of_scope")
JUNK = ["__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", "*.egg-info/", ".coverage",
        "node_modules/", ".DS_Store"]
WORKER_IDENTITY = ("Council of Nicks worker", "nicks-worker@localhost.invalid")


STATE_LOCK = threading.RLock()     # the plan's canonical state clone: integration writes, dispatch reads
FILE_LOCK = threading.Lock()       # plan.md / result.md / events.jsonl written from worker threads


class PlanError(ValueError):
    pass


class DevError(RuntimeError):
    pass


def _log(msg: str) -> None:
    import sys
    print(f"[nicks dev] {msg}", file=sys.stderr, flush=True)


def _sha(text: str | bytes) -> str:
    return hashlib.sha256(text.encode() if isinstance(text, str) else text).hexdigest()


def dev_dir() -> Path:
    return state_dir() / "dev"


# --------------------------------------------------------------------------- git (orchestrator side only)

def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TZ")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0",
               GIT_AUTHOR_DATE=time.strftime("%Y-%m-%dT%H:%M:%S"), LC_ALL="C")
    return env


def g(gitdir: Path, worktree: Path | None, *args: str, check: bool = True, input_: bytes | None = None) -> str:
    """git with an EXPLICIT git dir: never discovered from (worker-controlled) worktree contents, no hooks,
    no fsmonitor, no user/system config."""
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
           "-c", "protocol.file.allow=always", f"--git-dir={gitdir}"]
    if worktree is not None:
        cmd.append(f"--work-tree={worktree}")
    p = subprocess.run(cmd + list(args), capture_output=True, env=_git_env(), input=input_,
                       cwd=str(worktree or gitdir))
    if check and p.returncode != 0:
        raise DevError(f"git {' '.join(args[:3])} failed: {p.stderr.decode(errors='replace').strip()[:500]}")
    return p.stdout.decode("utf-8", errors="replace")


def _repo_root(repo: str) -> Path:
    p = subprocess.run(["git", "-C", repo, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if p.returncode != 0:
        raise PlanError(f"{repo} is not a git repository")
    return Path(p.stdout.strip())


def _resolve(root: Path, ref: str) -> str:
    p = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise PlanError(f"cannot resolve {ref!r} in {root}")
    return p.stdout.strip()


# --------------------------------------------------------------------------- plan (Story Architect)

def _strs(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)


def _bad_glob(pat: str) -> str | None:
    if pat.startswith(("/", "~")) or "\\" in pat:
        return "must be a repository-relative path or glob"
    parts = pat.split("/")
    if ".." in parts:
        return "must not contain '..'"
    if parts[0] == ".git" or ".git" in parts:
        return "must not touch .git"
    if pat.strip() in ("*", "**", "**/*", "./", "."):
        return "is the whole repository: not a bounded worker scope"
    return None


def validate_manifest(m: dict, root: Path, base_sha: str, settings, existing_ids: set[str] = frozenset(),
                      partial: bool = False) -> list[str]:
    """Return a list of problems (empty = valid). `partial` validates an amendment's task list only."""
    errs: list[str] = []
    if not isinstance(m, dict):
        return ["manifest must be a JSON object"]
    if not partial:
        if not isinstance(m.get("feature"), str) or not m["feature"].strip():
            errs.append("`feature` (the user's request, restated) is required")
        stories = m.get("stories")
        if not isinstance(stories, list) or not stories:
            errs.append("`stories` must be a non-empty list")
            stories = []
        sids = []
        for i, s in enumerate(stories):
            if not isinstance(s, dict) or not isinstance(s.get("id"), str) or not s.get("title"):
                errs.append(f"stories[{i}] needs `id` and `title`")
                continue
            if not _strs(s.get("acceptance")) or not s["acceptance"]:
                errs.append(f"story {s['id']}: `acceptance` must be a non-empty list of criteria")
            sids.append(s["id"])
        if len(sids) != len(set(sids)):
            errs.append("story ids must be unique")
        story_ids = set(sids)
    else:
        story_ids = set(m.get("_story_ids") or [])
    if "integration_verify" in m and (not _strs(m["integration_verify"])):
        errs.append("`integration_verify` must be a list of commands")
    tasks = m.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return errs + ["`tasks` must be a non-empty list"]
    tids = [t.get("id") for t in tasks if isinstance(t, dict)]
    if len(tids) != len(set(tids)) or set(tids) & set(existing_ids):
        errs.append("task ids must be unique (also across amendments)")
    known = set(tids) | set(existing_ids)
    for i, t in enumerate(tasks):
        if not isinstance(t, dict) or not isinstance(t.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9][\w.-]{0,40}", t["id"]):
            errs.append(f"tasks[{i}]: `id` must be a short identifier ([A-Za-z0-9_.-])")
            continue
        tid = t["id"]
        missing = [f for f in TASK_FIELDS if f not in t]
        if missing:
            errs.append(f"task {tid}: missing {', '.join(missing)}")
            continue
        if t["story"] not in story_ids:
            errs.append(f"task {tid}: unknown story {t['story']!r}")
        goal = t["goal"]
        if not isinstance(goal, str) or not goal.strip() or "\n" in goal.strip() or len(goal) > 300:
            errs.append(f"task {tid}: `goal` must be ONE clear sentence (one line, <= 300 chars)")
        for f in ("acceptance", "verify", "scope"):
            if not _strs(t[f]) or not t[f]:
                errs.append(f"task {tid}: `{f}` must be a non-empty list of strings")
        for f in ("context_files", "out_of_scope", "depends_on"):
            if not isinstance(t[f], list) or not all(isinstance(x, str) for x in t[f]):
                errs.append(f"task {tid}: `{f}` must be a list of strings")
        if not t["out_of_scope"]:
            errs.append(f"task {tid}: `out_of_scope` must name at least one thing the worker must not do")
        if _strs(t["scope"]):
            if len(t["scope"]) > settings.dev_max_scope_entries and not t.get('microtask'):
                errs.append(f"task {tid}: scope has {len(t['scope'])} entries (> {settings.dev_max_scope_entries}); "
                            "decompose it into smaller tasks")
            for pat in t["scope"]:
                why = _bad_glob(pat)
                if why:
                    errs.append(f"task {tid}: scope entry {pat!r} {why}")
        for cf in t.get("context_files") or []:
            if not isinstance(cf, str) or _bad_glob(cf):
                errs.append(f"task {tid}: context file {cf!r} is not a repository-relative path")
            elif subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{base_sha}:{cf}"],
                                capture_output=True).returncode != 0 and not any(fnmatch.fnmatch(cf, s) for s in t["scope"]):
                errs.append(f"task {tid}: context file {cf!r} does not exist at the base commit (and is not in scope)")
        for d in t.get("depends_on") or []:
            if d not in known:
                errs.append(f"task {tid}: depends on unknown task {d!r}")
            if d == tid:
                errs.append(f"task {tid}: depends on itself")
        if "context" in t and not isinstance(t["context"], str):
            errs.append(f"task {tid}: `context` must be text")
    # cycles (within this manifest; earlier tasks cannot depend on later amendments)
    graph = {t["id"]: [d for d in t.get("depends_on", []) if d in set(tids)] for t in tasks
             if isinstance(t, dict) and isinstance(t.get("id"), str)}
    state: dict[str, int] = {}

    def visit(n) -> bool:
        if state.get(n) == 1:
            return True
        if state.get(n) == 2:
            return False
        state[n] = 1
        cyc = any(visit(d) for d in graph.get(n, []))
        state[n] = 2
        return cyc
    if any(visit(n) for n in graph):
        errs.append("task dependencies contain a cycle")
    return errs


def render_plan(plan: dict, tasks: list[dict]) -> str:
    m = plan["manifest"]
    out = [f"# Development plan `{plan['id']}`", "",
           f"- Repository: `{plan['repo']}` at base `{plan['base_sha'][:12]}`",
           f"- Request (verbatim): {plan['request']}", "", "## Feature", "", m.get("feature", "")]
    if m.get("architecture"):
        out += ["", "## Architecture notes (Architect Nick)", "", m["architecture"]]
    if m.get("constraints"):
        out += ["", "## Constraints", ""] + [f"- {c}" for c in m["constraints"]]
    out += ["", "## Stories", ""]
    for s in m.get("stories", []):
        out += [f"### {s['id']}: {s['title']}", ""] + ([s["narrative"], ""] if s.get("narrative") else [])
        out += [f"- [ ] {a}" for a in s.get("acceptance", [])] + [""]
    d = plan.get("detail") or {}
    if d.get("critic_required") or d.get("critique"):
        c, r = d.get("critique") or {}, d.get("revision") or {}
        out += ["## Plan Critic (V1D.1)", "",
                f"- Critique: {c.get('status', 'not yet run')}"
                + (f" by `{(c.get('critic') or {}).get('model') or (c.get('critic') or {}).get('requested_model')}`, "
                   f"`{c.get('path')}`" if c.get("status") == "done" else ""),
                "- Revision: " + ("not yet" if not r else ("kept unchanged" if r.get("kept") else "revised")
                                  + f"; Architect Nick's response `{r.get('response')}`"), ""]
    if d.get("debate_required"):
        db_ = d.get("debate") or {}
        out += ["## Stake-Based Design Debate (experimental)", "",
                f"- Status: **{db_.get('status', 'not started')}**"
                + (f" ({db_['ended']['reason']})" if db_.get("ended") else ""),
                f"- Chips left: {db_.get('chips')}; claims {db_.get('bets', 0)}, calls {db_.get('calls', 0)}, "
                f"folds {db_.get('folds', 0)}; unresolved {', '.join(db_.get('unresolved') or []) or 'none'}",
                f"- Transcript: `{db_.get('transcript', '-')}`"]
        if db_.get("tradeoffs"):
            out += ["- Open semantic trade-offs recorded by Architect Nick (decide or accept them explicitly):"] + \
                   [f"  - {x}" for x in db_["tradeoffs"]]
        out += [""]
    out += ["## Worker tasks", ""]
    for t in tasks:
        sp = t["spec"]
        out += [f"### {t['task_id']} ({sp['story']}) — status: **{t['status']}**" + (f" ({t['reason']})" if t.get("reason") else ""),
                "", f"**Goal:** {sp['goal']}", ""]
        if sp.get("context"):
            out += [sp["context"], ""]
        out += ["- Depends on: " + (", ".join(sp["depends_on"]) or "nothing"),
                "- Context files: " + (", ".join(f"`{c}`" for c in sp["context_files"]) or "none"),
                "- May modify only: " + ", ".join(f"`{s}`" for s in sp["scope"]),
                "- Acceptance:"] + [f"  - {a}" for a in sp["acceptance"]]
        out += ["- Verification (run by the engine, no network):"] + [f"  - `{v}`" for v in sp["verify"]]
        out += ["- Out of scope:"] + [f"  - {o}" for o in sp["out_of_scope"]] + [""]
    return "\n".join(out) + "\n"


def write_plan_md(db, plan_id: str) -> Path:
    plan = db.dev_plan(plan_id)
    p = Path(plan["dir"]) / "plan.md"
    text = render_plan(plan, db.dev_tasks(plan_id))
    with FILE_LOCK:
        p.write_text(text, encoding="utf-8")
    return p


def create_plan(db, settings, repo: str, request: str, manifest: dict, base: str = "HEAD",
                debate: bool = False, directory: Path | None = None) -> dict:
    from .rework import context
    value = context(settings)
    if not value:
        return _create_plan(db, settings, repo, request, manifest, base, debate, directory)
    from .config import state_dir
    reservation = state_dir() / 'rework-plans' / value['id']
    reservation.mkdir(parents=True, exist_ok=True)
    with plan_lock({'id': value['id'], 'dir': str(reservation)}):
        for row in db.all('SELECT id, detail FROM dev_plans'):
            if (json.loads(row['detail'] or '{}').get('rework') or {}).get('id') == value['id']:
                raise PlanError('this rework already has plan ' + row['id'] + '; resume it, never reset its budget')
        return _create_plan(db, settings, repo, request, manifest, base, debate, directory)


def _create_plan(db, settings, repo, request, manifest, base, debate, directory):
    root = _repo_root(repo)
    base_sha = _resolve(root, base)
    from .rework import validate_plan
    rework = validate_plan(settings, root, base_sha, manifest, debate)
    errs = validate_manifest(manifest, root, base_sha, settings)
    if errs:
        raise PlanError("invalid plan manifest:\n- " + "\n- ".join(errs))
    plan_id = time.strftime("p%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    d = (directory or dev_dir()) / plan_id
    d.mkdir(parents=True)
    text = json.dumps(manifest, indent=2, ensure_ascii=False)
    (d / "manifest.json").write_text(text, encoding="utf-8")
    (d / "request.txt").write_text(request, encoding="utf-8")
    dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True).stdout
    with db.tx():
        db.execute("INSERT INTO dev_plans (id, created_at, repo, base_sha, request, manifest, manifest_sha256, dir) "
                   "VALUES (?,?,?,?,?,?,?,?)", (plan_id, time.time(), str(root), base_sha, request, text, _sha(text), str(d)))
        for t in manifest["tasks"]:
            db.execute("INSERT INTO dev_tasks (plan_id, task_id, status, spec, updated_at) VALUES (?,?,?,?,?)",
                       (plan_id, t["id"], "pending", json.dumps(t), time.time()))
    from .policy import snapshot as policy_snapshot
    req = {"execution_policy": policy_snapshot(settings)}
    if rework:
        req["rework"] = rework
    if debate:                                             # Stake-Based Design Debate V1 replaces the critic
        req["debate_required"] = True
    elif getattr(settings, "dev_plan_critic", False):      # V1D.1: this plan must pass the Plan Critic step
        req["critic_required"] = True
    if getattr(settings, "dev_adversarial_tests", False):  # V1D.2: and the Adversarial Test Designer stage
        req["adversarial_required"] = True
    if req:
        db.set_dev_plan(plan_id, detail=req)
    write_plan_md(db, plan_id)
    plan = db.dev_plan(plan_id)
    plan["warnings"] = (["the repository has uncommitted changes; they are NOT part of the plan's base commit"]
                        if dirty.strip() else [])
    return plan


def critic_pending(plan: dict) -> str | None:
    """Why this plan may not execute yet because of the V1D.1 Plan Critic step (None = it may)."""
    d = plan.get("detail") or {}
    if not d.get("critic_required"):
        return None                                   # plans created before V1D.1 or with dev_plan_critic off
    if (d.get("critique") or {}).get("status") != "done":
        return "the plan has not been reviewed by the Plan Critic yet: `nicks dev critique <plan>`"
    if not d.get("revision"):
        return ("Claude has not answered the Plan Critic yet: read the critique, then `nicks dev revise <plan> "
                "--file <revised manifest> --response ...` (or `--keep --response ...` if no change is warranted)")
    return None


def debate_pending_(plan: dict) -> str | None:
    from .debate import debate_pending
    return debate_pending(plan)


def adversarial_pending(plan: dict) -> str | None:
    """Why this plan may not execute yet because of the V1D.2 Adversarial Test Designer stage (None = it may)."""
    d = plan.get("detail") or {}
    if not d.get("adversarial_required"):
        return None
    st = (d.get("adversarial") or {}).get("status")
    if st in ("frozen", "unavailable"):
        return None                       # unavailable is recorded explicitly; no coverage is claimed
    if st == "proposed":
        return ("the adversarial tests are proposed but not frozen: resolve ambiguous ones and freeze them "
                "(`nicks dev testfreeze <plan> ...`)")
    return "the Adversarial Test Designer has not run yet: `nicks dev testdesign <plan>`"


def design_tests(db, settings, plan_id: str, log=_log) -> dict:
    """Evil Nick's Adversarial Test Designer, after the Plan Critic cycle and before any implementation."""
    from .testdesign import design
    plan = db.dev_plan(plan_id)
    if (why := critic_pending(plan)):
        raise PlanError(f"finish the Plan Critic cycle first: {why}")
    if any(t["status"] != "pending" for t in db.dev_tasks(plan["id"])) or db.dev_attempts(plan["id"]):
        raise PlanError("adversarial tests are designed before implementation; this plan has already started")
    from .debate import debate_pending
    if (why := debate_pending(plan)):
        raise PlanError(f"finish the design debate first: {why}")
    rec = design(db, settings, plan, log)
    rpath = Path(plan["dir"]) / "adversarial" / "record.json"
    summary = {"status": rec["status"], "record": str(rpath), "reason": rec.get("reason"),
               "counts": (rec.get("validation") or {}).get("counts"),
               "generations": [{k: g[k] for k in ("pass", "ok", "model", "seconds", "error_class")} for g in rec["generations"]]}
    db.set_dev_plan(plan["id"], detail=dict(db.dev_plan(plan["id"]).get("detail") or {}, adversarial=summary))
    _event(plan, "testdesign", status=rec["status"], counts=summary["counts"])
    return rec


def freeze_tests(db, settings, plan_id: str, resolutions: dict, rationale: str) -> dict:
    """Claude's resolution of ambiguous tests, then the freeze (before implementation; never repeated)."""
    from .testdesign import freeze
    plan = db.dev_plan(plan_id)
    d = (plan.get("detail") or {}).get("adversarial") or {}
    if d.get("status") != "proposed":
        raise PlanError(f"nothing to freeze (adversarial stage is {d.get('status') or 'not run'})")
    if db.dev_attempts(plan["id"]):
        raise PlanError("implementation has started; adversarial tests can no longer be frozen")
    if not rationale.strip():
        raise PlanError("--response is required: how ambiguous tests were resolved against the request")
    rec = json.loads(Path(d["record"]).read_text(encoding="utf-8"))
    # Preserve rejected resolution attempts as evidence too, without mutating the
    # stage or promoting unrecognised test ids. A retry can replay the same request
    # after an atomic filesystem freeze but before the SQLite summary was committed.
    import tempfile
    attempt = Path(tempfile.mkdtemp(prefix="resolution-", dir=Path(d["record"]).parent))
    (attempt / "response.txt").write_bytes(rationale.encode("utf-8"))
    (attempt / "request.json").write_text(json.dumps(resolutions, indent=2), encoding="utf-8")
    try:
        fz = freeze(plan, rec, resolutions, rationale, recover=True)
    except (ValueError, OSError, RuntimeError) as exc:
        (attempt / "error.txt").write_text(str(exc), encoding="utf-8")
        _event(plan, "testfreeze-rejected", error=str(exc), evidence=str(attempt))
        raise PlanError(f"adversarial freeze failed: {exc}; evidence: {attempt}") from exc
    (attempt / "result.json").write_text(json.dumps(fz, indent=2), encoding="utf-8")
    db.set_dev_plan(plan["id"], detail=dict(db.dev_plan(plan["id"]).get("detail") or {}, adversarial=dict(d, **fz)))
    _event(plan, "testfreeze", counts=fz["counts"], manifest_sha256=fz["manifest_sha256"])
    write_plan_md(db, plan["id"])
    return fz


def critique_plan(db, settings, plan_id: str, log=_log) -> dict:
    """Run the independent Plan Critic once on an unexecuted plan (nicks/plancritic.py)."""
    from .plancritic import run_critic
    plan = db.dev_plan(plan_id)
    if (plan.get("detail") or {}).get("revision"):
        raise PlanError("the plan was already revised after its critique; V1D.1 allows one critique-and-revision cycle")
    if any(t["status"] != "pending" for t in db.dev_tasks(plan["id"])) or db.dev_attempts(plan["id"]):
        raise PlanError("the Plan Critic reviews a plan before implementation; this plan has already started")
    rec = run_critic(db, settings, plan, log)
    detail = dict(db.dev_plan(plan["id"]).get("detail") or {}, critique=rec)
    db.set_dev_plan(plan["id"], detail=detail)
    _event(plan, "critique", status=rec["status"], critic=(rec.get("critic") or {}).get("model"))
    write_plan_md(db, plan["id"])
    return rec


def revise_plan(db, settings, plan_id: str, manifest: dict | None, response: str) -> dict:
    """Claude's ONE revision after the critique. `manifest` None = keep the plan unchanged (with a reasoned response).
    The original manifest, the critique and the response are all preserved."""
    plan = db.dev_plan(plan_id)
    d = plan.get("detail") or {}
    if (d.get("critique") or {}).get("status") != "done":
        raise PlanError("run the Plan Critic first (`nicks dev critique`)")
    if d.get("revision"):
        raise PlanError("the plan was already revised once; V1D.1 allows one critique-and-revision cycle")
    if not response.strip():
        raise PlanError("--response is required: how each critique point was addressed, or why it was rejected")
    if any(t["status"] != "pending" for t in db.dev_tasks(plan["id"])) or db.dev_attempts(plan["id"]):
        raise PlanError("this plan has already started; it can no longer be revised (use `nicks dev amend`)")
    pdir = Path(plan["dir"])
    (pdir / "critique" / "response.md").write_text(response, encoding="utf-8")
    rev = {"at": time.time(), "kept": manifest is None, "response": str(pdir / "critique" / "response.md"),
           "original_sha256": plan["manifest_sha256"]}
    if manifest is not None:
        from .rework import validate_existing_plan
        validate_existing_plan(plan, manifest)
        errs = validate_manifest(manifest, Path(plan["repo"]), plan["base_sha"], settings)
        if errs:
            raise PlanError("invalid revised manifest:\n- " + "\n- ".join(errs))
        text = json.dumps(manifest, indent=2, ensure_ascii=False)
        if not (pdir / "manifest-original.json").exists():
            shutil.copyfile(pdir / "manifest.json", pdir / "manifest-original.json")
        (pdir / "manifest-revised.json").write_text(text, encoding="utf-8")
        rev["revised_sha256"] = _sha(text)
        with db.tx():
            db.execute("UPDATE dev_plans SET manifest=?, manifest_sha256=? WHERE id=?", (text, _sha(text), plan["id"]))
            db.execute("DELETE FROM dev_tasks WHERE plan_id=?", (plan["id"],))
            for t in manifest["tasks"]:
                db.execute("INSERT INTO dev_tasks (plan_id, task_id, status, spec, updated_at) VALUES (?,?,?,?,?)",
                           (plan["id"], t["id"], "pending", json.dumps(t), time.time()))
    db.set_dev_plan(plan["id"], detail=dict(db.dev_plan(plan["id"]).get("detail") or {}, revision=rev))
    _event(plan, "revised", kept=manifest is None)
    write_plan_md(db, plan["id"])
    return rev


REPLACEABLE = ("needs-decomposition", "pending", "blocked")


def amend_plan(db, settings, plan_id: str, replace: str | None, fragment: dict, reason: str = "") -> list[str]:
    """Claude's semantic decomposition, applied by the orchestrator.

    replace=<task>: supersede that unfinished task by smaller ones; tasks that depended on it now depend on ALL
    replacements; the superseded task is never executed again. replace=None: add tasks (e.g. an integration fix).
    Every amendment needs a reason and is logged; verified work is never touched. Endless replanning is cut off by
    a decomposition depth limit (a split of a split of ...) and a per-plan amendment budget."""
    plan = db.dev_plan(plan_id)
    if not reason.strip():
        raise PlanError("an amendment needs --reason (why the task is split / added)")
    from .rework import check_deadline, validate_tasks
    check_deadline(plan)
    if (plan.get("detail") or {}).get("rework"):
        validate_tasks(plan["detail"]["rework"], fragment.get("tasks", []), all_blockers=False)
    tasks = {t["task_id"]: t for t in db.dev_tasks(plan["id"])}
    log_path = Path(plan["dir"]) / "amendments.jsonl"
    n_prev = len(log_path.read_text().splitlines()) if log_path.exists() else 0
    if n_prev >= settings.dev_max_amendments:
        raise PlanError(f"amendment budget exhausted ({n_prev}/{settings.dev_max_amendments}); this plan needs human "
                        "attention rather than more replanning")
    depth = 0
    if replace is not None:
        if replace not in tasks:
            raise PlanError(f"no task {replace!r} in plan {plan['id']}")
        if tasks[replace]["status"] not in REPLACEABLE:
            raise PlanError(f"task {replace} is {tasks[replace]['status']}; only {', '.join(REPLACEABLE)} tasks can be replaced")
        depth = int(tasks[replace]["spec"].get("split_depth", 0)) + 1
        if depth > settings.dev_max_decomposition_depth:
            db.set_dev_task(plan["id"], replace, "failed",
                            f"decomposition limit reached (depth {depth - 1}); needs human attention")
            _event(plan, "failed", task=replace, reason="decomposition depth limit")
            write_plan_md(db, plan["id"])
            raise PlanError(f"task {replace} was already split {depth - 1} time(s) (limit "
                            f"{settings.dev_max_decomposition_depth}); it is now `failed` and needs human attention")
    frag = dict(fragment, _story_ids=[s_["id"] for s_ in plan["manifest"]["stories"]])
    errs = validate_manifest(frag, Path(plan["repo"]), plan["base_sha"], settings,
                             existing_ids=set(tasks), partial=True)
    dead = {tid for tid, t in tasks.items() if t["status"] in ("superseded", "failed")} | ({replace} if replace else set())
    for t in fragment.get("tasks") or []:
        for d in t.get("depends_on") or []:
            if d in dead:
                errs.append(f"task {t.get('id')}: depends on {d}, which is {'being replaced' if d == replace else tasks[d]['status']}")
    if errs:
        raise PlanError("invalid amendment:\n- " + "\n- ".join(errs))
    new_ids = [t["id"] for t in fragment["tasks"]]
    with db.tx():
        if replace is not None:
            db.set_dev_task(plan["id"], replace, "superseded", f"decomposed into {', '.join(new_ids)}: {reason}")
        for t in fragment["tasks"]:
            spec = dict(t, split_from=replace, split_depth=depth, split_reason=reason) if replace else dict(t, added_reason=reason)
            # A narrower correction is not permission to drop earlier story
            # obligations. Keep their provenance, without freezing test names
            # or requiring an identical implementation of the tests.
            prior = [old for old in tasks.values() if old['spec']['story'] == t['story']
                     and (old['status'] == 'verified' or old['task_id'] == replace)]
            history = {}
            for old in prior:
                for record in old['spec'].get('acceptance_history', []):
                    history[record['task']] = record
                history[old['task_id']] = {
                    'task': old['task_id'], 'spec_sha256': _sha(json.dumps(old['spec'], sort_keys=True)),
                    'commit': (old.get('result') or {}).get('commit'),
                    'acceptance': old['spec']['acceptance'], 'verify': old['spec']['verify'],
                    'context': old['spec'].get('context', ''), 'out_of_scope': old['spec']['out_of_scope']}
            spec['acceptance_history'] = list(history.values())
            db.execute("INSERT INTO dev_tasks (plan_id, task_id, status, spec, updated_at) VALUES (?,?,?,?,?)",
                       (plan["id"], t["id"], "pending", json.dumps(spec), time.time()))
        if replace is not None:
            for tid, row in tasks.items():
                if replace in row["spec"]["depends_on"]:
                    spec = dict(row["spec"], depends_on=[d for d in row["spec"]["depends_on"] if d != replace] + new_ids)
                    db.execute("UPDATE dev_tasks SET spec=? WHERE plan_id=? AND task_id=?",
                               (json.dumps(spec), plan["id"], tid))
    with open(log_path, "a") as f:
        f.write(json.dumps({"t": time.time(), "replace": replace, "reason": reason, "depth": depth,
                            "tasks": fragment["tasks"]}) + "\n")
    _event(plan, "amended", task=replace, new=new_ids, reason=reason)
    if plan.get("status") in ("needs-decomposition", "implemented", "integration-failed", "blocked", "planned", None):
        db.set_dev_plan(plan["id"], status="planned")
    write_plan_md(db, plan["id"])
    return new_ids


def _event(plan: dict, kind: str, **kw) -> None:
    with FILE_LOCK, open(Path(plan["dir"]) / "events.jsonl", "a") as f:
        f.write(json.dumps({"t": round(time.time(), 3), "event": kind, **kw}) + "\n")


def ready_tasks(db, plan_id: str) -> list[dict]:
    tasks = db.dev_tasks(plan_id)
    status = {t["task_id"]: t["status"] for t in tasks}
    return [t for t in tasks if t["status"] == "pending"
            and all(status.get(d) == "verified" for d in t["spec"]["depends_on"])]


# --------------------------------------------------------------------------- worktree

def _dep_order(db, plan_id: str, task_id: str) -> list[dict]:
    """Verified transitive dependencies of a task, dependencies first."""
    tasks = {t["task_id"]: t for t in db.dev_tasks(plan_id)}
    seen, order = set(), []

    def visit(tid):
        for d in tasks[tid]["spec"]["depends_on"]:
            if d not in seen:
                seen.add(d)
                visit(d)
                order.append(tasks[d])
    visit(task_id)
    return order


def _clone(repo: str, gitdir: Path, wt: Path) -> None:
    p = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "clone", "--quiet", "--no-checkout",
                        f"--separate-git-dir={gitdir}", repo, str(wt)], capture_output=True, text=True, env=_git_env())
    if p.returncode != 0:
        raise DevError(f"cannot clone {repo}: {p.stderr.strip()[:400]}")
    (gitdir / "hooks").exists() and shutil.rmtree(gitdir / "hooks")
    (gitdir / "info").mkdir(exist_ok=True)
    (gitdir / "info" / "exclude").write_text("\n".join(JUNK) + "\n")
    g(gitdir, None, "config", "user.name", WORKER_IDENTITY[0])
    g(gitdir, None, "config", "user.email", WORKER_IDENTITY[1])
    g(gitdir, None, "config", "commit.gpgsign", "false")
    g(gitdir, None, "remote", "remove", "origin", check=False)      # no path back to the user's repository


def _pick(gitdir: Path, wt: Path, src_gitdir: str, commit: str, what: str) -> None:
    g(gitdir, wt, "fetch", "--quiet", "--no-tags", src_gitdir, commit)
    pick = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", f"--git-dir={gitdir}", f"--work-tree={wt}",
                           "cherry-pick", "--allow-empty", "--keep-redundant-commits", commit],
                          capture_output=True, text=True, env=_git_env(), cwd=str(wt))
    if pick.returncode != 0:
        raise DevError(f"{what} does not apply cleanly: {pick.stderr.strip()[:300]}")


def state_paths(plan: dict) -> tuple[Path, Path]:
    d = Path(plan["dir"]) / "state"
    return d / "git", d / "worktree"


def ensure_state(db, plan: dict) -> tuple[Path, Path, str]:
    """The plan's CUMULATIVE VERIFIED STATE: a private clone whose HEAD is the plan base plus every verified task,
    in execution order (branch nicks/<plan>/cumulative). Sequential V1B execution builds every new task on it, so a
    task's parent is exactly this state and integration never needs merging. Rebuilt from the verified commits if
    it is missing (crash, or a plan verified before V1B)."""
    gitdir, wt = state_paths(plan)
    head = plan.get("head")
    with STATE_LOCK:
        if gitdir.exists() and head and g(gitdir, None, "cat-file", "-t", head, check=False).strip() == "commit":
            # The journal's head is canonical. An integration interrupted half-way (cherry-pick in progress, or the
            # branch moved before the journal was updated) is rolled back here; its orphan commit is never used.
            branch = f"nicks/{plan['id']}/cumulative"
            if (gitdir / "CHERRY_PICK_HEAD").exists() or g(gitdir, None, "rev-parse", "HEAD").strip() != head:
                g(gitdir, wt, "cherry-pick", "--quit", check=False)
                g(gitdir, wt, "reset", "--quiet", "--hard", head)
            if g(gitdir, None, "rev-parse", "--verify", "--quiet", branch, check=False).strip() != head:
                g(gitdir, None, "branch", "-f", branch, head)
            # An interrupted clone rebuild can lose the worktree's discovery
            # marker while its Git store and verified journal head survive.
            # Recover only a missing marker, never follow or overwrite one.
            marker = wt / '.git'
            if not marker.exists():
                if marker.is_symlink():
                    raise DevError('cumulative Git marker is a dangling symlink; refusing recovery')
                with marker.open('x', encoding='utf-8') as stream:
                    stream.write(f'gitdir: {gitdir.resolve()}\n')
                _event(plan, 'state-git-marker-restored', head=head, gitdir=str(gitdir), marker=str(marker))
            return gitdir, wt, head
        return _build_state(db, plan, gitdir, wt)


def _build_state(db, plan: dict, gitdir: Path, wt: Path) -> tuple[Path, Path, str]:
    if gitdir.exists():
        make_writable(gitdir.parent)
        shutil.rmtree(gitdir.parent)
    gitdir.parent.mkdir(parents=True, exist_ok=True)
    _clone(plan["repo"], gitdir, wt)
    g(gitdir, wt, "checkout", "--quiet", "--detach", "--force", plan["base_sha"])
    for t in sorted((t for t in db.dev_tasks(plan["id"]) if t["status"] == "verified"),
                    key=lambda t: (t.get("result") or {}).get("integrated_at") or (t.get("result") or {}).get(
                        "verified_at") or t["updated_at"]):
        r = t["result"]
        _pick(gitdir, wt, r["gitdir"], r["commit"], f"verified task {t['task_id']}")
    head = g(gitdir, None, "rev-parse", "HEAD").strip()
    g(gitdir, None, "branch", "-f", f"nicks/{plan['id']}/cumulative", head)
    db.set_dev_plan(plan["id"], head=head)
    plan["head"] = head
    return gitdir, wt, head


def prepare_worktree(db, plan: dict, task_id: str, adir: Path, head: str | None = None) -> tuple[Path, Path, str]:
    """Private clone of the user's repository at a verified revision of the plan's canonical state (by default the
    current head; the scheduler passes the head it dispatched against). Dependencies are verified by construction."""
    gitdir, wt = adir / "git", adir / "worktree"
    if head is None:
        _, _, head = ensure_state(db, plan)
    sgit, _ = state_paths(plan)
    _clone(plan["repo"], gitdir, wt)
    if head != plan["base_sha"]:
        with STATE_LOCK:
            g(gitdir, wt, "fetch", "--quiet", "--no-tags", str(sgit), head)
    g(gitdir, wt, "checkout", "--quiet", "--detach", "--force", head)
    start = g(gitdir, None, "rev-parse", "HEAD").strip()
    return gitdir, wt, start


def collect_changes(gitdir: Path, wt: Path, start: str, scope: list[str], settings, secrets_: list[str]) -> dict:
    """What the worker changed, judged from git by the engine (never from the worker's own report)."""
    g(gitdir, wt, "add", "-A")
    raw = g(gitdir, wt, "diff", "--cached", "--raw", "-z", "--no-renames", start)
    entries, toks = [], raw.split("\0")
    i = 0
    while i < len(toks) - 1:
        meta = toks[i]
        if not meta.startswith(":"):
            i += 1
            continue
        old_mode, new_mode, _, _, status = meta[1:].split(" ")
        entries.append({"path": toks[i + 1], "status": status[0], "old_mode": old_mode, "new_mode": new_mode})
        i += 2
    violations = []
    for e in entries:
        path = e["path"]
        if not any(fnmatch.fnmatch(path, pat) or path.startswith(pat.rstrip("/") + "/") for pat in scope):
            violations.append(f"{path}: outside the permitted scope")
        if e["new_mode"] == "120000":
            violations.append(f"{path}: symbolic links are not accepted from workers")
        if e["new_mode"] == "160000":
            violations.append(f"{path}: nested repositories are not accepted")
        parts = path.split("/")
        if ".git" in parts:
            violations.append(f"{path}: git metadata")
        f = wt / path
        if e["status"] != "D" and f.is_file() and f.stat().st_size > settings.dev_max_file_bytes:
            violations.append(f"{path}: {f.stat().st_size} bytes (> {settings.dev_max_file_bytes})")
    patch = g(gitdir, wt, "diff", "--cached", "--binary", start)
    for s in secrets_:
        if s and len(s) >= 12 and s in patch:
            violations.append("the change contains credential material")
            break
    return {"files": entries, "violations": violations, "patch": patch}


# --------------------------------------------------------------------------- verification (engine-run)

def verify(gitdir: Path, out: Path, commands: list[str], timeout: float, keep_going: bool = False,
           prefix: str = "verify") -> dict:
    """Export exactly the staged change into a fresh tree and run each command in a network-less jail. By default
    the first failure stops the run (later commands usually depend on earlier ones); `keep_going` runs every
    command independently (the integration ratchet needs each command's own result)."""
    if not bwrap_path():
        raise SandboxUnavailable("the Development Engine requires bubblewrap (bwrap)")
    tree, home = out / "tree", out / "home"
    for p in (tree, home):
        if p.exists():
            make_writable(p)
            shutil.rmtree(p)
    tree.mkdir(parents=True)
    home.mkdir()
    g(gitdir, None, "checkout-index", "-a", "-f", f"--prefix={tree}/")
    runs = []
    for i, cmd in enumerate(commands, 1):
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1", "PWD": str(tree), "TMPDIR": "/tmp"}
        argv = wrap(["/bin/sh", "-c", cmd], snapshot=tree, home=home, binary="/bin/sh", network=False, writable=True)
        t0 = time.time()
        try:
            p = subprocess.run(argv, cwd=str(tree), env=env, capture_output=True, timeout=timeout)
            rc, output = p.returncode, (p.stdout + p.stderr).decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired as exc:
            rc, output = None, f"timed out after {timeout:.0f}s\n" + ((exc.stdout or b"") + (exc.stderr or b"")).decode(errors="replace")
        log = out / f"{prefix}-{i}.log"
        log.write_text(f"$ {cmd}\n{output}", encoding="utf-8")
        runs.append({"command": cmd, "exit_code": rc, "seconds": round(time.time() - t0, 2),
                     "output_tail": output[-4000:], "log": str(log), "log_sha256": _sha(log.read_bytes())})
        if rc != 0 and not keep_going:
            break                                   # later commands usually depend on earlier ones
    shutil.rmtree(home, ignore_errors=True)
    make_writable(tree)
    shutil.rmtree(tree, ignore_errors=True)
    passed = len(runs) == len(commands) and all(r["exit_code"] == 0 for r in runs)
    return {"passed": passed, "runs": runs}


# --------------------------------------------------------------------------- prompts (free-form output)

TASK_PROMPT = """You are an implementation worker on the Council of Nicks. Claude, the architect, has broken a
feature into small tasks. You implement exactly ONE task, in the repository working copy that is your current
directory.

Feature: {feature}
Story {story_id}: {story_title}
{story_narrative}
YOUR TASK ({task_id}): {goal}
{context}
Read these files first:
{context_files}

You may create, modify or delete ONLY paths matching:
{scope}
Any change outside these paths makes the engine reject your whole attempt.

Acceptance criteria:
{acceptance}

Verification. When you finish, the engine runs these commands itself, on a clean copy of your changes and
without network access:
{verify}
Run them yourself before you finish.

Out of scope (do NOT do these):
{out_of_scope}

Rules:
- Make the smallest change that meets the acceptance criteria. Follow the existing code style.
- Edit the files directly in this working copy. Do not commit, create branches, or touch .git.
- Commands have no network access. Do not install packages.
- If the task is impossible or ambiguous as written, change nothing and explain why.

When you are done, say briefly what you changed and how you checked it, in any format.
"""

REPAIR_PROMPT = """

REPAIR ROUND {n}. Your previous changes are still in this working copy. The engine ran the verification on
them and it FAILED:
$ {command}  (exit {exit_code})
{output}

Fix the problem, staying within the same permitted paths. Do not weaken, skip or delete the verification or its
tests unless the task explicitly says to.
"""


def build_task_prompt(plan: dict, spec: dict) -> str:
    m = plan["manifest"]
    story = next((s for s in m.get("stories", []) if s["id"] == spec["story"]), {"id": spec["story"], "title": ""})
    bullet = lambda xs: "\n".join(f"- {x}" for x in xs) or "- (none)"
    ctx = ""
    if m.get("architecture"):
        ctx += f"\nArchitecture notes from the architect:\n{m['architecture']}\n"
    if m.get("constraints"):
        ctx += "\nProject constraints:\n" + bullet(m["constraints"]) + "\n"
    if spec.get("context"):
        ctx += f"\nTask context:\n{spec['context']}\n"
    if (plan.get('detail') or {}).get('rework'):
        from .rework import brief
        ctx += '\n' + brief(plan['detail']['rework'])
        ctx += '\nRetained acceptance obligations (preserve meaningful coverage):\n' + json.dumps(plan['detail']['rework']['obligations'], indent=2)
    if spec.get('acceptance_history'):
        ctx += ('\nEarlier story obligations retained across this correction (original criteria and provenance):\n'
                + json.dumps(spec['acceptance_history'], indent=2, ensure_ascii=False)
                + '\nPreserve mandatory behavior AND its meaningful regression coverage. Tests may be rewritten, '
                'renamed or moved; do not drop a required case merely because the current suite passes. '
                'In your normal final response, map affected earlier cases to current test locations and explain '
                'any replacements or unresolved coverage. This is evidence, not an automatic approval.\n')
    if (plan.get("detail") or {}).get("adversarial", {}).get("status") == "frozen":
        from .testdesign import contract_context
        ctx += contract_context(plan)              # CONTRACT tests only; holdout tests never reach a worker
    return TASK_PROMPT.format(
        feature=m.get("feature", ""), story_id=story["id"], story_title=story.get("title", ""),
        story_narrative=(story.get("narrative") or "") + "\n", task_id=spec["id"], goal=spec["goal"], context=ctx,
        context_files=bullet(spec["context_files"]), scope=bullet(spec["scope"]), acceptance=bullet(spec["acceptance"]),
        verify="\n".join(f"  $ {v}" for v in spec["verify"]), out_of_scope=bullet(spec["out_of_scope"]))


# --------------------------------------------------------------------------- workers

@dataclass
class Worker:
    route: str
    engine: str          # codex | opencode
    model: str
    role: str            # luna | free
    route_obj: object = None
    args: tuple = ()


@dataclass
class WorkerResult:
    ok: bool
    error_class: str | None
    error: str | None
    model: str | None
    text: str
    notes: list
    injected: bool = False
    retry_after: float | None = None       # the provider's own reset hint (e.g. OpenCode's retryAfterMs)


class WorkerPool:
    """Chooses workers in `dev_workers` order: named subscription roles first, then free OpenCode routes via the
    existing Router (cooldowns, quarantine and health are shared with Council reviews).

    Default development workflow (`dev_workers` left at its default): only the Luna and Haiku subscription workers,
    never free routes; each story gets one implementer drawn at random from the available ones (`implementer`)."""

    def __init__(self, db, settings, log=_log, discover_free=True):
        from .runner import FaultPlan
        self.db, self.s, self.log = db, settings, log
        from .policy import default_dev_workers, resolve, worker_order
        resolve(settings)
        self.worker_order = worker_order(settings)
        self.default = default_dev_workers(settings)
        self._impl_lock = threading.Lock()
        self.faults = FaultPlan(os.environ.get("NICKS_FAULTS"))
        self.subs: dict[str, Worker] = {}
        self.unavailable: dict[str, str] = {}
        if any(r != "free" for r in self.worker_order):
            from .subscription import subscription_routes
            routes, problems = subscription_routes(settings)
            self.unavailable.update(problems)
            for r in routes:
                role = next((k for k, v in settings.council_subscription.items()
                             if f"{v.get('provider')}/{v.get('model')}" == r.route), None)
                if role not in self.worker_order:
                    continue
                spec = settings.council_subscription[role]
                if r.kind != "codex":
                    self.subs[role] = Worker(r.route, "claude-proposal", r.model, role, r, tuple(spec.get("args") or ()))
                    continue
                self.subs[role] = Worker(r.route, "codex", r.model, role, r, tuple(spec.get("args") or ()))
        from .router import Router
        self.sub_health = Router(db, [w.route_obj for w in self.subs.values()])
        self.router = None
        if "free" in self.worker_order and not settings.restricted and discover_free:
            from .registry import DiscoveryError, discover
            from .router import Router
            try:
                routes = [r for r in discover(settings, db) if r.eligible and r.tools]
                self.router = Router(db, routes, allow_local=settings.allow_local,
                                     local_concurrency=settings.local_concurrency)
            except DiscoveryError as exc:
                self.unavailable["free"] = str(exc)

    _lock = threading.Lock()

    def require(self) -> None:
        """Default workflow: fail clearly, before anything is dispatched, when neither Luna nor Haiku is available
        (free models are never substituted). A no-op for explicitly configured worker chains."""
        if not self.default or any(r in self.subs for r in self.worker_order):
            return
        from .policy import NoDevSubscription, dev_implementer
        try:
            dev_implementer([])
        except NoDevSubscription as exc:
            why = "; ".join(f"{k}: {v}" for k, v in self.unavailable.items())
            raise DevError(str(exc) + (f" ({why})" if why else "")) from exc

    def implementer(self, plan: dict, spec: dict) -> str | None:
        """Default workflow only: the story's implementer role, drawn once per story among the AVAILABLE subscription
        workers and kept in the plan directory, so parallel tasks and resumes of one story agree. None otherwise."""
        if not self.default:
            return None
        from .policy import dev_implementer
        self.require()
        available = [r for r in self.worker_order if r in self.subs]
        story = spec.get("story") or spec["id"]
        path = Path(plan["dir"]) / "implementers.json"
        with self._impl_lock:
            chosen = json.loads(path.read_text()) if path.exists() else {}
            if story not in chosen:
                role = dev_implementer(available)
                chosen[story] = role
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(json.dumps(chosen, indent=2) + "\n")
                os.replace(tmp, path)
                self.log(f"story {story}: implementer {role} (drawn at random from {', '.join(available)})")
            return chosen[story]

    def pick(self, excluded: set[str], prefer: str | None = None) -> Worker | None:
        """Thread-safe. Free routes keep the Router's per-route/local concurrency; a subscription route serves at
        most `subscription_concurrency` workers at once. `prefer` (the story's implementer) is tried first."""
        with self._lock:
            inflight = self.__dict__.setdefault("_sub_inflight", Counter())
            order = [prefer] + [r for r in self.worker_order if r != prefer] if prefer in self.worker_order \
                else self.worker_order
            for role in order:
                if role == "free":
                    if not self.router:
                        continue
                    r = self.router.pick(excluded, Counter(), pool=lambda r: getattr(r, "kind", "opencode") == "opencode")
                    if r:
                        return Worker(r.route, "opencode", r.model, "free", r)
                elif role in self.subs and self.subs[role].route not in excluded \
                        and not self.sub_health.blocked(self.subs[role].route_obj)[0] \
                        and inflight[self.subs[role].route] < max(1, self.s.subscription_concurrency):
                    inflight[self.subs[role].route] += 1
                    return self.subs[role]
            return None

    def release(self, w: Worker, ok: bool, res: WorkerResult | None, seconds: float) -> None:
        with self._lock:
            if w.engine != "opencode":
                inflight = self.__dict__.setdefault("_sub_inflight", Counter())
                inflight[w.route] = max(0, inflight[w.route] - 1)
                if res is not None and not res.injected:
                    if ok:
                        self.sub_health.record_success(w.route_obj, seconds)
                    elif res.error_class in INFRA_CLASSES:
                        self.sub_health.record_failure(w.route_obj, res.error_class, res.error or "",
                                                       retry_after=res.retry_after)
                return
            if not self.router:
                return
        self.router.release(w.route_obj)
        if res is None or res.injected:
            return
        if ok:
            self.router.record_success(w.route_obj, seconds)
        elif res.error_class in INFRA_CLASSES:
            self.router.record_failure(w.route_obj, res.error_class, res.error or "", retry_after=res.retry_after)

    def run(self, w: Worker, prompt: str, wt: Path, gitdir: Path, out: Path, timeout: float, title: str,
            scope: list[str] | None = None, context_files: list[str] | None = None,
            microtask: dict | None = None, verify_commands: list[str] | None = None) -> WorkerResult:
        out.mkdir(parents=True, exist_ok=True)
        (out / "prompt.txt").write_text(prompt, encoding="utf-8")
        overlays = [wt / ".git", gitdir]
        fault = self.faults.take(w.route)
        if fault:
            msg = f"{fault} (injected by NICKS_FAULTS; provider not contacted)"
            (out / "response.txt").write_text("")
            cls = {"server_error": "transient", "region": "ineligible", "crash": "runtime"}.get(fault, fault)
            return WorkerResult(False, cls if cls in INFRA_CLASSES or cls == "empty" else "transient", msg, None, "",
                                [msg], injected=True)
        from .policy import allows
        if (w.engine == "opencode" and self.s.restricted) or (w.engine != "opencode" and
                not allows(self.s, "claude" if w.engine == "claude-proposal" else "codex")):
            return WorkerResult(False, "unavailable", "worker excluded by execution policy", None, "", [])
        if microtask is not None:
            if w.engine not in ('opencode', 'codex') or not scope or not verify_commands:
                return WorkerResult(False, 'insufficiently_prepared', 'microtasks require a prepared OpenCode or Codex task', None, '', [])
            from .microtasks import run as run_microtask, run_codex
            if w.engine == 'codex':
                r = run_codex(w.model, microtask, scope, verify_commands, wt, out, timeout, extra_args=w.args)
            else:
                r = run_microtask(w.route_obj, microtask, scope, verify_commands, wt, out, timeout)
            return WorkerResult(r.ok, r.error_class, r.error, r.model, r.text, list(r.notes))
        if w.engine == "claude-proposal":
            from .neutral import ClaudeRunner, NeutralUnavailable
            from .patchworker import apply_proposal, source_context, SCHEMA
            if scope is None:
                return WorkerResult(False, "proposal", "engine did not provide proposal scope", None, "", [])
            context = source_context(wt, context_files, scope, self.s.dev_critic_context_bytes,
                                     self.s.dev_critic_file_bytes)
            proposal_prompt = (prompt + "\nYou have no filesystem tools. The orchestrator will apply your proposal "
                "in an isolated worktree and run scope checks and tests. Return ONLY JSON matching " + SCHEMA +
                "\nExplicit allowed scope: " + json.dumps(scope) + context)
            try:
                res = ClaudeRunner(sandbox=True, sandbox_extra_ro=self.s.sandbox_extra_ro,
                                   sandbox_extra_rw=self.s.sandbox_extra_rw).run(w.model, proposal_prompt, wt, out, timeout)
            except NeutralUnavailable as exc:
                return WorkerResult(False, "auth", str(exc), None, "", [])
            body = (out / "response.txt").read_text(encoding="utf-8") if (out / "response.txt").exists() else ""
            if not res.ok:
                return WorkerResult(False, res.error_class, res.error, res.model, body, list(res.notes))
            if not res.model:
                return WorkerResult(False, "attribution", "Claude worker did not report its actual model", None, body, list(res.notes))
            try:
                apply_proposal(wt, body, scope)
            except OSError as exc:
                # An I/O failure may also prevent rollback. Discard this entire
                # attempt; never repair or verify a possibly partial application.
                return WorkerResult(False, "proposal_apply", str(exc), res.model, body, list(res.notes))
            except ValueError as exc:
                return WorkerResult(False, "proposal", str(exc), res.model, body, list(res.notes))
            return WorkerResult(True, None, None, res.model, body, list(res.notes))
        if w.engine == "codex":
            from .evil import CodexRunner, EvilUnavailable
            try:
                r = CodexRunner(sandbox=True, model=w.model, extra_args=w.args, writable=True, ro_overlays=overlays,
                                sandbox_extra_ro=self.s.sandbox_extra_ro, sandbox_extra_rw=self.s.sandbox_extra_rw
                                ).run(prompt, wt, out, timeout,
                                      **({'session_store': self.session_store} if getattr(self, 'session_store', None) else {}))
            except EvilUnavailable as exc:
                return WorkerResult(False, "auth", str(exc), None, "", [])
            text = (out / "response.txt").read_text(encoding="utf-8") if (out / "response.txt").exists() else ""
            return WorkerResult(r.ok, r.error_class, r.error, r.model, text, list(r.notes))
        from .providers import provider_env
        from .runner import FaultPlan, OpenCodeRunner
        oc = OpenCodeRunner(steps=self.s.dev_agent_steps, faults=FaultPlan(None), sandbox=True, implement=True,
                            ro_overlays=overlays, sandbox_extra_ro=self.s.sandbox_extra_ro,
                            sandbox_extra_rw=self.s.sandbox_extra_rw, provider_env=provider_env(self.s))
        r = oc.run(w.route_obj, prompt, wt, out, timeout, title)
        return WorkerResult(r.ok, r.error_class, r.error, r.model or w.route, r.text, list(r.notes),
                            retry_after=getattr(r, "retry_after", None))


# --------------------------------------------------------------------------- task execution

@contextmanager
def plan_lock(plan: dict):
    path = Path(plan["dir"]) / "lock"
    f = open(path, "a+")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DevError(f"plan {plan['id']} is being run by another nicks process")
        yield
    finally:
        f.close()


def recover(db, plan_id: str, log=_log) -> int:
    """We hold the plan lock, so any `running` attempt belongs to a process that died: mark it interrupted
    (an infrastructure failure; its worktree is never reused) and make its task runnable again."""
    n = 0
    for a in db.dev_attempts(plan_id):
        if a["status"] == "running":
            db.finish_dev_attempt(a["id"], status="interrupted", failure_kind="infra", error_class="interrupted",
                                  error="the orchestrator stopped during this attempt; work discarded",
                                  finished_at=time.time())
            n += 1
    for t in db.dev_tasks(plan_id):
        if t["status"] == "running":
            db.set_dev_task(plan_id, t["task_id"], "pending", "recovered after an interrupted run")
        elif t["status"] == "integrating":       # interrupted integration: re-queue; the state is reconciled
            db.set_dev_task(plan_id, t["task_id"], "implemented", "integration interrupted; re-queued")
            n += 1
    if n:
        log(f"recovered {n} interrupted attempt(s); their worktrees are discarded")
    return n


def _login_secrets() -> list[str]:
    from .evil import codex_home, login_secrets
    out = []
    try:
        out += login_secrets((codex_home() / "auth.json").read_bytes())
    except OSError:
        pass
    return out


def _secrets(settings) -> list[str]:
    from .providers import all_keys
    try:
        keys = all_keys(settings)
    except Exception:
        keys = []
    return _login_secrets() + keys


def run_task(db, settings, plan_ref: str | None = None, task_id: str | None = None, log=_log,
             pool: WorkerPool | None = None) -> dict:
    plan = db.dev_plan(plan_ref) if plan_ref else db.latest_dev_plan()
    if not plan:
        raise DevError(f"no plan matching {plan_ref!r}" if plan_ref else "no development plans yet")
    from .rework import check_deadline
    check_deadline(plan)
    from .policy import restore
    restore(settings, (plan.get("detail") or {}).get("execution_policy"))
    if not bwrap_path() or not settings.sandbox:
        raise SandboxUnavailable("the Development Engine requires the bubblewrap sandbox (workers can write)")
    if (why := critic_pending(plan) or debate_pending_(plan) or adversarial_pending(plan)):
        raise DevError(f"plan {plan['id']}: {why}")
    with plan_lock(plan):
        if (db.dev_plan(plan['id']).get('detail') or {}).get('competition', {}).get('status') not in (None, 'integrated'):
            raise DevError('this plan has a competing implementation in progress; resume `dev compete`')
        recover(db, plan["id"], log)
        ready = ready_tasks(db, plan["id"])
        if task_id:
            task = next((t for t in db.dev_tasks(plan["id"]) if t["task_id"] == task_id), None)
            if not task:
                raise DevError(f"no task {task_id!r} in plan {plan['id']}")
            if task not in ready and task["status"] == "pending":
                raise DevError(f"task {task_id} is not ready: its dependencies are not all verified")
            if task["status"] != "pending":
                raise DevError(f"task {task_id} is {task['status']}")
        else:
            if not ready:
                return {"plan": plan["id"], "status": "nothing-ready", "tasks": _task_summary(db, plan["id"])}
            task = ready[0]
        pool = pool or WorkerPool(db, settings, log)
        if isinstance(pool, WorkerPool):
            pool.require()
        if settings.dev_preflight:
            _, _, head = ensure_state(db, plan)
            if not preflight_gate(db, settings, db.dev_plan(plan["id"]), task, head, log):
                return _finish(db, db.dev_plan(plan["id"]), task["task_id"], "needs-decomposition", None, log)
        r = _execute(db, settings, plan, task, pool, log)
        if r["outcome"] == "implemented":
            integrate_pending(db, settings, db.dev_plan(plan["id"]), log)
            return _finish(db, db.dev_plan(plan["id"]), task["task_id"],
                           {"verified": "verified"}.get(next(t for t in db.dev_tasks(plan["id"])
                                                             if t["task_id"] == task["task_id"])["status"], "integration-failed"),
                           r.get("attempt"), log)
        return r


def _task_summary(db, plan_id) -> dict:
    return {t["task_id"]: t["status"] for t in db.dev_tasks(plan_id)}


_NOT_FOUND = re.compile(r"(^|\n)(/bin/)?(ba|da)?sh: (\d+: |line \d+: )?[^:\n]+: (command )?not found")


def verification_broken(run: dict) -> str | None:
    """Why a FAILED verification run shows the command itself cannot run in the verification environment (rather than
    the implementation being wrong), or None. Evidence-based only: missing executables, git metadata, exit 126/127."""
    out = run.get("output_tail") or ""
    if "not a git repository" in out.lower():
        return "it needs git metadata; verification runs on an exported tree without .git"
    m = _NOT_FOUND.search(out)
    if m:
        return f"executable not found: {m.group(0).strip()[:120]}"
    if run.get("exit_code") in (126, 127):
        return f"exit {run['exit_code']} (command not executable or not found)"
    return None


def _return_to_claude(db, settings, plan, tid, spec, aid, log, *, cause: str, why: str) -> dict:
    """Back to the Story Architect for a NON-implementation reason (specification, verification environment,
    repeated timeouts): the task's implementation budget is untouched."""
    db.set_dev_task(plan["id"], tid, "needs-decomposition", f"{cause}: {why}; return to Claude (`nicks dev amend`)"[:500])
    _event(plan, "needs-decomposition", task=tid, cause=cause)
    return _finish(db, plan, tid, "needs-decomposition", aid, log)


def _give_up(db, settings, plan, tid, spec, impl_failures, aid, log) -> dict:
    """Implementation budget used up: back to Claude for decomposition, or terminal at the depth limit."""
    if int(spec.get("split_depth", 0)) >= settings.dev_max_decomposition_depth:
        db.set_dev_task(plan["id"], tid, "failed", f"{impl_failures} implementation failures at decomposition depth "
                        f"{spec.get('split_depth', 0)} (limit {settings.dev_max_decomposition_depth}); needs human attention")
        _event(plan, "failed", task=tid)
        return _finish(db, plan, tid, "failed", aid, log)
    db.set_dev_task(plan["id"], tid, "needs-decomposition",
                    f"{impl_failures} implementation failures; return to Claude for decomposition (`nicks dev amend`)")
    _event(plan, "needs-decomposition", task=tid)
    return _finish(db, plan, tid, "needs-decomposition", aid, log)


def _execute(db, settings, plan: dict, task: dict, pool: WorkerPool, log, deadline: float | None = None,
             head: str | None = None) -> dict:
    value = (plan.get("detail") or {}).get("rework")
    if value:
        deadline = min(deadline if deadline is not None else float("inf"), value["deadline"])
    tid, spec = task["task_id"], task["spec"]
    prior = db.dev_attempts(plan["id"], tid)
    impl_failures = sum(1 for a in prior if a["failure_kind"] in ("implementation", "scope"))
    if impl_failures >= settings.dev_max_impl_failures:
        return _give_up(db, settings, plan, tid, spec, impl_failures, None, log)
    prefer = pool.implementer(plan, spec) if isinstance(pool, WorkerPool) else None   # raises: no subscription
    db.set_dev_task(plan["id"], tid, "running")
    tdir = Path(plan["dir"]) / "tasks" / tid
    base_prompt = build_task_prompt(plan, spec)
    secrets_ = _secrets(settings)
    excluded: set[str] = set()
    n = len(prior)
    for _ in range(settings.dev_max_attempts):
        if deadline is not None and deadline - time.time() < 90:
            db.set_dev_task(plan["id"], tid, "pending", "paused: this execute call's time budget is used up")
            return _finish(db, plan, tid, "paused", None, log)
        w = pool.pick(excluded, prefer) if prefer else pool.pick(excluded)
        if not w and isinstance(pool, WorkerPool) and pool.default and impl_failures \
                and all(sw.route in excluded for sw in pool.subs.values()):
            # Default workflow: every available subscription model has now failed this task and free models are
            # never substituted, so it goes back to the Architect instead of waiting for capacity that cannot help.
            return _give_up(db, settings, plan, tid, spec, impl_failures, None, log)
        if not w:
            reason = "no eligible worker capacity right now (" + "; ".join(
                f"{k}: {v}" for k, v in pool.unavailable.items()) + ")" if pool.unavailable else "no eligible worker capacity right now"
            db.set_dev_task(plan["id"], tid, "pending", reason[:500])
            log(f"task {tid}: {reason}")
            return _finish(db, plan, tid, "no-capacity", None, log)
        n += 1
        aid = f"{plan['id']}-{tid}-a{n}-{secrets.token_hex(2)}"
        adir = tdir / f"a{n}"
        adir.mkdir(parents=True, exist_ok=True)
        db.start_dev_attempt({"id": aid, "plan_id": plan["id"], "task_id": tid, "n": n, "worker": w.route,
                              "engine": w.engine, "status": "running", "started_at": time.time(), "pid": os.getpid(),
                              "dir": str(adir)})
        log(f"task {tid}: attempt {n} -> {w.route} ({w.role})")
        try:
            gitdir, wt, start = prepare_worktree(db, plan, tid, adir, head)
        except DevError as exc:
            pool.release(w, False, None, 0)
            db.finish_dev_attempt(aid, status="infra-failed", failure_kind="infra", error_class="worktree",
                                  error=str(exc)[:600], finished_at=time.time())
            db.set_dev_task(plan["id"], tid, "blocked", str(exc)[:500])
            return _finish(db, plan, tid, "blocked", aid, log)
        outcome = _attempt(db, settings, plan, spec, w, pool, aid, adir, gitdir, wt, start, base_prompt, secrets_, log,
                           deadline)
        if outcome == "implemented":
            return _finish(db, plan, tid, "implemented", aid, log)
        if outcome == "infra":
            excluded.add(w.route)
            continue
        if outcome == "timeout":
            excluded.add(w.route)
            n_to = sum(1 for a in db.dev_attempts(plan["id"], tid) if a["failure_kind"] == "timeout")
            if n_to >= settings.dev_max_timeouts:
                return _return_to_claude(db, settings, plan, tid, spec, aid, log, cause="timeout", why=(
                    f"{n_to} worker timeouts on different models and no implementation failure; the task may be too "
                    "large for one attempt (partial patches preserved as evidence)"))
            continue
        if outcome == "environment":
            return _return_to_claude(db, settings, plan, tid, spec, aid, log, cause="verification-env", why=(
                "a verification command cannot run in the verification environment (see the attempt's error); fix the "
                "task's checks, no implementation failure was charged"))
        if outcome == 'preparation':
            return _return_to_claude(db, settings, plan, tid, spec, aid, log, cause='insufficiently_prepared',
                                    why='microtask exceeded its context contract or needs missing implementation context')
        excluded.add(w.route)                      # an implementation failure: give another model a chance
        impl_failures += 1
        if impl_failures >= settings.dev_max_impl_failures:
            return _give_up(db, settings, plan, tid, spec, impl_failures, aid, log)
    db.set_dev_task(plan["id"], tid, "pending", f"attempt budget ({settings.dev_max_attempts}) for this run used up")
    return _finish(db, plan, tid, "attempts-exhausted", None, log)


def _attempt(db, settings, plan, spec, w, pool, aid, adir, gitdir, wt, start, base_prompt, secrets_, log,
             deadline: float | None = None) -> str:
    detail: dict = {"start": start, "rounds": []}
    prompt = base_prompt
    t0 = time.monotonic()
    model = None
    for round_ in range(settings.dev_max_repairs + 1):
        rdir = adir / f"round-{round_}"
        timeout = settings.dev_worker_timeout
        if deadline is not None:               # leave time for verification inside this execute call
            timeout = max(30.0, min(timeout, deadline - time.time() - 60))
        if isinstance(pool, WorkerPool):
            res = pool.run(w, prompt, wt, gitdir, rdir, timeout, f"nicks dev {spec['id']}",
                           scope=spec["scope"], context_files=spec["context_files"],
                           **({'microtask': spec['microtask'], 'verify_commands': spec['verify']} if spec.get('microtask') else {}))
        else:
            res = pool.run(w, prompt, wt, gitdir, rdir, timeout, f"nicks dev {spec['id']}")
        if res.error_class == "timeout" and timeout < settings.dev_worker_timeout:
            res.error_class = "deadline"       # cut short by the call's budget, not the worker's fault
        model = res.model or model
        rec = {"round": round_, "worker_ok": res.ok, "error_class": res.error_class, "error": res.error,
               "model": res.model, "notes": res.notes, "response": str(rdir / "response.txt")}
        detail["rounds"].append(rec)
        if not res.ok and res.error_class == 'insufficiently_prepared':
            pool.release(w, False, res, time.monotonic()-t0)
            db.finish_dev_attempt(aid, status='needs-preparation', failure_kind='planning', error_class=res.error_class,
                                  error=res.error, actual_model=model, repairs=round_, finished_at=time.time(), detail=detail)
            return 'preparation'
        if not res.ok and not res.injected and (res.error_class == "timeout" or res.error_class in INFRA_CLASSES):
            rec["partial"] = _preserve_partial(gitdir, wt, start, adir)
        if not res.ok and res.error_class == "timeout" and not res.injected:
            # V1D.1: a timeout is not evidence that the worker misunderstood or mis-implemented the task (Round 1: a
            # slow local model's partial edit was correct). It is its own failure kind: never charged to the
            # implementation budget, the partial work is kept as evidence (never integrated), and another worker is
            # tried. Repeated timeouts on different workers send the task back to Claude (possibly oversized).
            pool.release(w, False, res, time.monotonic() - t0)
            db.finish_dev_attempt(aid, status="timeout", failure_kind="timeout", error_class="timeout",
                                  error=(res.error or "")[:600], actual_model=model, repairs=round_,
                                  finished_at=time.time(), detail=detail)
            log(f"  worker timed out ({res.error}); not an implementation failure; partial work kept as evidence, "
                "switching worker")
            return "timeout"
        if not res.ok and res.error_class in INFRA_CLASSES:
            pool.release(w, False, res, time.monotonic() - t0)
            db.finish_dev_attempt(aid, status="infra-failed", failure_kind="infra", error_class=res.error_class,
                                  error=(res.error or "")[:600], actual_model=model, repairs=round_,
                                  finished_at=time.time(), detail=detail)
            log(f"  infrastructure failure ({res.error_class}): {(res.error or '')[:160]} -> switching worker")
            return "infra"
        if not res.ok and res.error_class == "proposal":
            if round_ < settings.dev_max_repairs:
                prompt = base_prompt + "\nThe previous proposal was rejected before application: " + str(res.error) + "\nCorrect it and return one valid proposal."
                log("  invalid proposal; repair round with the same worker")
                continue
            pool.release(w, True, res, time.monotonic() - t0)
            db.finish_dev_attempt(aid, status="proposal-rejected", failure_kind="implementation", error_class="proposal",
                                  error=(res.error or "")[:600], actual_model=model, repairs=round_,
                                  finished_at=time.time(), detail=detail)
            return "implementation"
        ch = collect_changes(gitdir, wt, start, spec["scope"], settings, secrets_)
        rec["files"] = ch["files"]
        if ch["violations"]:
            (adir / "rejected.patch").write_text(ch["patch"], encoding="utf-8")
            pool.release(w, True, res, time.monotonic() - t0)
            db.finish_dev_attempt(aid, status="scope-violation", failure_kind="scope", error_class="scope",
                                  error="; ".join(ch["violations"])[:600], actual_model=model, repairs=round_,
                                  finished_at=time.time(), detail=detail)
            log(f"  SCOPE VIOLATION, changes rejected: {'; '.join(ch['violations'])[:200]}")
            return "implementation"
        if not ch["files"]:
            pool.release(w, True, res, time.monotonic() - t0)
            db.finish_dev_attempt(aid, status="no-change", failure_kind="implementation", error_class="no-change",
                                  error="the worker made no changes", actual_model=model, repairs=round_,
                                  finished_at=time.time(), detail=detail)
            log("  the worker made no changes")
            return "implementation"
        v = verify(gitdir, rdir, spec["verify"], settings.dev_verify_timeout)
        rec["verification"] = v
        if v["passed"]:
            pool.release(w, True, res, time.monotonic() - t0)
            commit, patch_path = _commit(gitdir, wt, plan, spec, w, model, aid, v, adir)
            detail["patch"] = str(patch_path)
            detail["branch"] = f"nicks/{plan['id']}/{spec['id']}"
            db.finish_dev_attempt(aid, status="verified", actual_model=model, repairs=round_, commit_sha=commit,
                                  patch_sha256=_sha(patch_path.read_bytes()), finished_at=time.time(), detail=detail)
            # Verified in its OWN worktree only: queued for the integration queue (integrate_task), which decides
            # whether it holds on the canonical state. Dependants wait for `verified` (= integrated).
            db.set_dev_task(plan["id"], spec["id"], "implemented", None, result={
                "attempt": aid, "commit": commit, "start": start, "gitdir": str(gitdir), "worktree": str(wt),
                "branch": detail["branch"], "patch": str(patch_path), "worker": w.route, "model": model,
                "files": [f["path"] for f in ch["files"]], "implemented_at": time.time()})
            log(f"  {spec['id']} IMPLEMENTED: {len(ch['files'])} file(s) changed, {len(v['runs'])} verification "
                "command(s) passed in its own worktree; queued for integration")
            return "implemented"
        failed = next(r for r in v["runs"] if r["exit_code"] != 0)
        log(f"  verification failed: `{failed['command']}` exit {failed['exit_code']}")
        broken = verification_broken(failed)
        if broken:
            # The check itself cannot run here: no repair round and no other model can fix that (Round 1: three correct
            # patches failed `git diff ... | awk` with exit 127). Back to Claude; nothing charged to the worker.
            pool.release(w, True, res, time.monotonic() - t0)
            (adir / "failed.patch").write_text(ch["patch"], encoding="utf-8")
            db.finish_dev_attempt(aid, status="verify-broken", failure_kind="environment", error_class="verification-env",
                                  error=f"`{failed['command']}` cannot run in the verification environment: {broken}"[:600],
                                  actual_model=model, repairs=round_, finished_at=time.time(), detail=detail)
            log(f"  the verification command cannot run here ({broken}); not an implementation failure")
            return "environment"
        if round_ < settings.dev_max_repairs:
            prompt = base_prompt + REPAIR_PROMPT.format(n=round_ + 1, command=failed["command"],
                                                        exit_code=failed["exit_code"], output=failed["output_tail"][-3000:])
            log(f"  repair round {round_ + 1} with the same worker")
    pool.release(w, True, res, time.monotonic() - t0)
    (adir / "failed.patch").write_text(ch["patch"], encoding="utf-8")
    db.finish_dev_attempt(aid, status="verify-failed", failure_kind="implementation", error_class="verification",
                          error=f"`{failed['command']}` exit {failed['exit_code']} after {settings.dev_max_repairs} repair round(s)",
                          actual_model=model, repairs=settings.dev_max_repairs, finished_at=time.time(), detail=detail)
    return "implementation"


def _preserve_partial(gitdir: Path, wt: Path, start: str, adir: Path) -> dict | None:
    """EVIDENCE ONLY: what a worker had changed when it timed out or failed on infrastructure. The attempt is still
    discarded (never integrated, never resumed); this keeps a record of how far it got (V1D: a slow local model's
    correct partial edit was otherwise visible only in a scratch worktree)."""
    try:
        g(gitdir, wt, "add", "-A")
        patch = g(gitdir, wt, "diff", "--cached", "--binary", start)
        files = g(gitdir, wt, "diff", "--cached", "--name-only", start).split()
    except DevError:
        return None
    if not patch.strip():
        return None
    p = adir / "partial.patch"
    p.write_text(patch, encoding="utf-8")
    return {"patch": str(p), "files": files}


def _commit(gitdir, wt, plan, spec, w, model, aid, v, adir) -> tuple[str, Path]:
    msg = (f"{spec['id']}: {spec['goal']}\n\nImplemented by a Council of Nicks worker and verified by the engine.\n\n"
           f"Nicks-Plan: {plan['id']}\nNicks-Task: {spec['id']}\nNicks-Attempt: {aid}\n"
           f"Nicks-Worker-Route: {w.route}\nNicks-Worker-Model: {model or 'unknown'}\n"
           + "".join(f"Nicks-Verified: {r['command']} (exit 0)\n" for r in v["runs"]))
    name = f"{WORKER_IDENTITY[0]} ({model or w.route})"
    g(gitdir, wt, "-c", f"user.name={name}", "commit", "--quiet", "--no-verify", "-F", "-", input_=msg.encode())
    commit = g(gitdir, None, "rev-parse", "HEAD").strip()
    g(gitdir, None, "branch", "-f", f"nicks/{plan['id']}/{spec['id']}", commit)
    patch = adir / "change.patch"
    patch.write_text(g(gitdir, None, "format-patch", "-1", "--stdout", "--binary", commit), encoding="utf-8")
    return commit, patch


# --------------------------------------------------------------------------- whole-plan execution (V1B)

TERMINAL_BAD = ("needs-decomposition", "failed", "blocked")


def waiting_on(db, plan_id: str) -> dict[str, list[str]]:
    """Pending tasks that cannot run because a (transitive) prerequisite needs decomposition, failed or is blocked."""
    tasks = {t["task_id"]: t for t in db.dev_tasks(plan_id)}
    memo: dict[str, list[str]] = {}

    def bad(tid, seen=()):
        if tid in memo:
            return memo[tid]
        out = []
        for d in tasks[tid]["spec"]["depends_on"]:
            if d not in tasks or d in seen:
                continue
            if tasks[d]["status"] in TERMINAL_BAD:
                out.append(d)
            out += bad(d, seen + (tid,))
        memo[tid] = sorted(set(out))
        return memo[tid]
    return {tid: bad(tid) for tid, t in tasks.items() if t["status"] == "pending" and bad(tid)}


def plan_status(db, plan_id: str) -> str:
    """Derived from task states, except the post-implementation stages that are recorded on the plan."""
    plan = db.dev_plan(plan_id)
    live = [t for t in db.dev_tasks(plan_id) if t["status"] != "superseded"]
    stored = plan.get("status") or "planned"
    if live and all(t["status"] == "verified" for t in live):
        return stored if stored in ("integration-verified", "integration-failed", "in-review", "complete", "rejected",
                                    "incomplete", "blocked-by-tests", "stale-review", "awaiting-judgment") else "implemented"
    if any(t["status"] == "failed" for t in live):
        return "blocked"
    if any(t["status"] == "needs-decomposition" for t in live):
        return "needs-decomposition"
    if any(t["status"] == "blocked" for t in live):
        return "blocked"
    return "executing" if any(t["status"] in ("verified", "implemented", "integrating") for t in live) else "planned"


def scopes_overlap(a: list[str], b: list[str], files: list[str]) -> bool:
    """Could two tasks' permitted scopes ever touch the same path? Deterministic and conservative: yes if one pattern
    matches the other, a directory scope contains the other, an existing file matches both, or both wildcard into
    the same literal directory."""
    def lit_dir(p):
        head = re.split(r"[*?\[]", p, maxsplit=1)[0]
        return head.rsplit("/", 1)[0] if "/" in head else ""
    for x in a:
        for y in b:
            if x == y or fnmatch.fnmatch(x, y) or fnmatch.fnmatch(y, x):
                return True
            xd, yd = x.rstrip("/"), y.rstrip("/")
            if y.startswith(xd + "/") or x.startswith(yd + "/"):
                return True
            if any(fnmatch.fnmatch(f, x) and fnmatch.fnmatch(f, y) for f in files):
                return True
            wild = lambda p: any(c in p for c in "*?[")
            if wild(x) and wild(y) and lit_dir(x) == lit_dir(y):
                return True
    return False


def _tracked_files(db, plan: dict) -> list[str]:
    gitdir, _, head = ensure_state(db, plan)
    return g(gitdir, None, "ls-tree", "-r", "--name-only", head).splitlines()


def integrate_task(db, settings, plan: dict, task: dict, log=_log) -> str:
    """The INTEGRATION QUEUE step for one `implemented` task (main thread only, serialized).

    Cherry-picks the worker's verified commit onto the canonical state (a fast-forward when it was built on the current
    head). A textual conflict is detected, never resolved by overwriting (no strategy options). The candidate tree must
    then pass the same gate whatever the completion order: the task's own verification (already run on this exact
    tree for a fast-forward), every verified task's verification, and every integration_verify command already green
    on the canonical head, all in a network-less jail. Success: the canonical head advances (journal first,
    atomically) and the task is `verified`. Failure: the state is rolled back, the worker's patch and evidence are
    kept, and the task is re-implemented against the latest verified state (bounded by dev_max_integration_retries)
    or returned to Claude."""
    tid, spec, r = task["task_id"], task["spec"], task["result"]
    with STATE_LOCK:
        plan = db.dev_plan(plan["id"])
        gitdir, wt, head = ensure_state(db, plan)
        db.set_dev_task(plan["id"], tid, "integrating", None, result=r)
        _event(plan, "integrating", task=tid, onto=head, commit=r["commit"], built_on=r["start"])
        n_int = sum(1 for a in db.dev_attempts(plan["id"], tid) if a["engine"] == "integration")
        idir = Path(plan["dir"]) / "tasks" / tid / f"integration-{n_int + 1}"
        idir.mkdir(parents=True, exist_ok=True)
        iid = f"{plan['id']}-{tid}-i{n_int + 1}-{secrets.token_hex(2)}"
        db.start_dev_attempt({"id": iid, "plan_id": plan["id"], "task_id": tid, "n": len(db.dev_attempts(plan["id"], tid)) + 1,
                              "worker": "orchestrator", "engine": "integration", "status": "running",
                              "started_at": time.time(), "pid": os.getpid(), "dir": str(idir)})
        g(gitdir, wt, "fetch", "--quiet", "--no-tags", r["gitdir"], r["commit"])
        detail = {"onto": head, "commit": r["commit"], "built_on": r["start"], "patch": r["patch"]}
        # The gate is the same whatever the completion order (see "Integration order" in the README): the candidate
        # tree must pass every verified task's checks and every integration_verify command already green on the
        # canonical head, so the canonical state can never regress, and a regression is charged to the task that
        # introduces it rather than to whichever task happens to integrate after it.
        prior = [t for t in db.dev_tasks(plan["id"]) if t["status"] == "verified"]
        own = list(spec["verify"])
        green = _green(db, settings, plan, gitdir, wt, head, idir)
        task_cmds: list[str] = []
        for t in prior:
            task_cmds += [c for c in t["spec"]["verify"] if c not in task_cmds]
        detail.update(reverified_with=[t["task_id"] for t in prior if _not_ancestor(gitdir, t, r["start"])],
                      regression_with=[t["task_id"] for t in prior], green_before=green)
        fail = None
        if r["start"] == head:
            g(gitdir, wt, "checkout", "--quiet", "--detach", "--force", r["commit"])
            new = r["commit"]
            detail["mode"] = "fast-forward (built on the current head: the worker's verification ran on this exact tree)"
            detail["own_verified"] = own                      # passed on this exact tree in the worker's worktree
            gate = _dedupe([c for c in green + task_cmds if c not in own])
        else:
            pick = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", f"--git-dir={gitdir}", f"--work-tree={wt}",
                                   "cherry-pick", "--allow-empty", "--keep-redundant-commits", r["commit"]],
                                  capture_output=True, text=True, env=_git_env(), cwd=str(wt))
            if pick.returncode != 0:
                conflicted = g(gitdir, wt, "diff", "--name-only", "--diff-filter=U", check=False).split()
                g(gitdir, wt, "cherry-pick", "--abort", check=False)
                g(gitdir, wt, "reset", "--quiet", "--hard", head)
                fail = ("conflict", f"conflicts with verified work in {', '.join(conflicted) or 'the tree'}; "
                                    f"built on {r['start'][:10]}, canonical head is {head[:10]}")
                (idir / "conflict.txt").write_text(pick.stdout + pick.stderr, encoding="utf-8")
            else:
                new = g(gitdir, None, "rev-parse", "HEAD").strip()
                detail["mode"] = "cherry-pick onto newer verified work"
            gate = _dedupe(own + green + task_cmds)
        if fail is None:
            detail["gate"] = gate
            if gate:                           # every check runs, so the evidence names everything that broke
                v = verify(gitdir, idir, gate, settings.dev_verify_timeout, keep_going=True)
                detail["verification"] = v
                if not v["passed"]:
                    bad = [x for x in v["runs"] if x["exit_code"] != 0]
                    g(gitdir, wt, "reset", "--quiet", "--hard", head)
                    kind = "combined-verification" if any(x["command"] in own for x in bad) and \
                        r["start"] != head else "regression"
                    fail = (kind, "; ".join(f"`{x['command']}` exit {x['exit_code']}" for x in bad)[:400]
                            + (" on the combined state" if kind == "combined-verification" else
                               " (passed on the canonical state before this task)"))
        if fail is None:
            # Ratchet: integration_verify commands that were not yet green and pass now become gates from here on.
            pending = [c for c in plan["manifest"].get("integration_verify") or [] if c not in green]
            newly = []
            if pending:
                vp = verify(gitdir, idir, pending, settings.dev_verify_timeout, keep_going=True, prefix="ratchet")
                newly = [x["command"] for x in vp["runs"] if x["exit_code"] == 0]
            detail["newly_green"] = newly
            pdetail = dict(db.dev_plan(plan["id"]).get("detail") or {}, green={"head": new, "commands": green + newly})
            _crash_point("integration-before-journal")       # git moved, journal not yet: rolled back on resume
            with db.tx():                      # journal first: the canonical head and the task move together
                db.set_dev_plan(plan["id"], head=new, detail=pdetail)
                db.set_dev_task(plan["id"], tid, "verified", None,
                                result=dict(r, integration_commit=new, integrated_onto=head, integrated_at=time.time(),
                                            verified_at=time.time()))
                db.finish_dev_attempt(iid, status="verified", commit_sha=new, finished_at=time.time(), detail=detail)
            _crash_point("integration-after-journal")        # journal done, branch ref not: reconciled on resume
            g(gitdir, None, "branch", "-f", f"nicks/{plan['id']}/cumulative", new)
            _event(plan, "integrated", task=tid, cumulative=new, mode=detail["mode"])
            log(f"  {tid} INTEGRATED -> canonical {new[:10]} ({detail['mode'].split(' (')[0]})")
            return "integrated"
        kind, why = fail
        db.finish_dev_attempt(iid, status="integration-failed", failure_kind="integration", error_class=kind,
                              error=why[:600], finished_at=time.time(), detail=detail)
        _event(plan, "integration-failed", task=tid, failure=kind, reason=why)
        n_fail = sum(1 for a in db.dev_attempts(plan["id"], tid) if a["failure_kind"] == "integration")
        if n_fail <= settings.dev_max_integration_retries:
            db.set_dev_task(plan["id"], tid, "pending", f"integration {kind}: {why}; re-implementing against the "
                            "latest verified state (worker patch preserved)")
            log(f"  {tid} integration {kind}: {why} -> re-implement against the latest verified state")
            return "retry"
        db.set_dev_task(plan["id"], tid, "needs-decomposition",
                        f"integration {kind} after {n_fail} attempt(s): {why}; Claude must resolve it "
                        "(`nicks dev amend`); the worker patches are preserved")
        log(f"  {tid} integration {kind} again -> returned to Claude")
        return "conflict"


def _dedupe(cmds: list[str]) -> list[str]:
    return list(dict.fromkeys(cmds))


def _green(db, settings, plan: dict, gitdir: Path, wt: Path, head: str, idir: Path) -> list[str]:
    """The integration_verify commands known to pass on the canonical head. Recorded with the head after every
    integration; evaluated on the head itself (each command independently) when no record for it exists, e.g. at
    the plan's first integration. A command that fails on the base (often: it checks the feature being built) is
    not a gate until some integration makes it pass."""
    cmds = list(plan["manifest"].get("integration_verify") or [])
    if not cmds:
        return []
    rec = (db.dev_plan(plan["id"]).get("detail") or {}).get("green")
    if rec and rec.get("head") == head:
        return [c for c in rec["commands"] if c in cmds]
    g(gitdir, wt, "reset", "--quiet", "--hard", head)
    v = verify(gitdir, idir, cmds, settings.dev_verify_timeout, keep_going=True, prefix="baseline")
    green = [x["command"] for x in v["runs"] if x["exit_code"] == 0]
    detail = dict(db.dev_plan(plan["id"]).get("detail") or {}, green={"head": head, "commands": green,
                                                                     "evaluated": True})
    db.set_dev_plan(plan["id"], detail=detail)
    _event(plan, "baseline", head=head, green=green, red=[c for c in cmds if c not in green])
    return green


def run_preflight(db, settings, plan: dict, task: dict, head: str) -> dict:
    """Acceptance Check Preflight for one task on `head` (cached per task and head)."""
    from .preflight import preflight_task
    tdir = Path(plan["dir"]) / "tasks" / task["task_id"]
    # Keyed by head AND the checks themselves: a revision that changes verify/scope must be preflighted again
    # (found in V1D Round 2: the revised commands were never checked; the pre-revision result was reused).
    key = _sha(json.dumps({"verify": task["spec"]["verify"], "scope": task["spec"]["scope"]}, sort_keys=True))[:10]
    cached = tdir / f"preflight-{head[:12]}-{key}.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))
    gitdir, _, _ = ensure_state(db, plan)
    with STATE_LOCK:
        pf = preflight_task(gitdir, head, task["spec"], settings.dev_verify_timeout, None)
    tdir.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(pf, indent=2), encoding="utf-8")
    return pf


def preflight_gate(db, settings, plan: dict, task: dict, head: str, log=_log) -> bool:
    """True if the task may be dispatched. A demonstrably broken or non-discriminating check sends the task back to
    Claude BEFORE any worker inference; it is a specification/verification problem, never a worker failure."""
    from .preflight import summary
    pf = run_preflight(db, settings, plan, task, head)
    _event(plan, "preflight", task=task["task_id"], head=head, status=pf["status"],
           verdicts={r["command"][:80]: r["verdict"] for r in pf["commands"]})
    if pf["status"] == "ok":
        warn = [w for r in pf["commands"] for w in r["warnings"]]
        log(f"preflight {task['task_id']}: {summary(pf)}" + (f" ({len(warn)} warning(s))" if warn else ""))
        return True
    db.set_dev_task(plan["id"], task["task_id"], "needs-decomposition",
                    ("preflight rejected the task's verification (no worker was run): " + " | ".join(pf["reasons"]))[:500])
    _event(plan, "needs-decomposition", task=task["task_id"], cause="preflight")
    log(f"preflight {task['task_id']}: {summary(pf)} -> back to Claude, no worker dispatched")
    write_plan_md(db, plan["id"])
    return False


_crash_hits: Counter = Counter()


def _crash_point(name: str) -> None:
    """Test hook (NICKS_CRASH_AT=<name>[:N]): SIGKILL the orchestrator the N-th time (default 1st) it reaches this
    point, to prove crash-safe integration."""
    spec = os.environ.get("NICKS_CRASH_AT") or ""
    want, _, nth = spec.partition(":")
    if want == name:
        _crash_hits[name] += 1
        if _crash_hits[name] == int(nth or 1):
            import signal
            os.kill(os.getpid(), signal.SIGKILL)


def _not_ancestor(gitdir: Path, t: dict, start: str) -> bool:
    """True if verified task t's integrated commit is NOT already contained in `start` (the worker never saw it)."""
    c = t["result"].get("integration_commit") or t["result"]["commit"]
    rc = subprocess.run(["git", f"--git-dir={gitdir}", "merge-base", "--is-ancestor", c, start],
                        capture_output=True, env=_git_env()).returncode
    return rc != 0


def integrate_pending(db, settings, plan: dict, log=_log) -> list[tuple[str, str]]:
    """Drain the integration queue in deterministic (plan) order."""
    out = []
    for t in db.dev_tasks(plan["id"]):
        if t["status"] == "implemented":
            out.append((t["task_id"], integrate_task(db, settings, plan, t, log)))
    if out:
        write_plan_md(db, plan["id"])
    return out


def execute_plan(db, settings, plan_ref: str | None = None, max_seconds: float | None = None, log=_log,
                 pool: WorkerPool | None = None) -> dict:
    """Dependency-aware parallel execution (V1C).

    Deterministic scheduler, no model involved: a task is dispatched only when every dependency is `verified`
    (integrated into the canonical state); up to `dev_parallel_workers` tasks run at once, never two whose permitted
    scopes overlap; each runs in its own isolated worktree built from the canonical head at dispatch. Finished tasks
    go through the serialized integration queue before anything depending on them becomes ready. Every decision is
    journaled (events.jsonl). A crash loses at most the in-flight attempts; on resume interrupted integrations are
    reconciled first and implemented-but-unintegrated work is integrated, never re-implemented."""
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    plan = db.dev_plan(plan_ref) if plan_ref else db.latest_dev_plan()
    if not plan:
        raise DevError(f"no plan matching {plan_ref!r}" if plan_ref else "no development plans yet")
    from .rework import check_deadline
    check_deadline(plan)
    from .policy import restore
    restore(settings, (plan.get("detail") or {}).get("execution_policy"))
    if not bwrap_path() or not settings.sandbox:
        raise SandboxUnavailable("the Development Engine requires the bubblewrap sandbox (workers can write)")
    if (why := critic_pending(plan) or debate_pending_(plan) or adversarial_pending(plan)):
        raise DevError(f"plan {plan['id']}: {why}")
    budget = settings.dev_execute_seconds if max_seconds is None else max_seconds
    deadline = min(time.time() + budget, (plan.get("detail", {}).get("rework") or {}).get("deadline", float("inf")))
    width = max(1, int(settings.dev_parallel_workers))
    executed: list[dict] = []
    integrations: list[tuple[str, str]] = []
    with plan_lock(plan):
        if (db.dev_plan(plan['id']).get('detail') or {}).get('competition', {}).get('status') not in (None, 'integrated'):
            raise DevError('this plan has a competing implementation in progress; resume `dev compete`')
        recover(db, plan["id"], log)
        ensure_state(db, plan)
        integrations += integrate_pending(db, settings, plan, log)       # finish work that survived a crash
        pool = pool or WorkerPool(db, settings, log)
        if isinstance(pool, WorkerPool):
            pool.require()        # default workflow without Luna/Haiku: stop here, never dispatch and retry
        stop = None
        running: dict = {}                                                # future -> task id
        with ThreadPoolExecutor(max_workers=width, thread_name_prefix="nicks-dev") as ex:
            while True:
                if stop is None and deadline - time.time() < 90:
                    stop = "paused"
                if stop is None:
                    plan = db.dev_plan(plan["id"])
                    busy = {tid: next(t for t in db.dev_tasks(plan["id"]) if t["task_id"] == tid)["spec"]["scope"]
                            for tid in running.values()}
                    files = _tracked_files(db, plan)
                    for t in ready_tasks(db, plan["id"]):
                        if len(running) >= width:
                            break
                        clash = [o for o, sc in busy.items() if scopes_overlap(t["spec"]["scope"], sc, files)]
                        if clash:
                            _event(plan, "deferred", task=t["task_id"], reason="scope overlaps running task(s)",
                                   with_=clash)
                            continue
                        head = plan.get("head") or plan["base_sha"]
                        if settings.dev_preflight and not preflight_gate(db, settings, plan, t, head, log):
                            continue                          # back to Claude before any worker inference
                        db.set_dev_task(plan["id"], t["task_id"], "running")
                        _event(plan, "dispatched", task=t["task_id"], head=head, concurrent=sorted(busy))
                        log(f"dispatch {t['task_id']} on {head[:10]}" + (f" (concurrently with {', '.join(sorted(busy))})"
                                                                         if busy else ""))
                        fut = ex.submit(_execute, db, settings, plan, t, pool, log, deadline, head)
                        running[fut] = t["task_id"]
                        busy[t["task_id"]] = t["spec"]["scope"]
                if not running:
                    break
                done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                for fut in done:
                    tid = running.pop(fut)
                    try:
                        r = fut.result()
                    except Exception as exc:                              # never lose the plan to a thread error
                        db.set_dev_task(plan["id"], tid, "pending", f"internal error: {exc!r}"[:500])
                        r = {"task": tid, "status": "pending", "outcome": "error"}
                    executed.append({k: r.get(k) for k in ("task", "status", "outcome")})
                    if r.get("outcome") in ("no-capacity", "paused", "attempts-exhausted") and stop is None:
                        stop = r["outcome"]                               # finish what runs; start nothing new
                integrations += integrate_pending(db, settings, db.dev_plan(plan["id"]), log)
        status = plan_status(db, plan["id"])
        if status in ("needs-decomposition", "blocked", "implemented", "executing", "planned"):
            db.set_dev_plan(plan["id"], status=status)
        _event(plan, "execute", executed=executed, integrations=integrations, status=status, stop=stop, width=width)
        write_plan_md(db, plan["id"])
    pending = [t["task_id"] for t in db.dev_tasks(plan["id"]) if t["status"] == "pending"]
    return {"plan": plan["id"], "status": status, "stop": stop, "executed": executed, "integrations": integrations,
            "waiting_on": waiting_on(db, plan["id"]), "pending": pending, "tasks": _task_summary(db, plan["id"])}


def integration_commands(plan: dict, db) -> list[str]:
    m = plan["manifest"]
    if m.get("integration_verify"):
        return list(m["integration_verify"])
    out: list[str] = []
    for t in db.dev_tasks(plan["id"]):
        if t["status"] == "verified":
            out += [c for c in t["spec"]["verify"] if c not in out]
    return out


def verify_integration(db, settings, plan: dict) -> dict:
    """Run the integration verification on the cumulative change (base + every verified task), engine-side."""
    live = [t for t in db.dev_tasks(plan["id"]) if t["status"] != "superseded"]
    if not live or any(t["status"] != "verified" for t in live):
        raise DevError("not every task is verified yet: " + ", ".join(f"{t['task_id']}={t['status']}" for t in live
                                                                       if t["status"] != "verified"))
    with plan_lock(plan):
        gitdir, wt, head = ensure_state(db, plan)
        g(gitdir, wt, "reset", "--quiet", "--hard", head)
        out = Path(plan["dir"]) / "integration" / (time.strftime("v%Y%m%d-%H%M%S-") + secrets.token_hex(2))
        out.mkdir(parents=True)                    # unique: two integrations within one second must not collide
        cmds = integration_commands(plan, db)
        v = verify(gitdir, out, cmds, settings.dev_verify_timeout)
        adv = dict((plan.get("detail") or {}).get("adversarial") or {})
        if v["passed"] and adv.get("status") == "frozen":
            from .testdesign import record_holdout, run_suite
            con = run_suite(plan, gitdir, head, "contract", settings)
            adv["contract_run"] = con
            if con["failed_accepted"] or not con["finished"]:
                v = dict(v, passed=False, adversarial_contract_failed=con["failed_accepted"] or ["(run did not finish)"])
            hold = run_suite(plan, gitdir, head, "holdout", settings)
            path, first = record_holdout(plan, hold)
            if first:
                adv["holdout_first_run"] = hold            # the historical first run, never overwritten
                adv["holdout_first_run_path"] = str(path)
            adv.setdefault("holdout_runs", []).append({"path": str(path), "head": head, "at": hold["at"],
                                                       "sha256": _sha(path.read_bytes()),
                                                       "failed": sorted(k for k, o in hold["results"].items()
                                                                        if o not in ("ok", "skipped"))})
        detail = dict(plan.get("detail") or {}, adversarial=adv) if adv else dict(plan.get("detail") or {})
        detail = dict(detail, integration={"head": head, "tree": g(gitdir, None, "rev-parse", head + "^{tree}").strip(),
                                           "passed": v["passed"], "dir": str(out),
                                           "commands": cmds, "runs": v["runs"]})
        db.set_dev_plan(plan["id"], status="integration-verified" if v["passed"] else "integration-failed", detail=detail)
        _event(plan, "integration", head=head, passed=v["passed"])
        write_plan_md(db, plan["id"])
    return {"head": head, **v, "commands": cmds, "dir": str(out)}


def judgment_blockers(settings=None) -> list[str]:
    """Billing-only preflight, respecting provider authorization rather than role/provider assignments."""
    from . import billing
    from .config import load_settings
    from .policy import allows
    settings = settings or load_settings()
    checks = [billing.check(p) for p in ("openai", "anthropic") if allows(settings, p)]
    if any(e.eligible for e in checks):
        return []
    return ["Nickquisition (no authorized eligible subscription): " + "; ".join(e.summary() for e in checks)]


def mark_awaiting_judgment(db, plan: dict, blockers: list[str]) -> None:
    detail = dict(plan.get("detail") or {}, awaiting_judgment={"since": time.time(), "blockers": blockers})
    db.set_dev_plan(plan["id"], status="awaiting-judgment", detail=detail)
    _event(plan, "awaiting-judgment", blockers=blockers)
    write_plan_md(db, plan["id"])


VERDICT_STATUS = {"APPROVED": "complete", "REJECTED": "rejected", "BLOCKED": "blocked-by-tests",
                  "INCOMPLETE": "incomplete", "INTEGRITY-FAILURE": "incomplete"}


def conclude(db, plan_ref: str) -> dict:
    """Record The Nickquisition's authoritative verdict on the plan's final review. Only APPROVED for the CURRENT
    cumulative state makes a plan `complete`; a rejected, blocked or incomplete verdict never does."""
    plan = db.dev_plan(plan_ref)
    job_id = plan.get("review_job")
    if not job_id:
        raise DevError("the plan has no final review yet (`nicks dev integrate`)")
    job = db.get_job(job_id)
    v = db.one("SELECT * FROM nq_verdicts WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,))
    if not v:
        from .config import load_settings
        from .policy import restore
        blockers = judgment_blockers(restore(load_settings(), (plan.get("detail") or {}).get("execution_policy")))
        if blockers:                   # implementation complete and preserved; judgment pending, never approved
            mark_awaiting_judgment(db, plan, blockers)
            return {"plan": plan["id"], "status": "awaiting-judgment", "verdict": None, "tally": None, "job": job_id,
                    "report": None, "blockers": blockers}
        raise DevError(f"The Nickquisition has not ruled on job {job_id} yet")
    reviewed_head = (job["snapshot"] or {}).get("head_commit")
    status = VERDICT_STATUS.get(v["verdict"], "incomplete")
    if reviewed_head != plan.get("head"):
        status = "stale-review"                # the plan changed after the review: judge the current state again
    dest = Path(plan["dir"]) / "verdict"
    dest.mkdir(exist_ok=True)
    shutil.copyfile(v["report_path"], dest / f"{job_id}-r{v['round']}-verdict.md")
    shutil.copyfile(v["json_path"], dest / f"{job_id}-r{v['round']}-verdict.json")
    detail = dict(plan.get("detail") or {}, verdict={"job": job_id, "round": v["round"], "verdict": v["verdict"],
                                                    "tally": v["tally"], "report": v["report_path"],
                                                    "reviewed_head": reviewed_head})
    db.set_dev_plan(plan["id"], status=status, detail=detail)
    _event(plan, "concluded", verdict=v["verdict"], status=status, job=job_id)
    write_plan_md(db, plan["id"])
    try:                                       # terminal state: write the run report (recorded data only)
        from . import report
        report.write(db, plan["id"])
    except Exception as exc:                   # reporting never changes or blocks the verdict
        _log(f"report not written: {type(exc).__name__}: {exc}")
    return {"plan": plan["id"], "status": status, "verdict": v["verdict"], "tally": v["tally"], "job": job_id,
            "report": v["report_path"]}


def status_report(db, plan_ref: str | None = None) -> str:
    plan = db.dev_plan(plan_ref) if plan_ref else db.latest_dev_plan()
    if not plan:
        raise DevError("no development plans yet")
    tasks = db.dev_tasks(plan["id"])
    waits = waiting_on(db, plan["id"])
    st = plan_status(db, plan["id"])
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    L = [f"PLAN {plan['id']}  status: {st}  ({', '.join(f'{k} {v}' for k, v in sorted(counts.items()))})",
         f"repo {plan['repo']}  base {plan['base_sha'][:12]}  cumulative {(plan.get('head') or plan['base_sha'])[:12]}"]
    if plan.get("review_job"):
        L.append(f"final review job {plan['review_job']}" + (f"; verdict {plan['detail']['verdict']['verdict']}"
                                                          if (plan.get("detail") or {}).get("verdict") else ""))
    L.append("")
    if (plan.get("detail") or {}).get("awaiting_judgment"):
        L += ["AWAITING_JUDGMENT: " + "; ".join(plan["detail"]["awaiting_judgment"]["blockers"])[:400]]
    L.append(f"{'task':<10} {'status':<20} {'deps':<14} {'att':>3}  {'worker / model':<34} commit / reason")
    for t in tasks:
        atts = [a for a in db.dev_attempts(plan["id"], t["task_id"]) if a["engine"] != "integration"]
        r = t.get("result") or {}
        who = f"{r.get('worker', '')} / {r.get('model', '')}" if r else ""
        why = ((r.get("integration_commit") or r.get("commit") or "")[:10] if r else "") or (t.get("reason") or "")
        if t["task_id"] in waits:
            why = f"waiting: prerequisite {', '.join(waits[t['task_id']])} not verified"
        L.append(f"{t['task_id']:<10} {t['status']:<20} {','.join(t['spec']['depends_on']) or '-':<14} {len(atts):>3}  "
                 f"{who[:34]:<34} {why[:90]}")
    timeline = sorted((a for a in db.dev_attempts(plan["id"]) if a["engine"] != "integration" and a["finished_at"]),
                      key=lambda a: a["started_at"])
    if timeline:
        t0 = timeline[0]["started_at"]
        L += ["", "worker timeline (s from first dispatch):"] + [
            f"  {a['task_id']:<8} {a['started_at'] - t0:7.1f} -> {a['finished_at'] - t0:7.1f}  {a['status']:<15} {a['worker']}"
            for a in timeline]
    nxt = {"planned": "nicks dev execute", "executing": "nicks dev execute",
           "needs-decomposition": "split the failed task: nicks dev amend <plan> --replace <task> --reason ... --file ...",
           "blocked": "human attention: a task failed at the decomposition limit or its worktree could not be built",
           "implemented": "nicks dev integrate", "integration-failed": "add a fix task: nicks dev amend <plan> --add ...",
           "integration-verified": "nicks dev integrate (runs the final Council review)",
           "in-review": "The Nickquisition on the final review job, then nicks dev conclude",
           "awaiting-judgment": "implementation complete and preserved; The Nickquisition is blocked by the billing "
                                "gate (`nicks billing`); when eligible: run it on the final review job, then conclude",
           "stale-review": "nicks dev integrate (the plan changed after its review)",
           "complete": "done: APPROVED by The Nickquisition", "rejected": "rejected by The Nickquisition: amend the plan",
           "incomplete": "the verdict was INCOMPLETE: resolve and convene again", "blocked-by-tests": "fix the failing tests"}
    L += ["", f"next: {nxt.get(st, '-')}"]
    return "\n".join(L)


# --------------------------------------------------------------------------- integration (what gets judged)

def _topo_verified(db, plan_id: str) -> list[dict]:
    """All verified tasks, dependencies first, otherwise in plan order."""
    tasks = [t for t in db.dev_tasks(plan_id) if t["status"] == "verified"]
    by_id, out, seen = {t["task_id"]: t for t in tasks}, [], set()

    def visit(t):
        if t["task_id"] in seen:
            return
        seen.add(t["task_id"])
        for d in t["spec"]["depends_on"]:
            if d in by_id:
                visit(by_id[d])
        out.append(t)
    for t in tasks:
        visit(t)
    return out


def integrate(db, plan: dict, task_id: str | None = None) -> dict:
    """The ACCUMULATED implementation to judge, always against the plan's original base commit.

    With a task: that task plus every verified dependency (its own clone already holds them, on top of the base).
    Without: every verified task of the plan, cherry-picked in dependency order into a fresh integration clone.
    The Nickquisition therefore never judges a task's increment in isolation: a defect introduced by T1 stays in
    the diff it reviews after T2."""
    tasks = {t["task_id"]: t for t in db.dev_tasks(plan["id"])}
    if task_id:
        t = tasks.get(task_id)
        if not t or t["status"] != "verified":
            raise DevError(f"task {task_id!r} of plan {plan['id']} is not verified; nothing to review")
        chain = [d["task_id"] for d in _dep_order(db, plan["id"], task_id)] + [task_id]
        r = t["result"]
        gitdir, wt, head = Path(r["gitdir"]), Path(r["worktree"]), r["commit"]
    elif plan.get("head") or any(t["status"] == "verified" for t in tasks.values()):
        # V1B: the plan's cumulative verified state IS the accumulated implementation.
        gitdir, wt, head = ensure_state(db, plan)
        chain = [t["task_id"] for t in _topo_verified(db, plan["id"])]
        if not chain:
            raise DevError(f"plan {plan['id']} has no verified task to review")
    else:
        order = _topo_verified(db, plan["id"])
        if not order:
            raise DevError(f"plan {plan['id']} has no verified task to review")
        chain = [t["task_id"] for t in order]
        last = order[-1]
        if [d["task_id"] for d in _dep_order(db, plan["id"], last["task_id"])] + [last["task_id"]] == chain:
            r = last["result"]                     # the last task's clone already contains everything
            gitdir, wt, head = Path(r["gitdir"]), Path(r["worktree"]), r["commit"]
        else:
            idir = Path(plan["dir"]) / "integration" / time.strftime("i%Y%m%d-%H%M%S")
            idir.mkdir(parents=True)
            gitdir, wt = idir / "git", idir / "worktree"
            p = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "clone", "--quiet", "--no-checkout",
                                f"--separate-git-dir={gitdir}", plan["repo"], str(wt)],
                               capture_output=True, text=True, env=_git_env())
            if p.returncode != 0:
                raise DevError(f"cannot clone {plan['repo']}: {p.stderr.strip()[:400]}")
            g(gitdir, None, "config", "user.name", "Council of Nicks integration")
            g(gitdir, None, "config", "user.email", WORKER_IDENTITY[1])
            g(gitdir, None, "remote", "remove", "origin", check=False)
            g(gitdir, wt, "checkout", "--quiet", "--detach", "--force", plan["base_sha"])
            for t in order:
                r = t["result"]
                g(gitdir, wt, "fetch", "--quiet", "--no-tags", r["gitdir"], r["commit"])
                pick = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", f"--git-dir={gitdir}", f"--work-tree={wt}",
                                       "cherry-pick", "--allow-empty", "--keep-redundant-commits", r["commit"]],
                                      capture_output=True, text=True, env=_git_env(), cwd=str(wt))
                if pick.returncode != 0:
                    raise DevError(f"verified task {t['task_id']} does not integrate cleanly: {pick.stderr.strip()[:300]}")
            head = g(gitdir, None, "rev-parse", "HEAD").strip()
            g(gitdir, None, "branch", "-f", f"nicks/{plan['id']}/integration", head)
    anc = subprocess.run(["git", f"--git-dir={gitdir}", "merge-base", "--is-ancestor", plan["base_sha"], head],
                         capture_output=True, env=_git_env())
    if anc.returncode != 0:
        raise DevError(f"the plan base {plan['base_sha'][:12]} is not an ancestor of {head[:12]}; refusing to review")
    return {"gitdir": str(gitdir), "worktree": str(wt), "base": plan["base_sha"], "head": head, "tasks": chain}


def review_verification(db, plan: dict, head: str) -> dict:
    """Freeze existing engine records into the job packet, not inaccessible host paths.

    This is evidence delivery only: no tests, decisions or approvals are generated.
    Older records without log digests remain explicitly unsealed historical data.
    """
    plan = db.dev_plan(plan['id']) or plan
    detail = plan.get('detail') or {}
    result = {'reviewed_head': head, 'integration': None, 'adversarial_runs': []}
    integration = detail.get('integration')
    if integration:
        record = json.loads(json.dumps(integration))
        record['relation'] = 'current' if record['head'] == head else 'historical'
        for run in record['runs']:
            path = Path(run['log'])
            if not path.is_file() or path.is_symlink():
                raise DevError('missing verification log: ' + str(path))
            raw = path.read_bytes()
            digest = _sha(raw)
            if run.get('log_sha256') and run['log_sha256'] != digest:
                raise DevError('verification log changed after execution: ' + str(path))
            run['integrity'] = 'verified' if run.get('log_sha256') else 'legacy: no execution-time digest'
            run['delivered_sha256'] = digest
            run['full_log'] = raw.decode('utf-8')
        result['integration'] = record
    else:
        result['limitation'] = 'No integration verification record; unavailable evidence is not a pass.'
    adv = detail.get('adversarial') or {}
    records = [r for r in (adv.get('contract_run'), adv.get('holdout_first_run')) if r]
    # The first holdout remains immutable. Deliver subsequent runs too, including
    # their exact revisions, rather than presenting only the pre-correction failure.
    for ref in adv.get('holdout_runs', []):
        path = Path(ref['path'])
        if not path.is_file() or path.is_symlink():
            raise DevError('missing adversarial verification record: ' + str(path))
        raw = path.read_bytes()
        if ref.get('sha256') and _sha(raw) != ref['sha256']:
            raise DevError('adversarial verification record changed after execution: ' + str(path))
        record = json.loads(raw)
        if record.get('head') != ref['head']:
            raise DevError('adversarial verification revision mismatch: ' + str(path))
        if record not in records:
            records.append(record)
    for record in records:
        result['adversarial_runs'].append({**record, 'relation': 'current' if record['head'] == head else 'historical'})
    return result


def review_brief(db, plan: dict, tasks: list[str]) -> str:
    plan = db.dev_plan(plan["id"]) or plan     # fresh detail: a caller's plan may predate the integration runs
    specs = {t["task_id"]: t for t in db.dev_tasks(plan["id"])}
    out = [f"Development Engine plan {plan['id']}: the ACCUMULATED change of task(s) {', '.join(tasks)} against the "
           f"plan's original base {plan['base_sha'][:12]}, implemented by Council workers and verified per task.",
           f"Feature: {plan['manifest'].get('feature', '')}"]
    if (plan.get('detail') or {}).get('rework'):
        from .rework import brief
        out.append(brief(plan['detail']['rework']))
        out.append('Preserved acceptance obligations: ' + json.dumps(plan['detail']['rework']['obligations'], indent=2))
    for tid in tasks:
        t = specs[tid]
        r = t.get("result") or {}
        out.append(f"Task {tid} ({r.get('model') or r.get('worker') or '?'}): {t['spec']['goal']}")
        out += [f"  - acceptance: {a}" for a in t["spec"]["acceptance"]]
        out.append(f"  - permitted scope: {', '.join(t['spec']['scope'])}")
        if t['spec'].get('acceptance_history'):
            out.append('Prior acceptance obligations for this correction (not superseded by its narrower goal):\n'
                       + json.dumps(t['spec']['acceptance_history'], indent=2, ensure_ascii=False))
            out.append('Check changed/deleted tests against these obligations and the correction delta below. '
                       'Equivalent test rewrites are allowed; passing tests or reviewer agreement alone do not '
                       'establish retained coverage. Record current test locations or the exact unresolved gap.')
            if r.get('gitdir') and r.get('start') and r.get('commit'):
                from .snapshot import _exclude_pathspecs, redact
                delta = g(Path(r['gitdir']), None, 'diff', '--no-color', '--no-ext-diff', '--no-textconv',
                          r['start'], r['commit'], *_exclude_pathspecs())
                delta, redactions = redact(delta)
                out.append(f"Correction delta {r['start']} -> {r['commit']} (historical changes, not a new verdict; "
                           f"SHA256 {_sha(delta)}, standard secret-path filtering/redactions {redactions}):\n{delta}")
            else:
                out.append('Correction delta unavailable: no verified start/commit evidence. No preservation claim is made.')
    from .testdesign import evidence_brief, test_source_for_judges
    adv = evidence_brief(plan)
    if adv:
        out.append(adv)
        src = test_source_for_judges(plan) if (plan.get("detail") or {}).get("adversarial", {}).get("status") == "frozen" else ""
        if src:
            out.append("Adversarial test source (frozen):\n" + src)
    if (plan.get('detail') or {}).get('diagnostics'):
        from .diagnostic import history
        out.append(history(db, plan))
    return "\n".join(out)


# --------------------------------------------------------------------------- results for Claude

def _finish(db, plan, tid, outcome, aid, log) -> dict:
    task = next(t for t in db.dev_tasks(plan["id"]) if t["task_id"] == tid)
    path = write_result(db, plan, task)
    write_plan_md(db, plan["id"])
    log(f"task {tid}: {outcome} -> {path}")
    return {"plan": plan["id"], "task": tid, "status": task["status"], "outcome": outcome, "attempt": aid,
            "result_md": str(path), "result": task.get("result"), "tasks": _task_summary(db, plan["id"])}


def write_result(db, plan, task) -> Path:
    tid = task["task_id"]
    tdir = Path(plan["dir"]) / "tasks" / tid
    tdir.mkdir(parents=True, exist_ok=True)
    out = [f"# Task `{tid}` of plan `{plan['id']}`: {task['status'].upper()}", "",
           f"**Goal:** {task['spec']['goal']}", ""]
    if task.get("reason"):
        out += [f"Reason: {task['reason']}", ""]
    res = task.get("result") or {}
    if task["status"] == "verified":
        out += [f"- Implemented by: `{res['worker']}` (actual model **{res.get('model') or 'unknown'}**)",
                f"- Commit `{res['commit']}` on branch `{res['branch']}` in the private clone `{res['worktree']}`",
                f"- Patch: `{res['patch']}`",
                f"- Files changed: {', '.join(f'`{f}`' for f in res['files'])}", "",
                "Bring it into your repository (nothing was pushed or merged):",
                f"`git -C {plan['repo']} fetch {res['gitdir']} {res['branch']}`, then inspect `FETCH_HEAD`.", "",
                f"Judge it: `nicks dev review {plan['id']} {tid}` (this task plus its dependencies, against the plan "
                "base) or `nicks dev review " + plan['id'] + "` (every verified task), then The Nickquisition.", ""]
    elif task["status"] == "needs-decomposition":
        out += ["The Council could not implement this task within its budget. Decompose it into smaller tasks:",
                f"`nicks dev amend {plan['id']} --replace {tid} --file subtasks.json`", ""]
    out += ["## Attempts", ""]
    for a in db.dev_attempts(plan["id"], tid):
        out += [f"### Attempt {a['n']}: `{a['worker']}` -> **{a['status']}**"
                + (f" ({a['failure_kind']} failure)" if a["failure_kind"] else ""), "",
                f"- Actual model: {a['actual_model'] or 'unknown'}; repair rounds: {a['repairs'] or 0}; dir `{a['dir']}`"]
        if a["error"]:
            out.append(f"- Error: {a['error']}")
        for r in (a["detail"] or {}).get("rounds", []):
            files = ", ".join(f"{f['status']} `{f['path']}`" for f in r.get("files", [])) or "none"
            out.append(f"- Round {r['round']}: worker {'finished' if r['worker_ok'] else 'failed: ' + str(r.get('error_class'))}; "
                       f"changed: {files}")
            if r.get("partial"):
                out.append(f"  - unfinished work at the failure, kept as evidence only (discarded): "
                           f"`{r['partial']['patch']}` ({', '.join(r['partial']['files'])})")
            for vr in (r.get("verification") or {}).get("runs", []):
                out.append(f"  - `{vr['command']}` -> exit {vr['exit_code']} ({vr['seconds']}s), log `{vr['log']}`")
            resp = Path(r["response"]) if r.get("response") else None
            if resp and resp.exists() and resp.read_text(encoding="utf-8").strip():
                out += ["", "  Worker's final message (verbatim):", "", "  ```",
                        *["  " + ln for ln in resp.read_text(encoding="utf-8").strip().splitlines()], "  ```"]
        out.append("")
    p = tdir / "result.md"
    with FILE_LOCK:
        p.write_text("\n".join(out) + "\n", encoding="utf-8")
    return p
