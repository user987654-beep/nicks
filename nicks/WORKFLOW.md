# Host-independent workflow

The Python CLI owns scheduling, persistence, isolation, evidence and voting. Hosts only invoke it.
Set `NICKS_HOST=codex|claude` for attribution and `NICKS_SUBSCRIPTION_PROFILE=auto|claude-only|codex-only|both`
for authorization, or use the equivalent config.json fields. Set `NICKS_RESTRICTED=1` for subscription workers only.
Use `nicks config set subscriptions PROFILE`, `nicks config set mode hybrid|restricted`, and `nicks config show`
for persistent defaults. New jobs accept `--subscriptions` and `--mode`; existing jobs keep saved policy.
Restricted limits model destinations, not source privacy or application security. Claude Haiku development
uses tool-less JSON proposals validated/applied by Python in isolated worktrees, with normal scope and tests.
Profile exclusions apply to planning, critics, workers and judges; host choice never enables a provider.

For a complete independent judicial run use `nicks nickquisition run <job> --test-cmd "..."`.
Python runs Good's own investigation, Evil's blind attack, Neutral's blind review, the challenge,
then separate deliberations and ballots. Resume with the same command; inspect with `show` and `verdict`.
This is also available from Claude Code. The interactive Good Nick protocol below remains compatible.
Never submit Evil's or Neutral's ballot yourself. Report actual models and reduced provider/model diversity.

For automated planning use `nicks dev plan --architect --repo <repo> --request "..."`.
It uses these same instructions and the existing validator. Alternatively, the host can inspect the repository
and write a manifest with `--file`, then perform decomposition and revisions through `amend` and `revise`.
A Codex host can take the Architect role throughout, including debate, without Claude, and a Claude host without Codex.

For a single unattended native invocation, use `nicks dev deliver --repo <repo> --request-file <requirements>
--subscriptions codex-only|claude-only|both --mode restricted --max-seconds 7200`. This launches the installed
skill in one fresh eligible strong Architect host session, Codex CLI or Claude Code (`NICKS_HOST` chooses when
both are authorized; the only authorized one otherwise), using the existing private-home/private-tmp sandbox and
ordinary engine commands below. A Claude host is headless with file and shell tools only; run each command in the
foreground with a 600000 ms Bash timeout. Settings and all host/child evidence are retained under the printed `HOST`/`ENGINE_STATE` paths.
It does not merge user branches. A real final REJECT is a completed judicial outcome; a missing stage or ballot
is incomplete. The host cannot change the engine or source repository; implementation uses existing Council worker worktrees.
Delivery journals must be on durable storage: the default `~/.local/state/nicks` or a durable `NICKS_HOME`/
`XDG_STATE_HOME`. `deliver` refuses temporary locations (including symlink targets). Private `/tmp` remains scratch
space only. The repository, baseline and exact request identify one delivery; repeating it returns its recorded
result, or stops with the retained journal if execution was interrupted. It does not renew deadlines or replay
uncertain inference. Inspect the printed `ENGINE_STATE` with `NICKS_HOME=<that path> nicks dev status`, then use the
existing `dev execute`, review `resume`, or `nickquisition run` recovery commands for their own recorded stages.
Completed work, original failed attempts and valid ballots are retained; a missing response remains missing.

## User-authorized rework of a rejection

Use `NICKS_HOME=<rejected ENGINE_STATE> nicks dev rework PLAN --from REJECTED_JOB --max-seconds 3600`
to authorize **one** separately budgeted child delivery. The parent must be a concluded, intact REJECTED
judgment on a verified integration. The engine clones that exact rejected commit; it does not return to the
feature's original baseline or reset the parent's allowances. It prints the child `HOST` and `ENGINE_STATE`.
Repeating the same authorization resumes its durable identity without renewing the budget or replaying an
uncertain host call. Use the existing stage recovery commands in the printed child state after interruption.
Another cycle requires a new explicit user command naming the latest rejected child plan and job; delivery
hosts must never authorize their own next cycle.

For an everyday Codex-only delivery and rework, use this sequence (replace the example paths and IDs; for a
Claude-only delivery use `--subscriptions claude-only` throughout):

```sh
nicks dev deliver --repo /path/to/project --request-file /path/to/requirements.md --subscriptions codex-only --mode restricted --max-seconds 7200
# Use engine_state, plan and job from that command's JSON result.
NICKS_HOME=/path/to/parent/engine nicks dev status PARENT_PLAN --json
NICKS_HOME=/path/to/parent/engine nicks nickquisition show REJECTED_JOB
NICKS_HOME=/path/to/parent/engine nicks nickquisition verdict REJECTED_JOB --json
NICKS_HOME=/path/to/parent/engine nicks dev rework PARENT_PLAN --from REJECTED_JOB --subscriptions codex-only --mode restricted --max-seconds 3600
# Inspect the new child state and IDs returned by rework.
NICKS_HOME=/path/to/child/engine nicks dev status CHILD_PLAN --json
NICKS_HOME=/path/to/child/engine nicks nickquisition show DELTA_JOB
NICKS_HOME=/path/to/child/engine nicks nickquisition verdict DELTA_JOB --json
# Only after another explicit user authorization, if that child concluded REJECTED:
NICKS_HOME=/path/to/child/engine nicks dev rework CHILD_PLAN --from DELTA_JOB --subscriptions codex-only --mode restricted --max-seconds 3600
```

