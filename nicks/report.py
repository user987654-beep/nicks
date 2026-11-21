"""`nicks report JOB_ID`: a human-readable Markdown account of one Council of Nicks run.

Built only from what the run recorded: the SQLite journal (plans, tasks, attempts, review slots, Nickquisition
rounds, ballots and verdicts), usage telemetry (nicks/telemetry.py, aggregated by nicks/usage.py) and the evidence
files the run kept. No model is called. Quoted text (reviews, justifications, objections) is verbatim, bounded in
length and passed through a credential redactor; nothing is paraphrased or invented. The same journal state always
renders the same report (no generation timestamp).

Layout: `collect(db, ref)` returns a plain dict (reusable by aggregate reporting); `render(data)` turns it into
Markdown; `write(db, ref)` stores `report.md` next to the plan's or job's artifacts.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from . import usage as U

EXCERPT = 900                  # characters quoted from a long text; the full file is linked
REVIEW_EXCERPT = 600
NO_ISSUE = re.compile(r"\b(no\s+(?:actionable|blocking|functional|correctness|significant|material)?\s*"
                      r"(?:issues?|defects?|findings?|problems?|bugs?)\b|found\s+no\b|no\s+blocking\b)", re.I)
SECRETS = [re.compile(p) for p in (
    r"sk-(?:ant-)?[A-Za-z0-9_\-]{16,}", r"\bgh[pousr]_[A-Za-z0-9]{20,}", r"\bAKIA[0-9A-Z]{16}\b",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----", r"\brt_[A-Za-z0-9_\-]{20,}",
    r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S{8,}")]
JUDGES = {   # the characters as the project introduces them (README.md)
    "good": ("😇", "Good Nick", "A fair-minded mentor who wants you to succeed."),
    "evil": ("😈", "Evil Nick", "Hates everything, especially you and your changes."),
    "neutral": ("😐", "Neutral Nick", "A gray suit with a spreadsheet for a soul."),
}
VOTE = {"APPROVE": "✅ APPROVE", "REJECT": "❌ REJECT"}
TERMINAL = ("complete", "rejected", "blocked-by-tests", "incomplete", "stale-review", "failed")


# --------------------------------------------------------------------------- small helpers

def redact(text: str) -> str:
    for p in SECRETS:
        text = p.sub("[REDACTED]", text)
    return text


def read(path, limit: int | None = None) -> str | None:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, TypeError):
        return None
    return redact(text if limit is None else text[:limit * 4])


def excerpt(text: str | None, n: int = EXCERPT) -> tuple[str, bool]:
    text = (text or "").strip()
    if len(text) <= n:
        return text, False
    cut = text.rfind("\n", 0, n)
    return text[:cut if cut > n // 2 else n].rstrip(), True


def quote(text: str) -> str:
    return "\n".join("> " + line if line.strip() else ">" for line in text.splitlines()) or "> (empty)"


def ts(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t)) if t else "—"


def dur(seconds) -> str:
    if seconds is None:
        return "—"
    s = int(round(seconds))
    return f"{s // 3600}h {s // 60 % 60:02d}m {s % 60:02d}s" if s >= 3600 else f"{s // 60}m {s % 60:02d}s" if s >= 60 \
        else f"{seconds:.1f}s"


def num(v) -> str:
    return "n/a" if v is None else f"{v:,}"


def esc(text) -> str:
    return str(text if text is not None else "—").replace("|", "\\|").replace("\n", " ")


def _json(v, default):
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v) if v else default
    except ValueError:
        return default


# --------------------------------------------------------------------------- collection

def resolve(db, ref: str) -> tuple[str, dict]:
    """('plan', plan) or ('job', job) for a plan id, review job id or unique prefix."""
    if ref.startswith("p"):
        plan = db.dev_plan(ref)
        if plan:
            return "plan", plan
    job = db.find_job(ref)
    if job:
        return "job", job
    plan = db.dev_plan(ref)
    if plan:
        return "plan", plan
    raise LookupError(f"no development plan or review job matching {ref!r}")


def invocations(db, job_ids) -> list[dict]:
    ids = [j for j in job_ids if j]
    if not ids:
        return []
    try:
        return db.all(f"SELECT * FROM invocations WHERE job_id IN ({','.join('?' * len(ids))}) ORDER BY started_at, id",
                      tuple(ids))
    except sqlite3.Error:
        return []


def nested_invocations(plan_dir) -> list[dict]:
    """Invocations recorded in journals nested under a plan directory: `dev compete` runs each proposal in a private
    engine journal (competing/<proposal>/engine.db), and telemetry stores a call in its nearest journal, so these
    calls are in no other journal and belong to this plan's run."""
    rows = []
    for j in sorted(Path(plan_dir).rglob("*.db")) if plan_dir and Path(plan_dir).is_dir() else []:
        if "sandbox-home" in j.parts or j.name not in ("engine.db", "nicks.db"):
            continue
        try:
            conn = sqlite3.connect(f"file:{j}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows += [dict(r) for r in conn.execute("SELECT * FROM invocations ORDER BY started_at, id")]
            conn.close()
        except sqlite3.Error:
            continue
    return rows


def host_invocations(db) -> tuple[list[dict], dict | None]:
    """A `dev deliver` engine journal lives in <state>/hosts/<key>/engine; the host's own invocation (Architect
    Nick as the delivery host) is recorded in the parent journal. ([], None) when this is not a delivery."""
    root = Path(db.path).resolve().parent
    if root.name != "engine" or root.parent.parent.name != "hosts":
        return [], None
    key, parent = root.parent.name, root.parent.parent.parent / "nicks.db"
    rows = []
    if parent.is_file():
        try:
            conn = sqlite3.connect(f"file:{parent}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute("SELECT * FROM invocations WHERE job_id=? ORDER BY started_at", (key,))]
            conn.close()
        except sqlite3.Error:
            rows = []
    launch = _json(read(root.parent / "launch.json"), None)
    return rows, launch


def plan_review_jobs(db, plan: dict) -> list[str]:
    ids = []
    try:
        for line in (Path(plan["dir"]) / "events.jsonl").read_text().splitlines():
            ev = _json(line, {})
            if ev.get("job"):
                ids.append(ev["job"])
    except OSError:
        pass
    marker = f"Development Engine plan {plan['id']}"
    for row in db.all("SELECT id, params FROM jobs ORDER BY created_at"):
        if str(_json(row["params"], {}).get("brief") or "").startswith(marker):
            ids.append(row["id"])
    if plan.get("review_job"):
        ids.append(plan["review_job"])
    known = {r["id"]: r for r in db.all("SELECT id, created_at FROM jobs")}
    return sorted(dict.fromkeys(i for i in ids if i in known), key=lambda i: known[i]["created_at"])


def classify_review(text: str | None) -> str:
    """Deterministic label of a Council review's shape (not of its correctness)."""
    t = (text or "").strip()
    if not t:
        return "empty"
    if NO_ISSUE.search(t):
        return "brief approval, no specific findings" if len(t) < 600 else "approval with detailed analysis"
    return "findings reported"


def reconciliation(text: str | None) -> dict:
    """The Findings section of Good Nick's provisional assessment and, per reviewer label, how many findings it
    attributes to that reviewer ("Raised by: Nick B only")."""
    if not text:
        return {"findings": None, "raised": {}, "items": []}
    m = re.search(r"^#{2,4}\s*Findings\b.*?$(.*?)(?=^#{2,4}\s|\Z)", text, re.M | re.S)
    section = m.group(1).strip() if m else None
    raised, items = {}, []
    for item in re.split(r"\n(?=\d+\.\s)", section or ""):
        head = re.match(r"\d+\.\s+\*\*(.+?)\*\*", item.strip())
        by = re.search(r"Raised by:([^\n]*)", item)
        if not head:
            continue
        clause = re.split(r"[;(]", by.group(1))[0] if by else ""      # "B, C, E (...); A and D did not ..."
        who = sorted(set(re.findall(r"\b(?:Nick\s+)?([A-Z])\b", clause)))
        disp = re.search(r"Disposition:\s*([^\n]*)", item)
        items.append({"title": head.group(1), "raised_by": who, "disposition": disp.group(1).strip() if disp else None})
        for w in who:
            raised[w] = raised.get(w, 0) + 1
    return {"findings": section, "raised": raised, "items": items}


def collect_job(db, job: dict, root: Path) -> dict:
    jid = job["id"]
    params, summary = job.get("params") or {}, job.get("summary") or {}
    attempts = db.attempts(jid)
    prov = db.latest_decision(jid, "good", "provisional")
    prov_text = read(prov["path"]) if prov else None
    recon = reconciliation(prov_text)
    kinds = params.get("slot_kinds") or []
    slots = []
    for s in db.slots(jid):
        label = chr(ord("A") + s["slot"]) if s["slot"] < 26 else f"S{s['slot']}"
        mine = [a for a in attempts if a["slot"] == s["slot"]]
        final = next((a for a in mine if a["id"] == s["attempt_id"]), None)
        text = read(Path(final["dir"]) / "response.txt") if final else None
        slots.append({"label": label, "kind": kinds[s["slot"]] if s["slot"] < len(kinds) else None,
                      "status": s["status"], "note": s["note"], "final": final, "attempts": mine, "text": text,
                      "shape": classify_review(text) if final else "no usable review",
                      "raised": recon["raised"].get(label, 0)})
    runs = db.adversary_runs(jid)
    rounds = []
    for r in db.all("SELECT * FROM nq_rounds WHERE job_id=? ORDER BY round", (jid,)):
        ballots = {b["member"]: b for b in db.all("SELECT * FROM nq_ballots WHERE job_id=? AND round=?", (jid, r["round"]))}
        verdict = db.one("SELECT * FROM nq_verdicts WHERE job_id=? AND round=? ORDER BY id DESC LIMIT 1", (jid, r["round"]))
        rounds.append({"round": r["round"], "artifact": r["artifact"], "gates": _json(r["gates"], {}),
                       "evidence": r["evidence_path"], "opened_at": r["opened_at"], "ballots": ballots,
                       "verdict": dict(verdict, detail=_json(verdict["detail"], {})) if verdict else None})
    return {"job": job, "params": params, "summary": summary, "slots": slots, "attempts": attempts,
            "provisional": prov, "provisional_text": prov_text, "reconciliation": recon, "runs": runs,
            "neutral_draws": db.neutral_selections(jid), "rounds": rounds, "root": root,
            "strict": bool(params.get("strict_composition"))}


def collect(db, ref: str) -> dict:
    kind, obj = resolve(db, ref)
    root = Path(db.path).resolve().parent
    host_rows, launch = host_invocations(db)
    if kind == "job":
        jd = collect_job(db, obj, root)
        rows = invocations(db, [obj["id"]])
        return {"kind": "job", "root": root, "job": jd, "reviews": [jd], "invocations": rows, "host": [],
                "launch": None, "title": obj["id"]}
    plan = obj
    detail = plan.get("detail") or {}
    review_ids = plan_review_jobs(db, plan)
    reviews = [collect_job(db, db.get_job(j), root) for j in review_ids]
    tasks = db.dev_tasks(plan["id"])
    attempts = db.dev_attempts(plan["id"])
    implementers = _json(read(Path(plan["dir"]) / "implementers.json"), {})
    events = [_json(line, {}) for line in (read(Path(plan["dir"]) / "events.jsonl") or "").splitlines()]
    rows = invocations(db, [plan["id"], *review_ids]) + nested_invocations(plan.get("dir"))
    concluded = [e for e in events if e.get("event") == "concluded"]
    return {"kind": "plan", "root": root, "plan": plan, "detail": detail, "tasks": tasks, "attempts": attempts,
            "implementers": implementers, "events": events, "concluded": concluded[-1] if concluded else None,
            "reviews": reviews, "invocations": rows, "host": host_rows, "launch": launch, "title": plan["id"],
            "request": read(Path(plan["dir"]) / "request.txt") or plan.get("request")}


# --------------------------------------------------------------------------- rendering

def _rel(path, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(root))
    except (ValueError, TypeError, OSError):
        return str(path)


FAILED_ROUTES = "failed routes without usage"


def _collapse_failed(rows, key, where: str = "listed below"):
    """Model key that folds routes whose every call failed without reported usage into one row once there are
    more than three of them (each row still lands in exactly one group, so totals are unchanged)."""
    per: dict = {}
    for r in rows:
        per.setdefault(key(r), []).append(r)
    dead = {k for k, rs in per.items()
            if all(x.get("outcome") != "success" and x.get("total_tokens") is None for x in rs)}
    if len(dead) <= 3:
        return key
    return lambda r: f"{FAILED_ROUTES} ({len(dead)} models; {where})" if key(r) in dead else key(r)


def _usage_table(groups: dict, label: str, order=None) -> list[str]:
    keys = sorted(groups, key=order or (lambda k: (-(groups[k].value("total_tokens") or 0), str(k))))
    out = [f"| {label} | Calls | Failed | Usage n/a | Input | ↳ cache read | ↳ cache write | Output | ↳ reasoning | "
           "Total | Process time |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    flagged = False
    for k in keys:
        u = groups[k]
        cells = []
        for f in U.TOKEN_FIELDS:
            v = u.value(f)
            mark = "*" if v is not None and u.missing(f) else ""
            flagged |= bool(mark)
            cells.append(num(v) + mark)
        na = u.usage_status.get("unavailable", 0)
        out.append(f"| {esc(k)} | {u.calls} | {u.failed} | {na} | " + " | ".join(cells) + f" | {dur(u.process_seconds)} |")
    return out, flagged


def _usage_section(data: dict) -> list[str]:
    rows, host = data["invocations"], data["host"]
    L = ["## 📊 Token usage and time", ""]
    if not rows and not host:
        return L + ["No model invocations were recorded for this run (telemetry predates it, or nothing ran).", ""]
    eng, hst, allu = U.summarize(rows), U.summarize(host), U.summarize(rows + host)
    L += ["Every figure below is summed from the run's `invocations` telemetry rows; nothing is estimated. Cache "
          "reads/writes are part of *input* and reasoning is part of *output* (shown with ↳, never added again); "
          "total = input + output. `n/a` means no call in that row reported the value; `*` means some calls did "
          "not, and the sum covers only those that did. Failed and retried calls are included.", ""]
    L += ["| Scope | Calls | Failed | Usage reported | Partial | Unavailable | Total tokens |", "|---|---:|---:|---:|---:|---:|---:|"]
    scopes = [("Engine (workers, reviewers, judges)", eng)]
    if host:
        scopes.append(("Delivery host (Architect Nick, parent journal)", hst))
        scopes.append(("**All recorded**", allu))
    for name, u in scopes:
        L.append(f"| {name} | {u.calls} | {u.failed} | {u.usage_status.get('reported', 0)} | "
                 f"{u.usage_status.get('partial', 0)} | {u.usage_status.get('unavailable', 0)} | {num(u.value('total_tokens'))} |")
    every = rows + host
    flags = False
    for title, key, label, order in (("By stage", U.stage_key, "Stage", U.stage_sort),
                                     ("By role", U.role_key, "Role", None),
                                     ("By model", _collapse_failed(rows + host, U.model_key), "Model", None)):
        table, f = _usage_table(U.group(every, key), label, order)
        flags |= f
        L += ["", f"### {title}", ""] + table
    if flags:
        L += ["", "\\* some calls in this row did not report this value; the sum covers only the calls that did."]
    unavailable = [r for r in every if r.get("usage_status") != "reported"]
    if unavailable:
        L += ["", "<details><summary>Calls without complete usage</summary>", "",
              "| Stage | Role | Engine | Model | Outcome | Error | Usage |", "|---|---|---|---|---|---|---|"]
        for r in unavailable:
            L.append(f"| {esc(U.stage_key(r))} | {esc(r.get('role'))} | {esc(r['engine'])} | {esc(U.model_key(r))} | "
                     f"{esc(r['outcome'])} | {esc(r.get('error_class'))} | {esc(r.get('usage_status'))} |")
        L += ["", "</details>"]
    phases = U.group(every, U.stage_key)
    L += ["", "### Time by stage", "", "| Stage | Calls | Started | Wall-clock span | Process time (sum) |",
          "|---|---:|---|---:|---:|"]
    for k in sorted(phases, key=U.stage_sort):
        u = phases[k]
        L.append(f"| {esc(k)} | {u.calls} | {ts(u.first_start)} | {dur(u.span_seconds)} | {dur(u.process_seconds)} |")
    L += ["", "Wall-clock span runs from the stage's first model launch to its last finish; process time adds up "
          "every call, so it exceeds the span when calls ran in parallel. Orchestrator work between model calls "
          "(tests, integration) is not inside any call.", ""]
    return L


def _summary_plan(data: dict) -> list[str]:
    plan, detail = data["plan"], data["detail"]
    verdict = detail.get("verdict") or {}
    end = (data["concluded"] or {}).get("t")
    rows = data["invocations"] + data["host"]
    last = max([r.get("finished_at") or 0 for r in rows] + [a.get("finished_at") or 0 for a in data["attempts"]] + [0])
    status = plan.get("status") or "planned"
    wall = (end or last or None) and ((end or last) - plan["created_at"])
    impl = sorted({a["actual_model"] or a["worker"] for a in data["attempts"] if a["engine"] != "integration"})
    from collections import Counter
    reviewers = Counter(s["final"]["actual_model"] or s["final"]["route"] for r in data["reviews"][-1:]
                        for s in r["slots"] if s["final"])
    judges = []
    for r in data["reviews"]:
        for rnd in r["rounds"]:
            for m in ("good", "evil", "neutral"):
                b = rnd["ballots"].get(m)
                if b and b.get("model"):
                    judges.append(f"{JUDGES[m][1]}: {b['model']}")
    host_models = sorted({h.get("model_actual") or h.get("model_requested") for h in data["host"]} - {None})
    jobs = ", ".join("`" + r["job"]["id"] + "`" for r in data["reviews"])
    outcome = (f"**{verdict['verdict']}** ({verdict.get('tally')}) by The Nickquisition" if verdict.get("verdict")
               else "not judged yet" if status not in TERMINAL else status)
    L = [f"# 🏛️ Council of Nicks report — `{plan['id']}`", ""]
    L += [f"> **{esc(data['plan']['manifest'].get('feature') or 'Development plan')}** · status **{status}** · "
          f"outcome {outcome} · wall clock {dur(wall)}" + ("" if status in TERMINAL else " (run not concluded)"), ""]
    L += ["## 🧾 Execution summary", "", "| | |", "|---|---|",
          f"| Plan | `{plan['id']}` |", f"| Status | {status} |", f"| Final outcome | {outcome} |",
          f"| Started | {ts(plan['created_at'])} |",
          f"| Concluded | {ts(end) if end else '— (not concluded)'} |", f"| Wall clock | {dur(wall)} |",
          f"| Review job(s) | {jobs or '—'} |"]
    if data["host"] or data["launch"]:
        L.append(f"| Architect Nick (delivery host) | {', '.join(host_models) or (data['launch'] or {}).get('model') or '—'} |")
        secs = (data["launch"] or {}).get("seconds")
        if secs is not None:
            L.append(f"| Delivery wall clock (host process, includes planning before the plan existed) | {dur(secs)} |")
    else:
        L.append("| Architect Nick | interactive host session (outside the engine; its usage is not recorded here) |")
    L += [f"| Implementers | {', '.join(impl) or '—'} |", f"| Council reviewers (final review) | {', '.join(f'{m} ×{n}' for m, n in sorted(reviewers.items())) or '—'} |",
          f"| Judges | {'; '.join(dict.fromkeys(judges)) or '—'} |", ""]
    if data.get("request"):
        req, cut = excerpt(data["request"], 700)
        L += ["**Request** (verbatim" + (", truncated" if cut else "") + "):", "", quote(req), ""]
    return L


def _planning(data: dict) -> list[str]:
    d = data["detail"]
    L = ["## 🗺️ Planning", ""]
    deb = d.get("debate") or {}
    if deb:
        ended = deb.get("ended") or {}
        L.append(f"- **Stake-Based Design Debate:** {deb.get('status', '—')} after {deb.get('turns', '—')} turn(s); "
                 f"bets {deb.get('bets', 0)}, calls {deb.get('calls', 0)}, folds {deb.get('folds', 0)}; "
                 f"unresolved {len(deb.get('unresolved') or [])}." + (f" Recorded reason: “{esc(ended.get('reason'))}”"
                                                                       if ended.get("reason") else ""))
    adv = d.get("adversarial") or {}
    if adv:
        c = adv.get("counts") or {}
        gens = ", ".join(f"{g.get('model')} ({'ok' if g.get('ok') else g.get('error_class')})"
                         for g in adv.get("generations") or [])
        L.append(f"- **Adversarial test design:** {adv.get('status', '—')}; tests accepted {c.get('accepted', 0)}, "
                 f"uncertain {c.get('uncertain', 0)}, rejected {c.get('rejected', 0)}; designer: {gens or '—'}.")
    stories = data["plan"]["manifest"].get("stories") or []
    if stories:
        L.append(f"- **Stories:** " + "; ".join(f"{s['id']} {esc(s.get('title'))}" for s in stories) + ".")
    if not deb and not adv:
        L.append("- No debate or adversarial test design was recorded for this plan.")
    return L + [""]


def _implementation(data: dict) -> list[str]:
    tasks, atts, impl = data["tasks"], data["attempts"], data["implementers"]
    workers = [a for a in atts if a["engine"] != "integration"]
    L = ["## 🔨 Implementation", ""]
    if impl:
        L.append("Story implementers (drawn once per story): " + ", ".join(f"{k} → **{v}**" for k, v in sorted(impl.items())))
        L.append("")
    L += ["| Task | Story | Goal | Attempts | Implemented by | Repairs | Attempt time (incl. verification) | Status |",
          "|---|---|---|---:|---|---:|---:|---|"]
    for t in tasks:
        mine = [a for a in workers if a["task_id"] == t["task_id"]]
        ok = next((a for a in mine if a["status"] == "verified"), None)
        secs = sum((a["finished_at"] or a["started_at"]) - a["started_at"] for a in mine)
        goal, _ = excerpt(t["spec"].get("goal"), 90)
        L.append(f"| {t['task_id']} | {esc(t['spec'].get('story'))} | {esc(goal)} | {len(mine)} | "
                 f"{esc((ok or {}).get('actual_model') or (ok or {}).get('worker'))} | "
                 f"{sum(a.get('repairs') or 0 for a in mine)} | {dur(secs) if mine else '—'} | {t['status']} |")
    failed = [a for a in workers if a["status"] != "verified"]
    retries = len(workers) - len({a["task_id"] for a in workers})
    story_of = {t["task_id"]: t["spec"].get("story") for t in tasks}
    subs = (data["detail"].get("execution_policy") or {}).get("council_subscription") or {}
    drawn = {tid: (subs.get(impl.get(story)) or {}).get("model") for tid, story in story_of.items()}
    fallbacks = [a for a in workers if a["status"] == "verified" and drawn.get(a["task_id"])
                 and drawn[a["task_id"]] not in (a["actual_model"], a["worker"].split("/", 1)[-1])]
    L += ["", f"Worker attempts: **{len(workers)}** · retries: **{retries}** · failed attempts: **{len(failed)}** · "
          f"repair rounds: **{sum(a.get('repairs') or 0 for a in workers)}** · implemented by a model other than the "
          f"story's drawn implementer: **{len(fallbacks) if impl else 'n/a (no per-story draw recorded)'}** · "
          f"integration steps: "
          f"**{sum(1 for a in atts if a['engine'] == 'integration')}**", ""]
    if failed:
        L += ["| Attempt | Task | Worker | Status | Failure kind | Error |", "|---|---|---|---|---|---|"]
        for a in failed:
            err, _ = excerpt(redact(a.get("error") or ""), 140)
            L.append(f"| `{a['id'][-12:]}` | {a['task_id']} | {esc(a['worker'])} | {a['status']} | "
                     f"{esc(a.get('failure_kind'))} | {esc(err) if err else '—'} |")
        L.append("")
    return L


def _tests(data: dict) -> list[str]:
    d = data["detail"]
    L = ["## 🧪 Tests and acceptance", ""]
    integ = d.get("integration") or {}
    if integ:
        L.append(f"- **Integration verification** on `{(integ.get('head') or '')[:12]}`: "
                 + ("**passed**" if integ.get("passed") else "**failed**") + "; "
                 + "; ".join(f"`{esc(r.get('command'))}` → exit {r.get('exit_code')}" for r in integ.get("runs") or []))
    adv = d.get("adversarial") or {}
    for key, name in (("contract_run", "Contract tests (frozen)"), ("holdout_first_run", "Holdout tests (first run)")):
        run = adv.get(key)
        if run and isinstance(run.get("results"), dict):
            res = run["results"]
            ok = sum(1 for v in res.values() if v == "ok")
            bad = sorted(k for k, v in res.items() if v != "ok")
            L.append(f"- **{name}:** {ok}/{len(res)} ok" + (f"; not ok: {', '.join(f'`{b}`' for b in bad[:8])}" if bad else ""))
    for r in data["reviews"]:
        for rnd in r["rounds"]:
            g = rnd["gates"] or {}
            if g:
                L.append(f"- **Objective gate** (job `{r['job']['id']}`, round {rnd['round']}): **{g.get('status')}** — "
                         f"{esc(g.get('summary'))}" + (f" (`{esc(g.get('command'))}`)" if g.get("command") else ""))
    if len(L) == 2:
        L.append("- No test results were recorded.")
    L += ["", "These are the engine's own test runs. They, and not any model's statement, are the run's objective "
          "evidence; acceptance by The Nickquisition (below) is a judgement made on top of them.", ""]
    return L


def _reviews(jd: dict) -> list[str]:
    job, slots = jd["job"], jd["slots"]
    summ = jd["summary"]
    L = [f"## 🕵️ Council review — `{job['id']}`", ""]
    comp = ", ".join(f"{s['label']}={s['kind']}" for s in slots if s["kind"]) or "—"
    L.append(f"{len(slots)} blind slot(s) ({comp})" + (" · strict composition: a slot is filled only by its own model"
                                                      if jd["strict"] else "") +
             f" · usable {summ.get('usable', '—')}/{summ.get('requested', len(slots))} · job status **{job['status']}**.")
    subs = {f"{v.get('provider')}/{v.get('model')}": k
            for k, v in ((jd["params"].get("execution_policy") or {}).get("council_subscription") or {}).items()}
    L += ["", "| Reviewer | Planned slot | Filled by | Attempts | Result | Shape of the review | "
          "Findings credited in reconciliation |", "|---|---|---|---:|---|---|---:|"]
    for s in slots:
        f = s["final"] or {}
        filled = subs.get(f.get("route"), "free") if f else None
        moved = " (fallback)" if f and s["kind"] and filled != s["kind"] else ""
        L.append(f"| Nick {s['label']} | {esc(s['kind'])} | {esc(f.get('actual_model') or f.get('route'))}{moved} | "
                 f"{len(s['attempts'])} | "
                 f"{s['status']} | {s['shape']} | {s['raised'] if jd['reconciliation']['items'] else 'not recorded'} |")
    failed = [a for a in jd["attempts"] if a["status"] != "ok"]
    if failed:
        by: dict = {}
        for a in failed:
            msg, _ = excerpt(redact(a.get("error") or ""), 110)
            by.setdefault((a["status"], a.get("error_class"), msg), []).append(a["route"])
        L += ["", f"**Failed or replaced attempts: {len(failed)}** (retries and fallbacks before a slot was filled):", "",
              "| Count | Status | Error class | Recorded error | Routes |", "|---:|---|---|---|---|"]
        for (st, cls, msg), routes in sorted(by.items(), key=lambda x: (-len(x[1]), str(x[0]))):
            L.append(f"| {len(routes)} | {st} | {esc(cls)} | {esc(msg) if msg else '—'} | "
                     f"{esc(', '.join(sorted(set(routes))))} |")
    L += ["", "Shape is a mechanical label of the text (length and an explicit no-issue statement), not a quality "
          "judgement. Reviews are blind: each reviewer saw only the frozen change, never another review.", ""]
    for s in slots:
        if not s["final"]:
            L += [f"**Nick {s['label']}** — no usable review" + (f" ({esc(s['note'])})" if s["note"] else "") + ".", ""]
            continue
        txt, cut = excerpt(s["text"], REVIEW_EXCERPT)
        L += [f"**Nick {s['label']}** · {esc(s['final'].get('actual_model') or s['final']['route'])} · {s['shape']}"
              + (f" · full text: `{_rel(Path(s['final']['dir']) / 'response.txt', jd['root'])}`" if cut else ""), "",
              quote(txt + (" …" if cut else "")), ""]
    recon = jd["reconciliation"]
    if jd["provisional"]:
        L += [f"### Reconciliation — Good Nick's provisional assessment: **{jd['provisional']['decision']}**", ""]
        if recon["items"]:
            L += ["| Finding | Raised by | Disposition (verbatim) |", "|---|---|---|"]
            for it in recon["items"]:
                L.append(f"| {esc(it['title'])} | {', '.join('Nick ' + w for w in it['raised_by']) or '—'} | "
                         f"{esc(it['disposition'])} |")
            L.append("")
        elif recon["findings"]:
            sec, cut = excerpt(recon["findings"])
            L += ["Findings section (verbatim" + (", truncated" if cut else "") + "):", "", quote(sec), ""]
        else:
            L += ["(No structured findings section in the provisional assessment.) Full text: "
                  f"`{_rel(jd['provisional']['path'], jd['root'])}`", ""]
    return L


def _judge(member: str, b: dict | None, runs: list, draws: list, jd: dict) -> list[str]:
    icon, name, tagline = JUDGES[member]
    vote = ("— no ballot" if not b else VOTE.get(b.get("vote"), "⚠️ no valid vote") if b.get("valid")
            else "⚠️ INVALID ballot (not counted)")
    L = [f"### {icon} {name} — {vote}", "", f"*{tagline}*", ""]
    if not b:
        return L + ["**No ballot was recorded.**", ""]
    L += [f"**Model:** {esc(b.get('model'))} · ballot {esc(b.get('source'))}", ""]
    if member == "neutral" and draws:
        L += ["Drawn at random: " + "; ".join(f"{d['provider']}/{d['model']} ({d['reason']})" for d in draws), ""]
    if not b.get("valid") and b.get("reason"):
        L += [f"Why the ballot was not counted (recorded): {esc(b['reason'])}", ""]
    L += ["**Recorded justification** (verbatim):", "", quote(redact(b.get("justification") or "(none recorded)")), ""]
    stages = {"evil": (("attack", "Blind attack on the change"), ("challenge", "Challenge to Good Nick's provisional decision")),
              "neutral": (("review", "Independent blind review"),),
              "good": (("investigation", "Own investigation"),)}[member]
    for stage, title in stages:
        done = [r for r in runs if r["member"] == member and r["stage"] == stage and r["status"] == "ok"]
        if not done:
            continue
        txt = read(Path(done[-1]["dir"]) / "response.txt")
        if txt is None:
            L += [f"*{title}:* evidence file missing.", ""]
            continue
        part, cut = excerpt(txt)
        L += [f"<details><summary>{title} · {esc(done[-1].get('model'))}"
              + (" (excerpt)" if cut else "") + "</summary>", "", quote(part + (" …" if cut else "")),
              "", f"Full text: `{_rel(Path(done[-1]['dir']) / 'response.txt', jd['root'])}`", "", "</details>", ""]
    if b.get("deliberation"):
        L += [f"Deliberation: `{_rel(b['deliberation'], jd['root'])}`", ""]
    return L


def _nickquisition(jd: dict) -> list[str]:
    L = []
    for rnd in jd["rounds"]:
        v = rnd["verdict"] or {}
        det = v.get("detail") or {}
        L += [f"## ⚖️ The Nickquisition — job `{jd['job']['id']}`, round {rnd['round']}", "",
              "*The Council does the work. The Nickquisition judges it. Two votes decide. No single Nick rules.*", ""]
        g = rnd["gates"] or {}
        L += [f"Three judges, one identical evidence package, artifact `{rnd['artifact'][:16]}…`. "
              f"Objective gate: **{g.get('status', '—')}**" + (f" — {esc(g.get('summary'))}" if g.get("summary") else "") + ".", ""]
        L += ["| | Judge | Model | Vote |", "|---|---|---|---|"]
        for m in ("good", "evil", "neutral"):
            b = rnd["ballots"].get(m)
            vote = (VOTE.get(b.get("vote"), "⚠️") if b.get("valid") else "⚠️ invalid") if b else "— no ballot"
            L.append(f"| {JUDGES[m][0]} | {JUDGES[m][1]} | {esc((b or {}).get('model'))} | {vote} |")
        L.append("")
        for m in ("good", "evil", "neutral"):
            L += _judge(m, rnd["ballots"].get(m), jd["runs"], jd["neutral_draws"] if m == "neutral" else [], jd)
        if not v:
            L += ["### 🔨 Verdict: **not yet computed**", "", "The round is open; no verdict has been recorded.", ""]
            continue
        votes = det.get("votes") or {}
        board = " · ".join(f"{JUDGES[m][0]} {VOTE.get(votes.get(m), '—').split()[0]}" for m in ("good", "evil", "neutral"))
        cast = [x for x in votes.values() if x in VOTE]
        split = ("unanimous" if cast and len(set(cast)) == 1 and len(cast) == 3 else
                 "split decision" if len(set(cast)) > 1 else "incomplete bench")
        L += [f"### 🔨 Verdict: **{v['verdict']}** ({v['tally']})", "", f"**{board} → {v['verdict']} {v['tally']}, "
              f"{split}.**", "",
              "Computed by the orchestrator from the recorded ballots and the objective gate (2 of 3 decide); "
              "the rationale below is the record, not a summary written for this report:", ""]
        L.append("- Votes: " + ", ".join(f"{JUDGES[m][1]} {votes.get(m, '—')}" for m in ("good", "evil", "neutral")
                                         if m in JUDGES))
        if det.get("majority_vote"):
            L.append(f"- Majority: {det['majority_vote']}")
        L.append("- Dissent: " + ("; ".join(map(str, det.get("dissent"))) if det.get("dissent") else "none"))
        L.append("- Blockers: " + ("; ".join(map(str, det.get("blockers"))) if det.get("blockers") else "none"))
        if det.get("missing"):
            L.append("- Missing ballots: " + ", ".join(map(str, det["missing"])))
        if det.get("invalid"):
            L.append("- Invalid ballots: " + "; ".join(f"{k}: {esc(x)}" for k, x in det["invalid"].items()))
        if det.get("unmet"):
            L.append("- Unmet conditions: " + "; ".join(map(str, det["unmet"])))
        L.append(f"- Ballot integrity: {det.get('integrity', '—')}; evidence delivery: {det.get('delivery_integrity', '—')}")
        L.append(f"- Objective gate: {g.get('status', '—')}")
        L += ["", f"Engine verdict record: `{_rel(v['report_path'], jd['root'])}`", "",
              "> **What this verdict means.** It is the three judges' recorded assessment of the evidence. It is not "
              "an independent verification of code quality: the objective evidence is the engine's test runs above, "
              "and the judges state in their own words what they did and did not check.", ""]
    if not jd["rounds"]:
        L += [f"## ⚖️ The Nickquisition — job `{jd['job']['id']}`", "",
              "The Nickquisition has not convened for this job; no ballots or verdict are recorded.", ""]
    return L


def _evidence(data: dict) -> list[str]:
    root = data["root"]
    L = ["## 📁 Evidence", ""]
    if data["kind"] == "plan":
        L.append(f"- Plan directory: `{_rel(data['plan']['dir'], root)}`")
        deb = (data["detail"].get("debate") or {}).get("transcript")
        if deb:
            L.append(f"- Debate transcript: `{_rel(deb, root)}`")
    for r in data["reviews"]:
        pk = (r["summary"] or {}).get("packet")
        if pk:
            L.append(f"- Council packet (`{r['job']['id']}`): `{_rel(pk, root)}`")
        for rnd in r["rounds"]:
            L.append(f"- Nickquisition evidence, round {rnd['round']}: `{_rel(rnd['evidence'], root)}`")
    L.append(f"- Journal: `{_rel(Path(root) / 'nicks.db', root)}` (paths above are relative to `{root}`)")
    return L + [""]


def render(data: dict) -> str:
    if data["kind"] == "plan":
        L = _summary_plan(data) + _planning(data) + _implementation(data) + _tests(data) + _usage_section(data)
    else:
        jd = data["job"]
        job = jd["job"]
        end = max([job.get("updated_at") or 0] + [(rnd["verdict"] or {}).get("created_at") or 0 for rnd in jd["rounds"]])
        L = [f"# 🏛️ Council of Nicks report — review job `{job['id']}`", "",
             "| | |", "|---|---|", f"| Job | `{job['id']}` ({job['kind']}) |", f"| Status | {job['status']} |",
             f"| Started | {ts(job['created_at'])} |", f"| Last activity | {ts(end)} |",
             f"| Wall clock | {dur(end - job['created_at'])} |", ""]
        L += _usage_section(data)
    for jd in data["reviews"]:
        L += _reviews(jd) + _nickquisition(jd)
    L += _evidence(data)
    L += ["---", "*Generated by `nicks report` from recorded data only; no model was consulted. Quoted texts are "
          "verbatim (shortened where marked, credentials redacted).*", ""]
    return "\n".join(L)


def path_for(data: dict) -> Path:
    if data["kind"] == "plan":
        return Path(data["plan"]["dir"]) / "report.md"
    from .review import job_dir
    jd = job_dir(data["job"]["job"]["id"])
    return jd / "report.md" if jd.exists() else data["root"] / "jobs" / data["job"]["job"]["id"] / "report.md"


def write(db, ref: str) -> Path:
    data = collect(db, ref)
    out = path_for(data)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(render(data), encoding="utf-8")
    tmp.replace(out)
    return out
