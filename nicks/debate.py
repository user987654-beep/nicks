"""Stake-Based Design Debate V1 (experimental planning mode).

Architect Nick (the Story Architect who owns the plan) and Evil Nick (an eligible agent, one session resumed
turn by turn) debate the plan BEFORE implementation. Each starts with a fixed number of chips. Spent chips are gone:
there are no rewards, refunds, transfers or scores. The chips decide how much debate an issue receives, never
whether a claim is true.

* BET    raise one concrete claim and stake 2..20 chips.
* CALL   dispute it by matching the stake (both stakes are consumed), then a bounded showdown: at most two
         responses per participant, each with a stance (maintain | concede | agree).
* FOLD   accept it as a planning concern to address (free; not a proven fact).
* REVISE only Architect Nick revises the plan; every version is validated, preflighted and hashed.
* APPROVE <hash>; CONSENSUS when both approve the same version.
* DISSENT end the debate without approving (only when you can no longer bet).

Evidence: any action may carry `repro`, a shell command the orchestrator runs on the unmodified repository in the
network-less verification jail (exit 0 = the stated premise holds). A `blocking` claim needs a stake of at least
BLOCKING_MIN_STAKE and a repro that exits 0. Left unresolved, it blocks implementation (DESIGN_BLOCKED).
No model ever judges a showdown.

The orchestrator owns all state. It is an append-only, hash-chained event log; balances are never stored but derived
by replaying the turns through the pure reducer below, so a model cannot set its own balance or act for the other
agent. Every original message and model response is kept verbatim.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

AGENTS = ("architect", "evil")
NAMES = {"architect": "Architect Nick", "evil": "Evil Nick"}
MIN_STAKE, MAX_STAKE = 2, 20
BLOCKING_MIN_STAKE = 10
SHOWDOWN_RESPONSES = 2              # per participant per disputed claim (the caller's CALL message is its first)
STANCES = ("maintain", "concede", "agree")
PRIMARY = ("BET", "CALL", "FOLD", "ARGUE", "APPROVE", "DISSENT")
DUPLICATE_JACCARD = 0.6
OUTCOMES = ("CONSENSUS", "BUDGET_EXHAUSTED", "DESIGN_BLOCKED", "UNRESOLVED")
BLOCK_RE = re.compile(r"```nicks-action[ \t]*\n(.*?)```", re.S)


class DebateError(RuntimeError):
    """An action that the protocol does not allow (no state change)."""


def other(a: str) -> str:
    return "evil" if a == "architect" else "architect"


def plan_hash(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def _canon(x) -> str:
    return json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# =========================================================================== control plane

def parse_control(text: str) -> list[dict]:
    """The LAST ```nicks-action block of a message: {"action": ...} or {"actions": [...]}. Free-form text around it
    is the argument and is never interpreted."""
    blocks = BLOCK_RE.findall(text or "")
    if not blocks:
        raise DebateError("no ```nicks-action control block found (end the message with exactly one)")
    try:
        data = json.loads(blocks[-1])
    except ValueError as exc:
        raise DebateError(f"the nicks-action block is not valid JSON: {exc}") from None
    acts = data.get("actions") if isinstance(data, dict) and "actions" in data else [data]
    if not isinstance(acts, list) or not acts or not all(isinstance(a, dict) for a in acts):
        raise DebateError("the nicks-action block must be an object {\"action\": ...} or {\"actions\": [...]}")
    for a in acts:
        a["action"] = str(a.get("action", "")).upper()
    return acts


# =========================================================================== the reducer (pure, deterministic)

def initial_state(chips: int, max_claims: int, max_turns: int, version: dict, max_reattacks: int = 0) -> dict:
    """`max_reattacks` (V1.1): independent revision re-attack cycles before a REVISED plan's agreement is final
    (0 = V1 behaviour). `reattacks` is also the epoch of Evil Nick's session: a new cycle means a fresh session."""
    return {"chips": {a: chips for a in AGENTS}, "chips_start": chips, "max_claims": max_claims,
            "max_turns": max_turns, "claims": {}, "order": [], "turns": 0, "to_move": "evil",
            "phase": {"kind": "idle"}, "versions": [version], "approvals": {a: None for a in AGENTS},
            "ended": None, "spent": {a: 0 for a in AGENTS},
            "max_reattacks": max_reattacks, "reattacks": 0, "reattack_base": 0, "reattack_log": []}


def current(state: dict) -> dict:
    return state["versions"][-1]


def _spend(state: dict, actor: str, n: int) -> None:
    """The ONLY way chips leave a balance. Nothing ever adds chips."""
    if n < 0 or state["chips"][actor] < n:
        raise DebateError(f"{NAMES[actor]} has {state['chips'][actor]} chips; cannot spend {n}")
    state["chips"][actor] -= n
    state["spent"][actor] += n


def _words(c: dict) -> set[str]:
    return set(re.findall(r"[a-z0-9_]{3,}", (c.get("title", "") + " " + c.get("claim", "")).lower()))


def _duplicate_of(state: dict, bet: dict) -> str | None:
    w = _words(bet)
    for cid in state["order"]:
        o = _words(state["claims"][cid])
        if w and o and len(w & o) / len(w | o) >= DUPLICATE_JACCARD:
            return cid
    return None


def _affordable(state: dict, actor: str, n: int) -> bool:
    return state["chips"][actor] >= n


def can_bet(state: dict, actor: str) -> bool:
    return _affordable(state, actor, MIN_STAKE) and len(state["order"]) < state["max_claims"]


def allowed(state: dict, actor: str) -> list[str]:
    """Primary actions `actor` may take now (REVISE may accompany any of them, Architect Nick only)."""
    if state["ended"] or state["to_move"] != actor:
        return []
    ph = state["phase"]
    if ph["kind"] == "pending":
        c = state["claims"][ph["claim"]]
        return ["CALL", "FOLD"] if _affordable(state, actor, c["stake"]) else ["FOLD"]
    if ph["kind"] == "showdown":
        return ["ARGUE"]
    # Out of chips but not out of claims: a verified BLOCKING defect may still be flagged at stake 0 (V1.1), so it
    # stays visible; check_turn enforces that only such an unfunded claim is possible.
    bet = can_bet(state, actor) or (len(state["order"]) < state["max_claims"] and not _affordable(state, actor, MIN_STAKE))
    return (["BET"] if bet else []) + ["APPROVE"] + ([] if can_bet(state, actor) else ["DISSENT"])


def _need(a: dict, *keys: str) -> None:
    missing = [k for k in keys if not str(a.get(k, "")).strip()]
    if missing:
        raise DebateError(f"{a['action']} needs: {', '.join(missing)}")


