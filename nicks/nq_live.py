"""The Nickquisition in the terminal, as it happens.

Two moments, both built only from what the journal recorded (no model call, nothing paraphrased):

* the *announcement*, when a judging round opens (the Council's blind reviews and reconciliation have passed the
  gate). It names the round and the objective gate and shows no ballot;
* the *panel*, once the orchestrator has recorded the round's verdict. A verdict exists only after every
  required ballot is locked, so no judge's vote or justification is ever shown before all are cast.

`render_panel` reuses the report collector (nicks/report.py) and its redaction. `Watcher` is used by
`nicks dev deliver` to show both moments in the user's terminal while the delivery host is still working; it
reads the delivery's engine journal read-only and remembers what it has shown, so a resumed run never repeats a
panel. Nothing here changes voting, verdicts or acceptance.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import textwrap
import threading
from pathlib import Path

from . import report as R

WIDTH = 78
FLAVOUR = {"APPROVED": "The Council's work stands.", "REJECTED": "Back to the workshop.",
           "BLOCKED": "The objective gate holds the line.", "INCOMPLETE": "The bench was not complete; no ruling.",
           "INTEGRITY-FAILURE": "The record failed its integrity check; no ruling."}
VOTE = {"APPROVE": "✅ APPROVE", "REJECT": "❌ REJECT"}
COLOURS = {"bold": "1", "green": "32", "red": "31", "yellow": "33", "dim": "2"}


class Style:
    def __init__(self, colour: bool):
        self.colour = colour

    def __call__(self, text: str, *names: str) -> str:
        if not self.colour or not names:
            return text
        return f"\033[{';'.join(COLOURS[n] for n in names)}m{text}\033[0m"


def use_colour(stream) -> bool:
    return hasattr(stream, "isatty") and stream.isatty() and not os.environ.get("NO_COLOR")


def _wrap(text: str, indent: str, limit: int = 1400) -> list[str]:
    body, cut = R.excerpt(R.redact(text or ""), limit)
    lines = []
    for para in (body + (" …" if cut else "")).splitlines() or [""]:
        lines += textwrap.wrap(para, WIDTH - len(indent), break_long_words=True) or [""]
    return [indent + ln for ln in lines]


def _job(db, job_id: str) -> dict:
    job = db.get_job(job_id)
    if not job:
        raise LookupError(f"no job {job_id}")
    return R.collect_job(db, job, Path(db.path).resolve().parent)


def render_announcement(db, job_id: str, rnd: int, colour: bool = False) -> str:
    s = Style(colour)
    row = db.one("SELECT * FROM nq_rounds WHERE job_id=? AND round=?", (job_id, rnd)) or {}
    gates = R._json(row.get("gates"), {})
    bar = "═" * WIDTH
    return "\n".join([
        "", s(bar, "bold"), s(f" ⚖️  THE NICKQUISITION CONVENES — job {job_id}, round {rnd}", "bold"), s(bar, "bold"),
        " The Council's blind reviews and reconciliation are complete. Three judges now deliberate",
        " independently: 😇 Good Nick, 😈 Evil Nick and 😐 Neutral Nick. Ballots stay sealed until",
        " every vote is locked.",
        f" Objective gate: {str(gates.get('status', '—')).upper()}" + (f" — {gates['summary']}" if gates.get("summary") else ""),
        ""])


def render_panel(db, job_id: str, rnd: int | None = None, colour: bool = False) -> str:
    """The full judgement of one round. Empty string while the round has no recorded verdict (ballots sealed)."""
    s = Style(colour)
    jd = _job(db, job_id)
    rounds = [r for r in jd["rounds"] if rnd is None or r["round"] == rnd]
    if not rounds or not rounds[-1]["verdict"]:
        return ""
    r = rounds[-1]
    v, det = r["verdict"], r["verdict"]["detail"] or {}
    votes, dissent = det.get("votes") or {}, det.get("dissent") or []
    dissenters = set(dissent) | {m for m, x in votes.items() if det.get("majority_vote") and x != det["majority_vote"]}
    bar, rule = "═" * WIDTH, "─" * WIDTH
    g = r["gates"] or {}
    L = ["", s(bar, "bold"), s(f" ⚖️  THE NICKQUISITION — job {job_id}, round {r['round']}", "bold"), s(bar, "bold"),
         " The Council does the work. The Nickquisition judges it. Two votes decide.",
         f" Objective gate: {str(g.get('status', '—')).upper()}" + (f" — {g['summary']}" if g.get("summary") else ""), ""]
    for m in ("good", "evil", "neutral"):
        icon, name, tagline = R.JUDGES[m]
        b = r["ballots"].get(m)
        if not b:
            L += [f" {icon} {s(name.upper(), 'bold')}  — no ballot recorded", ""]
            continue
        if b.get("valid"):
            vote = VOTE.get(b.get("vote"), b.get("vote") or "—")
            vote = s(vote, "green" if b.get("vote") == "APPROVE" else "red")
        else:
            vote = s("⚠️ INVALID BALLOT (not counted)", "yellow")
        flag = s("   ◀ DISSENT", "yellow", "bold") if m in dissenters else ""
        L += [f" {icon} {s(name.upper(), 'bold')}  ({b.get('model') or 'model not recorded'})  {vote}{flag}",
              s(f"    “{tagline}”", "dim")]
        if not b.get("valid") and b.get("reason"):
            L += _wrap(f"Why it was not counted: {b['reason']}", "    ")
        just = b.get("justification")
        L += (_wrap(just, "    │ ") if just else ["    │ (no justification recorded)"]) + [""]
    concerns = []
    concerns += [("BLOCKER", x) for x in det.get("blockers") or []]
    concerns += [("DISSENT", f"{R.JUDGES.get(m, ('', m, ''))[1]} voted {votes.get(m)} against the majority "
                             f"({det.get('majority_vote')}); justification above") for m in sorted(dissenters)]
    concerns += [("INVALID BALLOT", f"{R.JUDGES.get(k, ('', k, ''))[1]}: {x}") for k, x in (det.get("invalid") or {}).items()]
    concerns += [("MISSING BALLOT", R.JUDGES.get(x, ("", x, ""))[1]) for x in det.get("missing") or []]
    concerns += [("UNMET", x) for x in det.get("unmet") or []]
    if str(g.get("status", "")).lower() in ("fail", "failed", "blocked") or g.get("blocking"):
        concerns.append(("OBJECTIVE GATE", g.get("summary") or g.get("status")))
    L.append(s(rule, "dim"))
    if concerns:
        L.append(s(" ⚠️  Objections, disagreements and blocking findings (as recorded):", "yellow", "bold"))
        for kind, text in concerns:
            L += _wrap(f"{kind}: {text}", "    • ", 400)
    else:
        L.append(" No dissent, no blockers, no invalid or missing ballots were recorded.")
    cast = [x for x in votes.values() if x in VOTE]
    kind = ("unanimous" if len(cast) == 3 and len(set(cast)) == 1 else
            "split decision" if len(set(cast)) > 1 else "incomplete bench")
    colour_v = {"APPROVED": "green", "REJECTED": "red"}.get(v["verdict"], "yellow")
    board = "  ".join(f"{R.JUDGES[m][0]} {VOTE.get(votes.get(m), '— ').split()[0]}" for m in ("good", "evil", "neutral"))
    L += ["", s(f" 🔨 VERDICT: {v['verdict']} {v['tally'].replace('-', '–')}  ({kind})", colour_v, "bold"),
          f"    {board}   · {FLAVOUR.get(v['verdict'], '')}",
          s("    The judges' recorded assessment, computed by the orchestrator (2 of 3 decide). The engine's", "dim"),
          s("    test runs are the measured evidence; this verdict is not independent proof of quality.", "dim"),
          f"    Full record: {v['report_path']}", s(bar, "bold"), ""]
    return "\n".join(L)


def print_panel(db, job_id: str, rnd: int | None = None, stream=None) -> bool:
    stream = stream or sys.stdout
    try:
        text = render_panel(db, job_id, rnd, colour=use_colour(stream))
    except Exception as exc:                      # display must never change a command's outcome
        print(f"[nicks] nickquisition display unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False
    if text:
        print(text, file=stream, flush=True)
    return bool(text)


class Watcher:
    """Shows each round's announcement and, once its verdict is recorded, its panel; each exactly once."""

    def __init__(self, journal: Path, state: Path, stream=None, interval: float = 3.0):
        self.journal, self.state, self.interval = Path(journal), Path(state), interval
        self.stream = stream or sys.stdout
        try:
            self.shown = set(json.loads(self.state.read_text()))
        except (OSError, ValueError):
            self.shown = set()
        self._stop = threading.Event()
        self._thread = None

    def _mark(self, key: str) -> None:
        self.shown.add(key)
        tmp = self.state.with_name(self.state.name + ".tmp")
        tmp.write_text(json.dumps(sorted(self.shown)))
        os.replace(tmp, self.state)

    def poll(self) -> list[str]:
        """One pass; returns the keys newly shown. Never raises."""
        new = []
        if not self.journal.is_file():
            return new
        try:
            from .stats import open_readonly
            db = open_readonly(self.journal)
        except Exception:
            return new
        try:
            rounds = db.all("SELECT job_id, round FROM nq_rounds ORDER BY opened_at")
            done = {(x["job_id"], x["round"]) for x in db.all("SELECT job_id, round FROM nq_verdicts")}
            colour = use_colour(self.stream)
            for r in rounds:
                key = (r["job_id"], r["round"])
                ann, pan = f"announce:{key[0]}:{key[1]}", f"verdict:{key[0]}:{key[1]}"
                if ann not in self.shown and pan not in self.shown:
                    print(render_announcement(db, *key, colour=colour), file=self.stream, flush=True)
                    self._mark(ann)
                    new.append(ann)
                if key in done and pan not in self.shown:
                    text = render_panel(db, *key, colour=colour)
                    if text:
                        print(text, file=self.stream, flush=True)
                        self._mark(pan)
                        new.append(pan)
        except (sqlite3.Error, OSError, LookupError, KeyError, ValueError) as exc:
            print(f"[nicks] nickquisition display paused: {type(exc).__name__}: {exc}", file=sys.stderr)
        finally:
            db.close()
        return new

    def start(self) -> "Watcher":
        def loop():
            while not self._stop.wait(self.interval):
                self.poll()
        self._thread = threading.Thread(target=loop, name="nicks-nq-display", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self.poll()                                 # a verdict recorded just before the host exited
