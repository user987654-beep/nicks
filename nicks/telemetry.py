"""Per-invocation usage telemetry: one SQLite row for every model CLI process the engine launches.

What counts as an invocation: a real OpenCode, Codex or Claude Code process (a Council reviewer, Development
Engine worker, Evil/Good/Neutral Nick, Architect, Test Designer, Plan Critic, debate turn, probe or the
delivery host). Every retry, fallback model and resumed run is its own process and so its own row; work that
never launches a process (billing-gate refusals, NICKS_FAULTS injections, cached results replayed on resume,
offline context capture) is not an invocation and is not recorded.

How it is wired, so that nothing is counted twice:

* the three leaf runners (``OpenCodeRunner.run``, ``CodexRunner.run``, ``ClaudeRunner.run``) are decorated
  with :func:`instrument`; wrappers that only delegate to them (``CouncilRunner``, ``RoleRunner``) are not;
* :func:`nicks.runner.run_supervised`, the single place a model CLI process is started, calls
  :func:`launched` / :func:`exited`; a row exists only once a process was actually started;
* the decorator finishes the row from the runner's result and the provider's own usage report in the
  archived ``events.jsonl``.

Attribution comes from the run's evidence directory (``jobs/<job>/...``, ``dev/<plan>/...``), from the
journal row whose ``dir`` contains it (attempts, dev_attempts, adversary_runs) and from :func:`labels`
set by callers that know more (e.g. ``RoleRunner`` sets the Council role). The row is written to the
journal that owns that directory, so it lives next to the job and attempt records it describes.

Never stored: prompts, responses, tool output, credentials or any other content. Token counts are what the
provider's CLI reported; a value the provider did not report is NULL, never 0 and never an estimate.
Telemetry must never change engine behaviour: every failure in here is swallowed.
"""
from __future__ import annotations

