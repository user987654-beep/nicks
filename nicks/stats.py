"""`nicks stats`: descriptive statistics across Council of Nicks development runs.

Every run is assembled with the same collector as `nicks report` (nicks/report.py), so a run's figures here and in
its report.md cannot disagree. Token figures come only from the `invocations` telemetry (aggregated with
nicks/usage.py); a value a provider did not report stays unknown (None), never 0, and every sum or average says how
many runs or calls it covers. A delivery host's own usage is read once per run from its parent journal. Journals
are opened read-only: statistics never modify a historical journal. No model is called.

Structure for reuse (benchmarks, dashboards): `discover()` -> journal paths, `runs()` -> one plain dict per run,
`aggregate(runs)` -> the full JSON-serialisable summary, `render_text(summary)` -> the CLI tables.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections import Counter
from pathlib import Path

from . import report as R
from . import usage as U

ACCEPTED = "APPROVED"
IMPL_STAGES = ("implementation",)
REVIEW_STAGES = ("Council blind review",)
JUDGE_STAGES = ("judges' blind review", "Evil's challenge", "Nickquisition deliberation and vote")
FEW = 5        # below this many comparable runs, comparisons are flagged as too few to compare


def _dev_models() -> tuple[str, ...]:
    """The default development workflow's subscription models (Luna, Haiku) as configured by default."""
    from .config import Settings
    from .policy import DEV_ROLES
    subs = Settings().council_subscription
    return tuple(subs[r]["model"] for r in DEV_ROLES if r in subs)


DEV_MODELS = _dev_models()


# --------------------------------------------------------------------------- journals

def discover(paths) -> list[Path]:
    """Journal files under the given state directories or journal paths, nested delivery engines included
    (<state>/hosts/<key>/engine/nicks.db), each listed once."""
    found: dict[str, Path] = {}
    for p in map(Path, paths):
        p = p.expanduser()
        cands = [p] if p.is_file() else [p / "nicks.db", *sorted(p.glob("hosts/*/engine/nicks.db")),
                                          *sorted(p.glob("hosts/*/engine/hosts/*/engine/nicks.db"))]
        for c in cands:
            if c.is_file():
                found.setdefault(str(c.resolve()), c.resolve())
    return list(found.values())


def open_readonly(path: Path):
    """A DB-compatible handle on a read-only connection (no schema migration, no writes)."""
    from .db import DB
    db = DB.__new__(DB)
    db.path, db.lock = Path(path), threading.RLock()
    db.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    db.conn.row_factory = sqlite3.Row
    return db


# --------------------------------------------------------------------------- one run

def _model_of(route_or_model: str | None) -> str | None:
    return route_or_model.split("/", 1)[-1] if route_or_model else None


def _tests(detail: dict, reviews: list) -> dict:
    adv = detail.get("adversarial") or {}
    out = {}
    for key, name in (("contract_run", "contract"), ("holdout_first_run", "holdout")):
        res = (adv.get(key) or {}).get("results")
        out[name] = ({"ok": sum(1 for v in res.values() if v == "ok"), "total": len(res)}
                     if isinstance(res, dict) and res else None)
    integ = detail.get("integration") or {}
    out["integration_passed"] = integ.get("passed") if integ else None
    gates = [rnd["gates"].get("status") for r in reviews for rnd in r["rounds"] if rnd.get("gates")]
    out["gate"] = gates[-1] if gates else None
    return out


def _corrections(plan: dict, reviews: list) -> dict:
    """Recorded correction activity. The journal does not link a specific finding to a specific correction task,
    so these are counts side by side, not an attribution."""
    first_review = min((r["job"]["created_at"] for r in reviews), default=None)
    added_after = 0
    for line in (R.read(Path(plan["dir"]) / "amendments.jsonl") or "").splitlines():
        rec = R._json(line, {})
        if first_review is not None and (rec.get("t") or 0) > first_review:
            added_after += len(rec.get("tasks") or [])
    findings = sum(len(r["reconciliation"]["items"]) for r in reviews)
    structured = any(r["reconciliation"]["items"] or r["reconciliation"]["findings"] for r in reviews)
    blocking = sum(1 for r in reviews for it in r["reconciliation"]["items"]
                   if it.get("disposition") and "blocking" in it["disposition"].lower()
                   and not any(w in it["disposition"].lower() for w in ("non-blocking", "not blocking")))
    return {"findings_recorded": findings if structured else None, "findings_marked_blocking": blocking if structured else None,
            "correction_tasks_after_review": added_after, "review_jobs": len(reviews),
            "judging_rounds": sum(len(r["rounds"]) for r in reviews),
            "rework_cycle": bool((plan.get("detail") or {}).get("rework"))}


