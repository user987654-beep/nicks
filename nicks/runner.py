"""OpenCode worker execution.

Each Nick is one ``opencode run --standalone --format json`` process:

* the prompt goes over stdin (no argv size limits);
* a per-run config (``OPENCODE_CONFIG_CONTENT``) defines a read-only ``nick-reviewer`` agent
  and a provider policy that denies every provider except the selected route's provider,
  so OpenCode itself refuses to call anything else;
* project-level OpenCode config in the reviewed repo is disabled, so a repository cannot
  grant its reviewers extra permissions;
* stdout (the JSON event stream) is archived byte-for-byte; the assistant's text parts are
  concatenated, unmodified, into ``response.txt``.

The only content judgement made here is objective: did the worker produce any text at all.
"""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import telemetry
from .config import opencode_bin
from .sandbox import SandboxUnavailable, bwrap_path, wrap
from .snapshot import SECRET_GLOBS, sha256_file

AGENT = "nick-reviewer"
IMPL_AGENT = "nick-implementer"   # Development Engine workers (nicks/dev.py): may edit and run commands
# Workers get an allowlisted environment: no provider keys, cloud credentials or tokens from the
# operator's shell can reach OpenCode (OpenCode keeps its own credentials in its auth store).
# Operators can pass extra names/prefixes with NICKS_WORKER_ENV="HTTPS_PROXY,MY_VAR_" (comma-separated).
_ENV_ALLOW = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TZ", "TMPDIR",
              "PWD", "LC_", "XDG_", "OPENCODE_", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy",
              "https_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS")


def base_env() -> dict:
    extra = tuple(x.strip() for x in os.environ.get("NICKS_WORKER_ENV", "").split(",") if x.strip())
    allow = _ENV_ALLOW + extra
    env = {k: v for k, v in os.environ.items() if k in allow or k.startswith(tuple(a for a in allow if a.endswith("_")))}
    env.pop("OPENCODE_CONFIG_CONTENT", None)
    env.update(
        OPENCODE_DISABLE_PROJECT_CONFIG="1",
        OPENCODE_DISABLE_CLAUDE_CODE="1",
        OPENCODE_DISABLE_AUTOUPDATE="1",
    )
    return env


def worker_config(provider: str, steps: int, base_url: str | None = None, implement: bool = False) -> dict:
    """Read-only reviewer agent; with implement=True, an implementer that may also edit files and run shell
    commands. Either way the real boundary is the bubblewrap jail (a writable worktree only for implementers);
    these rules are OpenCode's own, second layer."""
    perms = [{"action": "*", "resource": "*", "effect": "deny"}]
    allowed = ("read", "grep", "glob", "list") + (("edit", "shell") if implement else ())
    perms += [{"action": a, "resource": "*", "effect": "allow"} for a in allowed]
    perms += [{"action": "read", "resource": g, "effect": "deny"} for g in SECRET_GLOBS]
    perms += [{"action": a, "resource": "/proc/**", "effect": "deny"} for a in ("read", "grep", "glob", "list")]
    cfg = {
        "share": "disabled",
        "update": "disable",
        "snapshots": False,
        "agents": {
            (IMPL_AGENT if implement else AGENT): {
                "mode": "primary",
                "description": ("Council of Nicks implementation worker (jailed worktree)" if implement
                                else "Council of Nicks blind reviewer (read-only)"),
                "steps": steps,
                "permissions": perms,
            }
        },
        "policies": [
            {"action": "provider.use", "resource": "*", "effect": "deny"},
            {"action": "provider.use", "resource": provider, "effect": "allow"},
            {"action": "mcp.use", "resource": "*", "effect": "deny"},
            {"action": "websearch.use", "resource": "*", "effect": "deny"},
            # NB: do not deny integration.use - OpenCode providers are integrations.
        ],
    }
    if base_url:
        # Sandboxed workers cannot see the operator's OpenCode config, so they get only a credential-free
        # loopback endpoint: a local model server, or the key proxy for an external free provider.
        cfg["providers"] = {provider: {"settings": {"baseURL": base_url}}}
    return cfg


