"""Pre-inference subscription gate: subscription-backed runs only on the user's existing subscriptions.

POLICY (2026-11-07, set by the user): the existing Claude Team and ChatGPT/Codex subscriptions are authorized
resources, INCLUDING their organization-configured extra usage / workspace credits. Consuming those credits is
explicitly accepted, so an open "paid path" (extra usage enabled, credits available, an allowance already used)
is not a reason to block. What stays prohibited is separately billed API-key inference, and selecting a model
that is not included in the subscription (credit-only models such as Fable).

Before any Claude Code or Codex execution this module therefore establishes:

* that the model is INCLUDED in the subscription: credit-only models (Fable) are permanently excluded, even though
  extra usage is authorized for ordinary subscription runs;
* that the run will authenticate with a SUBSCRIPTION login (Claude.ai OAuth / Codex `auth_mode=chatgpt`), never an
  API key; this is checked locally and is fail-closed;
* that the account is not on a usage-based API plan, when the account reports its plan; read with the
  subscription's own OAuth credential through the read-only, non-inference endpoints the official clients use:
  Anthropic `GET /api/oauth/profile` + `GET /api/oauth/usage`; OpenAI `GET chatgpt.com/backend-api/wham/usage`.

The same readings are REPORTED (extra usage / credits enabled, limit, used, plan windows) so nobody claims "zero
incremental charges" when credits can be consumed. They never block: quota exhaustion, spend-limit refusals,
authentication failures and unavailable models are detected from the provider's actual responses at run time
(the runners classify them as quota / auth / unavailable and the router fails over to another eligible route).
An unreadable account report is a reporting gap, not a block, once the subscription login is verified.

Free and local OpenCode workers never go through this gate; they have their own free-only policy. The parent,
interactive Claude Code session is outside the gate's scope.
"""
from __future__ import annotations

import fnmatch
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

ANTHROPIC_API = "https://api.anthropic.com"
CHATGPT_USAGE = "https://chatgpt.com/backend-api/wham/usage"
TTL = 60.0                                     # seconds a reading is reused within one process
# NOT INCLUDED in the subscription: billed to extra-usage credits even within the plan. Permanently excluded from every
# automatic selection (user decision, 2026-11-07): model eligibility depends on whether a model is included in the
# subscription, never on whether the account could currently pay for it, and it is not re-enabled when credits reset.
CREDIT_BILLED_MODELS = ("claude-fable*", "*fable*")
PAYG_PLAN_TYPES = ("usage_based", "api", "payg")


class BillingBlocked(RuntimeError):
    """Raised BEFORE inference: the run would not use an authorized subscription login."""


@dataclass
class Eligibility:
    provider: str
    eligible: bool
    reasons: list = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    source: str = "live"
    checked_at: float = field(default_factory=time.time)

    def summary(self) -> str:
        return ("ELIGIBLE: " if self.eligible else "BLOCKED: ") + "; ".join(self.reasons)


_cache: dict[str, Eligibility] = {}
_lock = threading.Lock()


def _state_file() -> Path:
    from .config import state_dir
    return state_dir() / "billing.json"


