"""Host metadata and durable provider authorization, independent of agent roles."""
import random
from dataclasses import asdict
from copy import deepcopy

PROFILES = {"auto", "claude-only", "codex-only", "both"}
DEFAULT_COMPOSITION = ["luna", "haiku", "free", "free", "free"]   # `nicks review` (and explicit configurations)
DEFAULT_DEV_WORKERS = ["luna", "free"]   # stored default; in development it means the subscription defaults below
DEV_ROLES = ("luna", "haiku")            # the default development workflow: subscription workers only, never free
DEV_REVIEWERS = 3
_rng = random.SystemRandom()             # OS randomness; tests patch this


class NoDevSubscription(ValueError):
    """Neither the Codex (Luna) nor the Claude (Haiku) subscription worker is available."""
FIELDS = ("host", "subscription_profile", "authorized_subscriptions", "restricted", "role_models", "hybrid_council", "council_subscription",
          "council_composition", "dev_workers", "dev_plan_critic_models", "dev_plan_critic_specs",
          "neutral_openai_models", "neutral_anthropic_models", "evil_model")


def validate(s):
    if s.subscription_profile not in PROFILES:
        raise ValueError("unknown subscription_profile: " + str(s.subscription_profile))
    if s.host not in ("auto", "codex", "claude"):
        raise ValueError("host must be auto, codex or claude")
    if type(s.restricted) is not bool or type(s.hybrid_council) is not bool:
        raise ValueError("restricted and hybrid_council must be booleans")
    authorized = s.authorized_subscriptions
    if authorized is not None:
        if not isinstance(authorized, list) or any(p not in ("codex", "claude") for p in authorized):
            raise ValueError("authorized_subscriptions must list codex and/or claude")
        if any(not _profile_allows(s, p) for p in authorized):
            raise ValueError("authorized_subscriptions exceeds subscription profile")
    if not isinstance(s.role_models, dict):
        raise ValueError("role_models must map roles to provider/model lists")
    for role, specs in s.role_models.items():
        if not isinstance(specs, list) or any(not isinstance(x, dict) or
                x.get("provider") not in ("codex", "claude", "openai", "anthropic") or
                not isinstance(x.get("model"), str) or not x["model"] for x in specs):
            raise ValueError("invalid provider/model list for role " + str(role))
    if not isinstance(s.council_subscription, dict):
        raise ValueError("council_subscription must be a role mapping")
    for role, spec in s.council_subscription.items():
        if not isinstance(spec, dict) or spec.get("provider") not in ("codex", "claude") or not isinstance(spec.get("model"), str):
            raise ValueError("invalid subscription worker " + str(role))
    for name in ("council_composition", "dev_workers", "dev_plan_critic_models", "neutral_openai_models", "neutral_anthropic_models"):
        val = getattr(s, name)
        if not isinstance(val, list) or any(not isinstance(x, str) for x in val):
            raise ValueError(name + " must be a string list")


def _profile_allows(s, provider):
    return s.subscription_profile in ("auto", "both") or s.subscription_profile == provider + "-only"


def allows(s, provider):
    validate(s)
    provider = {"openai": "codex", "anthropic": "claude"}.get(provider, provider)
    return (provider in ("codex", "claude") and _profile_allows(s, provider) and
            (s.authorized_subscriptions is None or provider in s.authorized_subscriptions))


def resolve(s):
    """Resolve once per effective policy. Empty auto detection is a pinned empty set."""
    validate(s)
    if s.authorized_subscriptions is None:
        if s.subscription_profile == "auto":
            from .subscription import detect_authorized_subscriptions
            s.authorized_subscriptions = detect_authorized_subscriptions(s)
        else:
            s.authorized_subscriptions = [p for p in ("codex", "claude") if _profile_allows(s, p)]
    validate(s)
    return s


def snapshot(s):
    resolve(s)
    data = asdict(s)
    return {k: deepcopy(data[k]) for k in FIELDS}


def restore(s, policy):
    if policy is not None and not isinstance(policy, dict):
        raise ValueError("malformed saved execution policy")
    for k in FIELDS:
        if k in (policy or {}):
            setattr(s, k, deepcopy(policy[k]))
    # Legacy snapshots predate auto pinning: fail closed to the original explicit
    # profile, resolving legacy auto once rather than inheriting current defaults.
    if policy and "authorized_subscriptions" not in policy:
        s.authorized_subscriptions = None
    validate(s)
    return resolve(s)


