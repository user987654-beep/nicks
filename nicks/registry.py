"""Model discovery and the free-only eligibility policy.

Routes are discovered live from OpenCode (``GET /api/model``), never from a hardcoded
shortlist. A route is eligible for automatic use only when one of these holds:

* **local-endpoint** – its base URL is a loopback address (or the operator explicitly
  lists the provider in ``local_providers``): no cloud billing is possible.
* **zero-cost-anonymous** – OpenCode reports explicit zero prices for every token class
  *and* the route uses the anonymous ``public`` key, i.e. no account exists to bill.
* **zero-cost-credentialed** – zero prices on an account credential; only when the
  operator opts in with ``allow_credentialed_zero_cost``.

Anything else (priced, unpriced/unknown, disabled, operator-excluded) is ineligible, with
a recorded reason. Missing pricing is treated as *not free*.
"""
from __future__ import annotations

import ipaddress
import json
import re
import subprocess
import urllib.request
from dataclasses import asdict, dataclass, fields
from urllib.parse import urlparse

from .config import Settings, opencode_bin, state_dir


class DiscoveryError(RuntimeError):
    pass


@dataclass
class Route:
    route: str            # "<provider>/<model>" exactly as OpenCode's -m expects
    provider: str
    model: str
    name: str
    family: str           # as reported by OpenCode ("" if undisclosed)
    diversity_key: str    # coarse lineage used to prefer independent model families
    eligible: bool
    basis: str            # why it is free (empty if ineligible)
    reason: str           # human-readable eligibility explanation
    local: bool
    tools: bool
    context: int
    endpoint: str = ""    # base URL host:port (never credentials)
    kind: str = "opencode"  # opencode (free/local via OpenCode) | codex | claude (subscription Council workers)
    multi: bool = False     # may serve several slots of one job as independent instances (subscription workers)
    free_type: str = "UNKNOWN"
    quota_pool: str = ""    # stable non-secret account/gateway/model allowance identity
    quota_scope: str = "provider"
    adapter: str = ""       # openai: inject live catalog model into the existing OpenCode worker
    auth_required: bool = False
    identity_known: bool = True
    verified_until: float = 0  # external eligibility lease; re-discover after this timestamp
    expires_at: float = 0
    quota_remaining: float | None = None
    pricing_source: str = ""

    def row(self) -> dict:
        d = asdict(self)
        d["eligible"] = int(self.eligible)
        d["local"] = int(self.local)
        d["tools"] = int(self.tools)
        return d

    @classmethod
    def from_row(cls, r: dict) -> "Route":
        route = cls(
            route=r["route"], provider=r["provider"], model=r["model"], name=r.get("name") or "",
            family=r.get("family") or "", diversity_key=r.get("diversity_key") or "",
            eligible=bool(r["eligible"]), basis=r.get("basis") or "", reason=r.get("reason") or "",
            local=bool(r.get("local")), tools=bool(r.get("tools")), context=int(r.get("context") or 0),
            endpoint=r.get("endpoint") or "",
        )
        # A single JSON column extends old journals without changing historical job records.
        metadata = r.get("metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        for f in fields(cls):
            if f.name in r and f.name not in ("eligible", "local", "tools"):
                setattr(route, f.name, r[f.name])
            if f.name in metadata:
                setattr(route, f.name, metadata[f.name])
        return route


def diversity_key(family: str, model: str) -> str:
    """Coarse model lineage: 'qwen3_5' and 'qwen2' -> 'qwen'; 'deepseek-flash' -> 'deepseek'.

    Heuristic labelling only; the arbiter sees the raw family too.
    """
    src = (family or model.rsplit("/", 1)[-1]).lower()
    src = re.sub(r"-free$", "", src)
    src = re.sub(r"\d+", "-", src)
    parts = [p for p in re.split(r"[-_.\s:]+", src) if p]
    return parts[0] if parts else (family or model).lower()


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _numbers(x):
    if isinstance(x, bool):
        return
    if isinstance(x, (int, float)):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _numbers(v)
    elif isinstance(x, list):
        for v in x:
            yield from _numbers(v)


def _valid_cost(cost) -> bool:
    """All token classes must be present and numeric; unknown leaves cannot hide beside a zero."""
    tiers = cost if isinstance(cost, list) else [cost]
    def leaf(value):
        if isinstance(value, dict):
            return bool(value) and all(leaf(v) for v in value.values())
        import math
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return bool(tiers) and all(isinstance(t, dict) and {"input", "output"} <= t.keys() and leaf(t) for t in tiers)


def classify(model: dict, settings: Settings) -> tuple[bool, str, str, bool]:
    """Return (eligible, basis, reason, local). Never inspects or returns credential values."""
    provider = model.get("providerID", "")
    route = f"{provider}/{model.get('id', '')}"
    cfg = model.get("settings") or {}
    host = urlparse(cfg.get("baseURL") or "").hostname
    local = _is_loopback(host) or provider in settings.local_providers

    if route in settings.exclude_routes or provider in settings.exclude_routes:
        return False, "", "excluded by operator", local
    if model.get("enabled") is False or model.get("status") not in (None, "active"):
        return False, "", f"model not active (status={model.get('status')!r}, enabled={model.get('enabled')!r})", local
    if local:
        why = f"loopback endpoint {host}" if _is_loopback(host) else "operator-declared local provider"
        return True, "local-endpoint", why, True

    cost = model.get("cost")
    nums = list(_numbers(cost))
    configured = (getattr(settings, "free_providers", None) or {}).get(provider)
    if configured:                                   # operator-registered external free provider
        policy = configured.get("policy", "zero-priced")
        allow = configured.get("models")
        if allow and model.get("id") not in allow:
            return False, "", f"not in the {provider} free-provider model allowlist", False
        if policy == "free-tier-account":
            from .federation import FREE_ACCOUNTS, PRESETS
            if provider not in FREE_ACCOUNTS or cfg.get("baseURL", "").rstrip("/") != PRESETS[provider][0]:
                return False, "", "no verified free-account contract for this provider endpoint", False
            return True, "free-tier-account", (f"operator declares the {provider} account has no billing method "
                                               "(free tier); cannot be charged"), False
        if _valid_cost(cost) and nums and all(n == 0 for n in nums):
            return True, "zero-cost-configured-key", f"all token prices are 0 ({provider} key, zero-priced policy)", False
        return False, "", f"priced or unpriced route under the {provider} zero-priced policy", False
    if not cost or not nums or not _valid_cost(cost):
        return False, "", "no pricing metadata; cannot verify zero cost", False
    if any(n != 0 for n in nums):
        return False, "", f"priced route (cost={json.dumps(cost)})", False
    if cfg.get("apiKey") == "public":
        return True, "zero-cost-anonymous", "all token prices are 0 and no account credential is used", False
    if settings.allow_credentialed_zero_cost:
        return True, "zero-cost-credentialed", "all token prices are 0 (operator allowed credentialed routes)", False
    return False, "", "zero-priced but uses an account credential (set allow_credentialed_zero_cost to permit)", False


def route_from_model(model: dict, settings: Settings) -> Route:
    eligible, basis, reason, local = classify(model, settings)
    provider = model.get("providerID", "")
    mid = model.get("id", "")
    family = model.get("family") or ""
    caps = model.get("capabilities") or {}
    base_url = (model.get("settings") or {}).get("baseURL") or ""
    u = urlparse(base_url)
    endpoint = f"{u.scheme}://{u.hostname}{':' + str(u.port) if u.port else ''}{u.path}" if u.hostname else ""
    return Route(
        route=f"{provider}/{mid}", provider=provider, model=mid, name=model.get("name") or mid,
        family=family, diversity_key=diversity_key(family, mid),
        eligible=eligible, basis=basis, reason=reason, local=local,
        tools=bool(caps.get("tools")), context=int((model.get("limit") or {}).get("context") or 0),
        endpoint=endpoint,
        free_type="LOCAL" if local else ("RECURRING_FREE" if eligible else "UNKNOWN"),
        quota_pool=f"{provider}:local" if local else f"{provider}:account",
        quota_scope="local" if local else "provider",
    )


def _http_json(url: str, timeout: float = 3.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback only
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def check_local_routes(routes: list[Route], settings: Settings) -> None:
    """Objective availability checks for local endpoints (mutates eligibility with a reason).

    * the OpenAI-compatible endpoint must answer ``GET <base>/models``;
    * if it is LM Studio (``/api/v0/models`` reports load state), models that are not loaded
      are skipped unless ``local_load_on_demand`` is set: JIT-loading a large model can take
      minutes and evict whatever the operator has loaded.
    """
    by_endpoint: dict[str, list[Route]] = {}
    for r in routes:
        if r.local and r.eligible and r.endpoint:
            by_endpoint.setdefault(r.endpoint, []).append(r)
    for endpoint, group in by_endpoint.items():
        try:
            _http_json(endpoint.rstrip("/") + "/models")
        except (OSError, ValueError) as exc:
            for r in group:
                r.eligible, r.reason = False, f"local endpoint unreachable ({exc.__class__.__name__})"
            continue
        u = urlparse(endpoint)
        states: dict[str, str] = {}
        try:
            data = _http_json(f"{u.scheme}://{u.netloc}/api/v0/models")
            states = {m["id"]: m.get("state", "") for m in data.get("data", []) if isinstance(m, dict) and "id" in m}
        except (OSError, ValueError, AttributeError):
            pass  # not LM Studio: no load-state information
        for r in group:
            st = states.get(r.model)
            if st and st != "loaded" and not settings.local_load_on_demand:
                r.eligible, r.reason = False, f"local model {st} (set local_load_on_demand=true to allow JIT loading)"
            elif st == "loaded":
                r.reason += "; model loaded"


DISCOVERY_PLACEHOLDER = "nicks-discovery-placeholder-not-a-key"


def fetch_models(timeout: float = 90, settings: Settings | None = None) -> list[dict]:
    from .providers import provider_env
    from .runner import base_env  # local import avoids a cycle

    sd = state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    env = base_env()
    for keys in (provider_env(settings).values() if settings else []):
        # OpenCode lists a provider's models once its key variable is set; listing never needs the real key
        # (models come from the models.dev catalog), so discovery gets a placeholder and the key stays here.
        env.update({name: DISCOVERY_PLACEHOLDER for name in keys})
    try:
        proc = subprocess.run(
            [opencode_bin(), "api", "--standalone", "GET", "/api/model"],
            capture_output=True, text=True, timeout=timeout, env=env, cwd=str(sd),
        )
    except OSError as exc:
        raise DiscoveryError(f"cannot run OpenCode ({exc}); install OpenCode or set NICKS_OPENCODE_BIN") from exc
    except subprocess.TimeoutExpired as exc:
        raise DiscoveryError(f"OpenCode model discovery timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise DiscoveryError(f"OpenCode model discovery failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise DiscoveryError(f"OpenCode returned non-JSON model list: {proc.stdout[:300]!r}") from exc
    models = data.get("data") if isinstance(data, dict) else data
    if not isinstance(models, list):
        raise DiscoveryError("unexpected /api/model response shape")
    return [m for m in models if isinstance(m, dict) and m.get("type", "chat") == "chat"]


def fetch_price_list(endpoint: str, timeout: float = 20) -> dict[str, dict]:
    """{model id: pricing} from the provider's own public model list (`GET <endpoint>/models`, no key sent)."""
    req = urllib.request.Request(endpoint.rstrip("/") + "/models",
                                 headers={"User-Agent": "council-of-nicks", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https endpoint from the catalog
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    return {m["id"]: m.get("pricing") for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id")}


def _zero_price(pricing) -> bool:
    if not isinstance(pricing, dict) or not ({"prompt", "completion"} <= pricing.keys() or
                                               {"input", "output"} <= pricing.keys()):
        return False
    try:
        return all(not isinstance(v, bool) and float(v) == 0 for v in pricing.values())
    except (TypeError, ValueError):
        return False


def check_provider_prices(routes: list[Route], settings: Settings) -> None:
    """Second, independent price source for the zero-priced policy (mutates eligibility with a reason).

    OpenCode's catalog shows 0 for OpenRouter's variable-priced meta-routers (openrouter/auto, fusion, ...), which
    OpenRouter itself prices at -1 and bills at the price of whatever model they pick (observed live). So a
    keyed route is eligible under `zero-priced` only if the provider's own live price list ALSO prices every
    component at exactly 0. Unverifiable (no list, no pricing, model missing) means not eligible.
    """
    by_endpoint: dict[str, list[Route]] = {}
    for r in routes:
        if r.eligible and r.basis == "zero-cost-configured-key":
            by_endpoint.setdefault(r.endpoint, []).append(r)
    for endpoint, group in by_endpoint.items():
        try:
            prices = fetch_price_list(endpoint) if endpoint.startswith("https://") else None
        except (OSError, ValueError) as exc:
            prices, why = None, f"price list unavailable ({exc.__class__.__name__})"
        else:
            why = "provider publishes no https price list"
        for r in group:
            if prices is None:
                r.eligible, r.reason = False, f"cannot verify zero price with the provider: {why}"
            elif not _zero_price(prices.get(r.model)):
                r.eligible = False
                r.reason = (f"provider's own price list does not price this route at 0 "
                            f"({prices.get(r.model) if r.model in prices else 'not listed'})")
            else:
                r.reason += "; zero price confirmed by the provider's live price list"


def discover(settings: Settings, db=None) -> list[Route]:
    if settings.restricted:
        return []
    from .onboarding import prepare, check_pools
    prepare(settings)
    from .federation import discover_providers
    external, diagnostics = discover_providers(settings)
    try:
        models = fetch_models(settings=settings, timeout=20) if settings.auto_onboarding else fetch_models(settings=settings)
    except DiscoveryError as exc:
        if not settings.free_providers:
            raise
        models = []  # an OpenCode catalog outage must not discard independent provider catalogs
        diagnostics["opencode"] = {"status": "OPTIONAL_UNAVAILABLE", "error": str(exc), "inference_verified": False}
    direct = {p for p, cfg in settings.free_providers.items() if cfg.get("adapter") == "openai"}
    routes = [route_from_model(m, settings) for m in models if m.get("providerID") not in direct]
    if settings.free_providers:
        check_provider_prices(routes, settings)
    if settings.allow_local:
        check_local_routes(routes, settings)
    if settings.auto_onboarding:
        from .local_discovery import discover_local
        local, local_diagnostics = discover_local(settings)
        managed = {base.rstrip("/") + "/v1" for base in settings.local_endpoints.values()}
        routes = [r for r in routes if not (r.provider in settings.local_endpoints and r.endpoint in managed)]
        routes += local
        diagnostics.update(local_diagnostics)
    routes += external
    if db is not None:
        db.save_routes([r.row() for r in routes])
        db.save_provider_discovery(diagnostics)
        check_pools(settings, db, routes)
    return routes