def _http_json(url: str, headers: dict, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"User-Agent": "council-of-nicks", "Accept": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https endpoints
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _fixture(provider: str):
    """Test hook (NICKS_BILLING_FIXTURE=<json file>), never used unless explicitly set by the operator/tests."""
    path = os.environ.get("NICKS_BILLING_FIXTURE")
    if not path:
        return None
    data = json.loads(Path(path).read_text())
    if provider not in data:
        raise BillingBlocked(f"billing fixture has no {provider} entry")
    entry = data[provider]
    if isinstance(entry, dict) and entry.get("error"):
        raise OSError(entry["error"])
    return entry


# --------------------------------------------------------------------------- Anthropic

def _anthropic_reading() -> tuple[dict, dict, str]:
    fx = _fixture("anthropic")
    if fx is not None:
        return fx.get("profile") or {}, fx.get("usage") or {}, "fixture"
    from .neutral import claude_credentials
    cred = json.loads(claude_credentials().read_text()).get("claudeAiOauth") or {}
    tok = cred.get("accessToken")
    if not tok:
        raise BillingBlocked("no Claude.ai OAuth access token")
    h = {"Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20", "Content-Type": "application/json"}
    return _http_json(ANTHROPIC_API + "/api/oauth/profile", h), _http_json(ANTHROPIC_API + "/api/oauth/usage", h), "live"


def credit_billed_anthropic_models() -> list[str]:
    """Display names Claude Code itself treats as billed to usage credits (its cached feature flag), if any."""
    try:
        d = json.loads((Path.home() / ".claude.json").read_text())
        names = (d.get("cachedGrowthBookFeatures") or {}).get("tengu_usage_overage_included_models") or []
        return [str(n) for n in names]
    except (OSError, ValueError):
        return []


def _model_credit_billed(model: str) -> bool:
    if any(fnmatch.fnmatch(model.lower(), pat) for pat in CREDIT_BILLED_MODELS):
        return True
    fam = model.lower().replace("claude-", "").split("-")[0]
    return any(n.lower().split()[0] == fam for n in credit_billed_anthropic_models() if n.strip())


def assess_anthropic(profile: dict, usage: dict) -> tuple[bool, list[str], dict]:
    """Authorization + a usage report. Blocks only a usage-based (API-billed) account."""
    org = (profile or {}).get("organization") if isinstance(profile, dict) else None
    eu = (usage or {}).get("extra_usage") if isinstance(usage, dict) else None
    org = org if isinstance(org, dict) else {}
    eu = eu if isinstance(eu, dict) else {}
    windows = {k: usage.get(k) for k in ("five_hour", "seven_day")
               if isinstance(usage, dict) and isinstance(usage.get(k), dict)}
    facts = {"billing_type": org.get("billing_type"), "has_extra_usage_enabled": org.get("has_extra_usage_enabled"),
             "extra_usage_is_enabled": eu.get("is_enabled"), "extra_usage_user_disabled": eu.get("user_disabled"),
             "extra_usage_monthly_limit": eu.get("monthly_limit"), "extra_usage_used_credits": eu.get("used_credits"),
             "extra_usage_utilization": eu.get("utilization"),
             **{f"{k}_utilization": w.get("utilization") for k, w in windows.items()}}
    billing = str(org.get("billing_type") or "")
    if any(p in billing for p in PAYG_PLAN_TYPES):
        return False, [f"billing type {billing!r} is usage-based API billing, not a subscription"], facts
    reasons = ["subscription login (Claude.ai OAuth)"]
    if not org and not eu:
        reasons.append("account usage report unavailable: extra-usage consumption cannot be reported")
    elif org.get("has_extra_usage_enabled") is not False and eu.get("is_enabled") is not False \
            and eu.get("user_disabled") is not True and eu.get("monthly_limit") != 0:
        reasons.append("extra usage ENABLED (authorized): runs beyond the plan bill extra-usage credits"
                       + (f", monthly limit {eu.get('monthly_limit')}" if eu.get("monthly_limit") is not None else "")
                       + (f"; used so far: {eu.get('used_credits')}" if eu.get("used_credits") is not None else ""))
    else:
        reasons.append("extra usage disabled: included plan capacity only")
    if any((w.get("utilization") or 0) >= 100 for w in windows.values()):
        reasons.append("a plan window is at 100%: runs bill extra usage or are refused by the provider")
    return True, reasons, facts


# --------------------------------------------------------------------------- OpenAI / Codex

def _openai_reading() -> tuple[dict, str]:
    fx = _fixture("openai")
    if fx is not None:
        return fx.get("usage") or {}, "fixture"
    from .evil import codex_home
    auth = json.loads((codex_home() / "auth.json").read_text())
    t = auth.get("tokens") or {}
    if not t.get("access_token"):
        raise BillingBlocked("no ChatGPT access token in the Codex login")
    return _http_json(CHATGPT_USAGE, {"Authorization": f"Bearer {t['access_token']}",
                                      "ChatGPT-Account-Id": t.get("account_id") or ""}), "live"


def assess_openai(usage: dict) -> tuple[bool, list[str], dict]:
    """Authorization + a usage report. Blocks only a usage-based (API-billed) plan."""
    usage = usage if isinstance(usage, dict) else {}
    cr = usage.get("credits") if isinstance(usage.get("credits"), dict) else {}
    lim = (usage.get("spend_control") or {}).get("individual_limit") or {}
    rl = usage.get("rate_limit") or {}
    facts = {"plan_type": usage.get("plan_type"), "has_credits": cr.get("has_credits"), "unlimited": cr.get("unlimited"),
             "credits_balance": cr.get("balance"), "credit_limit": lim.get("limit"), "credits_used": lim.get("used"),
             "rate_limit_allowed": rl.get("allowed"), "rate_limit_reached": rl.get("limit_reached")}
    plan = str(usage.get("plan_type") or "")
    if plan and any(p in plan for p in PAYG_PLAN_TYPES):
        return False, [f"plan type {plan!r} is usage-based API billing, not a subscription"], facts
    reasons = [f"subscription login (ChatGPT {plan or 'plan'})"]
    if not usage:
        reasons.append("account usage report unavailable: credit consumption cannot be reported")
    elif cr.get("has_credits") or cr.get("unlimited"):
        reasons.append("workspace credits AVAILABLE (authorized): runs beyond the plan's rate limits spend credits"
                       + (f", individual limit {lim.get('limit')}" if lim.get("limit") is not None else "")
                       + (f"; used so far: {lim.get('used')}" if lim.get("used") is not None else ""))
    else:
        reasons.append("no credits available: included plan capacity only")
    if rl.get("allowed") is False or rl.get("limit_reached") is True:
        reasons.append("plan rate limit reached: runs spend credits or are refused by the provider")
    return True, reasons, facts


# --------------------------------------------------------------------------- gate

def _subscription_login(provider: str) -> str:
    """Fail-closed local check that runs authenticate with a subscription login, never an API key."""
    if os.environ.get("NICKS_BILLING_FIXTURE"):
        return "fixture"
    if provider == "anthropic":
        from .neutral import check_claude_subscription
        return check_claude_subscription()
    from .evil import check_subscription_auth
    return check_subscription_auth()


def check(provider: str, refresh: bool = False) -> Eligibility:
    """Fresh (TTL-cached) eligibility for 'anthropic' or 'openai'. Never raises. BLOCKED only when the run would not
    use a subscription login (or the account is on usage-based API billing); an unreadable usage report is not a
    block once the login is verified."""
    key = f"{provider}|{os.environ.get('NICKS_BILLING_FIXTURE') or 'live'}"   # a fixture answer never serves a live check
    with _lock:
        cached = _cache.get(key)
        if cached and not refresh and time.time() - cached.checked_at < TTL:
            return cached
    if provider not in ("anthropic", "openai"):
        ok, reasons, facts, source = False, [f"unknown provider {provider!r}"], {}, "-"
    else:
        try:
            login = _subscription_login(provider)
        except Exception as exc:                 # EvilUnavailable / NeutralUnavailable: no subscription login
            login, ok, facts, source = None, False, {}, "local"
            reasons = [f"not a subscription login ({str(exc)[:200]}); separately billed API-key inference is not "
                       "authorized"]
        if login is not None:
            try:
                if provider == "anthropic":
                    profile, usage, source = _anthropic_reading()
                    ok, reasons, facts = assess_anthropic(profile, usage)
                else:
                    usage, source = _openai_reading()
                    ok, reasons, facts = assess_openai(usage)
            except urllib.error.HTTPError as exc:
                ok, facts, source = True, {}, "live"
                reasons = [f"subscription login ({login})",
                           f"account usage report unavailable (HTTP {exc.code}): consumption cannot be reported"]
            except (OSError, ValueError, KeyError, BillingBlocked) as exc:
                ok, facts, source = True, {}, "live"
                reasons = [f"subscription login ({login})", "account usage report unavailable "
                           f"({exc.__class__.__name__}: {str(exc)[:120]}): consumption cannot be reported"]
    e = Eligibility(provider, ok, reasons, facts, source)
    with _lock:
        _cache[key] = e
    try:
        f = _state_file()
        cur = json.loads(f.read_text()) if f.exists() else {}
        cur[provider] = asdict(e)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(cur, indent=2))
    except (OSError, ValueError):
        pass
    return e


def model_allowed(provider: str, model: str | None) -> tuple[bool, str]:
    """Is this model INCLUDED in the existing subscription? Credit-only models (Fable) never are, whatever the account
    flags, the remaining credits or the CLI's own model list say. Used by every automatic selection (Neutral's draw,
    subscription Council routes) and again by `require()` immediately before any process starts."""
    if model and _model_credit_billed(model):
        return False, (f"{model} is not included in the subscription (billed to extra-usage credits); credit-only models "
                       "are permanently excluded")
    return True, ""


def require(provider: str, model: str | None = None) -> Eligibility:
    """Call immediately before any subscription-backed inference. Raises BillingBlocked (provider not contacted) only
    when the run would not use an authorized subscription login."""
    ok, why = model_allowed(provider, model)
    if not ok:
        raise BillingBlocked(f"billing gate: {why}")
    e = check(provider)
    if not e.eligible:
        raise BillingBlocked(f"billing gate ({provider}): {'; '.join(e.reasons)}")
    return e


def reset_cache() -> None:
    with _lock:
        _cache.clear()
