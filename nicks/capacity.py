"""Bounded capability observations, without model scoring or changing the worker framework."""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict

from .config import state_dir
from .registry import discover
from .router import Router

PROMPT = """Read probe.py using the read tool if available. The function should return the first
item or None for empty input. Identify the bug and provide corrected Python code and three input/output
test cases. Return a JSON object with keys bug, fixed_code, tests; keep the response below 150 words.
If tools are unavailable, here is the complete source:
def first(xs):
    return xs[1] if xs else None
"""


def probe_routes(settings, db, requested: list[str], timeout: float = 60) -> dict:
    if not 1 <= timeout <= 90 or not 1 <= len(requested) <= 8:
        raise ValueError("probe budget: 1–8 explicit routes, each 1–90 seconds")
    if settings.restricted:
        raise ValueError("free capacity probes are forbidden in restricted mode")
    from .cli import make_runner
    from .subscription import CouncilRunner
    routes = {r.route: r for r in discover(settings, db)}
    router = Router(db, list(routes.values()), allow_local=settings.allow_local)
    root = state_dir() / "provider-probes" / (time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3))
    root.mkdir(parents=True)
    runner = CouncilRunner(make_runner(settings, steps=3), settings)
    observations = []
    for index, rid in enumerate(dict.fromkeys(requested)):
        route = routes.get(rid)
        if not route or not route.eligible or (route.local and not settings.allow_local):
            observations.append({"route": rid, "status": "INELIGIBLE", "reason": route.reason if route else "not discovered"})
            continue
        blocked, _, reason = router.blocked(route)
        if blocked:
            observations.append({"route": rid, "status": "COOLDOWN", "reason": reason})
            continue
        attempt = root / str(index)
        snapshot = attempt / "source"
        snapshot.mkdir(parents=True)
        (snapshot / "probe.py").write_text("def first(xs):\n    return xs[1] if xs else None\n")
        (attempt / "prompt.txt").write_text(PROMPT)
        result = runner.run(route, PROMPT, snapshot, attempt / "output", timeout, "nicks capability probe")
        action = None
        if result.ok:
            router.record_success(route, result.duration)
        else:
            action = router.record_failure(route, result.error_class or "transient", result.error or "", result.retry_after)
        record = {"route": rid, "quota_pool": route.quota_pool, "free_type": route.free_type,
                  "context_advertised": route.context or None, "tools_advertised": route.tools,
                  "status": "VERIFIED" if result.ok else "FAILED", "action": action,
                  "observations": asdict(result), "artifacts": str(attempt)}
        (attempt / "result.json").write_text(json.dumps(record, indent=2) + "\n")
        observations.append(record)
    report = {"checked_at": time.time(), "directory": str(root), "probes": observations,
              "note": "VERIFIED means genuine response, not a quality score; inspect the preserved originals."}
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report
