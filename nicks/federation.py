"""Small catalog adapters for free capacity; execution stays in OpenCode and KeyProxy.

The presets contain endpoints and account contracts, never promotional model lists.
Live prices (or short-lived operator evidence from an account console) establish eligibility.
Catalog access is not evidence of inference. Keys are used only in this parent process.
"""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .providers import ProviderConfigError, _read_key

PRESETS = {
    "modelscope": ("https://api-inference.modelscope.cn/v1", "MODELSCOPE_API_KEY", "account"),
    "siliconflow": ("https://api.siliconflow.cn/v1", "SILICONFLOW_API_KEY", "model"),
    "siliconflow-intl": ("https://api.siliconflow.com/v1", "SILICONFLOW_API_KEY", "model"),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY", "model"),
    "kilo": ("https://ai-gateway.kilo.ai/api/v1", "KILO_API_KEY", "gateway"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "account"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "account"),
    "google": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "account"),
    "nvidia": ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY", "account"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "account"),
    "zai": ("https://api.z.ai/api/paas/v4", "ZHIPU_API_KEY", "account"),
    "bigmodel": ("https://open.bigmodel.cn/api/paas/v4", "ZHIPU_API_KEY", "account"),
    "huggingface": ("https://router.huggingface.co/v1", "HF_TOKEN", "account"),
}
# Only these documented free account products have a no-billing account policy.
# This is an operator declaration about their actual account, never inferred from having a key.
FREE_ACCOUNTS = {"modelscope", "groq", "google", "nvidia"}
FREE_TYPES = {"PERMANENT_FREE", "RECURRING_FREE", "TRIAL_CREDIT", "LOCAL", "SUBSCRIPTION", "PAID", "UNKNOWN"}
LEASE_SECONDS = 3600


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never send an account key to a redirect destination


def fetch_catalog(endpoint: str, key: str | None = None) -> list[dict]:
    u = urlparse(endpoint)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ProviderConfigError("external catalog requires an https base URL without credentials/query/fragment")
    headers = {"Accept": "application/json", "User-Agent": "council-of-nicks"}
    google = endpoint == PRESETS["google"][0]
    if key:
        headers["x-goog-api-key" if google else "Authorization"] = key if google else f"Bearer {key}"
    url = "https://generativelanguage.googleapis.com/v1beta/models" if google else endpoint.rstrip("/") + "/models"
    if u.hostname in ("api.siliconflow.cn", "api.siliconflow.com"):
        url += "?sub_type=chat"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as resp:
        data = json.loads(resp.read(8_000_001))
    rows = data.get("models" if google else "data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("model catalog must contain a data array")
    if google:
        rows = [{"id": m["name"].removeprefix("models/"), "name": m.get("displayName"),
                 "context_length": m.get("inputTokenLimit")} for m in rows
                if isinstance(m, dict) and isinstance(m.get("name"), str) and
                "generateContent" in m.get("supportedGenerationMethods", [])]
    return [m for m in rows if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]]


def finite_number(value, *, default=None):
    if isinstance(value, bool):
        return default
    try:
        n = float(value)
        return n if math.isfinite(n) else default
    except (ValueError, TypeError):
        return default


def read_entitlements(cfg: dict, now: float) -> dict:
    """Explicit console observations, not fabricated live billing APIs. Expire within 24h.

    Trial evidence requires provider-side hard stops, so races or other consumers cannot spill to paid use.
    A zero balance is terminal until a new observation is supplied; time passing does not renew a trial.
    """
    path = cfg.get("entitlements_file")
    if not path:
        return {}
    data = json.loads(Path(path).expanduser().read_text())
    if not isinstance(data, dict):
        raise ValueError("account evidence must be an object")
    checked = finite_number(data.get("checked_at"), default=0)
    valid = finite_number(data.get("valid_until"), default=0)
    if not 0 < checked <= now < valid <= checked + 86400:
        raise ValueError("account evidence expired/invalid; refresh checked_at and valid_until (maximum 24h)")
    if not isinstance(data.get("models"), dict):
        raise ValueError("account evidence requires a models object")
    if not all(isinstance(k, str) and isinstance(v, dict) for k, v in data["models"].items()):
        raise ValueError("account model evidence must contain objects keyed by model IDs")
    return data


