"""Paths and operator settings.

Settings come from built-in defaults, then ``$NICKS_HOME/config.json`` (optional),
then CLI overrides. Nothing here names specific models: the free pool is discovered.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path


def state_dir() -> Path:
    env = os.environ.get("NICKS_HOME")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "nicks"


def opencode_bin() -> str:
    env = os.environ.get("NICKS_OPENCODE_BIN")
    if env:
        return env
    found = shutil.which("opencode")
    if found:
        return found
    return str(Path.home() / ".opencode" / "bin" / "opencode")


@dataclass
class Settings:
    rework_context: dict | None = None # immutable user-authorized child-cycle contract reference
    host: str = "auto"                 # interaction environment, never a provider selector
    subscription_profile: str = "auto" # auto, claude-only, codex-only, both
    authorized_subscriptions: list | None = None
    restricted: bool = False           # subscription-backed Council workers only
    role_models: dict = field(default_factory=dict) # role -> [{provider: codex|claude, model: ...}]
    # How many independent reviewers to aim for, and the minimum for a non-degraded verdict.
    nicks: int = 5
    min_nicks: int = 2
    # Wall-clock limits (seconds). Defaults keep a foreground run under Claude Code's 10 min Bash cap.
    attempt_timeout: float = 420.0
    local_attempt_timeout: float = 480.0
    job_timeout: float = 540.0
    # Failover budget.
    max_attempts_per_slot: int = 4
    min_attempt_time: float = 150.0   # don't start an attempt with less job time left than this
    # V1.2 deadline-aware fallback: while a subscription replacement is possible, free attempts end this much
    # before the job deadline (partial output kept), so a Luna/Haiku worker can still fill the slot.
    fallback_reserve: float = 150.0
    min_fallback_time: float = 60.0   # never launch a subscription attempt with less job time than this
    max_wait_for_cooldown: float = 60.0
    # Local inference (loopback endpoints such as LM Studio / Ollama).
    allow_local: bool = True
    local_concurrency: int = 1
    local_load_on_demand: bool = False
    # Free-only policy knobs. Credentialed zero-priced routes are off by default because an
    # account credential can be billed if pricing metadata is stale or wrong.
    allow_credentialed_zero_cost: bool = False
    local_providers: list = field(default_factory=list)
    exclude_routes: list = field(default_factory=list)
    # Worker filesystem isolation (bubblewrap). Disabling it leaves only OpenCode's own permission rules.
    sandbox: bool = True
    sandbox_extra_ro: list = field(default_factory=list)
    sandbox_extra_rw: list = field(default_factory=list)
    # Evil Nick (Codex CLI on a ChatGPT subscription). Model "" = Codex's default for the subscription.
    evil_timeout: float = 480.0
    evil_model: str = ""
    # Neutral Nick: curated high-capability models reachable through the existing subscriptions. Availability is
    # never assumed: OpenAI models must also appear in Codex's live catalog, and every model is health-tracked
    # (`nicks neutral models --probe`). Lightweight tiers (Haiku, *-luna, older generations) are excluded.
    neutral_anthropic_models: list = field(default_factory=lambda: [      # subscription-included only (never Fable)
        "claude-opus-5-5", "claude-sonnet-5-5"])
    neutral_openai_models: list = field(default_factory=lambda: [
        "gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-sol"])
    neutral_timeout: float = 480.0
    neutral_max_attempts: int = 3
    neutral_inline_bytes: int = 200_000
    # The Nickquisition: per-execution timeout for deliberation and vote runs, and per test-gate run.
    nq_timeout: float = 420.0
    nq_gate_timeout: float = 300.0
    # Hybrid Council: permanent subscription workers plus free workers, with subscription overflow when free
    # capacity is exhausted. Slot kinds: "luna", "haiku" (named subscription workers) and "free".
    hybrid_council: bool = True
    council_composition: list = field(default_factory=lambda: ["luna", "haiku", "free", "free", "free"])
    council_subscription: dict = field(default_factory=lambda: {
        "luna": {"provider": "codex", "model": "gpt-6-luna", "args": ["-c", 'model_reasoning_effort="low"']},
        "haiku": {"provider": "claude", "model": "claude-haiku-5-5"}})
    subscription_concurrency: int = 2      # parallel instances per subscription route
    subscription_max_attempts: int = 10    # per job; the Nickquisition shares these subscriptions
    council_inline_bytes: int = 100_000    # changed files inlined into the Council prompt (tool-less workers)
    # Related UNCHANGED files packaged for reviewers without file tools (nicks/context.py): total and per file.
    review_context_bytes: int = 80_000
    review_context_file_bytes: int = 40_000
    # External free providers via OpenCode, e.g. {"openrouter": {"key_file": "...", "env": ["OPENROUTER_API_KEY"],
    # "policy": "zero-priced"}}; see `nicks providers`.
    free_providers: dict = field(default_factory=dict)
    auto_onboarding: bool = True       # trusted anonymous services + existing credential references
    auto_disabled_providers: list = field(default_factory=list)  # explicit removals survive rediscovery
    local_endpoints: dict = field(default_factory=dict)  # discovered loopback LM Studio / Ollama bases
    # Development Engine (nicks/dev.py): write-capable Council workers in disposable jailed worktrees.
    # Worker chain: named subscription roles that have a kernel-enforced credential boundary for write work
    # (Codex only: Claude Code's in-process tools could read its own OAuth login) and "free" OpenCode routes.
    dev_workers: list = field(default_factory=lambda: ["luna", "free"])
    dev_worker_timeout: float = 900.0
    dev_verify_timeout: float = 300.0
    dev_max_attempts: int = 4            # worker executions per task per run (infra + implementation)
    dev_max_impl_failures: int = 2       # then the task goes back to Claude for decomposition
    dev_max_repairs: int = 1             # repair rounds after failed verification, within one attempt
    dev_max_scope_entries: int = 8       # plan validation: a worker task names at most this many scope globs
    dev_max_file_bytes: int = 512_000    # a changed file larger than this is rejected
    dev_agent_steps: int = 60
    dev_max_decomposition_depth: int = 2  # a task split this many times that still fails is `failed` (human)
    dev_max_amendments: int = 8           # per plan: Claude cannot replan the same epic endlessly
    dev_execute_seconds: float = 540.0    # one `nicks dev execute` call (fits a 10-minute skill Bash call)
    dev_parallel_workers: int = 2         # V1C: independent ready tasks with disjoint scopes run concurrently
    dev_max_integration_retries: int = 1  # re-implementations against the latest state after an integration failure
    dev_plan_critic: bool = True          # V1D.1: an independent Plan Critic reviews every new plan before execution
    # Critic order and configs (V1D.1 diagnostic on the Round 1 plans: Haiku caught 3 of the 6 planner-prescribed defects,
    # Luna at medium effort 1, Luna at the Council's low effort 0; see docs/VALIDATION.md). Kept apart from
    # council_subscription so a critic configuration never becomes a Council review route.
    dev_plan_critic_models: list = field(default_factory=lambda: ["haiku", "luna-medium"])
    dev_plan_critic_specs: dict = field(default_factory=lambda: {
        "luna-medium": {"provider": "codex", "model": "gpt-6-luna", "args": ["-c", 'model_reasoning_effort="medium"']}})
    dev_critic_timeout: float = 600.0
    dev_critic_context_bytes: int = 120_000
    dev_critic_file_bytes: int = 40_000
    dev_adversarial_tests: bool = True    # V1D.2: Evil Nick designs contract + holdout tests before implementation
    dev_test_designer_timeout: float = 900.0
    # Stake-Based Design Debate V1 (experimental planning mode: `nicks dev plan --debate`)
    dev_debate_chips: int = 100           # per agent; spent chips are gone (no rewards, no refunds)
    dev_debate_max_claims: int = 12       # claims in total, both agents together
    dev_debate_max_turns: int = 60        # accepted turns: an infinite-loop guard
    dev_debate_max_seconds: float = 7200.0   # wall-clock budget for the whole debate
    dev_debate_max_evil_calls: int = 40   # inference budget: Evil Nick invocations, retries included
    dev_debate_turn_timeout: float = 900.0
    dev_debate_repro_timeout: float = 60.0
    dev_debate_reattacks: int = 2         # V1.1: independent re-attack cycles on negotiated revisions (0 = V1)
    dev_preflight: bool = True            # V1D.1: Acceptance Check Preflight before any worker is dispatched
    dev_max_timeouts: int = 3             # V1D.1: worker timeouts (any model, all runs) before the task returns to Claude
    # Packet / input limits.
    inline_limit_bytes: int = 300_000
    max_diff_bytes: int = 400_000
    agent_steps: int = 40


def load_settings(overrides: dict | None = None) -> Settings:
    s = Settings()
    known = {f.name for f in fields(Settings)}
    path = state_dir() / "config.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read execution configuration {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("config.json must contain a JSON object")
        for k, v in data.items():
            if k in known:
                setattr(s, k, v)
            else:
                print(f"[nicks] warning: unknown setting {k!r} in {path}", file=sys.stderr)
    env_values = {}
    if "NICKS_HOST" in os.environ: env_values["host"] = os.environ["NICKS_HOST"]
    if "NICKS_SUBSCRIPTION_PROFILE" in os.environ: env_values["subscription_profile"] = os.environ["NICKS_SUBSCRIPTION_PROFILE"]
    if "NICKS_RESTRICTED" in os.environ:
        mode_value = os.environ["NICKS_RESTRICTED"].strip().lower()
        if mode_value not in ("1", "true", "yes", "0", "false", "no"):
            raise ValueError("NICKS_RESTRICTED must be 1/true/yes or 0/false/no")
        env_values["restricted"] = mode_value in ("1", "true", "yes")
    for k, v in env_values.items(): setattr(s, k, v)
    for k, v in (overrides or {}).items():
        if v is not None and k in known:
            setattr(s, k, v)
    from .policy import validate
    validate(s)
    return s


def set_config(key: str, value: str) -> dict:
    path = state_dir() / "config.json"
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict): raise ValueError("config.json must contain a JSON object")
    if key == "subscriptions":
        if value not in ("auto", "claude-only", "codex-only", "both"): raise ValueError("subscriptions must be auto, claude-only, codex-only, or both")
        data["subscription_profile"] = value
        data.pop("authorized_subscriptions", None)
    elif key == "mode":
        if value not in ("hybrid", "restricted"): raise ValueError("mode must be hybrid or restricted")
        data["restricted"] = value == "restricted"
        data["hybrid_council"] = True
    else: raise ValueError(f"unknown config setting {key!r}")
    candidate = Settings()
    known = {f.name for f in fields(Settings)}
    for k, v in data.items():
        if k in known:
            setattr(candidate, k, v)
    from .policy import validate
    validate(candidate)
    path.parent.mkdir(parents=True, exist_ok=True)
    import os
    import tempfile
    fd, name = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(data, indent=2) + "\n")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return data