# --------------------------------------------------------------------------- classification

# error_class -> (counts against route health?, description)
ERROR_CLASSES = {
    "rate_limit": "rate limited",
    "quota": "free quota exhausted",
    "billing": "billing/paid-usage signal",
    "unavailable": "model unavailable / no route",
    "ineligible": "route forbidden for this account or region",
    "auth": "authentication failure",
    "transient": "transient provider/transport failure",
    "timeout": "attempt timed out",
    "request_error": "request rejected (e.g. context too long)",
    "content_filter": "blocked by provider content filter",
    "invalid_output": "provider returned invalid output",
    "empty": "no usable text produced",
    "runtime": "worker process failed",
    "interrupted": "interrupted by the operator",
    "deadline": "cut short by the job wall-clock limit (not the route's fault)",
    "isolation": "worker escaped its read-only snapshot (review discarded as non-blind)",
}

_TYPE_MAP = {
    "provider.rate-limit": "rate_limit",
    "provider.quota": "quota",
    "provider.free-limit": "quota",
    "provider.go-limit": "quota",
    "provider.chatgpt-limit": "quota",
    "provider.budget-limit": "billing",
    "provider.model-budget-limit": "billing",
    "provider.no-route": "unavailable",
    "provider.timeout": "transient",
    "provider.transport": "transient",
    "provider.internal": "transient",
    "provider.unknown": "transient",
    "provider.error": "transient",
    "provider.invalid-request": "request_error",
    "provider.unsupported-operation": "request_error",
    "provider.content-filter": "content_filter",
    "provider.invalid-output": "invalid_output",
}