def catalog_routes(provider: str, cfg: dict, models: list[dict], authenticated: bool,
                   evidence: dict | None = None, now: float | None = None):
    from .registry import Route, _zero_price, diversity_key
    now = time.time() if now is None else now
    evidence = evidence or {}
    out = []
    for m in models:
        mid = m["id"]
        modalities = (m.get("architecture") or {}).get("output_modalities")
        if (modalities is not None and set(modalities) != {"text"}) or m.get("type", "chat") not in ("chat", "model"):
            continue
        ent = evidence.get("models", {}).get(mid) or {}
        price = m.get("pricing")
        source = cfg["endpoint"].rstrip("/") + "/models"
        allowed = False
        free_type = "UNKNOWN"
        reason = "missing, incomplete or nonzero provider pricing"
        expires = 0
        remaining = None
        lease = now + LEASE_SECONDS
        policy = cfg.get("policy", "zero-priced")
        if _zero_price(price):
            allowed, free_type, reason = True, "PERMANENT_FREE", "all live provider price components are explicitly zero"
        elif policy == "free-tier-account" and provider in FREE_ACCOUNTS and cfg["endpoint"] == PRESETS[provider][0]:
            allowed, free_type = True, "RECURRING_FREE"
            reason = "operator attests documented free account with no billing enabled; quota exhaustion must stop service"
        elif ent:
            source = ent.get("source", "")
            free_type = ent.get("free_type", "UNKNOWN")
            lease = min(lease, finite_number(evidence.get("valid_until"), default=0))
            remaining = finite_number(ent.get("remaining"))
            expires = finite_number(ent.get("expires_at"), default=0)
            fresh = now < lease and source.startswith("https://")
            if free_type == "PERMANENT_FREE":
                allowed = fresh and _zero_price(ent.get("pricing"))
            elif free_type in ("TRIAL_CREDIT", "RECURRING_FREE"):
                allowed = (fresh and ent.get("hard_stop") is True and remaining is not None and remaining > 0
                           and expires > now)
            reason = "operator-verified account/pricing evidence with provider-side billing protection" if allowed else "account evidence lacks current free eligibility or hard billing stop"
        # Never let an entitlement override an explicitly priced endpoint for zero-priced policy.
        if policy == "zero-priced" and price is not None and not _zero_price(price):
            allowed, reason = False, "provider reports nonzero or ambiguous pricing (including metarouters)"
        if free_type not in FREE_TYPES:
            free_type, allowed = "UNKNOWN", False
        if cfg.get("models") and mid not in cfg["models"]:
            allowed, reason = False, "not in operator model allowlist"
        if m.get("active") is False or m.get("enabled") is False:
            allowed, reason = False, "model disabled in live catalog"
        if provider == "kilo" and m.get("isFree") is not True:
            allowed, reason = False, "Kilo does not mark this route eligible for free access"
        auth_required = not cfg.get("anonymous", False)
        if auth_required and not authenticated:
            allowed, reason = False, "SETUP_REQUIRED: missing provider API key"
        family = m.get("family") or ""
        # Router/stealth identities cannot contribute invented model-family diversity.
        unknown = bool(m.get("autoRouting")) or mid.startswith(("kilo-auto/", "openrouter/", "stealth/"))
        scope = cfg.get("quota_scope", "account")
        pool = cfg.get("quota_pool") or f"{provider}:default"
        if scope == "model":
            pool += ":" + mid
        context = finite_number(m.get("context_length") or m.get("context_window") or
                                (m.get("limit") or {}).get("context"), default=0)
        out.append(Route(route=f"{provider}/{mid}", provider=provider, model=mid, name=m.get("name") or mid,
                         family=family, diversity_key="unknown" if unknown else diversity_key(family, mid),
                         eligible=bool(allowed), basis="federated-free" if allowed else "", reason=reason, local=False,
                         tools="tools" in (m.get("supported_parameters") or []) or m.get("tool_call") is True,
                         context=int(context), endpoint=cfg["endpoint"], adapter="openai", free_type=free_type,
                         quota_pool=pool, quota_scope=scope, auth_required=auth_required, identity_known=not unknown,
                         verified_until=lease, expires_at=expires, quota_remaining=remaining, pricing_source=source))
    return out


def discover_providers(settings):
    routes, diagnostics = [], {}
    if settings.restricted:
        return routes, diagnostics
    for provider, cfg in settings.free_providers.items():
        if cfg.get("adapter") != "openai":
            continue
        try:
            key = _read_key(cfg)
            if not key and not cfg.get("anonymous") and cfg.get("auto_discovered"):
                diagnostics[provider] = {"status": "OPTIONAL_UNAVAILABLE", "error": "credential unavailable",
                                         "inference_verified": False}
                continue
            evidence = read_entitlements(cfg, time.time())
            models = fetch_catalog(cfg["endpoint"], key)
            found = catalog_routes(provider, cfg, models, bool(key), evidence)
            for route in found:
                if route.route in settings.exclude_routes or provider in settings.exclude_routes:
                    route.eligible, route.reason = False, "excluded by operator"
            routes += found
            diagnostics[provider] = {"status": "CONFIGURED" if key or cfg.get("anonymous") else "SETUP_REQUIRED",
                                     "catalog_models": len(models), "eligible_routes": sum(r.eligible for r in found),
                                     "inference_verified": False}
        except urllib.error.HTTPError as exc:
            diagnostics[provider] = {"status": "SETUP_REQUIRED" if exc.code in (401, 403) else "UNAVAILABLE",
                                     "error": f"catalog HTTP {exc.code}", "inference_verified": False}
        except (OSError, ValueError, TypeError, AttributeError, ProviderConfigError) as exc:
            # Do not print response bodies, headers, URLs containing keys or operator secrets.
            diagnostics[provider] = {"status": "INELIGIBLE", "error": f"catalog/account evidence unavailable ({type(exc).__name__})",
                                     "inference_verified": False}
    return routes, diagnostics


def dispatch_block(route, now=None) -> str:
    now = time.time() if now is None else now
    if route.adapter != "openai":
        return ""
    if not route.eligible or route.free_type not in {"PERMANENT_FREE", "RECURRING_FREE", "TRIAL_CREDIT"}:
        return "external route is not approved free capacity"
    if not route.verified_until or route.verified_until <= now:
        return "free eligibility evidence expired; rediscover provider"
    if route.expires_at and route.expires_at <= now:
        return "free quota/trial expired"
    if route.quota_remaining is not None and route.quota_remaining <= 0:
        return "free quota is exhausted"
    return ""
