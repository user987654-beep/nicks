"""SQLite journal: jobs, reviewer slots, attempts, discovered routes, route health and model-call telemetry.

Raw worker output never lives in the database; it is stored as files whose paths and
SHA-256 hashes are journaled here.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    repo        TEXT,
    params      TEXT,   -- JSON
    snapshot    TEXT,   -- JSON
    summary     TEXT    -- JSON
);
CREATE TABLE IF NOT EXISTS slots (
    job_id      TEXT NOT NULL,
    slot        INTEGER NOT NULL,
    status      TEXT NOT NULL,          -- pending | running | done | exhausted
    attempt_id  TEXT,                   -- the attempt that satisfied the slot
    note        TEXT,
    PRIMARY KEY (job_id, slot)
);
CREATE TABLE IF NOT EXISTS attempts (
    id            TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL,
    slot          INTEGER NOT NULL,
    route         TEXT NOT NULL,
    provider      TEXT NOT NULL,
    family        TEXT,
    diversity_key TEXT,
    basis         TEXT,
    status        TEXT NOT NULL,        -- running | ok | failed | interrupted | duplicate
    error_class   TEXT,
    error         TEXT,
    action        TEXT,                 -- what the router did about a failure
    started_at    REAL NOT NULL,
    finished_at   REAL,
    pid           INTEGER,
    exit_code     INTEGER,
    session_id    TEXT,
    tokens_in     INTEGER,
    tokens_out    INTEGER,
    cost          REAL,
    finish_reason TEXT,
    tool_calls    INTEGER,
    dir           TEXT,
    raw_sha256    TEXT,
    response_sha256 TEXT,
    response_bytes  INTEGER,
    prompt_sha256 TEXT,
    injected      INTEGER DEFAULT 0,
    actual_model  TEXT                  -- the model the worker reports it used (subscription workers)
);
CREATE INDEX IF NOT EXISTS attempts_job ON attempts(job_id, slot);
CREATE TABLE IF NOT EXISTS routes (
    route         TEXT PRIMARY KEY,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    name          TEXT,
    family        TEXT,
    diversity_key TEXT,
    eligible      INTEGER NOT NULL,
    basis         TEXT,
    reason        TEXT,
    local         INTEGER,
    tools         INTEGER,
    context       INTEGER,
    endpoint      TEXT,
    present       INTEGER NOT NULL,
    discovered_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS adversary_runs (   -- Evil Nick (and future non-council members)
    id              TEXT PRIMARY KEY,
    job_id          TEXT NOT NULL,
    member          TEXT NOT NULL,          -- 'evil'
    stage           TEXT NOT NULL,          -- 'attack' (blind) | 'challenge' (attacks the arbiter)
    status          TEXT NOT NULL,          -- running | ok | failed | interrupted
    error_class     TEXT,
    error           TEXT,
    engine          TEXT,                   -- e.g. 'codex-cli 0.167.0 (chatgpt subscription)'
    model           TEXT,
    thread_id       TEXT,
    started_at      REAL NOT NULL,
    finished_at     REAL,
    exit_code       INTEGER,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    dir             TEXT,
    prompt_sha256   TEXT,
    raw_sha256      TEXT,
    response_sha256 TEXT,
    response_bytes  INTEGER,
    challenges_decision INTEGER             -- stage 'challenge': the provisional decision it attacked
);
CREATE TABLE IF NOT EXISTS decisions (        -- declared by a council member; no votes are counted yet
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       TEXT NOT NULL,
    member       TEXT NOT NULL,             -- 'good' (legacy decisions; Nickquisition ballots live in nq_ballots)
    stage        TEXT NOT NULL,             -- 'provisional' | 'final'
    decision     TEXT NOT NULL,             -- approve | request-changes | needs-investigation
    unresolved   INTEGER,                   -- final: Evil Nick objections the member declares unresolved
    responds_to  TEXT,                      -- final: adversary run id answered
    skip_reason  TEXT,                      -- final without a challenge: why
    path         TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS neutral_selections (  -- every Neutral Nick draw, kept forever (no silent rerolls)
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT NOT NULL,
    provider      TEXT NOT NULL,             -- anthropic | openai
    model         TEXT NOT NULL,             -- requested model id
    reason        TEXT NOT NULL,             -- initial | fallback | operator-forced
    replaces_run  TEXT,                      -- fallback: the failed run that forced a new draw
    failure       TEXT,                      -- fallback: why that run failed (execution failure only)
    eligible      TEXT,                      -- JSON: providers and models eligible at draw time
    avoided       TEXT,                      -- JSON: models avoided (Good/Evil models, failed models)
    notes         TEXT,                      -- e.g. reduced provider diversity
    random_source TEXT,
    run_id        TEXT,                      -- the adversary_runs row executing this selection
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS nq_rounds (           -- The Nickquisition: one row per explicit deliberation round
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL,
    round           INTEGER NOT NULL,
    artifact        TEXT NOT NULL,            -- sha256 over (job, base, head tree/commit, diff sha256)
    artifact_detail TEXT NOT NULL,            -- JSON of the bound revision identity
    evidence_path   TEXT NOT NULL,            -- identical evidence package for all three members
    evidence_sha256 TEXT NOT NULL,
    gates           TEXT,                     -- JSON: objective test evidence (base vs head)
    opened_at       REAL NOT NULL,
    UNIQUE (job_id, round)
);
CREATE TABLE IF NOT EXISTS nq_ballots (          -- write-once; enforced by triggers below
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           TEXT NOT NULL,
    round            INTEGER NOT NULL,
    member           TEXT NOT NULL CHECK (member IN ('good', 'evil', 'neutral')),
    model            TEXT,                    -- actual model (Good: self-declared)
    valid            INTEGER NOT NULL,        -- 0: malformed / wrong artifact; never counted as a vote
    vote             TEXT CHECK (vote IS NULL OR vote IN ('APPROVE', 'REJECT')),
    artifact         TEXT NOT NULL,           -- the artifact the ballot names
    justification    TEXT,
    reason           TEXT,                    -- why an invalid ballot was rejected
    source           TEXT NOT NULL,           -- 'submitted-by-good' | 'collected-by-orchestrator'
    deliberation     TEXT,                    -- path of the member's verbatim deliberation
    deliberation_sha256 TEXT,
    vote_run         TEXT,                    -- adversary_runs id of the vote execution (Evil/Neutral)
    parser           TEXT,                    -- ballot parser version (NULL: legacy regex, before V1D.1)
    record_sha256    TEXT NOT NULL,           -- hash of the canonical ballot record
    created_at       REAL NOT NULL,
    UNIQUE (job_id, round, member)
);
CREATE TRIGGER IF NOT EXISTS nq_ballots_immutable_update BEFORE UPDATE ON nq_ballots
BEGIN SELECT RAISE(ABORT, 'Nickquisition ballots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS nq_ballots_immutable_delete BEFORE DELETE ON nq_ballots
BEGIN SELECT RAISE(ABORT, 'Nickquisition ballots are immutable'); END;
CREATE TABLE IF NOT EXISTS nq_evidence_bundles (
    job_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    artifact TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    delivery_version INTEGER NOT NULL DEFAULT 1,
    error TEXT,
    PRIMARY KEY(job_id, round)
);
CREATE TRIGGER IF NOT EXISTS nq_evidence_immutable_update BEFORE UPDATE ON nq_evidence_bundles
BEGIN SELECT RAISE(ABORT, 'Evidence manifests are immutable'); END;
CREATE TRIGGER IF NOT EXISTS nq_evidence_immutable_delete BEFORE DELETE ON nq_evidence_bundles
BEGIN SELECT RAISE(ABORT, 'Evidence manifests are immutable'); END;
CREATE TABLE IF NOT EXISTS nq_verdicts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    round       INTEGER NOT NULL,
    verdict     TEXT NOT NULL,                -- APPROVED | REJECTED | BLOCKED | INCOMPLETE
    tally       TEXT NOT NULL,                -- e.g. "2-1"
    detail      TEXT NOT NULL,                -- JSON (votes, missing, blockers, dissent)
    report_path TEXT NOT NULL,
    json_path   TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS health (
    scope                TEXT PRIMARY KEY,   -- 'route:<provider/model>' or 'provider:<id>'
    cooldown_until       REAL DEFAULT 0,
    reason               TEXT,
    error_class          TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    successes            INTEGER DEFAULT 0,
    failures             INTEGER DEFAULT 0,
    last_failure_at      REAL,
    last_success_at      REAL,
    last_used_at         REAL,
    latency_avg          REAL,
    quarantined          INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dev_plans (          -- Development Engine: Claude's plan (manifest kept verbatim)
    id          TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    repo        TEXT NOT NULL,
    base_sha    TEXT NOT NULL,
    request     TEXT NOT NULL,
    manifest    TEXT NOT NULL,                -- JSON, as validated
    manifest_sha256 TEXT NOT NULL,
    dir         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dev_tasks (
    plan_id     TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    status      TEXT NOT NULL,                -- pending | running | verified | needs-decomposition | superseded | blocked
    reason      TEXT,
    spec        TEXT NOT NULL,                -- JSON
    result      TEXT,                         -- JSON: winning attempt, commit, patch
    updated_at  REAL NOT NULL,
    PRIMARY KEY (plan_id, task_id)
);
CREATE TABLE IF NOT EXISTS dev_attempts (
    id           TEXT PRIMARY KEY,
    plan_id      TEXT NOT NULL,
    task_id      TEXT NOT NULL,
    n            INTEGER NOT NULL,
    worker       TEXT NOT NULL,               -- route, e.g. codex/gpt-6-luna or opencode/x-free
    engine       TEXT NOT NULL,               -- codex | opencode
    actual_model TEXT,                        -- what the provider reports it ran
    status       TEXT NOT NULL,               -- running | verified | verify-failed | scope-violation | no-change
                                              -- | timeout | infra-failed | interrupted
    failure_kind TEXT,                        -- infra | implementation | scope (NULL when verified)
    error_class  TEXT,
    error        TEXT,
    repairs      INTEGER DEFAULT 0,
    started_at   REAL NOT NULL,
    finished_at  REAL,
    pid          INTEGER,
    dir          TEXT NOT NULL,
    commit_sha   TEXT,
    patch_sha256 TEXT,
    detail       TEXT                         -- JSON: changed files, verification runs, notes
);
"""