def _stage_time(rows, stages) -> dict:
    sel = [r for r in rows if U.stage_key(r) in stages]
    u = U.summarize(sel)
    return {"calls": u.calls, "span_seconds": u.span_seconds, "process_seconds": round(u.process_seconds, 3) if sel else None}


def usage_dict(u: U.Usage) -> dict:
    return {"calls": u.calls, "failed": u.failed, "usage_status": dict(u.usage_status),
            **{f: u.value(f) for f in U.TOKEN_FIELDS},
            "calls_missing": {f: u.missing(f) for f in U.TOKEN_FIELDS if u.missing(f)},
            "process_seconds": round(u.process_seconds, 3)}


def run_record(db, plan_id: str, journal: Path) -> dict:
    data = R.collect(db, plan_id)
    plan, detail, reviews = data["plan"], data["detail"], data["reviews"]
    verdict = detail.get("verdict") or {}
    engine_rows, host_rows = data["invocations"], data["host"]
    workers = [a for a in data["attempts"] if a["engine"] != "integration"]
    live = [t for t in data["tasks"] if t["status"] != "superseded"]
    end = (data["concluded"] or {}).get("t")
    final = reviews[-1] if reviews else None
    impl_models = sorted({a["actual_model"] or _model_of(a["worker"]) for a in workers if a["status"] == "verified"})
    reviewer_models = Counter(s["final"]["actual_model"] or _model_of(s["final"]["route"])
                              for s in (final["slots"] if final else []) if s["final"])
    judges = {}
    if final and final["rounds"]:
        for m, b in final["rounds"][-1]["ballots"].items():
            judges[m] = {"model": b.get("model"), "vote": b.get("vote") if b.get("valid") else "INVALID"}
    status = plan.get("status") or "planned"
    return {
        "plan": plan["id"], "journal": str(journal), "feature": plan["manifest"].get("feature"),
        "status": status, "concluded": status in R.TERMINAL,
        "verdict": verdict.get("verdict"), "tally": verdict.get("tally"),
        "accepted": verdict.get("verdict") == ACCEPTED and status == "complete",
        "started_at": plan["created_at"], "concluded_at": end,
        "wall_seconds": (end - plan["created_at"]) if end else None,
        "delivery_seconds": (data["launch"] or {}).get("seconds"),
        "implementer_models": impl_models, "reviewer_models": dict(reviewer_models), "judges": judges,
        "tasks": len(live), "tasks_verified": sum(1 for t in live if t["status"] == "verified"),
        "worker_attempts": len(workers),
        "worker_attempts_failed": sum(1 for a in workers if a["status"] != "verified"),
        "retries": len(workers) - len({a["task_id"] for a in workers}),
        "repair_rounds": sum(a.get("repairs") or 0 for a in workers),
        "review_attempts_failed": sum(1 for r in reviews for a in r["attempts"] if a["status"] != "ok"),
        "failed_invocations": sum(1 for r in engine_rows + host_rows if r.get("outcome") != "success"),
        "failed_routes": sorted({U.model_key(r) for r in engine_rows + host_rows
                                 if r.get("outcome") != "success" and r.get("total_tokens") is None}),
        "usage": {"engine": usage_dict(U.summarize(engine_rows)), "host": usage_dict(U.summarize(host_rows)),
                  "all": usage_dict(U.summarize(engine_rows + host_rows))},
        "time": {"implementation": _stage_time(engine_rows, IMPL_STAGES),
                 "review": _stage_time(engine_rows, REVIEW_STAGES), "judging": _stage_time(engine_rows, JUDGE_STAGES)},
        "tests": _tests(detail, reviews), "corrections": _corrections(plan, reviews),
        "_rows": engine_rows + host_rows, "_engine_rows": engine_rows, "_host_rows": host_rows,
        "_attempts": workers, "_reviews": reviews,
    }