def check_turn(state: dict, actor: str, actions: list[dict], version: dict | None = None) -> None:
    """Validate a whole turn against the rules (raises DebateError; never mutates). `version` is the new plan
    version a REVISE in this turn produces; without it (before the revision is built) an APPROVE accompanying a
    REVISE is only checked structurally."""
    if state["ended"]:
        raise DebateError(f"the debate has ended ({state['ended']['outcome']})")
    if actor not in AGENTS:
        raise DebateError(f"unknown actor {actor!r}")
    if state["to_move"] != actor:
        raise DebateError(f"it is {NAMES[state['to_move']]}'s turn, not {NAMES[actor]}'s")
    prim = [a for a in actions if a["action"] in PRIMARY]
    rev = [a for a in actions if a["action"] == "REVISE"]
    unknown = [a["action"] for a in actions if a["action"] not in PRIMARY and a["action"] != "REVISE"]
    if unknown:
        raise DebateError(f"unknown action(s): {', '.join(unknown)}")
    if len(prim) != 1:
        raise DebateError(f"exactly one of {', '.join(PRIMARY)} per turn (got {len(prim)})")
    if rev and actor != "architect":
        raise DebateError("only Architect Nick revises the plan")
    if len(rev) > 1:
        raise DebateError("at most one REVISE per turn")
    a = prim[0]
    ok = allowed(state, actor)
    if a["action"] not in ok:
        raise DebateError(f"{a['action']} is not allowed now; allowed: {', '.join(ok) or 'nothing'}")
    ph = state["phase"]
    if a["action"] == "BET":
        _need(a, "title", "claim", "impact", "evidence", "why_stake", "resolution")
        st = a.get("stake")
        unfunded = st == 0 and not isinstance(st, bool) and not _affordable(state, actor, MIN_STAKE)
        if not isinstance(st, int) or isinstance(st, bool) or not (MIN_STAKE <= st <= MAX_STAKE or unfunded):
            raise DebateError(f"stake must be an integer {MIN_STAKE}..{MAX_STAKE}")
        if not _affordable(state, actor, st):
            raise DebateError(f"stake {st} exceeds {NAMES[actor]}'s balance of {state['chips'][actor]}")
        if unfunded and not (a.get("blocking") and str(a.get("repro", "")).strip()):
            raise DebateError("without chips for the minimum stake, only a BLOCKING claim with a repro may be flagged "
                              "(stake 0)")
        if a.get("blocking") and not unfunded and (st < BLOCKING_MIN_STAKE or not str(a.get("repro", "")).strip()):
            raise DebateError(f"a blocking claim needs a stake of at least {BLOCKING_MIN_STAKE} and a repro command")
        dup = _duplicate_of(state, a)
        reopens = a.get("reopens")
        if dup and reopens != dup:
            raise DebateError(f"this repeats claim {dup}; a claim may only be reopened with \"reopens\": \"{dup}\" "
                              "and NEW evidence (a repro different from before)")
        if reopens:
            prev = state["claims"].get(reopens)
            if not prev:
                raise DebateError(f"no claim {reopens} to reopen")
            if prev.get("reopened_by"):
                raise DebateError(f"claim {reopens} was already reopened once")
            if not str(a.get("repro", "")).strip() or a.get("repro") == prev.get("repro"):
                raise DebateError("reopening a claim requires new evidence: a repro different from the earlier one")
    elif a["action"] in ("CALL", "FOLD", "ARGUE"):
        if a.get("claim") != ph.get("claim"):
            raise DebateError(f"{a['action']} must name the open claim {ph.get('claim')}")
        if a["action"] == "ARGUE" and a.get("stance") not in STANCES:
            raise DebateError(f"ARGUE needs a stance: {', '.join(STANCES)}")
        if a["action"] == "ARGUE" and state["claims"][ph["claim"]]["responses"][actor] >= SHOWDOWN_RESPONSES:
            raise DebateError(f"{NAMES[actor]} has used its {SHOWDOWN_RESPONSES} showdown responses on {ph['claim']}")
    elif a["action"] == "APPROVE":
        h = str(a.get("plan", ""))
        target = version or (None if rev else current(state))
        if target and (len(h) < 12 or not target["sha256"].startswith(h)):
            raise DebateError(f"APPROVE must name the {'new' if version else 'current'} plan hash "
                              f"({target['sha256'][:16]}...); got {h[:20]!r}")
        if target and not target.get("preflight_ok", True):
            raise DebateError("this plan version fails the Acceptance Check Preflight; it cannot be approved until "
                              "Architect Nick REVISEs it")
    elif a["action"] == "DISSENT":
        _need(a, "reason")


def _end_showdown(state: dict, c: dict, outcome: str) -> None:
    c["status"], c["outcome"] = "settled", outcome
    state["phase"] = {"kind": "idle"}


def apply_turn(state: dict, actor: str, actions: list[dict], results: dict | None = None) -> dict:
    """Apply a validated turn. `results` carries what the orchestrator observed for it (new plan version, evidence
    runs); it is recorded in the event, so replay never re-runs anything."""
    results = results or {}
    check_turn(state, actor, actions, results.get("version"))
    s = copy.deepcopy(state)
    s["turns"] += 1
    n = s["turns"]
    if results.get("version"):                                  # REVISE: a new, validated plan version
        s["versions"].append(dict(results["version"], turn=n))
        s["approvals"] = {x: None for x in AGENTS}
    a = next(x for x in actions if x["action"] in PRIMARY)
    ev = results.get("evidence")
    ph = s["phase"]
    if a["action"] == "BET":
        cid = f"C{len(s['order']) + 1}"
        _spend(s, actor, a["stake"])
        s["claims"][cid] = {"id": cid, "by": actor, "turn": n, "stake": a["stake"], "status": "pending",
                            "outcome": None, "blocking": bool(a.get("blocking")),
                            **{k: a.get(k) for k in ("title", "claim", "impact", "evidence", "why_stake",
                                                     "resolution", "repro", "reopens")},
                            "repro_result": ev, "substantiated": bool(ev and ev.get("exit_code") == 0),
                            "showdown": [], "responses": {x: 0 for x in AGENTS}, "called_by": None}
        if a.get("reopens"):
            s["claims"][a["reopens"]]["reopened_by"] = cid
        s["order"].append(cid)
        s["phase"] = {"kind": "pending", "claim": cid}
    elif a["action"] == "CALL":
        c = s["claims"][ph["claim"]]
        _spend(s, actor, c["stake"])                            # matched; both stakes are now consumed
        c.update(status="showdown", called_by=actor)
        c["responses"][actor] = 1
        c["showdown"].append({"turn": n, "actor": actor, "stance": "maintain", "evidence": ev})
        s["phase"] = {"kind": "showdown", "claim": c["id"]}
    elif a["action"] == "FOLD":
        c = s["claims"][ph["claim"]]
        c.update(status="settled", outcome="accepted", folded=True, fold_evidence=ev)
        s["phase"] = {"kind": "idle"}
    elif a["action"] == "ARGUE":
        c = s["claims"][ph["claim"]]
        c["responses"][actor] += 1
        c["showdown"].append({"turn": n, "actor": actor, "stance": a["stance"], "evidence": ev})
        claimant = c["by"]
        prev = next((x for x in reversed(c["showdown"][:-1]) if x["actor"] != actor), None)
        if a["stance"] == "concede":
            _end_showdown(s, c, "withdrawn" if actor == claimant else "accepted")
        elif a["stance"] == "agree" and prev and prev["stance"] == "agree":
            _end_showdown(s, c, "agreed")
        elif all(c["responses"][x] >= SHOWDOWN_RESPONSES for x in AGENTS):
            _end_showdown(s, c, "unresolved")
    elif a["action"] == "APPROVE":
        s["approvals"][actor] = current(s)["sha256"]
    elif a["action"] == "DISSENT":
        s["dissent"] = {"by": actor, "turn": n, "reason": a["reason"]}
    rev = next((x for x in actions if x["action"] == "REVISE"), None)
    if rev and results.get("version"):
        s["versions"][-1]["addresses"] = rev.get("addresses") or []
    s["to_move"] = other(actor)
    if s["approvals"]["architect"] and s["approvals"]["architect"] == s["approvals"]["evil"] == current(s)["sha256"]:
        _on_agreement(s, n)
    elif a["action"] == "DISSENT":
        s["ended"] = terminal(s, f"{NAMES[actor]} dissented and can no longer bet: {a['reason'][:300]}",
                              "chips" if s["chips"][actor] < MIN_STAKE else "claims")
    elif s["turns"] >= s["max_turns"]:
        s["ended"] = terminal(s, f"turn limit ({s['max_turns']}) reached", "turns")
    return s