import contextvars
import functools
import inspect
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS invocations (      -- one row per launched model CLI process (nicks/telemetry.py)
    id                  TEXT PRIMARY KEY,
    job_id              TEXT,                 -- review job, dev plan, planning or delivery id; NULL: not attributable
    job_kind            TEXT,                 -- review | dev | planning | delivery | probe | other
    phase               TEXT,                 -- e.g. review, nickquisition, implementation, planning-debate
    role                TEXT,                 -- e.g. shorty, worker, evil, good, neutral, architect, critic
    step                TEXT,                 -- evidence directory relative to the journal (a path, never content)
    link_table          TEXT,                 -- attempts | dev_attempts | adversary_runs (the record it belongs to)
    link_id             TEXT,
    engine              TEXT NOT NULL,        -- opencode | codex | claude
    provider            TEXT,
    model_requested     TEXT,
    model_actual        TEXT,                 -- what the provider reported it ran (NULL: not disclosed)
    started_at          REAL NOT NULL,        -- wall clock at process launch
    finished_at         REAL,
    elapsed_s           REAL,                 -- process lifetime (monotonic)
    outcome             TEXT NOT NULL,        -- running | success | failure | timeout | interrupted | error | abandoned
    error_class         TEXT,
    exit_code           INTEGER,
    input_tokens        INTEGER,              -- ALL input tokens, cached and cache writes included
    cached_input_tokens INTEGER,              -- subset of input_tokens read from the provider's cache
    cache_write_tokens  INTEGER,              -- subset of input_tokens written to the provider's cache
    output_tokens       INTEGER,              -- ALL generated tokens, reasoning included
    reasoning_tokens    INTEGER,              -- subset of output_tokens
    total_tokens        INTEGER,              -- input_tokens + output_tokens
    usage_status        TEXT,                 -- reported | partial | unavailable
    usage_source        TEXT,                 -- which provider event the numbers come from
    usage_raw           TEXT,                 -- JSON: the provider's numeric usage fields, verbatim
    orchestrator_pid    INTEGER
);
CREATE INDEX IF NOT EXISTS invocations_job ON invocations(job_id, started_at);
"""

_labels: contextvars.ContextVar[dict] = contextvars.ContextVar("nicks_telemetry_labels", default={})
_current: contextvars.ContextVar = contextvars.ContextVar("nicks_telemetry_invocation", default=None)

_JOB_RE = re.compile(r"^r\d{8}-\d{6}-[0-9a-f]{4,}$")
_PLAN_RE = re.compile(r"^p\d{8}-\d{6}-[0-9a-f]{4,}$")
_MEMBERS = ("evil", "good", "neutral")
_LINKS = (("attempts", "job_id"), ("dev_attempts", "plan_id"), ("adversary_runs", "job_id"))


def _warn(what: str, exc: BaseException) -> None:
    try:
        print(f"[nicks telemetry] {what}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    except Exception:
        pass


@contextmanager
def labels(**kw):
    """Attribution hints for invocations started in this context (same thread): ``db`` (the journal to
    write to), ``role``, ``phase``, ``job_id``, ``job_kind``. Explicit labels win over path-derived ones."""
    token = _labels.set({**_labels.get(), **{k: v for k, v in kw.items() if v is not None}})
    try:
        yield
    finally:
        _labels.reset(token)


# --------------------------------------------------------------------------- usage normalisation

def _int(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None


def _numeric(d) -> dict:
    """Numeric leaves only (nested keys joined with '.'): provider usage without any free text."""
    out = {}
    for k, v in (d or {}).items():
        if isinstance(v, dict):
            out.update({f"{k}.{kk}": vv for kk, vv in _numeric(v).items()})
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
    return out


def _events(path: Path):
    try:
        lines = path.read_bytes().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            yield ev


def _empty(source: str) -> dict:
    return {"input_tokens": None, "cached_input_tokens": None, "cache_write_tokens": None, "output_tokens": None,
            "reasoning_tokens": None, "total_tokens": None, "usage_status": "unavailable", "usage_source": source,
            "usage_raw": None}


def _total(u: dict) -> dict:
    if u["input_tokens"] is not None and u["output_tokens"] is not None:
        u["total_tokens"] = u["input_tokens"] + u["output_tokens"]
    return u


def opencode_usage(path: Path, complete: bool = True) -> dict:
    """OpenCode ``step_finish`` events, one per model request. OpenCode's fields are disjoint (input excludes
    cache reads/writes, output excludes reasoning) and it coerces anything the provider did not report to 0,
    so a step with no input and no output is "not reported", and a zero reasoning/cache count is ambiguous
    and stored as NULL. The verbatim per-step numbers stay in usage_raw."""
    u = _empty("opencode.step_finish")
    steps, unreported = [], 0
    for ev in _events(path):
        if ev.get("type") != "step_finish" or ev.get("nicks_injected"):
            continue
        tok = (ev.get("part") or {}).get("tokens")
        if not isinstance(tok, dict):
            unreported += 1
            continue
        cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
        vals = {k: _int(tok.get(k)) or 0 for k in ("input", "output", "reasoning")}
        vals.update({f"cache.{k}": _int(cache.get(k)) or 0 for k in ("read", "write")})
        if not any(vals.values()):
            unreported += 1
            continue
        steps.append(vals)
    if not steps:
        return u
    s = {k: sum(x[k] for x in steps) for k in steps[0]}
    u.update(input_tokens=s["input"] + s["cache.read"] + s["cache.write"],
             cached_input_tokens=s["cache.read"] or None, cache_write_tokens=s["cache.write"] or None,
             output_tokens=s["output"] + s["reasoning"], reasoning_tokens=s["reasoning"] or None,
             usage_status="reported" if complete and not unreported else "partial",
             usage_raw=json.dumps({"steps": steps, "unreported_steps": unreported}))
    return _total(u)


def codex_usage(path: Path, complete: bool = True) -> dict:
    """Codex ``turn.completed`` usage (OpenAI semantics: input includes cached input, output includes
    reasoning). Absent keys stay NULL."""
    u = _empty("codex.turn.completed")
    turns = [_numeric(ev.get("usage")) for ev in _events(path) if ev.get("type") == "turn.completed"]
    turns = [t for t in turns if _int(t.get("input_tokens")) or _int(t.get("output_tokens"))]
    if not turns:
        return u

    def tot(key):
        vals = [_int(t.get(key)) for t in turns]
        return None if any(v is None for v in vals) else sum(vals)
    u.update(input_tokens=tot("input_tokens"), cached_input_tokens=tot("cached_input_tokens"),
             cache_write_tokens=tot("cache_write_input_tokens"), output_tokens=tot("output_tokens"),
             reasoning_tokens=tot("reasoning_output_tokens"),
             usage_status="reported" if complete else "partial", usage_raw=json.dumps({"turns": turns}))
    return _total(u)


def claude_usage(path: Path, complete: bool = True) -> dict:
    """Claude Code's final ``result`` event (Anthropic semantics: input_tokens excludes cache reads and cache
    creation; output_tokens includes thinking). Without a result event (killed, crashed) nothing is known."""
    u = _empty("claude.result")
    result = None
    for ev in _events(path):
        if ev.get("type") == "result":
            result = ev
    usage = (result or {}).get("usage")
    if not isinstance(usage, dict):
        return u
    raw = _numeric({k: v for k, v in usage.items() if k != "iterations"})
    base, out = _int(usage.get("input_tokens")), _int(usage.get("output_tokens"))
    if not base and not out:
        return u
    read, write = _int(usage.get("cache_read_input_tokens")), _int(usage.get("cache_creation_input_tokens"))
    details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    u.update(input_tokens=None if base is None else base + (read or 0) + (write or 0),
             cached_input_tokens=read, cache_write_tokens=write, output_tokens=out,
             reasoning_tokens=_int(details.get("thinking_tokens")),
             usage_status="reported" if complete else "partial", usage_raw=json.dumps(raw))
    return _total(u)


USAGE = {"opencode": opencode_usage, "codex": codex_usage, "claude": claude_usage}


# --------------------------------------------------------------------------- attribution

def _journal_for(out_dir: Path, hint) -> Path | None:
    if hint is not None:
        return Path(getattr(hint, "path", hint))
    for d in (out_dir, *out_dir.parents):
        for name in ("nicks.db", "engine.db"):
            p = d / name
            if p.is_file():
                return p
    return None


def _classify(rel: list[str]) -> dict:
    """Job, phase and role from an evidence path relative to its journal."""
    a: dict = {}
    for i, seg in enumerate(rel):
        if _JOB_RE.match(seg):
            a.update(job_id=seg, job_kind="review", rest=rel[i + 1:])
        elif _PLAN_RE.match(seg):
            a.update(job_id=seg, job_kind="dev", rest=rel[i + 1:])
    rest = a.pop("rest", None)
    if rest is None:
        top = rel[0] if rel else ""
        if top == "planning" and len(rel) > 1:
            return {"job_id": rel[1], "job_kind": "planning", "phase": "planning", "role": "architect"}
        if top == "hosts" and len(rel) > 1:
            return {"job_id": rel[1], "job_kind": "delivery", "phase": "delivery", "role": "architect"}
        if top in ("onboarding", "provider-probes"):
            return {"job_kind": "probe", "phase": "probe"}
        if top not in ("tasks", "debate", "critique", "adversarial"):
            return {"job_kind": "other"}
        a, rest = {"job_kind": "dev"}, rel     # a plan whose directory is its own journal root (competing)
    head = rest[0] if rest else ""
    if a["job_kind"] == "review":
        if head == "attempts":
            a.update(phase="review", role="shorty")
        elif head in _MEMBERS:
            a["role"] = head
            stage = rest[1].removeprefix(f"{a['job_id']}-{head}-").rsplit("-", 1)[0] if len(rest) > 1 else ""
            a["phase"] = ("nickquisition" if re.match(r"nq\d+-", stage) else
                          {"attack": "review", "challenge": "challenge"}.get(stage, stage or "review"))
        elif head == "bounded-investigations":
            a.update(phase="investigation", role=rest[1] if len(rest) > 1 else None)
        else:
            a["phase"] = head or "review"
    else:
        if head == "tasks":
            a.update(phase="implementation", role="worker")
        elif head == "debate":
            a.update(phase="planning-debate", role=rest[1] if len(rest) > 1 and rest[1] in _MEMBERS else None)
        elif head == "critique":
            m = re.match(r"attempt-\d+-(.+)", rest[1]) if len(rest) > 1 else None
            a.update(phase="plan-critique", role="critic" + (f":{m.group(1)}" if m else ""))
        elif head == "adversarial":
            a.update(phase="test-design", role="evil")
        else:
            a["phase"] = head or "dev"
    return a


def _link(conn, out_dir: Path) -> tuple[str, str, str] | None:
    """(table, id, job/plan id) of the journal record whose evidence directory contains out_dir."""
    cands = []
    for d in (out_dir, *out_dir.parents):
        cands += [str(d), os.path.realpath(d)]
    marks = ",".join("?" * len(cands))
    best = None
    for table, owner in _LINKS:
        try:
            for r in conn.execute(f"SELECT id, {owner}, dir FROM {table} WHERE dir IN ({marks})", cands):
                if best is None or len(r[2]) > len(best[3]):
                    best = (table, r[0], r[1], r[2])
        except sqlite3.Error:
            continue
    return best[:3] if best else None


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reap(conn) -> int:
    """Rows left 'running' by an orchestrator that no longer exists (crash, SIGKILL) become 'abandoned', so a
    resumed job neither double-counts nor leaves a phantom in-flight call. Their usage stays unknown (NULL)."""
    n = 0
    for (pid,) in conn.execute("SELECT DISTINCT orchestrator_pid FROM invocations WHERE outcome='running'").fetchall():
        if pid is not None and pid != os.getpid() and not _alive(pid):
            n += conn.execute("UPDATE invocations SET outcome='abandoned', error_class='orchestrator-exited' "
                              "WHERE outcome='running' AND orchestrator_pid=?", (pid,)).rowcount
    return n


# --------------------------------------------------------------------------- invocation lifecycle

class Invocation:
    def __init__(self, engine: str, provider: str | None, model: str | None, out_dir: Path):
        self.engine, self.provider, self.model = engine, provider, model
        self.out_dir = Path(out_dir)
        self.labels = dict(_labels.get())
        self.id: str | None = None
        self.journal: Path | None = None
        self.t0 = self.t1 = None
        self.started_at = None
        self.exit_code = None
        self.timed_out = self.interrupted = False
        self.done = False

    # Called from run_supervised, i.e. only when a process really starts.
    def launched(self) -> None:
        if self.id is not None:       # a runner that launches twice would be two invocations; never merge them
            self.finish(outcome="error", error_class="relaunched")
            self.id, self.done = None, False
        self.t0, self.started_at = time.monotonic(), time.time()
        try:
            self.journal = _journal_for(self.out_dir, self.labels.get("db"))
            if self.journal is None:
                return
            root = self.journal.parent
            try:
                rel = list(self.out_dir.resolve().relative_to(root.resolve()).parts)
            except ValueError:
                rel = []
            attr = _classify(rel)
            conn = _connect(self.journal)
            try:
                link = _link(conn, self.out_dir)
                if link:        # the journal record is authoritative for ownership
                    attr["job_id"] = link[2]
                    attr["job_kind"] = "dev" if link[0] == "dev_attempts" else "review"
                lab = self.labels
                self.id = f"inv-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"
                row = {"id": self.id, "job_id": lab.get("job_id") or attr.get("job_id"),
                       "job_kind": lab.get("job_kind") or attr.get("job_kind"),
                       "phase": lab.get("phase") or attr.get("phase"), "role": lab.get("role") or attr.get("role"),
                       "step": "/".join(rel) or str(self.out_dir), "link_table": link[0] if link else None,
                       "link_id": link[1] if link else None, "engine": self.engine, "provider": self.provider,
                       "model_requested": self.model, "started_at": self.started_at, "outcome": "running",
                       "orchestrator_pid": os.getpid()}
                reap(conn)
                conn.execute(f"INSERT INTO invocations({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                             tuple(row.values()))
            finally:
                conn.close()
        except Exception as exc:
            self.id = None
            _warn("could not record invocation start", exc)

    def exited(self, exit_code, timed_out: bool, interrupted: bool) -> None:
        self.t1 = time.monotonic()
        self.exit_code, self.timed_out, self.interrupted = exit_code, bool(timed_out), bool(interrupted)

    def finish(self, *, ok: bool = False, error_class: str | None = None, model_actual: str | None = None,
               outcome: str | None = None, usage: dict | None = None) -> None:
        if self.id is None or self.done or self.journal is None:
            return
        self.done = True
        try:
            if outcome is None:
                outcome = ("timeout" if self.timed_out or error_class == "timeout" else
                           "interrupted" if self.interrupted or error_class == "interrupted" else
                           "success" if ok else "failure")
            if usage is None:
                parse = USAGE.get(self.engine)
                complete = self.t1 is not None and not (self.timed_out or self.interrupted)
                usage = parse(self.out_dir / "events.jsonl", complete) if parse else _empty(self.engine)
            end = self.t1 if self.t1 is not None else time.monotonic()
            fields_ = {"finished_at": time.time(), "elapsed_s": round(end - self.t0, 3), "outcome": outcome,
                       "error_class": None if outcome == "success" else error_class, "exit_code": self.exit_code,
                       "model_actual": model_actual, **usage}
            conn = _connect(self.journal)
            try:
                conn.execute(f"UPDATE invocations SET {', '.join(f'{k}=?' for k in fields_)} WHERE id=?",
                             (*fields_.values(), self.id))
            finally:
                conn.close()
        except Exception as exc:
            _warn("could not record invocation result", exc)


def launched() -> None:
    inv = _current.get()
    if inv is not None:
        inv.launched()


def exited(exit_code, timed_out: bool, interrupted: bool) -> None:
    inv = _current.get()
    if inv is not None:
        inv.exited(exit_code, timed_out, interrupted)


@contextmanager
def invocation(engine: str, provider: str | None, model: str | None, out_dir: Path):
    """Track one leaf runner call. The row is created only if run_supervised launches a process inside it."""
    inv = Invocation(engine, provider, model, out_dir)
    token = _current.set(inv)
    try:
        yield inv
    except BaseException as exc:
        if inv.id is not None and not inv.done:
            interrupted = isinstance(exc, (KeyboardInterrupt, SystemExit))
            inv.finish(outcome="interrupted" if interrupted else "error", error_class=type(exc).__name__,
                       usage=None)
        raise
    finally:
        _current.reset(token)
        if inv.id is not None and not inv.done:
            inv.finish(outcome="error", error_class="no-result")


def instrument(engine: str, provider, model, out_dir: str, skip=None):
    """Decorate a leaf runner's ``run``. ``provider``/``model`` are callables (self, args) -> value;
    ``out_dir`` is the name of the argument holding the run's evidence directory; ``skip(self, args)`` true
    means the call cannot reach a model (e.g. offline request capture) and is not an invocation."""
    def deco(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            ctx = None                        # decided here; the runner itself is called exactly once, below
            try:
                bound = sig.bind(self, *args, **kwargs)
                bound.apply_defaults()
                a = bound.arguments
                if skip is None or not skip(self, a):
                    ctx = invocation(engine, provider(self, a), model(self, a), Path(a[out_dir]))
            except Exception as exc:          # never let telemetry stop a model call
                _warn("could not prepare invocation", exc)
            if ctx is None:
                return fn(self, *args, **kwargs)
            with ctx as inv:
                res = fn(self, *args, **kwargs)
                inv.finish(ok=bool(getattr(res, "ok", False)), error_class=getattr(res, "error_class", None),
                           model_actual=getattr(res, "model", None))
                return res
        return wrapper
    return deco


def rows(db, job_id: str | None = None) -> list[dict]:
    q, a = "SELECT * FROM invocations", ()
    if job_id:
        q, a = q + " WHERE job_id=?", (job_id,)
    return db.all(q + " ORDER BY started_at, id", a)