`show` lists ballots, original justifications and paths to deliberations; `verdict` checks the saved evidence
without new inference and prints the report path. Read that report's blocking findings before authorizing
rework. `dev deliver`/`dev rework` exit successfully for a **completed** workflow, including a legitimate
REJECT; inspect `verdict.verdict` separately from `complete_workflow`. `nickquisition verdict` returns 2 for
REJECTED. Keep every parent and child journal: the next cycle uses the latest rejected child's state and IDs,
not the original plan. Repeating the original command does not authorize a second cycle. INCOMPLETE or
integrity failure requires inspecting/recovering that existing job, not rework or an invented approval.

The Architect plans only remediation of the inherited blocker IDs. Use the ordinary Evil BET/CALL/FOLD
debate, stories, independent Council reviews, reconciliation and verified integration. Each task lists the
IDs it addresses in `rework_findings`; retain the supplied integration verification commands and earlier
acceptance obligations. Prefer test-only changes for missing-regression rejections. Explain the attempted
correction for **every ID** in the provisional arbitration, including current tests/source and unresolved dissent.

The engine selects Delta Nickquisition automatically for these child jobs. Run `nickquisition run` normally:
three fresh independent investigations, Evil challenge, deliberation and three genuine ballots. Its scope is
closure of inherited blockers and safety of the complete parent-to-current delta, not a new project audit.
Judges receive the previous verdict, current verification, exact revision identities, complete delta and local
source context; relevant surrounding snapshot source remains accessible. Unrelated pre-existing problems
belong in nonblocking followups. Scoped JSON ballots must assess every ID and attribute every blocker to an
inherited ID or actual changed code with current revision identities and supporting causal evidence. Use the
existing paired test gate when feasible. Missing/invalid scope evidence makes a ballot invalid, never an
approval. Manual ballots and incomplete-Council exceptions cannot bypass this path. Existing voting and
correction rules still apply. An approval certifies only this defined closure and delta safety.

# Council of Nicks

Two bodies, never to be confused:

- **The Council of Nicks** consists of subscription workers and eligible free/local workers. They do the review work. The `nicks`
  CLI freezes the diff, runs them blind, replaces failed ones and writes an **arbiter packet** with every
  original response.
- **The Nickquisition** is the three-member judicial body that judges the Council's work, with equal votes:
  - **Good Nick:** the host agent or a separately selected eligible agent, the constructive judge;
  - **Evil Nick:** an independently selected eligible subscription agent, the adversarial judge, who challenges the code,
    the Council and above all your conclusions;
  - **Neutral Nick:** a high-capability Claude or OpenAI model drawn at random per job, the impartial judge.

*The Council does the work. The Nickquisition judges it. Two votes decide. No single Nick rules.*

**You do not decide the outcome.** The orchestrator owns the workflow, runs Evil and Neutral Nick, records
every ballot, and computes the verdict in Python: 3-0 or 2-1 APPROVED, 1-2 or 0-3 REJECTED. A missing or
invalid ballot gives INCOMPLETE, and a confirmed new test failure gives BLOCKED. You cast exactly one ballot,
your own, and the CLI cannot cast or change anyone else's. Report the verdict as the CLI prints it; explain
it, never restate it differently.

Host and subscription policy are independent; host choice never authorizes a provider.

## Commands

| User asks | Run |
|---|---|
| `/nicks review [base]` | `nicks review [base]` (add `--uncommitted` to review working-tree changes; `--brief "..."` to pass scope/acceptance criteria) |
| `/nicks models` | `nicks models` (`--all` shows ineligible routes and why) |
| `/nicks status [job]` | `nicks status [job]` |
| `/nicks doctor` | `nicks doctor` (`--probe` sends a one-line prompt to each eligible free route) |
| `/nicks stats` | `nicks stats [--home DIR ...] [--json]`: statistics across concluded development runs (tokens, time, retries, tests, verdicts, Luna/Haiku comparisons); read-only, no model is called |
| `/nicks report [plan or job]` | `nicks report [ID]`: Markdown report of a run (models, attempts, tokens, Council reviews, The Nickquisition); no model is called. Written automatically by `dev conclude`. |
| resume a degraded/interrupted job | `nicks resume [job]` |
| `/nicks packet [job]` (re-read results later) | `nicks packet [job] --print`, then arbitrate |
| Evil Nick, blind attack / challenge | `nicks evil attack <job>` / `nicks evil challenge <job>` |
| Neutral Nick | `nicks neutral review <job> --good-model <your model id>`; also `nicks neutral status [job]`, `nicks neutral models [--probe]` |
| The Nickquisition | `nicks nickquisition open\|submit\|convene\|show\|verdict <job>` |
| `/nicks dev "<feature request>"` | the Development Engine workflow below (you plan; the Council implements; the Nickquisition judges) |
| record your decision | `nicks verdict <job> --stage provisional\|final --decision approve\|request-changes\|needs-investigation ...` |

Run `nicks …` as one plain command (no `cd`, subshells or `command -v` guards); that is what the skill's tool
permission covers. If `nicks` is not found, use the checkout's `bin/nicks` or install with `install.sh`.
Job files live under `~/.local/state/nicks/jobs/<job>/` (or `$NICKS_HOME`); open the packet, `diff.patch` and
`attempts/*/response.txt` with the **Read** tool rather than `cd`/`cat`. Running the project's own tests may
need the user's approval; if it is denied, say which claims therefore remain unverified.