def _on_agreement(s: dict, n: int) -> None:
    """Both agents approved the current version. Final CONSENSUS only if nothing negotiated since the last
    examination is unexamined; otherwise a REVISION RE-ATTACK cycle starts (fresh Evil Nick session, same chips),
    and after the last allowed cycle an unexamined revision ends UNRESOLVED, never consensus."""
    last = len(s["versions"]) - 1
    if last == s["reattack_base"] or s["max_reattacks"] == 0:
        s["ended"] = {"outcome": "CONSENSUS", "plan_sha256": current(s)["sha256"], "turn": n,
                      "reason": "both agents approved the same plan version"
                      + (f" after {s['reattacks']} revision re-attack cycle(s)" if s["reattacks"] else
                         ("" if s["max_reattacks"] == 0 else "; it was never revised, so no re-attack was needed"))}
    elif s["reattacks"] < s["max_reattacks"]:
        s["reattacks"] += 1
        s["reattack_log"].append({"cycle": s["reattacks"], "from": s["reattack_base"] + 1, "to": last + 1, "turn": n,
                                  "agreed_sha256": current(s)["sha256"]})
        s["reattack_base"] = last
        s["approvals"] = {x: None for x in AGENTS}
        s["phase"] = {"kind": "idle"}
        s["to_move"] = "evil"
    else:
        s["ended"] = {"outcome": "UNRESOLVED", "plan_sha256": current(s)["sha256"], "turn": n,
                      "reason": f"plan v{last + 1} was revised after the last of {s['max_reattacks']} revision "
                                "re-attack cycles and was never independently re-examined",
                      "blocking": open_blocking(s)}


def open_blocking(state: dict) -> list[str]:
    """Substantiated blocking claims not settled for their claimant: not withdrawn, and the claimant has not
    approved the final plan version."""
    out = []
    for cid in state["order"]:
        c = state["claims"][cid]
        if c["blocking"] and c["substantiated"] and c["outcome"] != "withdrawn" \
                and state["approvals"][c["by"]] != current(state)["sha256"]:
            out.append(cid)
    return out


def terminal(state: dict, reason: str, budget: str) -> dict:
    """The outcome when the debate stops without consensus. Never consensus by default."""
    blk = open_blocking(state)
    if blk:
        return {"outcome": "DESIGN_BLOCKED", "reason": reason, "blocking": blk, "budget": budget,
                "turn": state["turns"]}
    return {"outcome": "BUDGET_EXHAUSTED", "reason": reason, "budget": budget, "turn": state["turns"]}


def unresolved(state: dict) -> list[str]:
    return [cid for cid in state["order"] if state["claims"][cid]["outcome"] in (None, "unresolved")]


# =========================================================================== persistence (hash-chained events)

class Log:
    """Append-only events.jsonl; each event carries the hash of its predecessor. Replay verifies the chain and every
    referenced message file, so an edited balance, action or message is detected."""

    def __init__(self, root: Path):
        self.root = root
        self.path = root / "events.jsonl"

    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        out, prev = [], "0" * 64
        for i, line in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            ev = json.loads(line)
            body = {k: ev[k] for k in ("n", "at", "kind", "actor", "data")}
            if ev["prev"] != prev or ev["hash"] != hashlib.sha256((prev + _canon(body)).encode()).hexdigest() \
                    or ev["n"] != i:
                raise DebateError(f"debate log integrity failure at event {i}: the record was modified")
            for ref in ev["data"].get("files", []):
                p = self.root / ref["path"]
                if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != ref["sha256"]:
                    raise DebateError(f"debate log integrity failure: {ref['path']} was modified or removed")
            out.append(ev)
            prev = ev["hash"]
        return out

    def append(self, kind: str, actor: str | None, data: dict) -> dict:
        evs = self.events()
        prev = evs[-1]["hash"] if evs else "0" * 64
        body = {"n": len(evs), "at": time.time(), "kind": kind, "actor": actor, "data": data}
        ev = dict(body, prev=prev, hash=hashlib.sha256((prev + _canon(body)).encode()).hexdigest())
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return ev


def file_ref(root: Path, p: Path) -> dict:
    return {"path": p.relative_to(root).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}


def replay(log: Log) -> tuple[dict | None, list[dict]]:
    """(state, events). The state is rebuilt from the start event and the accepted turns only."""
    evs = log.events()
    state = None
    for ev in evs:
        if ev["kind"] == "start":
            d = ev["data"]
            state = initial_state(d["chips"], d["max_claims"], d["max_turns"], d["version"], d.get("max_reattacks", 0))
        elif ev["kind"] == "turn":
            state = apply_turn(state, ev["actor"], ev["data"]["actions"], ev["data"]["results"])
        elif ev["kind"] == "end" and state is not None and not state["ended"]:
            state["ended"] = ev["data"]
    return state, evs


# =========================================================================== orchestration

def debate_dir(plan: dict) -> Path:
    return Path(plan["dir"]) / "debate"


def run_repro(plan: dict, command: str, timeout: float) -> dict:
    """Evidence: run a shell command on a fresh copy of the unmodified repository in the network-less jail."""
    from .sandbox import wrap
    from .testdesign import _export
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="nicks-debate-repro-") as td:
        tree = Path(td) / "tree"
        _export(plan["repo"], plan["base_sha"], tree)
        home = Path(td) / "home"
        home.mkdir()
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1", "PWD": str(tree), "TMPDIR": "/tmp"}
        argv = wrap(["/bin/sh", "-c", command], snapshot=tree, home=home, binary="/bin/sh", network=False,
                    writable=True)
        try:
            p = subprocess.run(argv, cwd=str(tree), env=env, capture_output=True, timeout=timeout)
            out, rc = (p.stdout + p.stderr).decode("utf-8", errors="replace"), p.returncode
        except subprocess.TimeoutExpired:
            out, rc = f"timed out after {timeout:.0f}s", None
    return {"command": command, "exit_code": rc, "output": out[-2000:], "seconds": round(time.time() - t0, 1),
            "base": plan["base_sha"][:12]}


PRIORITIES = """Attack the PLAN in this order of importance (adapted from Ponytail, MIT License):
1. Correct: the plan's assumptions about the existing code are wrong; a requirement of the request is missing or
   contradicted; prescribed code is wrong; edge cases (empty, zero, last item, unicode, None) break; callers break.
2. Safe: data loss or corruption, secrets, missing input checks at trust boundaries.
3. Holds under real load: races, unbounded growth, O(n^2) on realistic input.
4. Tested: a task's verify commands could pass while the requirement is broken (tests that cannot fail).
5. Lean: abstraction, options or tasks the request does not need; architecture heavier than the problem.
Not worth chips: style, naming, wording, speculative "for later" concerns."""

