"""Bounded metadata discovery on known loopback servers. Never download or load models."""
from __future__ import annotations

import json
import urllib.request
from urllib.parse import urlparse

from .federation import NoRedirect
from .registry import Route, _is_loopback, diversity_key

DEFAULT_ENDPOINTS = {"lmstudio": "http://127.0.0.1:1234", "ollama": "http://127.0.0.1:11434"}


def local_json(url, payload=None):
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not _is_loopback(u.hostname) or u.username or u.password or u.query or u.fragment:
        raise ValueError("local discovery requires a credential-free loopback URL")
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload else None,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    # A local endpoint must not redirect to cloud inference or an authentication service.
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(req, timeout=2) as response:
        return json.loads(response.read(2_000_001))


def _lmstudio(base):
    try:
        data = local_json(base + "/api/v1/models")
        rows = data["models"]
        if not isinstance(rows, list):
            raise ValueError("invalid model list")
        return [{"id": m["key"], "name": m.get("display_name"),
                 "loaded": bool(m.get("loaded_instances")),
                 "context": min((i.get("config", {}).get("context_length") or m.get("max_context_length") or 0
                                 for i in m.get("loaded_instances", [])), default=m.get("max_context_length") or 0),
                 "tools": (m.get("capabilities") or {}).get("trained_for_tool_use") is True}
                for m in rows if m.get("type") == "llm"]
    except (OSError, ValueError, KeyError, TypeError):
        data = local_json(base + "/api/v0/models")
        return [{"id": m["id"], "name": m.get("id"), "loaded": m.get("state") == "loaded",
                 "context": m.get("max_context_length") or 0, "tools": False}
                for m in data["data"] if m.get("type") in ("llm", "vlm")]


def _ollama(base):
    tags = local_json(base + "/api/tags")["models"]
    running = {m["name"]: m for m in local_json(base + "/api/ps")["models"]}
    rows = []
    # Show is metadata only. Cap requests even if an operator has hundreds of installed models.
    show_budget = 8
    for m in tags:
        mid = m["name"]
        local = (not m.get("remote_model") and not m.get("remote_host") and ":cloud" not in mid and
                 (m.get("details") or {}).get("format") == "gguf" and (m.get("size") or 0) > 0)
        info = {}
        if local and mid in running and show_budget:
            show_budget -= 1
            info = local_json(base + "/api/show", {"model": mid})
            local = not info.get("remote_model") and not info.get("remote_host")
        caps = info.get("capabilities") or []
        if caps and "completion" not in caps:
            continue
        rows.append({"id": mid, "loaded": mid in running, "local": bool(local),
                     "context": running.get(mid, {}).get("context_length") or 0, "tools": "tools" in caps})
    return rows


def discover_local(settings):
    routes, diagnostics = [], {}
    if settings.restricted or not settings.allow_local:
        return routes, diagnostics
    for provider, base in settings.local_endpoints.items():
        if provider not in DEFAULT_ENDPOINTS or provider in settings.auto_disabled_providers:
            continue
        base = base.rstrip("/")
        try:
            rows = _lmstudio(base) if provider == "lmstudio" else _ollama(base)
            for m in rows:
                mid = m["id"]
                is_local = m.get("local", True)
                eligible = is_local and (m["loaded"] or settings.local_load_on_demand)
                reason = "local model loaded" if m["loaded"] else "local model unloaded (not loaded during discovery)"
                if not is_local:
                    reason = "remote/cloud model behind a local server; external billing is unverified"
                if provider in settings.exclude_routes or f"{provider}/{mid}" in settings.exclude_routes:
                    eligible, reason = False, "excluded by operator"
                routes.append(Route(route=f"{provider}/{mid}", provider=provider, model=mid, name=m.get("name") or mid,
                                    family="", diversity_key=diversity_key("", mid), eligible=eligible,
                                    basis="local-endpoint" if is_local else "", reason=reason, local=is_local,
                                    tools=m["tools"], context=int(m["context"]), endpoint=base + "/v1",
                                    adapter="local-openai", free_type="LOCAL" if is_local else "UNKNOWN",
                                    quota_pool=f"{provider}:local", quota_scope="local"))
            diagnostics[provider] = {"status": "CONFIGURED", "catalog_models": len(rows),
                                     "loaded_models": sum(m["loaded"] for m in rows), "inference_verified": False}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            diagnostics[provider] = {"status": "OPTIONAL_UNAVAILABLE", "error": f"local metadata unavailable ({type(exc).__name__})",
                                     "inference_verified": False}
    return routes, diagnostics
