---
name: nicks
description: Council of Nicks. Run independent blind code reviews of a Git diff on a rotating pool of free AI models (via OpenCode), then have The Nickquisition judge the work: Good Nick (constructive), Evil Nick (adversarial) and Neutral Nick (independently selected high-capability model) deliberate once and vote, and the orchestrator computes the 2-of-3 verdict. Also the Development Engine: you plan a feature as small user stories and worker tasks, a Council worker implements one task in a jailed worktree, the engine verifies it, and The Nickquisition judges the patch. Use for "/nicks review [base-ref]", "/nicks dev <feature request>", "/nicks models", "/nicks status [job]", "/nicks report [plan|job]", "/nicks stats", "/nicks doctor", or when the user asks for a multi-model / council / free-model review of their changes or wants the Council to implement a small feature.
argument-hint: "review [base-ref] [--uncommitted] [--brief \"...\"] | dev <feature request> | models | status [job] | report [plan|job] | stats | doctor [--probe]"
allowed-tools: Bash(nicks:*), Bash(git diff:*), Bash(git log:*), Bash(git show:*), Bash(git status:*), Bash(git ls-files:*), Read, Grep, Glob, Write
---

# Council of Nicks in Claude Code

Read [WORKFLOW.md](WORKFLOW.md) and follow its shared review, arbitration, planning, development,
resume and debate procedures. Arguments: `$ARGUMENTS`.
Claude Code is the host; the configured subscription profile determines eligible agents.

Subscription authorization is separate from provider availability. Policy changes apply to new jobs;
resume restores the saved policy. Haiku development proposals are tool-less, credential-safe JSON validated
and applied by Python in a private worktree.

Set `NICKS_HOST=claude` for attribution. Configure `nicks config set subscriptions` and
`nicks config set mode` independently; the Claude host may use Codex-only and requires no Claude inference.
Use `--subscriptions` / `--mode` only for new-job overrides. Restricted limits destinations, not privacy or
application security.

For initial unattended delivery use `nicks dev deliver`. With the Claude subscription authorized and
`NICKS_HOST=claude`, it launches one headless Claude Code host (Opus/Sonnet) in the jail; with only Codex
authorized it launches a Codex host. For a concluded REJECTED implementation, inspect the verdict and use
`dev rework PLAN --from JOB` only with explicit user authorization, following the shared
[rework command sequence](WORKFLOW.md#user-authorized-rework-of-a-rejection) and the returned child state.
Never start the next cycle automatically or treat workflow completion as judicial approval. For one ready
story use `dev compete PLAN TASK` (Codex Luna if authorized, otherwise Claude Haiku). `deliver`, `rework` and
`compete` outlast a foreground Bash call: run them with `run_in_background` (or ask the user to run them with
`! nicks ...`) and report the printed `HOST`/`ENGINE_STATE` and JSON result. If you are yourself a delivery
host (`NICKS_DELIVERY_HOST=1`), never call `deliver` or `rework`.

Large Nickquisition jobs use bounded original-evidence delivery on both hosts. Use `nickquisition run` to resume acknowledged delivery and existing ballots. `nickquisition scale`
explicitly continues an intact previous INCOMPLETE round without ballots. Never bypass missing delivery
receipts with a manual ballot or summarize originals for another judge.