On Codex, use supervised CLI execution and poll running commands for progress; keep the user informed.
On Claude Code, run `nicks review` in the **foreground** with the Bash tool and a **600000 ms timeout**, and do the whole
arbitration in this same turn. Never use `run_in_background`: this skill's tool permissions only apply while
it is being invoked, so a later turn (e.g. after a background notification) may be unable to read the packet.
The CLI caps a job at about 9 minutes by default, so a foreground call always returns in time; if it ends
`degraded`/`insufficient` because time or free capacity ran out, run `nicks resume <job>` (again in the
foreground) or report the partial coverage honestly. Progress, including every failover, goes to stderr; the
last stdout line is `PACKET: <path>`.

Before running a review:
- If no base is given, the CLI infers `origin/HEAD`, `origin/main`, `main`, ... and uses the merge-base. If the
  user has uncommitted work they clearly want reviewed, use `--uncommitted`.
- If the user stated goals or acceptance criteria, pass them with `--brief` so every Nick gets the same brief.
- Exit codes: 0 = complete or degraded (read the packet), 3 = fewer usable reviews than `min_nicks`,
  4 = no usable reviews, 1 = error. A non-zero exit still writes a packet when any attempt ran.

## Review workflow (all in this turn, all in the foreground)

**Phase A: independent investigation**
1. `nicks review …` → `PACKET: <path>`: the Council's work. By default it uses five workers: GPT-6 Luna and
   Claude Haiku (subscription workers) plus three free models, overflowing to more Luna/Haiku workers when free
   capacity is exhausted. Use `--no-subscription` only if the user asks for a free-only Council. When you report
   coverage, distinguish independent executions from distinct models; the packet's Coverage section lists both.
2. Read the packet and arbitrate (protocol below), using the Council's work and your own checks only. Record your
   **provisional assessment**, i.e. the full review in the output format below, on stdin:
   `nicks verdict <job> --stage provisional --decision <d> <<'EOF'` … `EOF`
3. **Require complete Council review and reconciliation before ordinary Nickquisition dispatch.** If a slot
   failed, use the existing bounded review resume or stop incomplete. Record your provisional assessment first.
   `nickquisition run` then performs a separate Good investigation and all three actual ballots; your assessment
   is not Good's judicial investigation or vote. An explicitly authorized exceptional judgment may use
   `--allow-incomplete-council "<reason>"`; the missing stages and reason remain in judicial evidence, never as
   review coverage. Do not use that exception merely to finish an unattended development task.

**Phase B: deliberation (one round)**
4. `nicks nickquisition open <job> --model <your own model id> [--test-cmd "<the project's test command>"]`
   → `ARTIFACT:` and `EVIDENCE:`.
   - The **orchestrator** runs Evil and Neutral Nick's Phase A itself: Evil's blind attack, Neutral's random draw
     and blind review, and Evil's challenge of your provisional assessment. You do not start them.
   - Pass the project's normal test command when there is an obvious one. It runs on the base and on the
     reviewed revision in a network-less jail; only a reproducible new failure is an objective blocker.
   - This can take several minutes.
5. For a large package, the engine creates a hashed evidence bundle. Run `nicks nickquisition run <job>`
   so each independent subscription judge reads bounded original pages and records delivery receipts. Manual
   submission cannot bypass those receipts. Resume with the same command; an explicit `nickquisition scale <job>`
   continues an intact previous INCOMPLETE round with no ballots, preserving its artifact and old results.
   For ordinary inline jobs, read the **whole** EVIDENCE file. It is exactly what Evil and Neutral Nick receive. Write your deliberation
   (any format):
   - answer Evil Nick's challenge and Neutral Nick's review on evidence;
   - acknowledge what you got wrong;
   - say what remains unresolved.

**Phase C: your ballot**
6. Cast your own vote, before the others deliberate:
   `nicks nickquisition submit <job> --artifact <ARTIFACT> --vote APPROVE|REJECT --model <your model id> --justification "<why>" <<'EOF'`
   … your deliberation … `EOF`.
   - APPROVE: accept in this form (minor follow-ups allowed).
   - REJECT: must not be accepted as is.

   Vote on the merits, not to please anyone.

**Phase D: the verdict**
7. `nicks nickquisition convene <job>`. The orchestrator runs Evil and Neutral Nick's deliberation and votes
   on the identical evidence, then prints `THE NICKQUISITION VERDICT: …` and `REPORT: <path>`. Read the report.
8. Present the result. Start with the verdict line exactly as printed, then each member's vote, any dissent
    and blockers, and what remains unresolved. You may explain the verdict but cannot change it.
    - If it says INCOMPLETE, name the missing or invalid ballot and why.
    - Bounded evidence delivery automatically retries one well-shaped acknowledgment with an incorrect page/manifest
      identifier per judge/stage. The invalid output and retry reservation are durable; accepted receipts and ballots
      are retained. Exhaustion stops explicitly. Final decisions and substantive rejections are never auto-retried.
    - `--retry-failed` remains the explicit recovery of failed *executions*, when authorized; it cannot fabricate
      acknowledgment or renew an exhausted automatic acknowledgment allowance.
    - A new deliberation needs an explicit `open --new-round`; never start one because you dislike the result.

`convene` and `verdict` exit with 0 for APPROVED, 2 for REJECTED or BLOCKED, and 3 for INCOMPLETE. A non-zero
exit here is the verdict, not a tool failure. `nicks nickquisition verdict <job>` recomputes the verdict and checks
ballot integrity at any time, with no model involved. If the user explicitly asks for a Council-only review, stop after step 2 and say clearly that
the Nickquisition did not judge it.

## Development Engine workflow (`/nicks dev "<feature request>"`)