class DB:
    """Thin, thread-safe wrapper. One connection guarded by a lock; WAL for multi-process use."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=30000")
            self.conn.executescript(SCHEMA)
            from .telemetry import SCHEMA as TELEMETRY_SCHEMA
            self.conn.executescript(TELEMETRY_SCHEMA)        # per-invocation usage telemetry (nicks/telemetry.py)
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(attempts)")}
            if "actual_model" not in cols:      # migration for journals created before hybrid Councils
                self.conn.execute("ALTER TABLE attempts ADD COLUMN actual_model TEXT")
            for col in ("quota_pool", "free_type"):
                if col not in cols:
                    self.conn.execute(f"ALTER TABLE attempts ADD COLUMN {col} TEXT")
            rcols = {r[1] for r in self.conn.execute("PRAGMA table_info(routes)")}
            if "metadata" not in rcols:
                self.conn.execute("ALTER TABLE routes ADD COLUMN metadata TEXT")
            self.conn.execute("CREATE TABLE IF NOT EXISTS provider_discovery "
                              "(provider TEXT PRIMARY KEY, detail TEXT NOT NULL, checked_at REAL NOT NULL)")
            self.conn.execute("CREATE TABLE IF NOT EXISTS onboarding_checks "
                              "(pool TEXT PRIMARY KEY, next_check REAL NOT NULL, detail TEXT NOT NULL)")
            pcols = {r[1] for r in self.conn.execute("PRAGMA table_info(dev_plans)")}
            for col, decl in (("status", "TEXT DEFAULT 'planned'"), ("head", "TEXT"), ("review_job", "TEXT"),
                              ("detail", "TEXT")):           # Development Engine V1B plan state
                if col not in pcols:
                    self.conn.execute(f"ALTER TABLE dev_plans ADD COLUMN {col} {decl}")
            ecols = {r[1] for r in self.conn.execute("PRAGMA table_info(nq_evidence_bundles)")}
            if "delivery_version" not in ecols:
                self.conn.execute("ALTER TABLE nq_evidence_bundles ADD COLUMN delivery_version INTEGER NOT NULL DEFAULT 1")
            bcols = {r[1] for r in self.conn.execute("PRAGMA table_info(nq_ballots)")}
            if "parser" not in bcols:           # V1D.1: which ballot parser judged the raw output (NULL = legacy regex)
                self.conn.execute("ALTER TABLE nq_ballots ADD COLUMN parser TEXT")
            for column in ('delivery_sha256', 'manifest_sha256'):
                if column not in bcols:
                    self.conn.execute(f"ALTER TABLE nq_ballots ADD COLUMN {column} TEXT")

    # -- Development Engine ------------------------------------------------------------
    def dev_plan(self, plan_id: str) -> dict | None:
        r = self.one("SELECT * FROM dev_plans WHERE id=? OR id LIKE ? ORDER BY created_at DESC",
                     (plan_id, plan_id + "%"))
        if r:
            r["manifest"] = json.loads(r["manifest"])
            r["detail"] = json.loads(r["detail"]) if r.get("detail") else {}
        return r

    def set_dev_plan(self, plan_id: str, **fields_) -> None:
        if "detail" in fields_ and not isinstance(fields_["detail"], str):
            fields_["detail"] = json.dumps(fields_["detail"])
        sets = ", ".join(f"{k}=?" for k in fields_)
        self.execute(f"UPDATE dev_plans SET {sets} WHERE id=?", (*fields_.values(), plan_id))

    def latest_dev_plan(self) -> dict | None:
        r = self.one("SELECT id FROM dev_plans ORDER BY created_at DESC LIMIT 1")
        return self.dev_plan(r["id"]) if r else None

    def dev_tasks(self, plan_id: str) -> list[dict]:
        rows = self.all("SELECT * FROM dev_tasks WHERE plan_id=? ORDER BY rowid", (plan_id,))
        for r in rows:
            r["spec"] = json.loads(r["spec"])
            r["result"] = json.loads(r["result"]) if r["result"] else None
        return rows

    def set_dev_task(self, plan_id: str, task_id: str, status: str, reason: str | None = None,
                     result: dict | None = None) -> None:
        sets, vals = ["status=?", "reason=?", "updated_at=?"], [status, reason, time.time()]
        if result is not None:
            sets.append("result=?")
            vals.append(json.dumps(result))
        self.execute(f"UPDATE dev_tasks SET {', '.join(sets)} WHERE plan_id=? AND task_id=?", (*vals, plan_id, task_id))

    def dev_attempts(self, plan_id: str, task_id: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM dev_attempts WHERE plan_id=?", [plan_id]
        if task_id:
            q, a = q + " AND task_id=?", a + [task_id]
        rows = self.all(q + " ORDER BY started_at, n", tuple(a))
        for r in rows:
            r["detail"] = json.loads(r["detail"]) if r["detail"] else {}
        return rows

    def start_dev_attempt(self, row: dict) -> None:
        cols = ", ".join(row)
        self.execute(f"INSERT INTO dev_attempts ({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))

    def finish_dev_attempt(self, attempt_id: str, **fields_) -> None:
        if "detail" in fields_:
            fields_["detail"] = json.dumps(fields_["detail"])
        sets = ", ".join(f"{k}=?" for k in fields_)
        self.execute(f"UPDATE dev_attempts SET {sets} WHERE id=?", (*fields_.values(), attempt_id))

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # -- primitives -------------------------------------------------------
    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def all(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()) -> dict | None:
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    # -- jobs ---------------------------------------------------------------
    def create_job(self, job_id: str, kind: str, repo: str, params: dict, snapshot: dict, slots: int) -> None:
        now = time.time()
        with self.tx() as c:
            c.execute(
                "INSERT INTO jobs(id, kind, status, created_at, updated_at, repo, params, snapshot) VALUES (?,?,?,?,?,?,?,?)",
                (job_id, kind, "pending", now, now, repo, json.dumps(params), json.dumps(snapshot)),
            )
            for i in range(slots):
                c.execute("INSERT INTO slots(job_id, slot, status) VALUES (?,?,?)", (job_id, i, "pending"))

    def get_job(self, job_id: str) -> dict | None:
        job = self.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job:
            for k in ("params", "snapshot", "summary"):
                job[k] = json.loads(job[k]) if job[k] else {}
        return job

    def find_job(self, prefix: str) -> dict | None:
        rows = self.all("SELECT id FROM jobs WHERE id LIKE ? ORDER BY created_at DESC", (prefix + "%",))
        if len(rows) == 1:
            return self.get_job(rows[0]["id"])
        if len(rows) > 1:
            exact = [r for r in rows if r["id"] == prefix]
            if exact:
                return self.get_job(prefix)
            raise LookupError(f"job prefix {prefix!r} is ambiguous: {', '.join(r['id'] for r in rows[:5])}")
        return None

    def latest_job(self) -> dict | None:
        row = self.one("SELECT id FROM jobs ORDER BY created_at DESC LIMIT 1")
        return self.get_job(row["id"]) if row else None

    def list_jobs(self, limit: int = 20) -> list[dict]:
        return self.all("SELECT id, kind, status, created_at, repo, summary FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))

    def update_job(self, job_id: str, *, status: str | None = None, summary: dict | None = None) -> None:
        sets, vals = ["updated_at=?"], [time.time()]
        if status is not None:
            sets.append("status=?")
            vals.append(status)
        if summary is not None:
            sets.append("summary=?")
            vals.append(json.dumps(summary))
        vals.append(job_id)
        self.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", vals)

    # -- slots ----------------------------------------------------------------
    def slots(self, job_id: str) -> list[dict]:
        return self.all("SELECT * FROM slots WHERE job_id=? ORDER BY slot", (job_id,))

    def set_slot(self, job_id: str, slot: int, status: str, note: str | None = None) -> None:
        self.execute("UPDATE slots SET status=?, note=? WHERE job_id=? AND slot=?", (status, note, job_id, slot))

    def complete_slot(self, job_id: str, slot: int, attempt_id: str, **attempt_fields) -> bool:
        """Atomically mark a slot done (exactly once) and its attempt ok.

        Returns False, and records the attempt as 'duplicate', if the slot was already done.
        One transaction: a crash can never leave a done slot whose attempt is not 'ok'.
        """
        with self.tx() as c:
            cur = c.execute(
                "UPDATE slots SET status='done', attempt_id=?, note=NULL WHERE job_id=? AND slot=? AND status!='done'",
                (attempt_id, job_id, slot),
            )
            won = cur.rowcount == 1
            fields_ = {**attempt_fields, "status": "ok" if won else "duplicate"}
            if not won:
                fields_["action"] = "slot already satisfied"
            sets = ", ".join(f"{k}=?" for k in fields_)
            c.execute(f"UPDATE attempts SET {sets} WHERE id=?", (*fields_.values(), attempt_id))
        return won

    # -- attempts ---------------------------------------------------------------
    def start_attempt(self, row: dict) -> None:
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        self.execute(f"INSERT INTO attempts({cols}) VALUES ({qs})", tuple(row.values()))

    def finish_attempt(self, attempt_id: str, **fields_) -> None:
        sets = ", ".join(f"{k}=?" for k in fields_)
        self.execute(f"UPDATE attempts SET {sets} WHERE id=?", (*fields_.values(), attempt_id))

    def attempts(self, job_id: str) -> list[dict]:
        return self.all("SELECT * FROM attempts WHERE job_id=? ORDER BY started_at, id", (job_id,))

    # -- council members beyond the free Nicks ----------------------------------------
    def adversary_runs(self, job_id: str, stage: str | None = None, member: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM adversary_runs WHERE job_id=?", [job_id]
        if stage:
            q, args = q + " AND stage=?", args + [stage]
        if member:
            q, args = q + " AND member=?", args + [member]
        return self.all(q + " ORDER BY started_at", tuple(args))

    def latest_ok_run(self, job_id: str, stage: str, member: str | None = None) -> dict | None:
        runs = [r for r in self.adversary_runs(job_id, stage, member) if r["status"] == "ok"]
        return runs[-1] if runs else None

    def add_decision(self, row: dict) -> int:
        cols = ", ".join(row)
        cur = self.execute(f"INSERT INTO decisions({cols}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))
        return cur.lastrowid

    def decisions(self, job_id: str) -> list[dict]:
        return self.all("SELECT * FROM decisions WHERE job_id=? ORDER BY id", (job_id,))

    def latest_decision(self, job_id: str, member: str, stage: str) -> dict | None:
        return self.one("SELECT * FROM decisions WHERE job_id=? AND member=? AND stage=? ORDER BY id DESC LIMIT 1",
                        (job_id, member, stage))

    def neutral_selections(self, job_id: str) -> list[dict]:
        return self.all("SELECT * FROM neutral_selections WHERE job_id=? ORDER BY id", (job_id,))

    def add_neutral_selection(self, row: dict) -> int:
        cols = ", ".join(row)
        return self.execute(f"INSERT INTO neutral_selections({cols}) VALUES ({', '.join('?' for _ in row)})",
                            tuple(row.values())).lastrowid

    # -- routes ---------------------------------------------------------------------
    def save_routes(self, routes: list[dict]) -> None:
        now = time.time()
        with self.tx() as c:
            c.execute("UPDATE routes SET present=0")
            for r in routes:
                c.execute(
                    """INSERT INTO routes(route, provider, model, name, family, diversity_key, eligible, basis, reason,
                                          local, tools, context, endpoint, present, discovered_at)
                       VALUES (:route,:provider,:model,:name,:family,:diversity_key,:eligible,:basis,:reason,
                               :local,:tools,:context,:endpoint,1,:now)
                       ON CONFLICT(route) DO UPDATE SET provider=excluded.provider, model=excluded.model,
                           name=excluded.name, family=excluded.family, diversity_key=excluded.diversity_key,
                           eligible=excluded.eligible, basis=excluded.basis, reason=excluded.reason,
                           local=excluded.local, tools=excluded.tools, context=excluded.context,
                           endpoint=excluded.endpoint, present=1, discovered_at=excluded.discovered_at""",
                    {**r, "now": now},
                )
                from .registry import Route
                metadata = {k: v for k, v in r.items() if k in Route.__dataclass_fields__}
                c.execute("UPDATE routes SET metadata=? WHERE route=?", (json.dumps(metadata), r["route"]))

    def save_provider_discovery(self, records: dict) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM provider_discovery")
            for provider, detail in records.items():
                c.execute("INSERT INTO provider_discovery VALUES (?,?,?)", (provider, json.dumps(detail), time.time()))

    def routes(self, present_only: bool = True) -> list[dict]:
        q = "SELECT * FROM routes" + (" WHERE present=1" if present_only else "") + " ORDER BY provider, model"
        return self.all(q)

    # -- health -------------------------------------------------------------------------
    def health(self, scope: str) -> dict:
        return self.one("SELECT * FROM health WHERE scope=?", (scope,)) or {"scope": scope}

    def put_health(self, scope: str, **vals) -> None:
        with self.lock:
            self.execute("INSERT OR IGNORE INTO health(scope) VALUES (?)", (scope,))
            if vals:
                sets = ", ".join(f"{k}=?" for k in vals)
                self.execute(f"UPDATE health SET {sets} WHERE scope=?", (*vals.values(), scope))

    def all_health(self) -> list[dict]:
        return self.all("SELECT * FROM health ORDER BY scope")

    def reset_health(self, scope: str | None = None) -> int:
        if scope:
            return self.execute("DELETE FROM health WHERE scope=?", (scope,)).rowcount
        return self.execute("DELETE FROM health").rowcount