PROTOCOL = """How the debate works (the orchestrator enforces it; nobody can change a balance or act for the other side):
- You and {opp} each started with {chips} chips. Spent chips are gone for good: no rewards, no refunds, no scoring,
  and nothing is gained by being right. Chips only decide how much debate an issue receives.
- BET: raise ONE concrete claim and stake {min}-{max} chips by EXPECTED IMPACT (not confidence):
  2-4 minor maintainability; 5-9 meaningful but local; 10-14 likely functional defect or regression;
  15-19 serious correctness or architecture problem; 20 critical (core function, data integrity, security).
- The other side then CALLs (matches the stake; both stakes are consumed) to dispute it, or FOLDs: a FOLD accepts the
  CONCERN as one to address (free; not a proven fact), NOT the proposed solution. Architect Nick chooses the solution,
  which may be narrower or simpler than the one proposed, and documents why; the claimant may then bet against it.
- After a CALL: a short showdown, at most {resp} responses each (the CALL message counts as the caller's first).
  Each ARGUE carries a stance: "maintain", "concede", or "agree" (you accept the resolution as it now stands,
  for example a revised plan). It ends on a concession, on agreement by both, or as UNRESOLVED.
- Only Architect Nick revises the plan. Each revision gets a new hash, and all approvals reset.
- APPROVE the current hash when the plan is acceptable to you. CONSENSUS = both approve the same version, EXCEPT
  that when the agreed version was revised during the debate, a FRESH, independent Evil Nick session first re-attacks
  the negotiated changes (at most {reattacks} such cycles; a revision after the last cycle is left UNRESOLVED).
  DISSENT (only when you can no longer bet) ends the debate without consensus.
- Evidence beats chips. Any action may carry "repro": a shell command the orchestrator runs on the UNMODIFIED
  repository in a network-less sandbox; exit 0 demonstrates your factual premise, and both sides see the output.
  A claim with "blocking": true needs a stake of at least {bmin} and a repro that exits 0; if it stays unresolved it
  blocks implementation.
- At most {claims} claims in total. Spend on what matters most first. Do not repeat a claim without new evidence.
  Concede when you are shown wrong. If you agree, say so: never invent disagreement.

End EVERY reply with exactly one control block (your argument goes above it, in free form):
```nicks-action
{{"action": "BET", "stake": 12, "title": "...", "claim": "...", "impact": "...", "evidence": "...",
 "why_stake": "...", "resolution": "...", "repro": "optional shell command", "blocking": false}}
```
Other actions: {{"action": "CALL", "claim": "C1"}}, {{"action": "FOLD", "claim": "C1"}},
{{"action": "ARGUE", "claim": "C1", "stance": "maintain|concede|agree"}}, {{"action": "APPROVE", "plan": "<hash>"}},
{{"action": "DISSENT", "reason": "..."}}. Any of them may add "repro"."""

EVIL_OPENING = """You are Evil Nick in a STAKE-BASED DESIGN DEBATE with Architect Nick, BEFORE any code is written.
Architect Nick wrote the implementation plan below for the user's request. Find what is actually wrong with the PLAN
and get it fixed. The purpose is a better plan, not winning an argument. Your workspace is a copy of the repository
at the plan's base commit: read it, and run commands (no network) to check your premises before you stake chips.

{priorities}

{protocol}

=== THE USER'S REQUEST {b} ===
{request}
=== END REQUEST {b} ===

=== THE PLAN, version {v} (hash {hash}) {b} ===
{manifest}
=== END PLAN {b} ===

=== SOURCE FILES THE PLAN TOUCHES (base commit) {b} ===
{sources}
=== END SOURCES {b} ===

{state}
"""

EVIL_UPDATE = """DEBATE UPDATE (turn {turn}).

{news}

{state}
"""

REATTACK_OPENING = """You are Evil Nick, called in FRESH for a REVISION RE-ATTACK (cycle {cycle} of {max}) in a stake-based design debate,
BEFORE any code is written. Architect Nick and an earlier Evil Nick session negotiated changes to the plan and both
approved version v{to}. That agreement is NOT final until the negotiated changes survive an independent attack: yours.

You are not that earlier session and owe its recommendations nothing. Do not assume a revision is right because Evil
Nick proposed it, or because Architect Nick accepted it: a fix for one problem often introduces another. You may
challenge the UNDERLYING APPROACH itself, including an approach Evil Nick recommended earlier. Examine everything the
debate changed (cumulatively, v1 -> v{to}; the newest changes since the last examination are v{frm} -> v{to}) and find
defects the changes INTRODUCED or LEFT UNRESOLVED:
1. correctness regressions;
2. performance and algorithmic complexity: count the work per input element and per definition, on large and
   adversarial inputs;
3. memory growth and resource consumption;
4. backward compatibility;
5. data integrity and mutation;
6. interactions with existing components;
7. unnecessary complexity;
8. whether the concern that motivated each change is actually resolved.
And answer these two questions explicitly, in your argument:
A. Is any constraint or invariant introduced during the debate STRONGER than the user's request actually demands?
B. Could a MATERIALLY SIMPLER design resolve the accepted concerns without the complexity accumulated in the current
   plan? If so, describe it concretely (and bet on it if it matters).
Do not weaken an explicit requirement of the request to get a simpler design; a real semantic trade-off must be named.
Do not repeat objections already answered unless you have NEW evidence (a reopened claim needs a new repro). Give
concrete technical reasoning and reproducible evidence; your workspace is the repository at the base commit (run
commands, no network). If the changes hold up, APPROVE the current hash: that is a valid and useful result.

{protocol}

You inherit Evil Nick's remaining chips: {chips} (spent chips are gone; nothing is replenished). With fewer than
{min} chips you can still flag a verified BLOCKING defect at stake 0 (with "blocking": true and a repro).

=== THE USER'S REQUEST {b} ===
{request}
=== END REQUEST {b} ===

=== THE ORIGINAL PLAN, v1 {b} ===
{v1}
=== END ORIGINAL PLAN {b} ===

=== THE REVISED PLAN, v{to} (hash {hash}) {b} ===
{manifest}
=== END REVISED PLAN {b} ===

=== THE CUMULATIVE CHANGES v1 -> v{to} (unified diff of the manifests) {b} ===
{diff}
=== END CHANGES {b} ===

=== CONCERNS ACCEPTED DURING THE DEBATE (what had to be addressed; the solutions were the Architect's choice) {b} ===
{concerns}
=== END ACCEPTED CONCERNS {b} ===

=== DESIGN APPROACHES, ASSUMPTIONS AND TRADE-OFFS INTRODUCED BY EACH REVISION (as documented by Architect Nick) {b} ===
{assumptions}
=== END APPROACHES {b} ===

=== PLAN GROWTH {b} ===
{growth}
=== END PLAN GROWTH {b} ===

=== THE DEBATE SO FAR: the arguments that motivated the changes (evidence, not authority) {b} ===
{record}
=== END DEBATE RECORD {b} ===

=== SOURCE FILES THE PLAN TOUCHES (base commit) {b} ===
{sources}
=== END SOURCES {b} ===

{state}
"""

ARCHITECT_GUIDE = """You are Architect Nick: you own this plan. Defend decisions that are right, with evidence; accept valid criticism
and revise the plan (REVISE with a complete manifest file); you may also BET against Evil Nick's proposals. Verify a
claim (read the code, run its repro yourself) before you CALL or FOLD. Write your message (free-form argument, then
exactly one ```nicks-action block) to a file and run:
  nicks dev debate {plan} turn --file <message.md>
A FOLD accepts the CONCERN, not the proposed solution: the solution is yours. Before you revise, consider whether the
original behaviour really needs to change, whether a narrower change satisfies the request, whether a substantially
simpler implementation exists, whether the proposed fix adds invariants, special cases or data structures, and whether
its complexity is proportional to the concern. Never silently weaken an explicit requirement of the user's request.
A REVISE names the full revised manifest and MUST document the approach: {{"actions": [{{"action": "FOLD", "claim": "C1"}},
{{"action": "REVISE", "manifest_file": "<absolute path>", "addresses": ["C1"], "summary": "...",
"approach": "the approach chosen, and why simpler or narrower alternatives were accepted or rejected",
"tradeoffs": ["any semantic trade-off or requirement interpretation that stays open (visible to the user)"]}}]}}.
Compute a revised manifest's hash (and check it) first with: nicks dev debate {plan} hash --file <manifest.json>
Check a premise in the jail (never run repository code on the host): nicks dev debate {plan} probe --file <cmd.sh>
When you both approve a REVISED plan, a fresh Evil Nick session re-attacks the negotiated changes before consensus:
answer its claims on the merits like any other (verify, FOLD/REVISE, CALL, concede); approve again when they are settled."""


