"""Automatic provider registration and small, durable first-use checks. No new worker framework."""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict
from pathlib import Path

from .config import opencode_bin, state_dir
from .federation import PRESETS
from .providers import _load, _save, config_lock, read_existing_key

# This is an authentication contract, not a model list. Never infer anonymous permission from GET /models.
ANONYMOUS = {"kilo": "https://kilo.ai/docs/gateway/authentication"}
PROBE_LIMIT = 2
PROBE_TIMEOUT = 20


def credential_reference(provider):
    env = {"zai": "ZAI_API_KEY", "siliconflow-intl": "SILICONFLOW_INTL_API_KEY"}.get(provider, PRESETS[provider][1])
    if os.environ.get(env):
        return {"key_env": env}
    root = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "opencode"
    for filename, kind in (("auth.json", "opencode-json"), ("opencode.db", "opencode-sqlite")):
        source = {"kind": kind, "path": str(root / filename), "provider": provider}
        if read_existing_key(source):
            return {"key_source": source}
    return None


def prepare(settings):
    """Merge only provider references into settings/config, after the caller restores any pinned policy."""
    if settings.restricted or not settings.auto_onboarding:
        return
    from .local_discovery import DEFAULT_ENDPOINTS
    with config_lock():
        cfg = _load()
        before = json.dumps(cfg, sort_keys=True)
        disabled = set(settings.auto_disabled_providers) | set(cfg.get("auto_disabled_providers", []))
        providers = cfg.setdefault("free_providers", {})
        for provider, entry in settings.free_providers.items():
            if provider not in disabled:
                providers.setdefault(provider, entry)  # preserve concurrent operator edits/removals
        for provider, (endpoint, env, scope) in PRESETS.items():
            if provider in providers or provider in disabled or provider in settings.exclude_routes:
                continue
            source = None if provider in ANONYMOUS else credential_reference(provider)
            if provider not in ANONYMOUS and not source:
                continue
            providers[provider] = {"adapter": "openai", "endpoint": endpoint, "env": [env],
                                   "policy": "zero-priced", "anonymous": provider in ANONYMOUS,
                                   "quota_pool": f"{provider}:default", "quota_scope": scope,
                                   "auto_discovered": True, **(source or {})}
        if settings.allow_local:
            endpoints = cfg.setdefault("local_endpoints", {})
            for provider, endpoint in settings.local_endpoints.items():
                endpoints.setdefault(provider, endpoint)
            for provider, endpoint in DEFAULT_ENDPOINTS.items():
                if provider not in disabled and provider not in settings.exclude_routes:
                    endpoints.setdefault(provider, endpoint)
            settings.local_endpoints = dict(endpoints)
        settings.free_providers = dict(providers)
        settings.auto_disabled_providers = sorted(disabled)
        if before != json.dumps(cfg, sort_keys=True):
            _save(cfg)


def check_pools(settings, db, routes):
    """At most two 20s calls per invocation, one per pool; success cached for a day.

    Claim each check before contacting a provider. A crashed check becomes retryable after five minutes;
    concurrent initializers cannot spend the same check twice. Normal quota health always takes precedence.
    """
    if settings.restricted or not settings.auto_onboarding or db is None or not settings.sandbox:
        return
    if not os.access(opencode_bin(), os.X_OK) or not shutil.which("bwrap"):
        return
    from .router import Router
    from .runner import FaultPlan
    from .cli import make_runner
    router = Router(db, routes, allow_local=settings.allow_local)
    pools = {}
    for r in sorted(router.routes.values(), key=lambda r: (r.local, not r.tools, not r.identity_known, r.route)):
        if not router.blocked(r)[0]:
            pools.setdefault(r.quota_pool or r.provider, r)
    used = 0
    runner = None
    for pool, route in pools.items():
        if used >= PROBE_LIMIT:
            break
        if router.blocked(route)[0]:
            continue  # a previous probe may have blocked this provider's other model pools
        now = time.time()
        # A real job's recent success makes an extra health prompt unnecessary.
        if any((db.health("route:" + r.route).get("last_success_at") or 0) > now - 86400
               for r in routes if (r.quota_pool or r.provider) == pool):
            continue
        with db.tx() as c:
            row = c.execute("SELECT next_check FROM onboarding_checks WHERE pool=?", (pool,)).fetchone()
            if row and row[0] > now:
                continue
            c.execute("INSERT OR REPLACE INTO onboarding_checks VALUES (?,?,?)",
                      (pool, now + 300, json.dumps({"status": "CHECKING", "route": route.route})))
        used += 1
        directory = state_dir() / "onboarding" / f"{time.time_ns()}"
        snapshot = directory / "source"
        snapshot.mkdir(parents=True)
        prompt = "Reply with exactly PONG. Do not call tools."
        (directory / "prompt.txt").write_text(prompt)
        import sys
        print(f"[nicks init] checking {route.provider} (bounded free inference)", file=sys.stderr, flush=True)
        try:
            runner = runner or make_runner(settings, steps=2, faults=FaultPlan(None))
            result = runner.run(route, prompt, snapshot, directory / "output", PROBE_TIMEOUT, "nicks startup check")
            if result.ok and not result.injected:
                router.record_success(route, result.duration)
            else:
                router.record_failure(route, result.error_class or "empty", result.error or "no response", result.retry_after)
            detail = {"status": "VERIFIED" if result.ok and not result.injected else "UNAVAILABLE",
                      "route": route.route, "pool": pool, "checked_at": now, "artifacts": str(directory),
                      "result": asdict(result)}
            retry = time.time() + (86400 if result.ok else 300)
        except (OSError, ValueError, RuntimeError) as exc:
            detail = {"status": "UNAVAILABLE", "route": route.route, "error": type(exc).__name__, "checked_at": now}
            retry = time.time() + 300
        (directory / "result.json").write_text(json.dumps(detail, indent=2) + "\n")
        db.execute("UPDATE onboarding_checks SET next_check=?, detail=? WHERE pool=?", (retry, json.dumps(detail), pool))