def _retry_after(err: dict) -> float | None:
    for k in ("retryAfterMs", "retry_after_ms"):
        v = err.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return v / 1000.0
    for k in ("retryAfter", "retry_after"):
        v = err.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    headers = (err.get("response") or {}).get("headers") or {}
    raw = None
    for k, v in headers.items() if isinstance(headers, dict) else []:
        if k.lower() == "retry-after":
            raw = v
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        return max(0.0, parsedate_to_datetime(str(raw)).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def classify_error(err: dict) -> tuple[str, float | None]:
    etype = str(err.get("type", ""))
    status = err.get("status") or (err.get("response") or {}).get("status")
    msg = f"{err.get('message', '')} {json.dumps(err.get('response', ''))[:500]}".lower()
    retry = _retry_after(err)

    if etype == "provider.auth":
        if "allocationquota.freetieronly" in msg:
            return "quota", retry
        return ("ineligible" if status == 403 or "country" in msg or "region" in msg else "auth"), retry
    if etype in _TYPE_MAP:
        cls = _TYPE_MAP[etype]
        if cls == "transient" and status == 429:
            cls = "rate_limit"
        return cls, retry
    if "allocationquota.freetieronly" in msg or "quota" in msg or "limit reached" in msg or "exhausted" in msg:
        return "quota", retry
    if status == 429 or "rate limit" in msg or "too many requests" in msg:
        return "rate_limit", retry
    if status == 402 or any(w in msg for w in ("payment", "billing", "insufficient credit", "credits")):
        return "billing", retry
    if "quota" in msg or "limit reached" in msg or "exhausted" in msg:
        return "quota", retry
    if status == 401:
        return "auth", retry
    if status == 403:
        return "ineligible", retry
    if status == 404:
        return "unavailable", retry
    if status in (400, 413, 422) or "context length" in msg or "too long" in msg:
        return "request_error", retry
    return "transient", retry


# --------------------------------------------------------------------------- event parsing

def parse_events(path: Path) -> dict:
    texts: dict[str, str] = {}
    order: list[str] = []
    errors: list[dict] = []
    unparsed: list[str] = []
    out = {"session_id": None, "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "finish_reason": None,
           "tool_calls": 0, "unparsed_lines": 0, "events": 0, "injected": False, "tool_uses": []}
    try:
        lines = path.read_bytes().decode("utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    for n, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            out["unparsed_lines"] += 1
            unparsed.append(line)
            continue
        if not isinstance(ev, dict):
            out["unparsed_lines"] += 1
            unparsed.append(line)
            continue
        out["events"] += 1
        out["session_id"] = out["session_id"] or ev.get("sessionID")
        out["injected"] = out["injected"] or bool(ev.get("nicks_injected"))
        part = ev.get("part") or {}
        etype = ev.get("type")
        if etype == "text":
            pid = part.get("id") or f"line{n}"
            if pid not in texts:
                order.append(pid)
            texts[pid] = part.get("text") or ""
        elif etype == "step_finish":
            tok = part.get("tokens") or {}
            out["tokens_in"] += int(tok.get("input") or 0)
            out["tokens_out"] += int(tok.get("output") or 0) + int(tok.get("reasoning") or 0)
            c = part.get("cost")
            if isinstance(c, (int, float)) and not isinstance(c, bool):
                out["cost"] += float(c)
            elif c is not None:
                out["cost"] = float("nan")  # unrecognised cost value: treat as not provably free
            out["finish_reason"] = part.get("reason") or out["finish_reason"]
        elif etype == "tool_use":
            out["tool_calls"] += 1
            state = part.get("state") or {}
            out["tool_uses"].append({"tool": part.get("tool"), "status": state.get("status"),
                                     "input": state.get("input") or {}, "output": state.get("output")})
        elif etype == "error":
            errors.append(ev.get("error") or {"message": "unknown error event"})
    out["texts"] = [texts[p] for p in order]
    out["errors"] = errors
    out["unparsed"] = unparsed
    return out


_PATH_KEYS = ("path", "filePath", "file_path", "directory", "dir", "cwd")


def isolation_violations(tool_uses: list[dict], root: Path) -> list[str]:
    """Paths outside the snapshot that a *completed* tool call touched or listed.

    Objective containment check: reviewers must only see the frozen snapshot, never the
    user's working copy or other Nicks' outputs.
    """
    root_s = os.path.realpath(str(root))

    def outside(p: str) -> bool:
        rp = os.path.realpath(p if p.startswith("/") else os.path.join(root_s, p))
        return not (rp == root_s or rp.startswith(root_s + os.sep))

    bad: list[str] = []
    for u in tool_uses:
        if u.get("status") != "completed":
            continue
        inp = u.get("input") if isinstance(u.get("input"), dict) else {}
        for k in _PATH_KEYS:
            v = inp.get(k)
            if isinstance(v, str) and outside(v):
                bad.append(f"{u.get('tool')} {k}={v}")
        if u.get("tool") in ("glob", "grep", "list") and isinstance(u.get("output"), str):
            for line in u["output"].splitlines():
                cand = line.strip().split(":", 1)[0]
                if cand.startswith("/") and outside(cand):
                    bad.append(f"{u.get('tool')} listed {cand}")
                    break
    return bad


# --------------------------------------------------------------------------- fault injection

class FaultPlan:
    """Deliberate failure injection for live failover demos and tests.

    ``NICKS_FAULTS="opencode/glm-*=rate_limit,lmstudio/*=timeout*always"`` makes the next
    matching attempt(s) fail *without contacting the provider*. Attempts are labelled as
    injected everywhere they are reported.
    """

    KINDS = {"rate_limit", "quota", "server_error", "unavailable", "region", "timeout", "empty", "billing", "crash",
             "unusable"}

    def __init__(self, spec: str | None):
        self.rules: list[list] = []
        self.lock = threading.Lock()
        for item in filter(None, (s.strip() for s in (spec or "").split(","))):
            pattern, _, kind = item.partition("=")
            kind, _, count = kind.partition("*")
            if kind not in self.KINDS:
                raise ValueError(f"unknown fault kind {kind!r} in NICKS_FAULTS (known: {sorted(self.KINDS)})")
            n = -1 if count == "always" else int(count or 1)
            self.rules.append([pattern, kind, n])

    def peek(self, route: str) -> str | None:
        with self.lock:
            return next((k for p, k, n in self.rules if n != 0 and fnmatch.fnmatch(route, p)), None)

    def take(self, route: str) -> str | None:
        with self.lock:
            for rule in self.rules:
                pattern, kind, n = rule
                if n != 0 and fnmatch.fnmatch(route, pattern):
                    if n > 0:
                        rule[2] = n - 1
                    return kind
        return None


# What a hallucinating tool-less reviewer produced live (shortened); the injection itself is recorded in notes.
INJECTED_TOOLCALLS = "\n".join(['<invoke name="Read">\n<parameter name="file_path">wordstats.py</parameter>\n</invoke>'] * 3)


def _injected_events(kind: str) -> tuple[list[dict], int, bool]:
    """(events, exit_code, timed_out) for an injected fault, mimicking OpenCode's output."""
    tag = " (injected by NICKS_FAULTS)"
    err = {
        "rate_limit": {"type": "provider.rate-limit", "message": "Rate limit exceeded" + tag, "status": 429, "retryAfter": 30},
        "quota": {"type": "provider.free-limit", "message": "Free usage limit reached" + tag, "status": 429},
        "server_error": {"type": "provider.internal", "message": "Upstream 503" + tag, "status": 503},
        "unavailable": {"type": "provider.no-route", "message": "Model unavailable" + tag},
        "region": {"type": "provider.auth", "message": "This model is not available in your country" + tag, "status": 403},
    }
    if kind in err:
        return [{"type": "error", "nicks_injected": True, "error": err[kind]}], 1, False
    if kind == "timeout":
        return [{"type": "step_start", "nicks_injected": True, "part": {"type": "step-start"}}], -9, True
    if kind == "empty":
        return [{"type": "step_finish", "nicks_injected": True,
                 "part": {"type": "step-finish", "reason": "stop", "cost": 0, "tokens": {"input": 1, "output": 0}}}], 0, False
    if kind == "unusable":     # a "successful" run whose output is only hallucinated tool calls
        return [
            {"type": "text", "nicks_injected": True, "part": {"id": "inj", "type": "text", "text": INJECTED_TOOLCALLS}},
            {"type": "step_finish", "nicks_injected": True,
             "part": {"type": "step-finish", "reason": "stop", "cost": 0, "tokens": {"input": 10, "output": 5}}},
        ], 0, False
    if kind == "billing":
        return [
            {"type": "text", "nicks_injected": True, "part": {"id": "inj", "type": "text", "text": "Looks fine." + tag}},
            {"type": "step_finish", "nicks_injected": True,
             "part": {"type": "step-finish", "reason": "stop", "cost": 0.42, "tokens": {"input": 10, "output": 5}}},
        ], 0, False
    return [], 1, False  # crash


# --------------------------------------------------------------------------- runner

@dataclass
class RunResult:
    ok: bool
    error_class: str | None = None
    error: str | None = None
    retry_after: float | None = None
    text: str = ""
    exit_code: int | None = None
    duration: float = 0.0
    session_id: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    finish_reason: str | None = None
    tool_calls: int = 0
    injected: bool = False
    pid: int | None = None
    raw_sha256: str | None = None
    response_sha256: str | None = None
    response_bytes: int = 0
    model: str | None = None
    notes: list = field(default_factory=list)


def kill_process_group(proc: subprocess.Popen) -> None:
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def run_supervised(cmd, cwd, env, stdin_text: str, raw_path: Path, err_path: Path, timeout: float,
                   stop: threading.Event | None, registry: set, lock: threading.Lock):
    """Run one worker process: prompt on stdin, stdout/stderr archived byte-for-byte, killed (whole
    process group) on timeout or stop. Returns (exit_code, timed_out, interrupted, pid)."""
    timed_out = interrupted = False
    deadline = time.monotonic() + timeout
    with open(raw_path, "wb") as fo, open(err_path, "wb") as fe:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.PIPE, stdout=fo, stderr=fe,
                                env=env, start_new_session=True)
        with lock:
            registry.add(proc)

        def feed():
            try:
                proc.stdin.write(stdin_text.encode("utf-8"))
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

        threading.Thread(target=feed, daemon=True).start()
        try:
            telemetry.launched()      # a model CLI process now exists: one invocation (nicks/telemetry.py)
            while True:
                try:
                    rc = proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if stop is not None and stop.is_set():
                        interrupted = True
                    elif time.monotonic() > deadline:
                        timed_out = True
                    else:
                        continue
                    kill_process_group(proc)
                    rc = proc.wait()
                    break
        finally:
            if proc.poll() is None:  # exceptional termination must not orphan inference
                kill_process_group(proc)
            with lock:
                registry.discard(proc)
    telemetry.exited(rc, timed_out, interrupted)
    return rc, timed_out, interrupted, proc.pid


class OpenCodeRunner:
    def __init__(self, steps: int = 40, faults: FaultPlan | None = None, binary: str | None = None,
                 sandbox: bool = True, sandbox_extra_ro=(), sandbox_extra_rw=(), provider_env: dict | None = None,
                 implement: bool = False, ro_overlays=(), request_proxy=None,
                 system_prompt: str | None = None, tool_less: bool = False, model_body: dict | None = None):
        if implement and not sandbox:
            raise SandboxUnavailable("write-capable OpenCode workers require the bubblewrap sandbox")
        self.steps = steps
        self.implement, self.ro_overlays = implement, list(ro_overlays)
        self.request_proxy, self.system_prompt, self.tool_less = request_proxy, system_prompt, tool_less
        self.model_body = model_body
        self.provider_env = provider_env or {}     # {provider: {ENV: key}} for configured free providers
        self.sandbox = sandbox
        self.sandbox_extra_ro = list(sandbox_extra_ro)
        self.sandbox_extra_rw = list(sandbox_extra_rw)
        if sandbox and not bwrap_path():
            raise SandboxUnavailable(
                "worker sandbox requires bubblewrap (bwrap); install it (e.g. `apt install bubblewrap`) or set "
                '"sandbox": false in $NICKS_HOME/config.json to rely only on OpenCode\'s permission rules (unsafe)')
        self.faults = faults or FaultPlan(os.environ.get("NICKS_FAULTS"))
        self.binary = binary or opencode_bin()
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()

    # -- process control -----------------------------------------------------
    def kill_all(self) -> None:
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            self._kill(p)

    _kill = staticmethod(kill_process_group)

    # -- main entry -----------------------------------------------------------------
    @telemetry.instrument("opencode", lambda s, a: a["route"].provider, lambda s, a: a["route"].model, "out_dir",
                          skip=lambda s, a: getattr(s.request_proxy, "offline", False))
    def run(self, route, prompt: str, cwd: Path, out_dir: Path, timeout: float, title: str,
            stop: threading.Event | None = None) -> RunResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        raw_path, err_path = out_dir / "events.jsonl", out_dir / "stderr.log"
        t0 = time.monotonic()
        timed_out = interrupted = False
        pid = None

        fault = self.faults.take(route.route)
        if fault:
            events, rc, timed_out = _injected_events(fault)
            raw_path.write_text("".join(json.dumps(e) + "\n" for e in events))
            err_path.write_text("worker crashed (injected by NICKS_FAULTS)\n" if fault == "crash" else "")
        else:
            cmd = [self.binary, "run", "--standalone", "--format", "json", "--agent",
                   IMPL_AGENT if self.implement else AGENT,
                   "-m", route.route, "--title", title]   # --title also stops OpenCode's own title-model call
            env = base_env()
            endpoint = route.endpoint if getattr(route, "local", False) else None
            keyed = self.provider_env.get(route.provider)
            proxy = self.request_proxy
            if proxy is not None:
                if keyed:
                    raise ValueError('explicit request proxy cannot receive provider credentials')
                endpoint = proxy.base_url
            elif keyed or route.adapter == "openai":
                # External free provider: the real key stays in this process. The jail gets a per-run token and
                # a loopback URL of a proxy that forwards only this route's approved model (nicks/keyproxy.py).
                from .keyproxy import KeyProxy, ProxyRefused
                try:
                    if not self.sandbox:   # unjailed, a worker could simply open the key file
                        raise ProxyRefused("external provider keys are used only with the worker sandbox enabled")
                    proxy = KeyProxy.for_route(route, next(iter(keyed.values())) if keyed else "",
                                               out_dir / "proxy.jsonl", timeout=max(1, timeout))
                except ProxyRefused as exc:
                    err_path.write_text(f"nicks key proxy refused to start: {exc}\n")
                    raw_path.write_text("")
                    return RunResult(ok=False, error_class="ineligible", error=str(exc)[:600],
                                     duration=round(time.monotonic() - t0, 2))
                env.update({name: proxy.token for name in (keyed or {})})
                endpoint = proxy.base_url
            cfg = worker_config(route.provider, self.steps, endpoint or None, implement=self.implement)
            if self.request_proxy is not None:
                cfg.setdefault('providers', {}).setdefault(route.provider, {}).setdefault('settings', {}).update(
                    baseURL=proxy.base_url, apiKey=proxy.token)
            agent = cfg['agents'][IMPL_AGENT if self.implement else AGENT]
            if self.system_prompt is not None:
                agent['system'] = self.system_prompt
            if self.tool_less:
                agent['permissions'] = [{'action': '*', 'resource': '*', 'effect': 'deny'}]
            if route.adapter in ("openai", "local-openai"):
                if route.adapter == "local-openai":
                    from urllib.parse import urlparse
                    from .registry import _is_loopback
                    if not route.eligible or not route.local or not _is_loopback(urlparse(route.endpoint or "").hostname):
                        raise ValueError("local transport requires an eligible loopback model")
                # This is still the same jailed OpenCode worker, with one catalog-discovered model.
                cfg["providers"][route.provider].update({
                    "package": "@ai-sdk/openai-compatible", "env": [],
                    "settings": {"baseURL": endpoint, "apiKey": proxy.token if proxy else "local"},
                    "models": {route.model: {"name": route.name, "modelID": route.model,
                        "type": "chat", "cost": {"input": 0, "output": 0},
                        "capabilities": {"tools": route.tools},
                        "limit": {"context": route.context or 32768, "output": 4096}}}})
                # Current OpenCode resolves credentials from env for built-in providers; this is a throwaway token.
                from .federation import PRESETS
                if proxy and route.provider in PRESETS:
                    env[PRESETS[route.provider][1]] = proxy.token
            # Adapter model registration must not overwrite caller limits/tool settings.
            if self.model_body is not None:
                cfg.setdefault('providers', {}).setdefault(route.provider, {}).setdefault('models', {}).setdefault(
                    route.model, {})['body'] = self.model_body
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps(cfg)
            home = None
            if self.sandbox:
                home = out_dir / "sandbox-home"
                home.mkdir(exist_ok=True)
                cmd = wrap([os.path.realpath(self.binary), *cmd[1:]], snapshot=cwd, home=home, binary=self.binary,
                           extra_ro=self.sandbox_extra_ro, extra_rw=self.sandbox_extra_rw,
                           writable=self.implement, ro_overlays=self.ro_overlays)
            # `opencode run` takes its project root from $PWD, not the process cwd (verified on v2.2.5).
            # Without this, workers would see whatever directory `nicks` was launched from.
            env["PWD"] = str(cwd)
            try:
                if proxy:
                    proxy.start()
                rc, timed_out, interrupted, pid = run_supervised(
                    cmd, cwd, env, prompt, raw_path, err_path, timeout, stop, self._procs, self._lock)
            finally:
                if proxy:
                    proxy.stop()
            if home is not None:
                shutil.rmtree(home, ignore_errors=True)  # OpenCode's throwaway db/cache; events.jsonl is kept
        keys = list(self.provider_env.get(route.provider, {}).values())
        if keys:   # defence in depth only: the real key never entered the jail
            from .evil import scrub_login_secrets
            scrub_login_secrets([raw_path, err_path], keys, window=12)
        res = self._finish(raw_path, err_path, out_dir, rc, timed_out, interrupted, t0, pid, timeout, bool(fault), cwd)
        if not fault and (self.provider_env.get(route.provider) or route.adapter == "openai"):
            _apply_proxy_log(res, out_dir / "proxy.jsonl")
            if not route.identity_known and (res.model == route.model or not res.model):
                res.model = None
                res.notes.append("provider did not disclose the underlying served model; identity unknown")
        return res

    def _finish(self, raw_path, err_path, out_dir, rc, timed_out, interrupted, t0, pid, timeout, injected, cwd) -> RunResult:
        parsed = parse_events(raw_path)
        text = "\n\n".join(parsed["texts"])
        unrecognised = not text.strip() and bool(parsed["unparsed"])
        resp_path = out_dir / "response.txt"
        # Never silently drop output we could not parse: surface it for diagnosis.
        resp_path.write_text("\n".join(parsed["unparsed"]) if unrecognised else text, encoding="utf-8")
        r = RunResult(
            ok=False, text=text, exit_code=rc, duration=round(time.monotonic() - t0, 2), pid=pid,
            session_id=parsed["session_id"], tokens_in=parsed["tokens_in"], tokens_out=parsed["tokens_out"],
            cost=parsed["cost"], finish_reason=parsed["finish_reason"], tool_calls=parsed["tool_calls"],
            injected=injected or parsed["injected"], raw_sha256=sha256_file(raw_path),
            response_sha256=sha256_file(resp_path), response_bytes=resp_path.stat().st_size,
        )
        stderr_tail = err_path.read_bytes()[-800:].decode("utf-8", errors="replace").strip() if err_path.exists() else ""
        if interrupted:
            r.error_class, r.error = "interrupted", "attempt interrupted before completion"
        elif timed_out:
            r.error_class, r.error = "timeout", f"no completion within {timeout:.0f}s; worker killed"
        elif parsed["errors"]:
            err = parsed["errors"][-1]
            r.error_class, r.retry_after = classify_error(err)
            r.error = f"{err.get('type', 'error')}: {err.get('message', '')}"[:600]
        elif rc not in (0, None):
            r.error_class, r.error = "runtime", f"opencode exited {rc}: {stderr_tail[-400:]}"
        elif not self.implement and (violations := isolation_violations(parsed["tool_uses"], cwd)):
            r.error_class = "isolation"
            r.error = ("worker accessed paths outside its snapshot; review not counted as blind: "
                       + "; ".join(violations[:5]))[:600]
        elif parsed["cost"] != 0:  # also catches NaN and negative values
            r.error_class = "billing"
            r.error = f"worker reported non-zero cost {parsed['cost']}; route treated as paid (output preserved)"
        elif unrecognised:
            r.error_class = "runtime"
            r.error = (f"unrecognised worker output format ({parsed['unparsed_lines']} non-event lines, no text parts); "
                       "raw lines preserved in response.txt")
        elif not text.strip():
            r.error_class, r.error = "empty", "worker produced no text"
        else:
            r.ok = True
        if r.ok and parsed["finish_reason"] == "length":
            r.notes.append("finish_reason=length: output may be truncated by the model's output limit")
        if text.strip() and not r.ok:
            r.notes.append("partial text was produced and is preserved on disk")
        if injected or parsed["injected"]:
            r.notes.append("failure injected by NICKS_FAULTS; provider was not contacted")
        return r


def _apply_proxy_log(res: RunResult, log_path: Path) -> None:
    """Record what the key proxy saw: the model the provider actually served, and every refused request."""
    from .keyproxy import proxy_records
    recs = proxy_records(log_path)
    if any(r.get("reported_cost") == "nonzero-or-ambiguous" for r in recs):
        res.ok, res.error_class = False, "billing"
        res.error = "upstream reported nonzero or ambiguous usage cost; output preserved, provider quarantined"
    served = [r["served_model"] for r in recs if r.get("decision") == "forwarded" and r.get("served_model")]
    if served:
        res.model = served[-1]
        if len(set(served)) > 1:
            res.notes.append("provider served several models in this run: " + ", ".join(sorted(set(served))))
    refused = [r for r in recs if r.get("decision") == "refused"]
    if refused:
        res.notes.append(f"key proxy refused {len(refused)} request(s) (not forwarded, never billed): "
                         + "; ".join(r.get("reason", "")[:120] for r in refused[:3]))
    if res.ok and not served:
        res.notes.append("key proxy recorded no served model")
