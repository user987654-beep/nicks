"""Free-only, health-aware route selection with cooldowns and quarantine.

Selection never looks at a model name. It ranks currently eligible routes by:
1. quota diversity, then family/provider diversity – unused pools and families first;
2. cloud before local (local GPUs are slow and usually serial);
3. tool-capable before tool-less (reviewers can inspect surrounding code);
4. recent health – fewer consecutive failures first;
5. rotation – least recently used first, so the pool is shared out over time.

Failure handling scopes:
* ``route``    – cool down just that model (unavailable, region block, transient);
* ``quota``    – cool down every route sharing a known allowance (quota or rate limit);
* ``provider`` – cool down every model of the provider (auth failure, legacy unknown shared quota);
* ``job``      – don't retry this route within the current job (empty output, request too
                  big, content filter), no global penalty;
* billing signals quarantine the provider until an operator clears it.
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import Counter

from .registry import Route

# class -> (scope, base cooldown seconds, max cooldown seconds)
POLICY = {
    "rate_limit":     ("route", 60, 900),
    "quota":          ("provider", 1800, 6 * 3600),
    "billing":        ("quarantine", 0, 0),
    "unavailable":    ("route", 3600, 6 * 3600),
    "ineligible":     ("route", 24 * 3600, 24 * 3600),
    "auth":           ("provider", 6 * 3600, 24 * 3600),
    "transient":      ("route", 120, 1800),
    "timeout":        ("route", 300, 3600),
    "runtime":        ("route", 120, 1800),
    "invalid_output": ("job", 0, 0),
    "request_error":  ("job", 0, 0),
    "content_filter": ("job", 0, 0),
    "empty":          ("job", 0, 0),
    "unusable":       ("job", 0, 0),      # model answered, but not with a review (nicks/usability.py)
    "billing_gate":   ("job", 0, 0),      # refused before inference by nicks/billing.py; provider not contacted
    "isolation":      ("job", 0, 0),
    "interrupted":    ("none", 0, 0),
    "deadline":       ("none", 0, 0),
    "proposal_apply": ("none", 0, 0),  # filesystem failure; discard attempt, not provider capacity
    "fallback_reserve": ("none", 0, 0),  # stopped by the scheduler to keep time for a fallback
}
SHARED_RATE_WINDOW = 120.0


def route_scope(r: Route) -> str:
    return f"route:{r.route}"


def provider_scope(r: Route) -> str:
    return f"provider:{r.provider}"


def quota_scope(r: Route) -> str:
    return f"quota:{r.quota_pool}" if r.quota_pool else provider_scope(r)


class Router:
    def __init__(self, db, routes: list[Route], *, allow_local: bool = True, local_concurrency: int = 1, clock=time.time,
                 multi_concurrency: int = 2):
        self.db = db
        self.clock = clock
        self.routes = {r.route: r for r in routes if r.eligible and (allow_local or not r.local)}
        self.local_concurrency = local_concurrency
        self.multi_concurrency = multi_concurrency   # bounded parallel instances of one subscription route
        self.in_flight: Counter = Counter()
        self.lock = threading.Lock()

    # -- availability --------------------------------------------------------
    def blocked(self, r: Route, now: float | None = None) -> tuple[bool, float, str]:
        now = self.clock() if now is None else now
        from .federation import dispatch_block
        if why := dispatch_block(r, now):
            return True, float("inf"), why
        for scope in (provider_scope(r), quota_scope(r), route_scope(r)):
            h = self.db.health(scope)
            if h.get("quarantined"):
                return True, float("inf"), f"{scope} quarantined: {h.get('reason')}"
            until = h.get("cooldown_until") or 0
            if until > now:
                return True, until, f"{scope} cooling down until {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(until))}: {h.get('reason')}"
        return False, 0.0, ""

    def candidates(self, exclude: set[str], need_context: int = 0, pool=None) -> list[Route]:
        now = self.clock()
        local_busy = sum(n for rid, n in self.in_flight.items() if rid in self.routes and self.routes[rid].local)
        out = []
        for r in self.routes.values():
            if pool is not None and not pool(r):
                continue
            if r.route in exclude or self.in_flight[r.route] >= (self.multi_concurrency if r.multi else 1):
                continue
            if need_context and r.context and r.context < need_context:
                continue
            if r.local and local_busy >= self.local_concurrency:
                continue
            if self.blocked(r, now)[0]:
                continue
            out.append(r)
        return out

    def pick(self, exclude: set[str], used_keys: Counter, need_context: int = 0, pool=None,
             randomize: bool = False) -> Route | None:
        """Best eligible route (optionally restricted to a pool). randomize=True draws uniformly with OS
        randomness among the candidates instead of ranking (used for subscription overflow)."""
        with self.lock:
            cands = self.candidates(exclude, need_context, pool)
            if not cands:
                return None
            if randomize:
                best = secrets.SystemRandom().choice(sorted(cands, key=lambda r: r.route))
                self.in_flight[best.route] += 1
                self.db.put_health(route_scope(best), last_used_at=self.clock())
                return best

            def rank(r: Route):
                h = self.db.health(route_scope(r))
                pool_used = sum(n for rid, n in self.in_flight.items()
                                if self.routes[rid].quota_pool and self.routes[rid].quota_pool == r.quota_pool)
                return (used_keys[("quota", r.quota_pool)] + pool_used,
                        used_keys[r.diversity_key], used_keys[("provider", r.provider)], r.local, not r.tools,
                        h.get("consecutive_failures") or 0, h.get("last_used_at") or 0.0, r.route)

            best = min(cands, key=rank)
            self.in_flight[best.route] += 1
            self.db.put_health(route_scope(best), last_used_at=self.clock())
            return best

    def release(self, r: Route) -> None:
        with self.lock:
            self.in_flight[r.route] -= 1
            if self.in_flight[r.route] <= 0:
                del self.in_flight[r.route]

    def busy(self, exclude: set[str]) -> bool:
        """True if some non-excluded route is reserved by another slot (it may fail and free up)."""
        with self.lock:
            return any(n > 0 and rid not in exclude for rid, n in self.in_flight.items())

    def next_available(self, exclude: set[str], need_context: int = 0) -> float | None:
        """Earliest time a currently blocked (not quarantined) route frees up, or None."""
        now = self.clock()
        best = None
        for r in self.routes.values():
            if r.route in exclude or (need_context and r.context and r.context < need_context):
                continue
            blocked, until, _ = self.blocked(r, now)
            if blocked and until != float("inf"):
                best = until if best is None else min(best, until)
        return best

    # -- outcomes ---------------------------------------------------------------------
    def record_success(self, r: Route, latency: float) -> None:
        with self.db.lock:
            h = self.db.health(route_scope(r))
            n = (h.get("successes") or 0) + 1
            avg = h.get("latency_avg")
            avg = latency if avg is None else avg + (latency - avg) / min(n, 20)
            self.db.put_health(route_scope(r), successes=n, consecutive_failures=0, last_success_at=self.clock(),
                               latency_avg=round(avg, 2), cooldown_until=0, reason=None, error_class=None)

    def record_failure(self, r: Route, cls: str, message: str = "", retry_after: float | None = None) -> str:
        """Update health and return a human-readable description of the action taken."""
        scope_kind, base, cap = POLICY.get(cls, POLICY["transient"])
        now = self.clock()
        with self.db.lock:
            h = self.db.health(route_scope(r))
            consecutive = (h.get("consecutive_failures") or 0) + (0 if scope_kind in ("none",) else 1)
            self.db.put_health(route_scope(r), failures=(h.get("failures") or 0) + (scope_kind != "none"),
                               consecutive_failures=consecutive, last_failure_at=now, error_class=cls,
                               reason=message[:300] or cls)
            if scope_kind == "none":
                return ("no route penalty (stopped by the scheduler to keep time for a fallback)"
                        if cls == "fallback_reserve" else "no route penalty (stopped by operator or job deadline)")
            if scope_kind == "job":
                return "excluded for the rest of this job"
            if scope_kind == "quarantine":
                self.db.put_health(provider_scope(r), quarantined=1, error_class=cls, last_failure_at=now,
                                   reason=f"billing signal from {r.route}: {message[:200]}")
                return f"provider {r.provider} QUARANTINED (paid-usage signal); clear with `nicks doctor --clear {provider_scope(r)}`"

            dur = retry_after if retry_after else min(cap, base * (2 ** max(0, consecutive - 1)))
            dur = max(1.0, min(float(dur), 24 * 3600.0))
            target = route_scope(r) if scope_kind == "route" else provider_scope(r)
            if r.quota_pool and cls in ("quota", "rate_limit"):
                target = quota_scope(r)
            self.db.put_health(target, cooldown_until=now + dur, error_class=cls, reason=message[:300] or cls,
                               last_failure_at=now)
            action = f"{target} cooldown {dur:.0f}s"

            if cls == "rate_limit" and not r.quota_pool:
                # Two different models of one provider rate-limited within the window => shared quota.
                peers = self.db.all(
                    "SELECT scope FROM health WHERE scope LIKE ? AND scope != ? AND error_class='rate_limit' AND last_failure_at >= ?",
                    (f"route:{r.provider}/%", route_scope(r), now - SHARED_RATE_WINDOW),
                )
                if peers:
                    self.db.put_health(provider_scope(r), cooldown_until=now + dur, error_class="rate_limit",
                                       reason=f"shared rate limit across {len(peers) + 1} {r.provider} routes",
                                       last_failure_at=now)
                    action += f"; shared rate limit detected -> provider:{r.provider} cooldown {dur:.0f}s"
            return action