def runs(paths, include_unconcluded: bool = False) -> tuple[list[dict], list[dict]]:
    """(runs, skipped). Unconcluded plans are skipped unless asked for; unreadable journals are reported."""
    out, skipped = [], []
    for j in discover(paths):
        try:
            db = open_readonly(j)
            plans = db.all("SELECT id FROM dev_plans ORDER BY created_at")
        except sqlite3.Error as exc:
            skipped.append({"journal": str(j), "reason": f"unreadable journal: {exc}"})
            continue
        for p in plans:
            try:
                rec = run_record(db, p["id"], j)
            except Exception as exc:                  # a historical or damaged run must not hide the others
                skipped.append({"plan": p["id"], "journal": str(j), "reason": f"{type(exc).__name__}: {exc}"})
                continue
            if rec["concluded"] or include_unconcluded:
                out.append(rec)
            else:
                skipped.append({"plan": p["id"], "journal": str(j), "reason": f"not concluded (status {rec['status']})"})
        db.close()
    return sorted(out, key=lambda r: r["started_at"]), skipped


# --------------------------------------------------------------------------- aggregation

def _known(vals):
    return [v for v in vals if v is not None]


def _stat(vals) -> dict:
    """Sum / mean over the runs that have a value, with how many runs that is."""
    k = _known(vals)
    return {"n": len(k), "of": len(vals), "sum": sum(k) if k else None,
            "mean": (sum(k) / len(k)) if k else None}


def _ratio(num, den) -> dict:
    return {"num": num, "den": den, "rate": (num / den) if den else None}


def _impl_by_model(rs) -> dict:
    out: dict = {}
    for r in rs:
        rows = [x for x in r["_rows"] if U.stage_key(x) in IMPL_STAGES]
        for a in r["_attempts"]:
            m = a["actual_model"] or _model_of(a["worker"]) or "unknown"
            d = out.setdefault(m, {"attempts": 0, "verified": 0, "failure_kinds": Counter(), "attempt_seconds": 0.0,
                                   "runs": set(), "_rows": []})
            d["attempts"] += 1
            d["verified"] += a["status"] == "verified"
            if a["status"] != "verified":
                d["failure_kinds"][a.get("failure_kind") or a["status"]] += 1
            d["attempt_seconds"] += (a["finished_at"] or a["started_at"]) - a["started_at"]
            d["runs"].add(r["plan"])
        for x in rows:
            m = U.model_key(x)
            out.setdefault(m, {"attempts": 0, "verified": 0, "failure_kinds": Counter(), "attempt_seconds": 0.0,
                               "runs": set(), "_rows": []})["_rows"].append(x)
    res = {}
    for m, d in sorted(out.items()):
        u = U.summarize(d["_rows"])
        res[m] = {"runs": len(d["runs"]), "attempts": d["attempts"], "verified": d["verified"],
                  "success": _ratio(d["verified"], d["attempts"]), "failure_kinds": dict(d["failure_kinds"]),
                  "attempt_seconds": round(d["attempt_seconds"], 1), "usage": usage_dict(u),
                  "tokens_per_verified_attempt": (u.value("total_tokens") / d["verified"])
                  if d["verified"] and u.value("total_tokens") is not None and not u.missing("total_tokens") else None}
    return res


def _review_by_model(rs) -> dict:
    out: dict = {}
    for r in rs:
        for jd in r["_reviews"]:
            for s in jd["slots"]:
                f = s["final"]
                if not f:
                    continue
                m = f["actual_model"] or _model_of(f["route"])
                d = out.setdefault(m, {"reviews": 0, "shapes": Counter(), "findings_credited": 0, "runs": set(),
                                       "structured": 0, "_rows": []})
                d["reviews"] += 1
                d["shapes"][s["shape"]] += 1
                d["runs"].add(r["plan"])
                if jd["reconciliation"]["items"]:
                    d["structured"] += 1
                    d["findings_credited"] += s["raised"]
        for x in r["_rows"]:
            if U.stage_key(x) in REVIEW_STAGES and x.get("model_actual"):
                out.setdefault(x["model_actual"], {"reviews": 0, "shapes": Counter(), "findings_credited": 0,
                                                   "runs": set(), "structured": 0, "_rows": []})["_rows"].append(x)
    res = {}
    for m, d in sorted(out.items()):
        u = U.summarize(d["_rows"])
        res[m] = {"runs": len(d["runs"]), "reviews": d["reviews"], "shapes": dict(d["shapes"]),
                  "findings_credited": d["findings_credited"] if d["structured"] else None,
                  "reviews_with_structured_reconciliation": d["structured"], "usage": usage_dict(u)}
    return res


