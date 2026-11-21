"""Subscription-backed Council workers (hybrid Council capacity).

The Council targets `council_composition` slots (default: 1 Luna + 1 Haiku + 3 free). Luna runs through the
hardened Codex runner (login-denied read-only command sandbox, low reasoning effort); Haiku through the
tool-less Claude runner. Both are Council *workers*: they produce reviews like the free Nicks and never vote.

Routes are eligible only when the CLI exists, the login is a subscription login (never an API key) and, for
Codex, the model appears in this login's live catalog. They are `multi`: one route may serve several slots of
a job as independent executions (bounded by `subscription_concurrency` and `subscription_max_attempts`).
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from .evil import CodexRunner, EvilUnavailable, check_subscription_auth, codex_bin
from .neutral import ClaudeRunner, NeutralUnavailable, check_claude_subscription, claude_bin, openai_catalog
from .registry import Route, diversity_key
from .policy import allows, resolve, route_allowed
from .runner import OpenCodeRunner, RunResult

SUBSCRIPTION_KINDS = ("codex", "claude")


def subscription_routes(settings) -> tuple[list[Route], dict[str, str]]:
    """(eligible routes, {role: reason-if-not-eligible}). Role names are the composition's slot kinds."""
    routes, problems = [], {}
    resolve(settings)
    catalog = None
    for role, spec in (settings.council_subscription or {}).items():
        provider, model = spec.get("provider"), spec.get("model")
        if not allows(settings, provider):
            problems[role] = "excluded by subscription profile"
            continue
        try:
            if provider == "codex":
                if not Path(codex_bin()).exists():
                    raise EvilUnavailable("Codex CLI not found")
                check_subscription_auth()
                catalog = catalog if catalog is not None else openai_catalog()
                if catalog is not None and model not in catalog:
                    raise EvilUnavailable(f"{model} is not in Codex's model catalog for this login")
                fam = model
            elif provider == "claude":
                if not Path(claude_bin()).exists():
                    raise NeutralUnavailable("Claude Code CLI not found")
                check_claude_subscription()
                fam = model
            else:
                raise EvilUnavailable(f"unknown subscription provider {provider!r}")
        except (EvilUnavailable, NeutralUnavailable) as exc:
            problems[role] = str(exc)
            continue
        from . import billing
        prov = "openai" if provider == "codex" else "anthropic"
        ok_bill, why = billing.model_allowed(prov, model)
        gate = billing.check(prov)
        if not ok_bill or not gate.eligible:       # pre-inference billing gate: never offer the route at all
            problems[role] = why or gate.summary()
            continue
        routes.append(Route(route=f"{provider}/{model}", provider=provider, model=model, name=f"{role} ({model})",
                            family=fam, diversity_key=_lineage(model), eligible=True, basis="subscription",
                            reason=f"{provider} subscription login", local=False, tools=provider == "codex",
                            context=0, kind=provider, multi=True, free_type="SUBSCRIPTION",
                            quota_pool=f"{provider}:subscription", quota_scope="subscription"))
    return routes, problems


def detect_authorized_subscriptions(settings):
    """Resolve auto authorization from locally valid subscription sessions and installed CLIs."""
    out = set()
    for provider in ("codex", "claude"):
        try:
            if provider == "codex" and Path(codex_bin()).exists():
                check_subscription_auth(); out.add("codex")
            elif provider == "claude" and Path(claude_bin()).exists():
                check_claude_subscription(); out.add("claude")
        except (EvilUnavailable, NeutralUnavailable):
            pass
    return sorted(out)


def _lineage(model: str) -> str:
    """claude-haiku-5-5 -> claude-haiku; gpt-6-luna -> gpt-luna (keeps tiers apart from other GPT/Claude routes)."""
    parts = [p for p in re.split(r"[-_.]", model.lower()) if p and not p.isdigit()]
    return "-".join(parts[:2]) if len(parts) > 1 else diversity_key("", model)


def role_of(route: Route, settings) -> str | None:
    for role, spec in (settings.council_subscription or {}).items():
        if f"{spec.get('provider')}/{spec.get('model')}" == route.route:
            return role
    return None


class CouncilRunner:
    """Same interface as OpenCodeRunner; dispatches subscription routes to the hardened Codex/Claude runners."""

    def __init__(self, opencode: OpenCodeRunner, settings):
        self.opencode = opencode
        self.s = settings
        kw = dict(sandbox_extra_ro=settings.sandbox_extra_ro, sandbox_extra_rw=settings.sandbox_extra_rw)
        self._claude = ClaudeRunner(sandbox=settings.sandbox, **kw) if settings.hybrid_council and allows(settings, "claude") else None
        self._codex_kw = dict(sandbox=settings.sandbox, **kw)

    @property
    def faults(self):
        return self.opencode.faults

    def kill_all(self) -> None:
        self.opencode.kill_all()
        if self._claude:
            for p in list(self._claude._procs):
                from .runner import kill_process_group
                kill_process_group(p)

    def run(self, route, prompt: str, cwd: Path, out_dir: Path, timeout: float, title: str, stop=None) -> RunResult:
        if not route_allowed(self.s, route):
            out_dir.mkdir(parents=True, exist_ok=True); (out_dir / "response.txt").write_text("")
            return RunResult(ok=False, error_class="auth", error="subscription route excluded by policy")
        if route.kind not in SUBSCRIPTION_KINDS or self.faults.peek(route.route):
            return self.opencode.run(route, prompt, cwd, out_dir, timeout, title, stop)   # injected faults too
        t0 = time.monotonic()
        try:
            if route.kind == "codex":
                spec = next((v for v in self.s.council_subscription.values()
                             if f"{v.get('provider')}/{v.get('model')}" == route.route), {})
                runner = CodexRunner(model=route.model, extra_args=spec.get("args") or [], **self._codex_kw)
                res = runner.run(prompt, cwd, out_dir, timeout)
            else:
                res = self._claude.run(route.model, prompt, cwd, out_dir, timeout)
        except (EvilUnavailable, NeutralUnavailable) as exc:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "response.txt").write_text("")
            return RunResult(ok=False, error_class="auth", error=str(exc), duration=time.monotonic() - t0)
        from .snapshot import sha256_file
        resp, raw = out_dir / "response.txt", out_dir / "events.jsonl"
        return RunResult(ok=res.ok, error_class=res.error_class, error=res.error, exit_code=res.exit_code,
                         duration=round(time.monotonic() - t0, 2), session_id=res.thread_id, tokens_in=res.tokens_in,
                         tokens_out=res.tokens_out, cost=0.0, tool_calls=res.commands, model=res.model,
                         raw_sha256=sha256_file(raw) if raw.exists() else None,
                         response_sha256=sha256_file(resp) if resp.exists() else None,
                         response_bytes=resp.stat().st_size if resp.exists() else 0, notes=list(res.notes),
                         text=resp.read_text(encoding="utf-8") if resp.exists() else "")