**Architect Nick plans. The Council implements. The Nickquisition judges.** The user only describes the feature. You
write the plan; they never write JSON or start individual tasks. Council workers (Luna, Haiku) are weak, so they
succeed only on small, unambiguous, independently verifiable tasks. Your decomposition is
what makes them succeed. Use the host as Architect Nick or `dev plan --architect`; both use the same manifest and Python engine.

**Default agents (no `dev_workers` / `council_composition` configured).** Each story is implemented by one
subscription worker drawn at random: Luna or Haiku when both subscriptions are available, otherwise the available
one. The final Council review is exactly three independent blind reviewers: 2 Luna + 1 Haiku or 1 Luna + 2 Haiku
at random (order shuffled) when both are available, else three Luna or three Haiku. Free/OpenCode models are never
used here. With neither subscription available the engine stops with an error. A reviewer whose model fails is
never replaced by another model: its slot stays unfilled, The Nickquisition refuses to convene, and
`nicks resume <job>` retries the same model. Explicitly configured worker chains and compositions keep their own
behaviour (e.g. `dev_workers: ["luna", "haiku", "free"]` restores free-worker failover).

**1. Understand.** Read the repository with Read, Grep, Glob and `git log`: its layout, the modules the feature
touches, existing patterns and naming, how tests are written and run (find the exact offline command), and any
constraints.

**2. Design as the Story Architect.** Decompose into user stories, each with acceptance criteria, then each story
into worker tasks. Split by complexity and verifiability, not by line count. A task is small enough when:
- it has exactly one responsibility, stated as a one-line goal;
- it touches a few named files (its `scope`);
- a weak model could do it from the task text plus the listed `context_files`, without design decisions. Make
  those decisions yourself and write them into `context`: function names, signatures, data formats, error
  behaviour. **Specify behaviour, not code.** Workers implement whatever you write literally. Give interfaces, the inputs that must work (including valid-but-unusual ones: missing or `None` values, other
  supported formats, non-ASCII text, interaction with other features) and expected results. Only prescribe code
  when the exact form matters, and then say why;
- its acceptance criteria are concrete, with exact inputs and outputs where possible, and its `verify` commands
  check them offline, preferably with tests the task itself adds;
- `out_of_scope` names the tempting neighbouring work it must not do.

Write `verify` for the environment the engine actually runs it in:
- a clean export of the task's tree, with **no `.git`**, so `git diff`/`git status` cannot work;
- no network and a private `/tmp`;
- only the system's own tools under `/usr`.

Check results with the project's own test runner or interpreter, for example `python3 -c "..."` assertions on the file
contents, rather than shell pipelines. A verify command that cannot pass whatever the worker does wastes the
task's whole budget and gets reported as a worker failure.