def summary(settings, db, routes):
    """Describe eligible capacity separately from successful inference; never include credential values."""
    from .router import Router
    from .sandbox import bwrap_path
    router = Router(db, routes, allow_local=settings.allow_local)
    active = [r for r in router.routes.values() if not router.blocked(r)[0]]
    free = [r for r in active if not r.local and r.kind == "opencode"]
    local = [r for r in active if r.local]
    subs = [r for r in active if r.kind in ("codex", "claude")]
    checks = []
    for row in db.all("SELECT detail FROM onboarding_checks"):
        detail = json.loads(row["detail"])
        result = detail.pop("result", {})
        checks.append({**detail, "served_model": result.get("model"), "duration": result.get("duration"),
                       "error_class": result.get("error_class")})
    verified_pools = sorted({r.quota_pool for r in free
                             if (db.health("route:" + r.route).get("last_success_at") or 0) > time.time() - 86400})
    diagnostics = {row["provider"]: json.loads(row["detail"]) for row in db.all("SELECT * FROM provider_discovery")}
    optional = {}
    if not settings.restricted:
        for provider in PRESETS:
            if provider in settings.auto_disabled_providers or provider in settings.exclude_routes:
                continue
            if not any(r.provider == provider for r in free):
                optional[provider] = ("not connected" if provider not in settings.free_providers else
                                      diagnostics.get(provider, {}).get("error") or "no eligible available free routes")
    prerequisites = []
    if not shutil.which("git"):
        prerequisites.append("Git is not installed")
    if settings.sandbox and not bwrap_path():
        prerequisites.append("bubblewrap is required for worker isolation")
    free_transport = os.access(opencode_bin(), os.X_OK) and settings.sandbox
    if not subs and (free or local) and not free_transport:
        prerequisites.append("OpenCode and the worker sandbox are required for free/local inference")
    available = bool(subs or (free_transport and (free or local))) and not prerequisites
    if not active:
        prerequisites.append("no authorized subscription is available" if settings.restricted else
                             "no eligible route is available now")
        if not settings.restricted:
            prerequisites += sorted({router.blocked(r)[2] for r in router.routes.values() if router.blocked(r)[0]})
            prerequisites += [f"{provider}: {detail['error']}" for provider, detail in diagnostics.items()
                              if detail.get("error") and provider in ("kilo", "opencode", "lmstudio", "ollama")]
    return {"mode": "restricted" if settings.restricted else "hybrid", "can_operate": available,
            "free_providers": sorted({r.provider for r in free}), "active_free_models": len(free),
            "free_quota_pools": sorted({r.quota_pool for r in free if r.quota_pool}),
            "verified_free_quota_pools": verified_pools,
            "local_models": len(local), "local_endpoints": sorted({r.endpoint for r in local}),
            "connected_subscriptions": sorted({r.provider for r in subs}), "missing_capabilities": prerequisites,
            "optional_unavailable": optional, "health_checks": checks if not settings.restricted else [],
            "note": "Active means currently eligible and outside cooldown; only successful inference is VERIFIED."}


def print_summary(report, file=None):
    print("Council: " + ("ready" if report["can_operate"] else "unavailable") + " (" + report["mode"] + ")", file=file)
    print(f"Free providers: {', '.join(report['free_providers']) or 'none'}; "
          f"active models: {report['active_free_models']}; independent quota pools: {len(report['free_quota_pools'])}", file=file)
    print(f"Local models ready: {report['local_models']}; connected subscriptions: "
          f"{', '.join(report['connected_subscriptions']) or 'none'}", file=file)
    print(f"Free quota pools with recent successful inference: {len(report['verified_free_quota_pools'])}", file=file)
    for reason in report["missing_capabilities"]:
        print("Unavailable: " + reason, file=file)
    if report["optional_unavailable"]:
        print("Optional, unavailable: " + ", ".join(report["optional_unavailable"]) + " (not required to start)", file=file)