def composition(s, kinds):
    validate(s)
    if s.restricted and not s.hybrid_council:
        raise ValueError("restricted Council cannot disable hybrid_council")
    eligible = [k for k, v in s.council_subscription.items() if allows(s, v["provider"])]
    if s.restricted and not s.council_subscription:
        raise ValueError("restricted Council needs an authorized subscription worker configuration")
    if list(kinds) == DEFAULT_COMPOSITION:
        anchors = [k for k in ("luna", "haiku") if k in eligible]
        if s.restricted:
            if len(anchors) == 1:
                return anchors * 5
            return anchors + ["subscription"] * (5 - len(anchors))
        return anchors + ["free"] * (5 - len(anchors))
    if s.restricted:
        return [k if k in eligible else "subscription" for k in kinds]
    return [k if k == "free" or k in eligible else "free" for k in kinds]


def default_dev_workers(s):
    return list(s.dev_workers) == DEFAULT_DEV_WORKERS


def default_dev_review(s):
    """The default development review applies unless a composition was configured explicitly."""
    return s.hybrid_council and list(s.council_composition) == DEFAULT_COMPOSITION


def _dev_roles(available, what):
    roles = [r for r in DEV_ROLES if r in set(available)]
    if not roles:
        raise NoDevSubscription(f"the default development workflow needs the Codex (Luna) or Claude (Haiku) subscription "
                                f"for {what}, and neither is available; free models are never substituted")
    return roles


def dev_implementer(available, rng=None):
    """One implementer for a story: Luna or Haiku at random when both are available, else the available one."""
    return (rng or _rng).choice(_dev_roles(available, "implementation"))


def dev_review_composition(available, rng=None):
    """Three blind reviewers: 2 Luna + 1 Haiku or 1 Luna + 2 Haiku at random when both are available, else three
    of the available one; the slot order is shuffled. Drawn independently of the implementer."""
    rng = rng or _rng
    roles = _dev_roles(available, "the blind reviews")
    if len(roles) == 1:
        return roles * DEV_REVIEWERS
    kinds = list(rng.choice([["luna", "luna", "haiku"], ["luna", "haiku", "haiku"]]))
    rng.shuffle(kinds)
    return kinds


def worker_order(s):
    """Defaults adapt to the profile; explicit bounded worker chains stay bounded."""
    validate(s)
    roles = list(s.dev_workers)
    if roles == DEFAULT_DEV_WORKERS:     # default development workflow: subscription workers only
        roles = [k for k in DEV_ROLES if k in s.council_subscription and allows(s, s.council_subscription[k]["provider"])]
    return [k for k in roles if (k == "free" and not s.restricted) or
            (k in s.council_subscription and allows(s, s.council_subscription[k]["provider"]))]


def route_allowed(s, route):
    if route.kind in ("codex", "claude"):
        return allows(s, route.kind)
    return not s.restricted

def candidates(db, s, role):
    from .neutral import provider_statuses
    statuses = provider_statuses(db, s)
    specs = s.role_models.get(role)
    if specs is None:
        specs = [{"provider": {"openai": "codex", "anthropic": "claude"}[p], "model": m}
                 for p, st in statuses.items() if st.available for m in st.models]
        # Prefer Codex for the adversary for compatibility; host never affects this choice.
        if role == "evil":
            specs.sort(key=lambda x: x["provider"] != "codex")
        if role == "evil" and s.evil_model:
            specs.sort(key=lambda x: x["model"] != s.evil_model)
    out = []
    for spec in specs:
        prov = {"codex": "openai", "claude": "anthropic"}.get(spec["provider"], spec["provider"])
        if allows(s, prov) and statuses[prov].available and spec["model"] in statuses[prov].models:
            out.append((prov, spec["model"]))
    return out


