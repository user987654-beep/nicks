---
name: nicks
description: Review Git diffs, arbitrate independently, plan and execute development tasks, or resume Council of Nicks jobs using its Python engine.
---

# Council of Nicks in Codex

Read [the shared workflow](WORKFLOW.md) before operating the Council.
Use `NICKS_HOST=codex`; choose the subscription profile independently. For a Codex-only run set
`NICKS_SUBSCRIPTION_PROFILE=codex-only`. Invoke the checkout's `bin/nicks` if `nicks` is not installed.

Act as Architect Nick using the shared manifest, revision, decomposition and debate instructions.
Use `nicks nickquisition run <job>` for independent subscription judges. Read the Python verdict and
report the actual models, missing ballots and diversity limitations. Resume development with `dev execute`,
reviews with `resume`, and judging with `nickquisition run`. Use `dev status`, `status`, `packet`, and
`nickquisition show|verdict` to inspect evidence. Do not replace Python orchestration with host subagents.

For initial unattended delivery use `dev deliver`. For a concluded REJECTED implementation, inspect the
verdict and use `dev rework PLAN --from JOB` only with explicit user authorization. Follow the shared
[rework command sequence](WORKFLOW.md#user-authorized-rework-of-a-rejection), including the returned child
state for delta-verdict inspection and any separately authorized next cycle. Never start the next cycle
automatically or treat workflow completion as judicial approval.

Configure `nicks config set subscriptions auto|claude-only|codex-only|both` and
`nicks config set mode hybrid|restricted` independently of the host. Use `--subscriptions` and `--mode`
for new-job overrides; resume restores saved policy and rejects conflicting overrides. Restricted permits
only authorized subscription destinations and guarantees neither source privacy nor application security.
Haiku development is available through tool-less JSON proposals applied by Python in isolated worktrees.

Large Nickquisition jobs use bounded original-evidence delivery on both hosts. Use `nickquisition run` to resume acknowledged delivery and existing ballots. `nickquisition scale`
explicitly continues an intact previous INCOMPLETE round without ballots. Never bypass missing delivery
receipts with a manual ballot or summarize originals for another judge.