def state_text(state: dict, actor: str) -> str:
    v = current(state)
    lines = [f"STATE: chips {NAMES['architect']} {state['chips']['architect']}, {NAMES['evil']} {state['chips']['evil']} "
             f"(of {state['chips_start']} each); claims {len(state['order'])}/{state['max_claims']}; "
             f"turn {state['turns']}/{state['max_turns']}; plan v{len(state['versions'])} hash {v['sha256']}"]
    if not v.get("preflight_ok", True):
        lines.append("THIS PLAN VERSION FAILS THE ACCEPTANCE CHECK PREFLIGHT (it cannot be approved until revised):\n  "
                     + "\n  ".join(v.get("preflight_problems") or []))
    if state.get("reattack_log") and not state["ended"]:
        r = state["reattack_log"][-1]
        lines.append(f"REVISION RE-ATTACK cycle {r['cycle']} of {state['max_reattacks']} in progress: a fresh Evil Nick "
                     f"session examines the negotiated changes v{r['from']} -> v{r['to']}; consensus needs both "
                     "approvals again (and any further revision is re-examined while cycles remain)")
    appr = [f"{NAMES[x]} approved {h[:16]}" for x, h in state["approvals"].items() if h]
    lines.append("Approvals of the current version: " + (", ".join(appr) if appr else "none"))
    for cid in state["order"]:
        c = state["claims"][cid]
        sd = "; showdown " + " / ".join(f"{NAMES[x['actor']].split()[0]}:{x['stance']}" for x in c["showdown"]) \
            if c["showdown"] else ""
        lines.append(f"  {cid} by {NAMES[c['by']]} stake {c['stake']}{' BLOCKING' if c['blocking'] else ''}"
                     f"{' (repro exit ' + str((c['repro_result'] or {}).get('exit_code')) + ')' if c['repro'] else ''}: "
                     f"{c['title']} -> {c['outcome'] or c['status']}{sd}")
    if state["ended"]:
        lines.append(f"ENDED: {state['ended']['outcome']}: {state['ended']['reason']}")
    else:
        ph = state["phase"]
        who = NAMES[state["to_move"]]
        what = {"pending": f"respond to open claim {ph.get('claim')}", "showdown": f"showdown on {ph.get('claim')}",
                "idle": "no open claim"}[ph["kind"]]
        lines.append(f"TO MOVE: {who} ({what}); allowed: {', '.join(allowed(state, state['to_move']))}"
                     + (" (+ REVISE)" if state["to_move"] == "architect" else ""))
        if state["to_move"] == actor:
            lines.append("YOUR TURN. End with exactly one ```nicks-action block.")
    return "\n".join(lines)



def growth_table(manifests: list[dict]) -> str:
    """Plan-size and verification growth per version (shown to every re-attack)."""
    rows = ["version | manifest chars | task context chars | verify commands (chars) | acceptance items | architecture chars"]
    for i, m in enumerate(manifests, start=1):
        tasks = m.get("tasks") or []
        rows.append(f"v{i} | {len(json.dumps(m, ensure_ascii=False))} | {sum(len(t.get('context') or '') for t in tasks)} | "
                    f"{sum(len(t.get('verify') or []) for t in tasks)} ({sum(len(v) for t in tasks for v in t.get('verify') or [])}) | "
                    f"{sum(len(t.get('acceptance') or []) for t in tasks)} | {len(m.get('architecture') or '')}")
    return "\n".join(rows)

