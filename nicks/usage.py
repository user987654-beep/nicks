"""Aggregation of per-invocation usage telemetry (the `invocations` table, nicks/telemetry.py).

Pure functions over invocation rows, shared by `nicks report` and future aggregate reporting. Semantics follow
the telemetry columns: cached and cache-write tokens are SUBSETS of input tokens, reasoning tokens a SUBSET of
output tokens, and total = input + output. They are never added on top of each other. A value the provider did not
report is NULL in the row; here it is never treated as 0: sums cover reported values only, and the number of
calls without that value is kept next to every sum.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens",
                "total_tokens")
PHASE_ORDER = ("delivery", "planning", "plan-critique", "planning-debate", "test-design", "implementation", "review",
               "investigation", "challenge", "nickquisition", "probe")


@dataclass
class Usage:
    calls: int = 0
    outcomes: Counter = field(default_factory=Counter)        # success / failure / timeout / ...
    usage_status: Counter = field(default_factory=Counter)    # reported / partial / unavailable
    sums: dict = field(default_factory=lambda: {f: 0 for f in TOKEN_FIELDS})
    known: dict = field(default_factory=lambda: {f: 0 for f in TOKEN_FIELDS})   # calls that reported each field
    process_seconds: float = 0.0                              # sum of process lifetimes (overlaps when parallel)
    first_start: float | None = None
    last_finish: float | None = None

    @property
    def failed(self) -> int:
        return self.calls - self.outcomes.get("success", 0)

    @property
    def span_seconds(self) -> float | None:
        """Wall-clock span from the first launch to the last finish (parallel calls overlap inside it)."""
        if self.first_start is None or self.last_finish is None:
            return None
        return max(0.0, self.last_finish - self.first_start)

    def value(self, f: str) -> int | None:
        """Sum of the reported values, or None when no call reported this field."""
        return self.sums[f] if self.known[f] else None

    def missing(self, f: str) -> int:
        """Calls whose value for this field is unknown (NULL)."""
        return self.calls - self.known[f]


def summarize(rows) -> Usage:
    u = Usage()
    for r in rows:
        u.calls += 1
        u.outcomes[r.get("outcome") or "unknown"] += 1
        u.usage_status[r.get("usage_status") or "unavailable"] += 1
        for f in TOKEN_FIELDS:
            v = r.get(f)
            if v is not None:
                u.sums[f] += int(v)
                u.known[f] += 1
        if r.get("elapsed_s") is not None:
            u.process_seconds += float(r["elapsed_s"])
        start = r.get("started_at")
        if start is not None:
            u.first_start = start if u.first_start is None else min(u.first_start, start)
            end = r.get("finished_at")
            end = end if end is not None else (start + float(r["elapsed_s"]) if r.get("elapsed_s") is not None else None)
            if end is not None:
                u.last_finish = end if u.last_finish is None else max(u.last_finish, end)
    return u


def group(rows, key) -> dict:
    """{key(row): Usage}. Every row lands in exactly one group, so group sums add up to summarize(rows)."""
    buckets: dict = {}
    for r in rows:
        buckets.setdefault(key(r), []).append(r)
    return {k: summarize(v) for k, v in buckets.items()}


def phase_key(r) -> str:
    return r.get("phase") or "other"


def role_key(r) -> str:
    return r.get("role") or "unattributed"


def model_key(r) -> str:
    """The model that actually ran when reported, else the requested one (marked)."""
    if r.get("model_actual"):
        return r["model_actual"]
    req = r.get("model_requested")
    return f"{req} (requested; actual not reported)" if req else "unknown"


def phase_sort(name: str):
    return (PHASE_ORDER.index(name) if name in PHASE_ORDER else len(PHASE_ORDER), name)


# Protocol stages for reporting: telemetry's `phase` plus the role where the phase alone is ambiguous (the judges'
# blind Phase A runs are recorded under phase `review` like the Council's reviewers).
STAGES = (("delivery", None, "delivery host (Architect Nick)"), ("planning", None, "planning (Architect Nick)"),
          ("plan-critique", None, "plan critique"), ("planning-debate", None, "design debate"),
          ("test-design", None, "adversarial test design"), ("implementation", None, "implementation"),
          ("competing", None, "competing implementations: comparison and reconciliation"),
          ("review", "shorty", "Council blind review"), ("review", None, "judges' blind review"),
          ("investigation", None, "judges' blind review"), ("challenge", None, "Evil's challenge"),
          ("nickquisition", None, "Nickquisition deliberation and vote"), ("probe", None, "probes"))
STAGE_ORDER = tuple(dict.fromkeys(name for _, _, name in STAGES))


def stage_key(r) -> str:
    phase, role = r.get("phase"), r.get("role")
    for ph, rl, name in STAGES:
        if ph == phase and (rl is None or rl == role):
            return name
    return phase or "other"


def stage_sort(name: str):
    return (STAGE_ORDER.index(name) if name in STAGE_ORDER else len(STAGE_ORDER), name)