class RoleRunner:
    """Select eligible subscription models for a role, using the existing hardened runners.

    Failed attempts are preserved in sibling directories. Only quota/auth/availability failures
    permit fallback; no judge conclusion is retried to obtain a preferred vote.
    """
    def __init__(self, db, settings, role, *, writable=False, text_only_fallback=False, retry_on_resume=False,
                 proposal_scope=None):
        self.db, self.s, self.role = db, settings, role
        self.writable = writable
        self.text_only_fallback = text_only_fallback
        self.retry_on_resume = retry_on_resume
        self.proposal_scope = proposal_scope
        self.pinned = False
        self.model = None
        self.provider = None

    def version(self):
        return "Council subscription role runner (" + self.role + ")"

    def run(self, *args, **kwargs):
        # Every model call below is a leaf runner invocation; attribute it to this role and journal.
        from . import telemetry
        with telemetry.labels(db=self.db, role=self.role):
            return self._run(*args, **kwargs)

    def _run(self, prompt, snapshot, out_dir, timeout, resume_thread=None, session_store=None, evidence_only=False):
        import json
        import shutil
        import time
        from pathlib import Path
        from .evil import CodexRunner, EvilResult, EvilUnavailable
        from .neutral import ClaudeRunner, NeutralUnavailable, _record_failure
        from .rework import context, limit
        timeout = limit(context(self.s), timeout)
        deadline = time.monotonic() + timeout
        failures = []
        blocked_providers = set()
        specs = candidates(self.db, self.s, self.role)
        if self.pinned:
            specs = [x for x in specs if x == (self.provider, self.model)]
        elif self.model:
            specs.sort(key=lambda x: x != (self.provider, self.model))
        res = EvilResult(ok=False, error_class="unavailable", error="no eligible subscription model for " + self.role)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        for i, (provider, model) in enumerate(specs):
            # Recheck provider health after a quota/auth failure; do not hammer its other models.
            if provider in blocked_providers:
                continue
            if (provider, model) not in candidates(self.db, self.s, self.role):
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            attempt = out_dir / ("attempt-" + str(i + 1))
            kw = dict(sandbox=self.s.sandbox, sandbox_extra_ro=self.s.sandbox_extra_ro,
                      sandbox_extra_rw=self.s.sandbox_extra_rw)
            try:
                if provider == "openai":
                    res = CodexRunner(model=model, writable=self.writable, **kw).run(prompt, snapshot, attempt, remaining,
                                                           resume_thread=resume_thread, session_store=session_store)
                else:
                    if self.writable and not self.text_only_fallback and not self.proposal_scope:
                        # Claude's in-process file tools cannot be separated from its OAuth login.
                        res = EvilResult(ok=False, error_class="isolation",
                                         error="write-capable Claude sessions lack a kernel credential boundary")
                        break
                    if not evidence_only:
                        prompt = "You have no tools. Use the supplied repository context; submit any reproducible evidence through the existing protocol.\n" + prompt
                    if resume_thread or session_store:
                        # Claude is tool-less: retain only this role's transcript in its private store.
                        store = Path(session_store) if session_store else out_dir
                        store.mkdir(parents=True, exist_ok=True)
                        history = store / "conversation.txt"
                        prior = history.read_text() if resume_thread and history.exists() else ""
                        res = ClaudeRunner(**kw).run(model, prior + "\n" + prompt, snapshot, attempt, remaining)
                        if res.ok:
                            history.write_text(prior + "\n" + prompt + "\n" + (attempt / "response.txt").read_text())
                            # Actual CLI session identity is retained; history belongs only to this role.
                    else:
                        # Claude is tool-less. Supply bounded repository context in the prompt,
                        # using only the engine's already sanitized snapshot.
                        from .patchworker import source_context, SCHEMA, apply_proposal
                        context = ("" if evidence_only else source_context(snapshot, budget=self.s.dev_critic_context_bytes,
                                                 per_file=self.s.dev_critic_file_bytes))
                        request = prompt + context
                        if self.proposal_scope:
                            request += "\nReturn ONLY a JSON proposal matching " + SCHEMA + "\nAllowed scope: " + json.dumps(self.proposal_scope)
                        res = ClaudeRunner(**kw).run(model, request, snapshot, attempt, remaining)
                        if res.ok and self.proposal_scope:
                            try:
                                apply_proposal(snapshot, (attempt / "response.txt").read_text(), self.proposal_scope)
                            except OSError as exc:
                                res.ok, res.error_class, res.error = False, "proposal_apply", str(exc)
                            except ValueError as exc:
                                res.ok, res.error_class, res.error = False, "proposal", str(exc)
            except (EvilUnavailable, NeutralUnavailable) as exc:
                res = EvilResult(ok=False, error_class="auth", error=str(exc))
            for name in ("response.txt", "last-message.txt", "events.jsonl", "stderr.log", "rollout.jsonl"):
                if (attempt / name).exists():
                    shutil.copyfile(attempt / name, out_dir / name)
                elif name == "last-message.txt":
                    (out_dir / name).unlink(missing_ok=True)
            if res.ok and (not res.model or (self.pinned and res.model != model)):
                res.ok, res.error_class, res.error = False, "attribution", "actual model missing or differs from the pinned investigating judge"
            self.provider, self.model = provider, res.model or model
            failures.append(dict(provider=provider, requested_model=model, actual_model=res.model,
                                 ok=res.ok, error_class=res.error_class, error=res.error))
            if res.ok or res.error_class not in ("quota", "rate_limit", "auth", "unavailable"):
                break
            if res.error_class in ("quota", "rate_limit", "auth"):
                blocked_providers.add(provider)
            if not self.retry_on_resume:
                _record_failure(self.db, provider, model, res.error_class, res.error or "")
        (out_dir / "selection.json").write_text(json.dumps(failures, indent=2))
        if not (out_dir / "response.txt").exists():
            (out_dir / "response.txt").write_text("")
        return res