class Debate:
    """One plan's debate: persistence, evidence, Evil Nick's persistent Codex session, termination."""

    def __init__(self, db, settings, plan_id: str, runner=None, log=lambda m: None):
        self.db, self.s, self.plan_id, self.log = db, settings, plan_id, log
        self.plan = db.dev_plan(plan_id)
        self.root = debate_dir(self.plan)
        self.events = Log(self.root)
        self._runner = runner

    # ----------------------------------------------------------------- helpers
    def state(self) -> tuple[dict | None, list[dict]]:
        return replay(self.events)

    def runner(self):
        if self._runner is None:
            from .policy import RoleRunner
            self._runner = RoleRunner(self.db, self.s, "evil", writable=True, text_only_fallback=True, retry_on_resume=True)
        return self._runner

    def _summary(self, state: dict, evs: list[dict]) -> dict:
        calls = [e for e in evs if e["kind"] == "evil_call"]
        claims = [state["claims"][c] for c in state["order"]]
        return {
            "status": (state["ended"] or {}).get("outcome") or ("interrupted" if evs and evs[-1]["kind"] == "interrupted"
                                                                else "running"),
            "ended": state["ended"], "chips": state["chips"], "spent": state["spent"], "turns": state["turns"],
            "versions": [{"v": i + 1, "sha256": v["sha256"], "turn": v.get("turn", 0), "addresses": v.get("addresses", []),
                          "approach": v.get("approach", ""), "tradeoffs": v.get("tradeoffs", [])}
                         for i, v in enumerate(state["versions"])],
            "tradeoffs": [f"v{i + 1}: {t}" for i, v in enumerate(state["versions"]) for t in v.get("tradeoffs") or []],
            "plan_sha256": current(state)["sha256"], "approvals": state["approvals"],
            "claims": [{k: c.get(k) for k in ("id", "by", "stake", "title", "blocking", "substantiated", "outcome",
                                               "called_by", "folded")} for c in claims],
            "bets": len(claims), "calls": sum(1 for c in claims if c["called_by"]),
            "folds": sum(1 for c in claims if c.get("folded")), "unresolved": unresolved(state),
            "open_blocking": open_blocking(state),
            "evil_calls": len(calls), "evil_failures": sum(1 for e in calls if not e["data"]["ok"]),
            "evil_models": sorted({e["data"].get("model") or "?" for e in calls if e["data"]["ok"]}),
            "evil_seconds": round(sum(e["data"]["seconds"] for e in calls), 1),
            "evil_tokens_in": sum(e["data"].get("tokens_in", 0) for e in calls),
            "evil_tokens_out": sum(e["data"].get("tokens_out", 0) for e in calls),
            "rejected_turns": sum(1 for e in evs if e["kind"] == "rejected_turn"),
            "wall_seconds": round((evs[-1]["at"] - evs[0]["at"]) if evs else 0, 1),
            "escalation": next((e["data"] for e in reversed(evs) if e["kind"] == "escalation"), None),
            "max_reattacks": state.get("max_reattacks", 0), "reattacks": state.get("reattack_log", []),
            "evil_sessions": len({(e["data"].get("epoch", 0), e["data"].get("thread_id")) for e in calls
                                  if e["data"]["ok"]})}

    def _sync_plan(self, state: dict, evs: list[dict]) -> dict:
        summ = self._summary(state, evs)
        (self.root / "summary.json").write_text(json.dumps(summ, indent=2, ensure_ascii=False), encoding="utf-8")
        self._render(state, evs)
        detail = dict(self.db.dev_plan(self.plan_id).get("detail") or {})
        detail["debate"] = {k: summ[k] for k in ("status", "ended", "chips", "plan_sha256", "turns", "bets", "calls",
                                                  "folds", "unresolved", "open_blocking", "escalation", "tradeoffs")}
        detail["debate"]["transcript"] = str(self.root / "transcript.md")
        self.db.set_dev_plan(self.plan_id, detail=detail)
        from . import dev
        dev.write_plan_md(self.db, self.plan_id)          # plan.md shows the current debate state and trade-offs
        return summ

    def _render(self, state: dict, evs: list[dict]) -> None:
        out = [f"# Stake-Based Design Debate: plan `{self.plan_id}`", ""]
        st = None
        for ev in evs:
            d = ev["data"]
            if ev["kind"] == "start":
                st = initial_state(d["chips"], d["max_claims"], d["max_turns"], d["version"], d.get("max_reattacks", 0))
                out += [f"Started: {d['chips']} chips each, at most {d['max_claims']} claims, {d['max_turns']} turns, "
                        f"{d.get('max_reattacks', 0)} revision re-attack cycle(s). Plan v1 `{d['version']['sha256'][:16]}`.", ""]
            elif ev["kind"] == "turn":
                before = st["reattacks"]
                st = apply_turn(st, ev["actor"], d["actions"], d["results"])
                msg = (self.root / d["files"][0]["path"]).read_text(encoding="utf-8") if d.get("files") else ""
                out += [f"## Turn {st['turns']}: {NAMES[ev['actor']]}", "", msg.strip(), ""]
                for k, r in (d["results"].get("evidence_all") or {}).items():
                    out.append(f"> Orchestrator ran `{r['command'][:200]}` on base {r['base']}: exit {r['exit_code']}")
                if d["results"].get("version"):
                    out.append(f"> New plan version v{len(st['versions'])} `{d['results']['version']['sha256'][:16]}`")
                out += [f"> Chips after: Architect {st['chips']['architect']}, Evil {st['chips']['evil']}", ""]
                if st["reattacks"] > before:
                    r = st["reattack_log"][-1]
                    out += [f"## REVISION RE-ATTACK cycle {r['cycle']} of {st['max_reattacks']}: both approved v{r['to']}; "
                            f"a FRESH Evil Nick session now examines the changes v{r['from']} -> v{r['to']} "
                            "(no consensus yet)", ""]
            elif ev["kind"] in ("rejected_turn", "interrupted", "escalation", "end"):
                out += [f"*{ev['kind']}* ({ev['actor'] or 'orchestrator'}): {json.dumps(d, ensure_ascii=False)[:600]}", ""]
        if state and state["ended"]:
            out += [f"**Outcome: {state['ended']['outcome']}**: {state['ended']['reason']}"]
        (self.root / "transcript.md").write_text("\n".join(out) + "\n", encoding="utf-8")

    def _end(self, state: dict, data: dict) -> dict:
        self.events.append("end", None, data)
        state = dict(state, ended=data)
        return state

    # ----------------------------------------------------------------- plan versions
    def check_manifest(self, manifest: dict, strict: bool = True) -> dict:
        """The existing manifest validation and the Acceptance Check Preflight, for a candidate version. A REVISE
        must pass both (strict); the opening version must be valid, and a preflight failure is recorded on it (it
        can then not be approved until revised)."""
        from . import dev
        from .rework import validate_existing_plan
        validate_existing_plan(self.plan, manifest)
        errs = dev.validate_manifest(manifest, Path(self.plan["repo"]), self.plan["base_sha"], self.s)
        if errs:
            raise DebateError("invalid manifest:\n- " + "\n- ".join(errs))
        res, bad = self._preflight(manifest)
        if bad and strict:
            raise DebateError("the Acceptance Check Preflight rejects this version:\n- " + "\n- ".join(bad))
        return {"sha256": plan_hash(manifest), "preflight": res, "preflight_ok": not bad, "preflight_problems": bad}

    def _preflight(self, manifest: dict) -> tuple[dict, list[str]]:
        from . import dev
        from .preflight import preflight_task, summary
        gitdir, _, head = dev.ensure_state(self.db, self.plan)
        res, bad = {}, []
        for t in manifest["tasks"]:
            with dev.STATE_LOCK:
                pf = preflight_task(gitdir, head, t, self.s.dev_verify_timeout, None)
            res[t["id"]] = {"status": pf["status"], "summary": summary(pf), "reasons": pf.get("reasons", [])}
            if pf["status"] != "ok":
                bad.append(f"{t['id']}: {summary(pf)}: " + " | ".join(pf.get("reasons", []))[:300])
        return res, bad

    def _store_version(self, manifest: dict, n: int) -> dict:
        vdir = self.root / "versions"
        vdir.mkdir(parents=True, exist_ok=True)
        p = vdir / f"v{n}.json"
        p.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"sha256": plan_hash(manifest), "file": p.relative_to(self.root).as_posix()}

    def _adopt(self, manifest: dict) -> None:
        """Make the version the plan's manifest (the tasks the Council will implement)."""
        from . import dev
        pdir = Path(self.plan["dir"])
        text = json.dumps(manifest, indent=2, ensure_ascii=False)
        if not (pdir / "manifest-original.json").exists():
            shutil.copyfile(pdir / "manifest.json", pdir / "manifest-original.json")
        (pdir / "manifest-debated.json").write_text(text, encoding="utf-8")
        with self.db.tx():
            self.db.execute("UPDATE dev_plans SET manifest=?, manifest_sha256=? WHERE id=?",
                            (text, dev._sha(text), self.plan_id))
            self.db.execute("DELETE FROM dev_tasks WHERE plan_id=?", (self.plan_id,))
            for t in manifest["tasks"]:
                self.db.execute("INSERT INTO dev_tasks (plan_id, task_id, status, spec, updated_at) VALUES (?,?,?,?,?)",
                                (self.plan_id, t["id"], "pending", json.dumps(t), time.time()))

    # ----------------------------------------------------------------- lifecycle
    def start(self) -> dict:
        state, evs = self.state()
        if state is not None:
            raise DebateError("this plan's debate has already started (use `turn`, `resume` or `status`)")
        from . import dev
        if any(t["status"] != "pending" for t in self.db.dev_tasks(self.plan_id)) or self.db.dev_attempts(self.plan_id):
            raise DebateError("the debate happens before implementation; this plan has already started")
        manifest = self.plan["manifest"]
        chk = self.check_manifest(manifest, strict=False)
        ver = self._store_version(manifest, 1)
        ver.update(preflight=chk["preflight"], preflight_ok=chk["preflight_ok"],
                   preflight_problems=chk["preflight_problems"])
        ws = self.root / "workspace"
        if ws.exists():
            shutil.rmtree(ws)
        from .testdesign import _export
        _export(self.plan["repo"], self.plan["base_sha"], ws)
        self.events.append("start", None, {"chips": self.s.dev_debate_chips, "max_claims": self.s.dev_debate_max_claims,
                                           "max_turns": self.s.dev_debate_max_turns, "version": ver,
                                           "max_seconds": self.s.dev_debate_max_seconds,
                                           "max_evil_calls": self.s.dev_debate_max_evil_calls,
                                           "max_reattacks": self.s.dev_debate_reattacks,
                                           "request_sha256": dev._sha(self.plan["request"]),
                                           "files": [file_ref(self.root, self.root / ver["file"])]})
        dev._event(self.plan, "debate-start", plan_sha256=ver["sha256"])
        return self.advance()

    def _evidence(self, actions: list[dict]) -> dict:
        ev = {}
        for a in actions:
            if str(a.get("repro", "")).strip():
                self.log(f"debate: running {a['action']} evidence in the jail: {a['repro'][:100]}")
                ev[a["action"]] = run_repro(self.plan, a["repro"], self.s.dev_debate_repro_timeout)
        return ev

    def submit(self, actor: str, message: str, source: str, replay: bool = False) -> dict:
        """Validate and apply one turn (Architect Nick's from the CLI, or Evil Nick's response). Raises DebateError
        (recorded, no state change) for a protocol violation."""
        state, evs = self.state()
        if state is None:
            raise DebateError("the debate has not started (`nicks dev debate <plan> start`)")
        n = state["turns"] + 1
        tdir = self.root / "turns" / f"{n:03d}-{actor}"
        k = len(list(tdir.glob("message*.md"))) if tdir.exists() else 0
        tdir.mkdir(parents=True, exist_ok=True)
        mpath = tdir / (f"message-{k}.md" if k else "message.md")
        mpath.write_text(message, encoding="utf-8")
        try:
            actions = parse_control(message)
            check_turn(state, actor, actions)             # structural, before any expensive check
            results: dict = {}
            rev = next((a for a in actions if a["action"] == "REVISE"), None)
            if rev:
                if not replay and len(str(rev.get("approach", "")).strip()) < 40:   # (historical replays predate it)
                    raise DebateError("REVISE must document \"approach\": the approach chosen for the accepted concern(s), "
                                      "and why simpler or narrower alternatives were accepted or rejected (a FOLD accepts "
                                      "the concern, not the proposed solution)")
                mf = Path(str(rev.get("manifest_file", "")))
                if not mf.is_file():
                    raise DebateError("REVISE needs \"manifest_file\": the path of the complete revised manifest JSON")
                manifest = json.loads(mf.read_text(encoding="utf-8"))
                chk = self.check_manifest(manifest)
                if chk["sha256"] == current(state)["sha256"]:
                    raise DebateError("REVISE: the manifest is identical to the current version")
                ver = self._store_version(manifest, len(state["versions"]) + 1)
                ver.update(preflight=chk["preflight"], preflight_ok=True)
                ver["summary"] = str(rev.get("summary", ""))[:2000]
                ver["approach"] = str(rev.get("approach", ""))[:4000]
                tr = rev.get("tradeoffs") or []
                ver["tradeoffs"] = [str(t)[:600] for t in (tr if isinstance(tr, list) else [tr])][:10]
                results["version"] = ver
                check_turn(state, actor, actions, ver)  # an APPROVE in a REVISE turn approves the NEW version
                rev["manifest_file"] = ver["file"]
            evidence = self._evidence(actions)
            prim = next(a for a in actions if a["action"] in PRIMARY)
            if prim["action"] in evidence:
                results["evidence"] = evidence[prim["action"]]
            results["evidence_all"] = evidence
            new = apply_turn(state, actor, actions, results)
        except DebateError as exc:
            self.events.append("rejected_turn", actor, {"reason": str(exc)[:2000], "source": source,
                                                        "files": [file_ref(self.root, mpath)]})
            raise
        files = [file_ref(self.root, mpath)]
        if results.get("version"):                  # every historical plan version is integrity-protected too
            files.append(file_ref(self.root, self.root / results["version"]["file"]))
        self.events.append("turn", actor, {"actions": actions, "results": results, "source": source, "files": files,
                                           **({"replayed": True} if replay else {})})
        if results.get("version"):
            self._adopt(json.loads((self.root / results["version"]["file"]).read_text(encoding="utf-8")))
        if new["ended"]:
            self.events.append("end", None, new["ended"])
        state, evs = self.state()
        self._sync_plan(state, evs)
        return state

    def _budget(self, state: dict, evs: list[dict]) -> dict | None:
        start = evs[0]
        calls = sum(1 for e in evs if e["kind"] == "evil_call")
        if calls >= start["data"]["max_evil_calls"]:
            return terminal(state, f"inference budget ({start['data']['max_evil_calls']} Evil Nick calls) used", "inference")
        if time.time() - start["at"] > start["data"]["max_seconds"]:
            return terminal(state, f"wall-clock budget ({start['data']['max_seconds']:.0f}s) used", "time")
        return None

    def _evil_prompt(self, state: dict, evs: list[dict], fresh: bool, error: str | None) -> str:
        from .plancritic import build_inputs
        protocol = PROTOCOL.format(opp="Architect Nick", chips=state["chips_start"], min=MIN_STAKE, max=MAX_STAKE,
                                   resp=SHOWDOWN_RESPONSES, bmin=BLOCKING_MIN_STAKE, claims=state["max_claims"],
                                   reattacks=state.get("max_reattacks", 0))
        if error:
            return (f"The orchestrator REJECTED your last reply (no state change): {error}\n\n"
                    + state_text(state, "evil"))
        v = current(state)
        manifest = (self.root / v["file"]).read_text(encoding="utf-8")
        if fresh and state["reattacks"]:
            return self._reattack_prompt(state, evs, protocol, manifest)
        if fresh:
            _, sources, _, _ = build_inputs(dict(self.plan, manifest=json.loads(manifest)), self.s)
            b = __import__("secrets").token_hex(6)
            history = ""
            turns = [e for e in evs if e["kind"] == "turn"]
            if turns:                                  # a lost session restarts with the full record
                history = "\n\nTHE DEBATE SO FAR (your earlier session was lost; this is the orchestrator's record):\n" + \
                    "\n".join(f"--- turn by {NAMES[e['actor']]} ---\n" +
                              (self.root / e["data"]["files"][0]["path"]).read_text(encoding="utf-8")[-6000:]
                              for e in turns)
            return EVIL_OPENING.format(priorities=PRIORITIES, protocol=protocol, b=b, request=self.plan["request"],
                                       v=len(state["versions"]), hash=v["sha256"], manifest=manifest,
                                       sources=sources, state=state_text(state, "evil")) + history
        last_evil = max((i for i, e in enumerate(evs) if e["kind"] == "turn" and e["actor"] == "evil"), default=-1)
        news = []
        for e in evs[last_evil + 1:]:
            if e["kind"] != "turn":
                continue
            msg = (self.root / e["data"]["files"][0]["path"]).read_text(encoding="utf-8")
            news.append(f"{NAMES[e['actor']]}'s message, verbatim:\n<<<\n{msg.strip()}\n>>>")
            for r in (e["data"]["results"].get("evidence_all") or {}).values():
                news.append(f"Orchestrator evidence run on base {r['base']}: `{r['command']}` -> exit {r['exit_code']}\n"
                            f"{r['output'][-1500:]}")
            if e["data"]["results"].get("version"):
                news.append(f"PLAN REVISED to v{len(state['versions'])} (hash {v['sha256']}); approvals reset. "
                            f"Summary: {e['data']['results']['version'].get('summary', '')}\nFull plan:\n{manifest}")
        return EVIL_UPDATE.format(turn=state["turns"] + 1, news="\n\n".join(news) or "(no new messages)",
                                  state=state_text(state, "evil"))

    def _reattack_prompt(self, state: dict, evs: list[dict], protocol: str, manifest: str) -> str:
        """The fresh, independent session of a revision re-attack cycle: the request, the original plan, the revised
        plan, the exact diff, the debate record (as evidence) and the sources. It names no known defect."""
        import difflib
        from .plancritic import build_inputs
        r = state["reattack_log"][-1]
        ver = lambda i: (self.root / state["versions"][i - 1]["file"]).read_text(encoding="utf-8")
        diff = "\n".join(difflib.unified_diff(ver(1).splitlines(), manifest.splitlines(), "v1", f"v{r['to']}",
                                              lineterm="", n=2))     # cumulative: everything the debate changed
        record = []
        for e in evs:
            if e["kind"] != "turn":
                continue
            msg = (self.root / e["data"]["files"][0]["path"]).read_text(encoding="utf-8")
            record.append(f"--- {NAMES[e['actor']]} ---\n{msg.strip()[-8000:]}")
            for x in (e["data"]["results"].get("evidence_all") or {}).values():
                record.append(f"(orchestrator evidence run on base {x['base']}: `{x['command'][:600]}` -> exit "
                              f"{x['exit_code']}; output tail: {x['output'][-600:]})")
            if e["data"]["results"].get("version"):
                record.append(f"(-> plan revised; summary: {e['data']['results']['version'].get('summary', '')})")
        concerns = []
        for cid in state["order"]:
            c = state["claims"][cid]
            if c["outcome"] in ("accepted", "agreed"):
                by_v = [f"v{i + 1}" for i, v in enumerate(state["versions"]) if cid in (v.get("addresses") or [])]
                concerns.append(f"- {cid} ({NAMES[c['by']]}, stake {c['stake']}, {c['outcome']}): {c['title']}\n"
                                f"  concern: {c['claim']}\n  proposed resolution (NOT binding): {c['resolution']}\n"
                                f"  addressed in: {', '.join(by_v) or 'no revision named it'}")
        assumptions = []
        for i, v in enumerate(state["versions"][1:], start=2):
            assumptions.append(f"- v{i} (addresses {', '.join(v.get('addresses') or []) or '-'}): {v.get('summary', '')}\n"
                               f"  approach: {v.get('approach') or '(not documented)'}"
                               + "".join(f"\n  open trade-off: {t}" for t in (v.get('tradeoffs') or [])))
        _, sources, _, _ = build_inputs(dict(self.plan, manifest=json.loads(manifest)), self.s)
        return REATTACK_OPENING.format(cycle=r["cycle"], max=state["max_reattacks"], to=r["to"], frm=r["from"],
                                       protocol=protocol, chips=state["chips"]["evil"], min=MIN_STAKE,
                                       b=__import__("secrets").token_hex(6), request=self.plan["request"], v1=ver(1),
                                       hash=current(state)["sha256"], manifest=manifest, diff=diff or "(no textual change)",
                                       concerns="\n".join(concerns) or "(none)",
                                       assumptions="\n".join(assumptions) or "(no revision)",
                                       growth=growth_table([json.loads(ver(i)) for i in range(1, len(state["versions"]) + 1)]),
                                       record="\n\n".join(record), sources=sources, state=state_text(state, "evil"))

    def evil_turn(self) -> dict:
        """Run Evil Nick until it makes one valid move (at most 2 protocol retries), or record an interruption."""
        state, evs = self.state()
        epoch = state["reattacks"]                 # a revision re-attack cycle is a FRESH, independent session
        thread = next((e["data"]["thread_id"] for e in reversed(evs)
                       if e["kind"] == "evil_call" and e["data"]["ok"] and e["data"].get("thread_id")
                       and e["data"].get("epoch", 0) == epoch), None)
        error = None
        for attempt in range(3):
            if (b := self._budget(state, evs)):
                state = self._end(state, b)
                return state
            fresh = thread is None
            prompt = self._evil_prompt(state, evs, fresh, error)
            k = sum(1 for e in evs if e["kind"] == "evil_call") + 1
            out = self.root / "evil" / f"call-{k:03d}"
            t0 = time.time()
            res = self.runner().run(prompt, self.root / "workspace", out, self.s.dev_debate_turn_timeout,
                                    resume_thread=thread, session_store=self.root / "codex-sessions")
            if not res.ok and thread and res.error_class in ("runtime", "transient", "empty"):   # e.g. session lost
                self.log(f"debate: resuming Evil Nick's session failed ({res.error_class}); starting a new session "
                         "with the full record")
                self.events.append("evil_call", "evil", self._call(res, out, time.time() - t0, True, epoch))
                state, evs = self.state()
                thread = None
                continue
            self.events.append("evil_call", "evil", self._call(res, out, time.time() - t0, bool(thread), epoch))
            state, evs = self.state()
            if not res.ok:
                self.events.append("interrupted", None, {"reason": f"Evil Nick failed: {res.error_class}: "
                                                                   f"{(res.error or '')[:300]}"})
                state, evs = self.state()
                self._sync_plan(state, evs)
                return state
            thread = res.thread_id or thread
            text = (out / "response.txt").read_text(encoding="utf-8")
            try:
                return self.submit("evil", text, source=f"evil/call-{k:03d}/response.txt")
            except DebateError as exc:
                error = str(exc)
                state, evs = self.state()
        self.events.append("interrupted", None, {"reason": f"Evil Nick broke the protocol 3 times: {error}"})
        state, evs = self.state()
        self._sync_plan(state, evs)
        return state

    @staticmethod
    def _call(res, out: Path, seconds: float, resumed: bool, epoch: int = 0) -> dict:
        return {"ok": res.ok, "error_class": res.error_class, "error": (res.error or "")[:300], "model": res.model,
                "thread_id": res.thread_id, "resumed": resumed, "epoch": epoch, "seconds": round(seconds, 1),
                "tokens_in": res.tokens_in, "tokens_out": res.tokens_out, "dir": out.name}

    def advance(self) -> dict:
        """Run Evil Nick's turns until it is Architect Nick's turn, the debate ends, or it is interrupted."""
        while True:
            state, evs = self.state()
            if state["ended"] or state["to_move"] != "evil":
                self._sync_plan(state, evs)
                return state
            state = self.evil_turn()
            if self.events.events()[-1]["kind"] == "interrupted":
                return state

    def resume(self) -> dict:
        state, evs = self.state()
        if state is None:
            raise DebateError("nothing to resume: the debate has not started")
        if state["ended"]:
            return state
        return self.advance()

    def architect_turn(self, message: str) -> dict:
        self.submit("architect", message, source="architect (CLI)")
        return self.advance()

    def probe(self, command: str) -> dict:
        """Architect Nick checks a premise the way Evil Nick can in its workspace: the command runs on the unmodified
        repository in the network-less jail (never on the host). Recorded for transparency; it is not a turn."""
        state, _ = self.state()
        if state is None:
            raise DebateError("the debate has not started")
        r = run_repro(self.plan, command, self.s.dev_debate_repro_timeout)
        self.events.append("probe", "architect", {"result": r})
        return r

    def escalate(self, decision: str, reason: str) -> dict:
        state, evs = self.state()
        if state is None or not state["ended"] or state["ended"]["outcome"] == "CONSENSUS":
            raise DebateError("escalation is only for a debate that ended without consensus")
        if decision not in ("proceed", "stop"):
            raise DebateError("decision must be proceed or stop")
        if not reason.strip():
            raise DebateError("an escalation decision needs the user's reason, verbatim")
        self.events.append("escalation", "user", {"decision": decision, "reason": reason,
                                                  "outcome": state["ended"]["outcome"],
                                                  "plan_sha256": current(state)["sha256"]})
        state, evs = self.state()
        self._sync_plan(state, evs)
        return state


def debate_pending(plan: dict) -> str | None:
    """Why a debate-mode plan may not execute yet (None = it may)."""
    d = plan.get("detail") or {}
    if not d.get("debate_required"):
        return None
    db_ = d.get("debate") or {}
    st = db_.get("status")
    if st == "CONSENSUS":
        if db_.get("plan_sha256") != plan_hash(plan["manifest"]):
            return "the plan's manifest is not the version both agents approved"
        return None
    if st in ("BUDGET_EXHAUSTED", "DESIGN_BLOCKED", "UNRESOLVED"):
        esc = db_.get("escalation") or {}
        if esc.get("decision") == "proceed":
            return None
        return (f"the design debate ended {st} without consensus; this needs the USER's decision "
                "(`nicks dev debate <plan> escalate --decision proceed|stop --reason \"<the user's words>\"`)")
    if st == "interrupted":
        return "the design debate was interrupted: `nicks dev debate <plan> resume`"
    if st == "running":
        return "the design debate is still running: it is Architect Nick's turn (`nicks dev debate <plan> status`)"
    return "the design debate has not started: `nicks dev debate <plan> start`"