def _tests_by_implementer(rs) -> dict:
    out: dict = {}
    for r in rs:
        key = r["implementer_models"][0] if len(r["implementer_models"]) == 1 else (
            "mixed: " + ", ".join(r["implementer_models"]) if r["implementer_models"] else "none")
        d = out.setdefault(key, {"runs": 0, "contract": [0, 0], "holdout": [0, 0], "integration": [0, 0],
                                 "gate": [0, 0], "accepted": 0})
        d["runs"] += 1
        d["accepted"] += r["accepted"]
        t = r["tests"]
        for name in ("contract", "holdout"):
            if t[name]:
                d[name][0] += t[name]["ok"]
                d[name][1] += t[name]["total"]
        if t["integration_passed"] is not None:
            d["integration"][0] += bool(t["integration_passed"])
            d["integration"][1] += 1
        if t["gate"] is not None:
            d["gate"][0] += t["gate"] == "pass"
            d["gate"][1] += 1
    return {k: {"runs": d["runs"], "nickquisition_accepted": d["accepted"],
                **{n: _ratio(*d[n]) for n in ("contract", "holdout", "integration", "gate")}} for k, d in sorted(out.items())}


def aggregate(rs: list[dict], skipped: list[dict] | None = None) -> dict:
    rows = [x for r in rs for x in r["_rows"]]
    total = U.summarize(rows)
    tok = [r["usage"]["all"]["total_tokens"] for r in rs]
    accepted = [r for r in rs if r["accepted"]]
    verified_tasks = sum(r["tasks_verified"] for r in rs)
    reported_tokens = total.value("total_tokens")
    calls_without = total.missing("total_tokens")     # their tokens are unknown: sums are lower bounds then
    t = [r["tests"] for r in rs]
    contract = [x["contract"] for x in t if x["contract"]]
    holdout = [x["holdout"] for x in t if x["holdout"]]
    integ = [x["integration_passed"] for x in t if x["integration_passed"] is not None]
    gates = [x["gate"] for x in t if x["gate"] is not None]
    impl_models = {m for r in rs for m in r["implementer_models"]}
    return {
        "runs": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rs],
        "skipped": skipped or [],
        "totals": {
            "runs": len(rs), "accepted_by_nickquisition": len(accepted),
            "verdicts": dict(Counter(r["verdict"] or "none" for r in rs)),
            "usage": usage_dict(total),
            "engine_usage": usage_dict(U.summarize([x for r in rs for x in r["_engine_rows"]])),
            "host_usage": usage_dict(U.summarize([x for r in rs for x in r["_host_rows"]])),
            "tasks": sum(r["tasks"] for r in rs), "tasks_verified": verified_tasks,
            "worker_attempts": sum(r["worker_attempts"] for r in rs),
            "worker_attempts_failed": sum(r["worker_attempts_failed"] for r in rs),
            "retries": sum(r["retries"] for r in rs), "repair_rounds": sum(r["repair_rounds"] for r in rs),
            "failed_invocations": sum(r["failed_invocations"] for r in rs),
        },
        "averages_per_run": {
            "total_tokens": _stat(tok), "wall_seconds": _stat([r["wall_seconds"] for r in rs]),
            "delivery_seconds": _stat([r["delivery_seconds"] for r in rs]),
            "worker_attempts": _stat([r["worker_attempts"] for r in rs]),
            "failed_invocations": _stat([r["failed_invocations"] for r in rs]),
        },
        "breakdown": {name: {str(k): usage_dict(v) for k, v in sorted(U.group(rows, key).items(), key=lambda kv: order(kv[0]))}
                      for name, key, order in (("stage", U.stage_key, U.stage_sort), ("role", U.role_key, str),
                                               ("model", R._collapse_failed(rows, U.model_key, "see runs[].failed_routes in --json"), str))},
        "comparisons": {
            "implementation_by_model": _impl_by_model(rs), "review_by_model": _review_by_model(rs),
            "tests_by_implementer": _tests_by_implementer(rs),
            "not_observed": {
                "as_implementer": [m for m in DEV_MODELS if m not in impl_models],
                "as_council_reviewer": [m for m in DEV_MODELS if not any(m in r["reviewer_models"] for r in rs)]},
            "note": (f"Descriptive only: {len(rs)} run(s); implementers {sorted(impl_models) or '—'}. Runs differ in "
                     "task, repository and request, so these figures do not show that one model is better."
                     + (f" Fewer than {FEW} runs: too few to compare." if len(rs) < FEW else "")),
        },
        "quality_vs_efficiency": {
            "tokens_per_verified_task": {
                "tokens": reported_tokens, "verified_tasks": verified_tasks,
                "value": (reported_tokens / verified_tasks) if verified_tasks and reported_tokens is not None else None,
                "calls_without_usage": calls_without, "lower_bound": bool(calls_without)},
            "tokens_per_accepted_run": {
                "tokens": reported_tokens, "accepted_runs": len(accepted),
                "value": (reported_tokens / len(accepted)) if accepted and reported_tokens is not None else None,
                "calls_without_usage": calls_without, "lower_bound": bool(calls_without),
                "note": "all tokens of all listed runs (including rejected or incomplete ones) divided by accepted runs"},
            "time": {stage: _stat([r["time"][stage]["span_seconds"] for r in rs])
                     for stage in ("implementation", "review", "judging")},
            "time_process": {stage: _stat([r["time"][stage]["process_seconds"] for r in rs])
                             for stage in ("implementation", "review", "judging")},
            "review_findings": {
                "findings_recorded": _stat([r["corrections"]["findings_recorded"] for r in rs]),
                "findings_marked_blocking": _stat([r["corrections"]["findings_marked_blocking"] for r in rs]),
                "correction_tasks_after_review": sum(r["corrections"]["correction_tasks_after_review"] for r in rs),
                "runs_with_more_than_one_review": sum(1 for r in rs if r["corrections"]["review_jobs"] > 1),
                "rework_cycles": sum(1 for r in rs if r["corrections"]["rework_cycle"]),
                "note": "the journal does not link a finding to the correction it caused; counts are side by side"},
            "objective_tests": {"integration_passed": _ratio(sum(1 for x in integ if x), len(integ)),
                                "contract_tests_ok": _ratio(sum(c["ok"] for c in contract), sum(c["total"] for c in contract)),
                                "holdout_tests_ok": _ratio(sum(c["ok"] for c in holdout), sum(c["total"] for c in holdout)),
                                "gate_pass": _ratio(sum(1 for g in gates if g == "pass"), len(gates))},
            "nickquisition_acceptance": _ratio(len(accepted), len(rs)),
        },
    }