Order the tasks with `depends_on`: a task depends on every task whose code it needs. Prefer a chain of small
tasks to one large one. Add `integration_verify` (the project's full offline test command) for the final check.

**3. Write and validate the plan.** Write the manifest with the Write tool to a scratch file outside the repository
(for example `/tmp/nicks-plan-<slug>.json`), with this shape:
`{"feature", "architecture", "constraints": [..], "integration_verify": [..], "stories": [{"id","title",
"narrative","acceptance":[..]}], "tasks": [{"id","story","goal","context_files":[..],"context","scope":[..],
"depends_on":[..],"acceptance":[..],"verify":[..],"out_of_scope":[..]}]}`.
Then run `nicks dev plan --repo <repo> --request "<the user's request, verbatim>" --file <manifest>`. The
orchestrator validates it and rejects missing fields, unbounded scopes, unknown or cyclic dependencies and missing
context files. Fix whatever it reports and retry. Show the user the plan summary (stories, tasks, order) and
the `PLAN_MD` path, where they can inspect or edit it.

**3b. Preflight and Plan Critic (required before execution).**
1. **Preflight.** Run `nicks dev preflight <plan>`. Every task's `verify` commands run in the real verification
   environment: on the unmodified tree, and with the task's scope removed. It reports, per command:
   - `broken`: uses git, a missing tool or a syntax error;
   - `non-discriminating`: passes either way, so it detects nothing;
   - `discriminating`.

   A task with a broken check, or with no discriminating check, is rejected; fix it in your revision. For tasks
   with prerequisites, "fails on the baseline" is expected.
2. **Critique.** Run `nicks dev critique <plan>`. An independent Plan Critic (Haiku, then Luna) reads the request,
   your manifest and the repository source, and looks for wrong assumptions, prescribed code that mishandles
   inputs, missing edge cases, bad dependencies, ambiguous criteria and checks that cannot discriminate. Read
   `critique/critique.md` in full.
3. **Verify every claim before acting on it.** Check each claim against the source, and run a tiny jailed
   reproduction if needed.
   Accept what holds, reject what doesn't, and say why.
4. **Revise, once.** Run `nicks dev revise <plan> --file <revised manifest> --response "<point by point: what you
   changed or why you rejected it>"`, or `--keep --response "..."` if nothing needs changing. There is one
   critique-and-revision cycle. The original manifest, the critique and your response are all kept.
   `nicks dev execute` refuses to start before this step is done.

**3c. Adversarial tests (required before execution when enabled).**
1. **Design.** Run `nicks dev testdesign <plan>` (foreground, 600000 ms timeout; it may take several minutes).
   Evil Nick, in a fresh jailed Codex session, writes contract tests (the workers will see them) and holdout tests
   (they will not). The engine validates each test on the unmodified tree, and the command prints how many are
   accepted, uncertain and rejected. If it reports the designer UNAVAILABLE, that is recorded, and you may go
   straight to step 4.
2. **Read the validation.** Open `adversarial/record.json` and the proposed test files. For every `uncertain`
   test, decide against the **user's request**, not your plan:
   - `accepted` only if the request (quote it) or existing behaviour justifies the expected result;
   - `rejected:<reason>` if the expectation is unsupported, contradicts the request, or tests your plan's private
     choices;
   - leave it `uncertain` if it is genuinely open: it is then frozen as evidence, but it never gates integration.

   A failing generated test can mean your plan is wrong. If it shows a real plan defect, say so in your response.
   The tests are already designed and the plan cannot be re-critiqued, so record it and let the evidence reach
   the judges.
3. **Freeze.** Run `nicks dev testfreeze <plan> --resolve ID=accepted --resolve ID=rejected:<reason> ...
   --response-file <file with your reasoning>`. Freezing is final: tests are never regenerated, and tampering is
   detected. `nicks dev execute` refuses to start until this step is done.

Never show holdout tests to workers, and never paste them into a task context or an amendment. The engine keeps
them out of the workers' jails; do not undo that by hand.

**3d. Design debate mode (experimental; `/nicks dev --debate "<request>"`).** It replaces 3b; preflight still
applies.
1. **Register.** Register the plan with `nicks dev plan --debate ...`. You are now **Architect Nick**: you own the
   plan, and you alone revise it. This is a planning role, separate from your later Good Nick vote.
2. **Start.** Run `nicks dev debate <plan> start` (foreground, 600000 ms timeout). The orchestrator:
   - preflights the plan;
   - opens one persistent Codex session for **Evil Nick** and prints its first move.

   Each side has 100 chips. Spent chips are gone: there are no rewards, refunds or scores. A claim costs 2–20 chips,
   staked by expected impact. Chips only decide how much debate an issue gets, never whether it is true.
3. **Each turn.** Write a message: your free-form argument, ending with exactly one ```` ```nicks-action ```` block.
   Then run `nicks dev debate <plan> turn --file <message.md>`. The orchestrator validates it, applies it, runs Evil
   Nick's reply and prints it.
   - **Verify before you answer.** Read the code. Check premises in the jail with
     `nicks dev debate <plan> probe --file <cmd.sh>`; never run repository code on the host.
   - **FOLD** when the concern is right. A FOLD accepts the *concern*, not the proposed solution: the solution is
     yours. Before you REVISE, ask yourself:
     - does the behaviour really need to change?
     - would a narrower change satisfy the request?
     - is there a substantially simpler implementation?
     - does the fix add invariants, special cases or data structures?
     - is its complexity proportional to the concern?

     Every REVISE must state its `approach`: what you chose, and why simpler alternatives were accepted or
     rejected. List open semantic trade-offs in `tradeoffs`; they show up in plan.md for the user. Never quietly
     weaken an explicit requirement to get a simpler plan.
   - **CALL** only when you believe it is wrong and the stake is worth it. The showdown that follows allows at most
     2 responses each, with stance `maintain`, `concede` or `agree`.
   - **Concede** when shown wrong.
   - **REVISE** with the complete manifest (`{"action": "REVISE", "manifest_file": "<abs path>", "addresses":
     [...]}`), together with your primary action. Check it first with `nicks dev debate <plan> hash --file <m>`:
     every version must pass validation and the preflight.
   - **APPROVE** the exact current hash when you consider the plan right.
   - **BET** to challenge Evil Nick's proposals when they would make the plan worse.
   - Attach a `repro` (run on the unmodified base; exit 0 = premise holds) to anything factual.
   - Never invent disagreement, and never fold just to finish.
4. **Revision re-attack.** When you both approve a plan that was revised during the debate, it is not
   consensus yet. A fresh, independent Evil Nick session (same chip balance, no memory of the earlier session)
   attacks the negotiated changes: you'll see `REVISION RE-ATTACK cycle k`. It sees the cumulative v1→current diff,
   the accepted concerns, your documented approaches, and plan-growth figures. It is asked whether any
   constraint is stronger than the request needs, and whether a materially simpler design exists. Answer its
   claims like any other; replacing a whole approach with a simpler one is a legitimate REVISE.
   There are at most 2 cycles; a revision made after the last one ends **UNRESOLVED**. Evil Nick may flag a
   verified blocking defect at stake 0 when it has no chips left.
5. **How it ends.**
   - **CONSENSUS:** both of you approved the same plan hash, and every negotiated revision survived a re-attack. Go on to 3c (if enabled) and 4.
   - **BUDGET_EXHAUSTED**, **DESIGN_BLOCKED** or **UNRESOLVED:** stop. Report both positions and the unresolved claims, and ask the
     user. Run `nicks dev debate <plan> escalate --decision proceed|stop --reason "<the user's words>"` only on the
     user's explicit instruction.
   - **INTERRUPTED** (Evil Nick failed): run `nicks dev debate <plan> resume`.

Workers receive only the agreed plan, never the transcript (`<plan>/debate/transcript.md`).

**4. Execute.** Run `nicks dev execute <plan>` (foreground, 600000 ms timeout). The orchestrator runs ready tasks in
dependency order, independent tasks with disjoint scopes concurrently (default 2 at once). Each result goes
through its integration queue before any dependant becomes ready. Give independent work disjoint `scope`s so it
can run in parallel. Each task is implemented by a Council worker in an isolated clone, on top of every
task already verified, then scope-checked and verified by the engine before anything depending on it can start.
The default is one implementation per task followed by independent reviews of the integrated change. Parallel tasks
and repair attempts are not independent competing implementations of the same story.

**Optional competing implementations:** after the same planning/debate/test-freeze gates, run
`nicks dev compete <plan> <task> --max-seconds 1800` for ONE selected story represented by one normal-sized ready task.
Two native Codex Luna executions receive the identical story and baseline in private Development Engine journals
(Claude Haiku tool-less proposals when Codex is not authorized; the engine is frozen in the competition record).
Neither can see the other proposal during implementation. A third Luna compares P/Q exports, patches and local
verification; each implementing session gets one challenge turn without the other implementer's challenge.
Haiku stages get the packet inlined and replay each earlier turn verbatim instead of resuming a session.
P/Q patches are content-only diffs; original mail patches and commit metadata stay in the private proposal journals.
Each stage receives a frozen evidence packet with its actual contributions and coverage. Challenge packets exclude
the other challenge; the reconciliation packet contains the comparison and both challenges verbatim. Ordinary blind
reviews and downstream judicial votes are labelled separately and never fabricated to populate this packet.
The third session applies the arbitration protocol below. There is no automatic winner from passing tests or a
majority count. A consequential unresolved disagreement leaves the competition incomplete. One explicitly requested
repair is allowed in a separate Luna worker attempt, then the original comparer must ratify it. Every proposal,
response, dissent and failed attempt remains available under `<plan>/competing/`.

Exactly one verified tree enters the existing durable integration queue. Resume `compete` with the same plan/task;
completed inference is not repeated and the original absolute deadline is not renewed. Interrupted model calls
remain incomplete rather than becoming decisions. Regular `execute` cannot bypass an unfinished competition.
Then run `dev integrate` and the unchanged three-judge `nickquisition run`, followed by `dev conclude`.
In this mode integration freezes the selected target with the original comparison contributions for the judges;
it does not launch three additional blind reviews or describe implementer challenges as blind reviews. The
judicial packet labels the comparison separately from ordinary review slots; all actual ballots are still required.
After a judicial rejection, the existing bounded `amend --add` and `execute` path can correct the cumulative
implementation. Required verification still precedes integration. `integrate` reuses a competing judicial job only
for the same head; a changed verified head gets a fresh job and independent investigations/ballots. Original
comparison contributions are labelled historical, and the prior verdict and deliberations enter the common
deliberation evidence as history, never as votes on the corrected revision. The rejection remains immutable.
Exit codes:
- **0**: all tasks verified; go to step 6.
- **5**: paused by its time budget; run the same command again. Resuming never repeats verified work.
- **3**: a task `needs-decomposition` after its bounded attempts. Go to step 5. Independent tasks have already
  run; tasks depending on it are waiting.
- **4**: no worker capacity right now; report it and stop.
- **6**: blocked; a task failed at the decomposition limit. Report it; it needs the user.

`nicks dev status <plan>` shows every task, its dependencies, attempts, worker, model, commit and the next action.

A task the preflight rejects at dispatch, a verification command that cannot run, and repeated worker timeouts
also return the task to you as `needs-decomposition`, with the cause in its reason. None of these charges the
worker with an implementation failure. Fix the check, or split the task, with `nicks dev amend`.

**5. Decompose a failed task.** Read its `result.md` (`nicks dev show <plan> <task>`): the workers' messages and the
engine's verification output. Work out *why* it was too hard, then write smaller replacement tasks that keep the
same overall acceptance. Apply them with:
`nicks dev amend <plan> --replace <task> --reason "<why it failed and how the split fixes that>" --file <tasks.json>`
where the file is `{"tasks": [...]}`. Tasks that depended on the replaced task automatically depend on all of
its replacements; verified work is untouched; the replaced task never runs again. Then continue with step 4.
Splitting has a depth limit and a per-plan budget. If the orchestrator refuses, stop and report: the plan
needs the user, not more replanning.
Amendments retain earlier acceptance/verification obligations from the same story, with their task and revision
provenance. Preserve meaningful coverage of those behaviors across corrections. Test names and implementations
may change; explain replacement coverage by current test location. The ordinary review packet includes the
correction delta as well as the cumulative change, so a required test removed during repair remains inspectable.
A green suite is not evidence that every earlier case is still covered. Review the mapping in the existing
arbitration stage; no additional reviewer or approval mechanism is introduced.

**Diagnostic Council review of an inspectable failure:** `nicks dev review <plan> <task> --diagnostic` freezes the
latest verification-failed patch and exact logs in a private evidence clone, then uses the ordinary independent
Council review engine. It is explicitly UNVERIFIED; canonical state and task verification status do not change.
Missing inference, empty patches and scope violations are not eligible. Repeating the command resumes its saved
review job rather than creating duplicate opinions. Inspect all responses and preserve minority findings.

If the evidence supports a correction, Architect Nick supplies one normal-sized replacement task and an explicit
`request-changes` assessment. Apply it with `dev amend <plan> --replace <task> --diagnostic-job <job> --file <tasks.json>
--response-file <assessment.md>`. This uses the existing amendment/decomposition budgets and requires every requested
independent review. It retains the original scope, dependencies, acceptance criteria, exclusions and verification
commands. The new worker receives the failed patch, failures and reviews as evidence and starts from the verified
canonical baseline. Ordinary verification and integration gates must pass; failed tests never become approvals.
`approve`, ambiguous responses and unresolved investigation cannot authorize this correction. A diagnostic job
cannot receive approval or enter Nickquisition. Once corrected and verified, run the normal final review and
Nickquisition on a new job; the historical failures and dissent accompany that final review. No valid correction
means stop with the original candidate and evidence intact.

**6. Integrate and judge.** Run `nicks dev integrate <plan>`. The engine runs `integration_verify` on the
cumulative change against the plan's original base.
- If it fails: add a fix task with `nicks dev amend <plan> --add --reason ... --file ...`, then step 4 again.
- If it passes: it runs the final Council review of the complete change (three blind subscription reviewers by
  default, see above) and prints `FINAL_REVIEW_JOB`.

With adversarial tests frozen, `integrate` also runs the contract suite (an accepted contract failure keeps
integration red: fix it with an `amend --add` task, as above) and the holdout suite, whose first run is recorded once.
The evidence file shows every holdout result. A holdout failure is evidence, not a verdict: in your arbitration, judge
whether the request or existing behaviour justifies the test's expectation, and reproduce it before relying on it.

Generated Python helpers are preserved with the tests. Shared helpers are visible to implementers; holdout-only
helpers belong under `holdout/`. Importing the project or a networking module does not itself violate policy;
verification still runs in a network-isolated jail. If no usable tests survive the bounded generation passes,
the engine records the optional stage as unavailable and permits ordinary project verification. This supplies
no generated-test coverage. A freeze with zero accepted tests also discloses that limitation. Invalid resolution
IDs are rejected with saved evidence and leave the stage unchanged; never guess an ID or accept a rejected test
to get past this guard. A restart can replay the identical saved freeze after a filesystem/database interruption.

Then arbitrate every review, record the provisional decision with `nicks verdict`, and run
`nicks nickquisition run <job> --test-cmd "<integration_verify>"` for fresh independent Good/Evil/Neutral judging.
The existing manual host-judge open/submit/convene interface remains available for explicitly chosen interactive use.
Completed reconciliation does not require unanimous agreement or an `approve` decision. An explicit
`request-changes`/`needs-investigation` assessment may refer its unresolved findings to the independent judiciary;
this is the existing adjudication path, not consensus approval or a bypass of missing reviews. State why correction
is not proceeding (for example, the existing correction allowance is exhausted) and preserve the disagreement.
The common judicial evidence labels that unresolved referral. It grants no extra correction allowance and never
converts missing/invalid reconciliation, failed mandatory verification or an unverified diagnostic patch into approval.

**Exit code 7 (AWAITING_JUDGMENT):** The Nickquisition cannot convene (the subscription gate refuses, e.g. no subscription login). Report that the
implementation is complete, integrated and preserved and that judgment is pending, including the blockers it
printed. Never call the plan approved, and never re-run workers. When `nicks billing` shows the providers
eligible, run the Nickquisition on the printed job and then `nicks dev conclude`.

**7. Conclude.** Run `nicks dev conclude <plan>`, which records the verdict. Only APPROVED makes the plan
`complete`. REJECTED, BLOCKED or INCOMPLETE never do.
- Report the verdict verbatim, the stories and tasks with their workers, models and commits, and where the result
  lives: branch `nicks/<plan>/cumulative` in the plan's private state clone. The status output gives the fetch
  command.
- Never merge into the user's branches unless they ask.

The engine, not you, enforces isolation, scope, verification, retry budgets, dependency order and the billing
gate. Never edit the private clones yourself to make a task pass.

## Arbitration protocol

Read the whole packet (`Read` the `PACKET:` path). It contains the review target, coverage, the manifest,
the full attempt and failover history, and each Nick's response verbatim between
`<<<NICK X BEGIN …>>>` / `<<<NICK X END …>>>` markers. If the responses are not inline (large runs), read
every listed `response.txt`. Do not skip any Nick.

1. **Understand the change yourself first.** Read the diff (`diff.patch`) and the relevant source in the
   user's repository. Know what the change is meant to do before weighing anyone's opinion.
2. **Read every usable Nick in full, in whatever format it used.** Markdown, plain text, JSON, broken JSON
   and mixed prose are all equally valid. Never discount or skip a response because of its format, length or
   tone. Judge content only.
3. **Identify the distinct claims.** Group findings that assert *materially the same defect*: the same root
   cause and the same failure. Do not merge distinct bugs just because they mention the same file, function or
   concept. Do not split one bug just because Nicks described it differently.
4. **Count agreement honestly.** For each claim, note which Nicks raised it independently. Check the
   manifest: Nicks of the same model family (flagged in Coverage) are not fully independent, so say so.
   Agreement raises confidence but is not proof.
5. **Verify against evidence.** For every claim that matters, check the actual code: the line, the execution
   path, the types and constraints. Run the repo's tests or a tiny reproduction when it is cheap and safe to
   do so. Confident language is not evidence. One well-supported finding outweighs any number of unsupported
   approvals, and you may reject a unanimous claim that the code disproves. A reproduction must establish a
   requirement violation, not merely demonstrate behavior the reviewer dislikes. Compare the expected result
   against the public request, frozen acceptance criteria or established behavior before accepting it.
   Cite the exact candidate commit/tree (or patch hash for an uncommitted candidate). Test results on an older
   revision remain historical evidence, not current failures or current verification. A suggested reproduction
   is not an executed reproduction. Missing logs and a reviewer's inability to run tests are evidence limitations,
   not by themselves functional defects or passing verification.
6. **Keep minority findings.** A severe, credible claim raised by a single Nick must be investigated, not
   dropped. A Nick reporting "no issues" is a valid opinion, but it does not cancel another Nick's evidence.
7. **Label each claim:** `verified` (you confirmed it in code/tests), `plausible` (credible, not confirmed),
   `rejected` (state why; cite the code), or `needs-recheck`. For each consequential claim, include a readable
   evidence record in Markdown, prose or JSON: original author/dissent; target revision; exact contract quote or
   established-behavior basis; source location and execution path or actual reproduction command plus expected
   and observed results; disposition and its reason. Mark evidence you could not obtain explicitly. Do not
   fabricate citations or fill missing evidence with reviewer agreement.
   Separately label `required-functional`, `required-regression/documentation`, or `optional-improvement`.
   A required regression omission needs the exact frozen acceptance case and the tests inspected to establish
   its absence. Additional coverage, documentation and style preferences without a contract/defect basis are
   optional suggestions, not new acceptance criteria. Disproven claims stay in the rejected-claims record with
   the rebuttal; genuine unresolved consequential disagreements stay visible and can block delivery.
8. **Targeted follow-up (optional).** If a consequential claim can't be settled, you may run a narrower
   review, e.g. `nicks review ... --brief "Check only whether X can happen in Y"` or `-n 1`. Report it as a
   follow-up check, not as an additional blind opinion on the whole diff.
9. **Report coverage truthfully.** State requested vs. usable reviewers, model families, failovers, and any
   degraded coverage. Two usable reviews never count as three. A provider outage is not a vote.
10. **Decide.** One of: **Approve** (no actionable blockers within scope; disclose coverage limits),
    **Request changes** (actionable defects adequately supported), or **Needs investigation** (consequential
    claims unresolved or coverage inadequate). Link each requested correction to its supporting evidence record.
    Missing evidence or an unverified consequential claim calls for investigation, not a fabricated verified
    defect. Optional suggestions alone do not require changes. No claim is automatically promoted or dismissed
    by its label, and no decision overrides failed mandatory verification or an exhausted correction budget.
    "No consensus" is an acceptable state; never manufacture one.

## Final output format

Keep it concise and PR-ready:

```
## Council of Nicks review: <decision> (provisional assessment, before the Nickquisition)

**Coverage:** <usable>/<requested> Nicks · <families> model families · Evil Nick: <attack/challenge status> · Neutral Nick: <provider/actual model or not run> · job <id>

### Findings
1. **<title>** (<severity>), `<file:line>`. **verified** / plausible / needs-recheck
   Raised by: Nick A, C (independently); dissent: <position and author>
   Target: <commit/tree or patch hash>; kind: <required-functional / required-regression/documentation / optional-improvement>
   Contract: <exact requirement or established-behavior basis>
   Evidence: <source path/reasoning, or executed command with expected and actual results; missing evidence if any>
   Disposition: <why this blocks, remains unresolved, or is optional>; correction: <evidence-backed change, if required>

### Rejected claims
- <claim> (Nick B): rejected because <code-based reason>

### Agreement summary
<where Nicks agreed, where they disagreed, which findings are single-Nick, any same-family caveats>

```

This format is for your provisional assessment (step 2). Your final answer to the user leads with the
Nickquisition verdict (step 10). Put the decision and the verified blockers first. Don't paste raw Nick output into the final answer; cite
Nicks by letter. The packet path stays available for anyone who wants the originals.

Reconciliation interfaces accept an explicit `{"decision":"approve"}` object (bare, including a trailing multiline
object, or in a `json`/`nicks-consensus` fence), the review heading above, a `Decision: approve` line, or the standalone
decision. The other decision values are `request-changes` and `needs-investigation`. No fence is mandatory. Missing,
qualified, duplicate JSON fields, or conflicting declarations fail safely; reviewer counts and positive sentiment
are never interpreted as approval. Full response text, dissent and parse errors are retained in separate
`reconciliation/response-*/` records. This parser only decodes the reconciler's explicit decision; the arbitration
rules above still determine its substance.
The complete bold sentence `**Decision: request-changes.**` (also `needs-investigation`) can be followed by a
same-line rationale. `**Decision: approve.**` is accepted on its own line; inline approval qualifications remain
rejected. Parsing an explicit request for changes does not verify its claims, grant another repair, or approve it.

`nicks verdict --stage provisional --file <assessment>` can decode that explicit decision when `--decision` is omitted; the existing
explicit `--decision` form remains supported. These are provisional/final host assessments, not judicial votes.
The automated `nickquisition run` path still requires its own independent Good investigation and all three actual
ballots. Reconciliation evidence must never be inserted as an automated Good investigation or judicial ballot.

## Ground rules
- Free models plus the user's existing subscriptions only. The subscriptions' extra usage / workspace credits are
  authorized (so runs may incur incremental charges up to the organization's limits); separately billed API-key
  inference is not, and neither is any model not included in the subscription (Claude Fable is permanently
  excluded; never select or suggest it). Never configure paid providers or API keys to "get more Nicks". Subscription-backed runs
  (Luna, Haiku, Evil and Neutral Nick) pass a pre-inference gate that refuses anything but a subscription
  login; if it refuses, report that to the user verbatim and never try to bypass it. When reporting results, say
  when subscription credits may have been consumed (`nicks billing`, "billed to EXTRA USAGE" notes); never claim
  zero incremental charges. If Codex says the login is revoked, tell the user to run `codex login --device-auth`.
- Reviewers only ever see a read-only snapshot with secret-like files removed. Don't paste secrets into `--brief`.
- Do not push, merge, or modify the user's code as part of `/nicks review` unless the user asks you to fix
  the findings afterwards.

Profile authorization is separate from current availability; changes apply to new jobs, while resume restores
a job's pinned policy. Haiku development proposals are credential-safe JSON
validated and applied by Python in a private worktree.