# --------------------------------------------------------------------------- text rendering

def _n(v, digits=0) -> str:
    if v is None:
        return "n/a"
    return f"{v:,.{digits}f}" if isinstance(v, float) and digits else f"{round(v):,}"


def _pct(r: dict) -> str:
    return "n/a" if r["rate"] is None else f"{r['num']}/{r['den']} ({100 * r['rate']:.0f}%)"


def table(headers, rows) -> list[str]:
    cells = [[str(c) for c in row] for row in rows]
    w = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)]
    numeric = lambda c: c == "n/a" or bool(re.fullmatch(r"[≥ ]*[\d,.]+\*?( \(\d+%\))?|\d+/\d+( \(\d+%\))?", c))
    num = [bool(cells) and all(numeric(r[i]) for r in cells) for i in range(len(headers))]
    fmt = lambda row: "  ".join(c.rjust(w[i]) if num[i] else c.ljust(w[i]) for i, c in enumerate(row)).rstrip()
    return [fmt(headers), "  ".join("-" * x for x in w)] + [fmt(r) for r in cells]


def _tok(u: dict, f: str) -> str:
    v = u.get(f)
    return _n(v) + ("*" if v is not None and u.get("calls_missing", {}).get(f) else "")


def render_text(s: dict) -> str:
    T, L = s["totals"], []
    L += ["COUNCIL OF NICKS — CROSS-RUN STATISTICS", "",
          f"Runs: {T['runs']}  ·  accepted by The Nickquisition: {T['accepted_by_nickquisition']}  ·  verdicts: "
          + (", ".join(f"{k} {v}" for k, v in sorted(T["verdicts"].items())) or "—")]
    if s["skipped"]:
        L.append(f"Skipped: {len(s['skipped'])} (" + "; ".join(f"{x.get('plan', x['journal'])}: {x['reason']}"
                                                               for x in s["skipped"][:5]) + ")")
    L += ["", "Figures come from recorded telemetry only. n/a = not reported; * = some calls did not report it "
          "(the sum covers those that did). Cache reads/writes are part of input, reasoning part of output.", ""]
    L += ["RUNS"] + table(
        ["Plan", "Outcome", "Wall", "Implementer", "Reviewers", "Engine tok", "Host tok", "Total tok", "Failed calls",
         "Retries", "Repairs", "Contract", "Holdout", "Gate"],
        [[r["plan"], f"{r['verdict'] or r['status']} {r['tally'] or ''}".strip(), R.dur(r["wall_seconds"]),
          ", ".join(r["implementer_models"]) or "—", ", ".join(f"{m}×{n}" for m, n in sorted(r["reviewer_models"].items())) or "—",
          _tok(r["usage"]["engine"], "total_tokens"), _tok(r["usage"]["host"], "total_tokens"),
          _tok(r["usage"]["all"], "total_tokens"), r["failed_invocations"], r["retries"], r["repair_rounds"],
          *(f"{x['ok']}/{x['total']}" if x else "n/a" for x in (r["tests"]["contract"], r["tests"]["holdout"])),
          r["tests"]["gate"] or "n/a"] for r in s["runs"]])
    u = T["usage"]
    L += ["", "TOTALS (all runs)"] + table(
        ["Scope", "Calls", "Failed", "Input", "Cache read", "Cache write", "Output", "Reasoning", "Total"],
        [[name, x["calls"], x["failed"], *(_tok(x, f) for f in U.TOKEN_FIELDS)]
         for name, x in (("engine", T["engine_usage"]), ("delivery host", T["host_usage"]), ("all", u))])
    A = s["averages_per_run"]
    L += ["", "AVERAGES PER RUN (over runs with a recorded value)"] + table(
        ["Metric", "Mean", "Runs"],
        [["total tokens", _n(A["total_tokens"]["mean"]), f"{A['total_tokens']['n']}/{A['total_tokens']['of']}"],
         ["wall clock (plan)", R.dur(A["wall_seconds"]["mean"]), f"{A['wall_seconds']['n']}/{A['wall_seconds']['of']}"],
         ["wall clock (delivery host)", R.dur(A["delivery_seconds"]["mean"]),
          f"{A['delivery_seconds']['n']}/{A['delivery_seconds']['of']}"],
         ["worker attempts", _n(A["worker_attempts"]["mean"], 1), f"{A['worker_attempts']['n']}/{A['worker_attempts']['of']}"],
         ["failed invocations", _n(A["failed_invocations"]["mean"], 1),
          f"{A['failed_invocations']['n']}/{A['failed_invocations']['of']}"]])
    for name in ("stage", "role", "model"):
        L += ["", f"BY {name.upper()}"] + table(
            [name.capitalize(), "Calls", "Failed", "Input", "Cache read", "Cache write", "Output", "Reasoning", "Total",
             "Process time"],
            [[k, x["calls"], x["failed"], *(_tok(x, f) for f in U.TOKEN_FIELDS), R.dur(x["process_seconds"])]
             for k, x in s["breakdown"][name].items()])
    C = s["comparisons"]
    L += ["", "MODEL COMPARISON — IMPLEMENTATION"] + table(
        ["Model", "Runs", "Attempts", "Verified", "Success", "Failures", "Attempt time", "Impl tokens", "Tokens/verified"],
        [[m, d["runs"], d["attempts"], d["verified"], _pct(d["success"]),
          ", ".join(f"{k} {v}" for k, v in d["failure_kinds"].items()) or "—", R.dur(d["attempt_seconds"]),
          _tok(d["usage"], "total_tokens"), _n(d["tokens_per_verified_attempt"])] for m, d in C["implementation_by_model"].items()])
    L += ["", "MODEL COMPARISON — COUNCIL REVIEW"] + table(
        ["Model", "Runs", "Reviews", "Brief approvals", "Detailed approvals", "With findings", "Findings credited",
         "Review tokens", "Process time"],
        [[m, d["runs"], d["reviews"], d["shapes"].get("brief approval, no specific findings", 0),
          d["shapes"].get("approval with detailed analysis", 0), d["shapes"].get("findings reported", 0),
          _n(d["findings_credited"]), _tok(d["usage"], "total_tokens"), R.dur(d["usage"]["process_seconds"])]
         for m, d in C["review_by_model"].items()])
    L += ["", "OBJECTIVE TESTS BY IMPLEMENTER"] + table(
        ["Implementer", "Runs", "Integration", "Contract tests", "Holdout tests", "Gate", "Accepted (Nickquisition)"],
        [[m, d["runs"], _pct(d["integration"]), _pct(d["contract"]), _pct(d["holdout"]), _pct(d["gate"]),
          f"{d['nickquisition_accepted']}/{d['runs']}"] for m, d in C["tests_by_implementer"].items()])
    no = C["not_observed"]
    if no["as_implementer"] or no["as_council_reviewer"]:
        L += ["", "Not observed in these runs: " + "; ".join(
            f"{what} {', '.join(ms)}" for what, ms in (("as implementer:", no["as_implementer"]),
                                                       ("as Council reviewer:", no["as_council_reviewer"])) if ms)
              + " (no data, so no comparison)."]
    L += ["", C["note"]]
    Q = s["quality_vs_efficiency"]
    tpt, tpa = Q["tokens_per_verified_task"], Q["tokens_per_accepted_run"]
    lb = lambda m: ("≥ " if m["lower_bound"] else "") + _n(m["value"])
    lbn = lambda m: f" ({m['calls_without_usage']} calls without usage: lower bound)" if m["lower_bound"] else ""
    rf, ot = Q["review_findings"], Q["objective_tests"]
    L += ["", "QUALITY VERSUS EFFICIENCY"] + table(["Metric", "Value", "Basis"], [
        ["tokens per verified task", lb(tpt), f"{_n(tpt['tokens'])} tokens / {tpt['verified_tasks']} tasks" + lbn(tpt)],
        ["tokens per accepted run", lb(tpa), f"{_n(tpa['tokens'])} tokens of all runs / {tpa['accepted_runs']} accepted"
         + lbn(tpa)],
        *[[f"{k} time (wall, mean)", R.dur(Q["time"][k]["mean"]), f"{Q['time'][k]['n']}/{Q['time'][k]['of']} runs; "
           f"process time mean {R.dur(Q['time_process'][k]['mean'])}"] for k in ("implementation", "review", "judging")],
        ["review findings recorded", _n(rf["findings_recorded"]["sum"]),
         f"{rf['findings_recorded']['n']}/{rf['findings_recorded']['of']} runs with a structured reconciliation"],
        ["  of which marked blocking", _n(rf["findings_marked_blocking"]["sum"]), "by the reconciliation's disposition"],
        ["correction tasks added after review", rf["correction_tasks_after_review"], "amendments after the first review"],
        ["runs with more than one review", rf["runs_with_more_than_one_review"], "a correction was reviewed again"],
        ["rework cycles", rf["rework_cycles"], "authorized child cycles after a rejection"],
        ["integration verification passed", _pct(ot["integration_passed"]), "engine-run, per run"],
        ["contract tests ok", _pct(ot["contract_tests_ok"]), "frozen adversarial tests, all runs"],
        ["holdout tests ok", _pct(ot["holdout_tests_ok"]), "first holdout run, all runs"],
        ["objective gate pass", _pct(ot["gate_pass"]), "final judging round per run"],
        ["accepted by The Nickquisition", _pct(Q["nickquisition_acceptance"]), "judges' verdict, not a measurement"]])
    L += ["", rf["note"] + ". Nickquisition acceptance is the judges' assessment; the test rows above are the "
          "independently measured outcomes.", ""]
    return "\n".join(L)


def compute(paths, include_unconcluded: bool = False) -> dict:
    rs, skipped = runs(paths, include_unconcluded)
    return aggregate(rs, skipped)


def to_json(summary: dict) -> str:
    return json.dumps(summary, indent=2, sort_keys=True, default=str)
