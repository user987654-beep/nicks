"""Arbiter packet: everything Claude needs to arbitrate, with no semantic processing.

The packet reports objective facts only (what ran, on which route, what came back, what
failed and why) and embeds every usable Nick response *verbatim*, delimited by boundary
markers that cannot collide with the content. It never merges, ranks, parses or summarises
findings; that is the arbiter's job.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path

from .config import Settings
from .review import job_dir


def _ts(t: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "-"


def _label(slot: int) -> str:
    return chr(ord("A") + slot) if slot < 26 else f"S{slot}"


def _cell(s) -> str:
    return str(s if s is not None else "-").replace("|", "\\|").replace("\n", " ")


def write_packet(db, job_id: str, settings: Settings) -> Path:
    job = db.get_job(job_id)
    snap, summ, params = job["snapshot"], job.get("summary") or {}, job["params"]
    attempts = db.attempts(job_id)
    slots = {s["slot"]: s for s in db.slots(job_id)}
    usable = sorted((a for a in attempts if a["status"] == "ok"), key=lambda a: a["slot"])
    d = job_dir(job_id)

    responses = []
    total = 0
    for a in usable:
        p = Path(a["dir"]) / "response.txt"
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        total += len(text.encode("utf-8"))
        responses.append((a, p, text))
    inline = total <= settings.inline_limit_bytes
    boundary = secrets.token_hex(4)
    while any(boundary in t for _, _, t in responses):
        boundary = secrets.token_hex(4)

    fam_counts: dict[str, list[str]] = {}
    for a in usable:
        if a["diversity_key"] not in ("", "unknown"):
            fam_counts.setdefault(a["diversity_key"], []).append(_label(a["slot"]))
    dup_fams = {k: v for k, v in fam_counts.items() if len(v) > 1}

    L: list[str] = []
    w = L.append
    w(f"# Council of Nicks: arbiter packet `{job_id}`\n")
    w(f"Job status: **{job['status']}** · created {_ts(job['created_at'])} · packet written {_ts(time.time())}\n")
    ep = params.get("execution_policy") or {}
    w("## Effective execution policy\n")
    w(f"- Host: {ep.get('host', 'unknown')} · subscriptions: {ep.get('subscription_profile', 'unknown')} · mode: {'restricted' if ep.get('restricted') else 'hybrid'}")
    w("- Diversity measures distinct model families; subscription authorization does not imply independent providers.\n")
    w("## Review target\n")
    w(f"- Repository: `{snap['repo']}`")
    if snap.get("base_sha"):
        w(f"- Base: `{snap['base_ref']}` → `{snap['base_sha']}` ({snap['base_mode']})")
    if snap["head_kind"] == "worktree":
        w(f"- Head: **uncommitted working tree** (synthesised tree `{snap['head_tree']}`, on top of `{snap['head_commit']}`)")
    elif snap["head_kind"] == "patch":
        w(f"- Head: supplied patch; snapshot = repository at `{snap['head_commit']}`")
    else:
        w(f"- Head: `{snap['head_ref']}` → `{snap['head_commit']}`")
    w(f"- Diff: `{snap['diff_path']}` ({snap['files_changed']} files, +{snap['insertions']} −{snap['deletions']}, "
      f"{snap['diff_bytes']} bytes, sha256 `{snap['diff_sha256'][:16]}…`)")
    w(f"- Snapshot shown to Nicks (read-only): `{snap['snapshot_dir']}` ({snap['snapshot_files']} files; "
      f"integrity after run: **{summ.get('snapshot_integrity', 'unknown')}**)")
    w(f"- Identical prompt to every Nick: `{d / 'prompt.txt'}` (sha256 `{snap.get('prompt_sha256', '')[:16]}…`)")
    if params.get("brief"):
        w(f"- Architect brief given to Nicks: {params['brief']}")
    excl = snap.get("excluded_from_diff", []) + snap.get("excluded_from_snapshot", [])
    if excl:
        w(f"- Withheld from Nicks as secret-like: {', '.join(f'`{e}`' for e in sorted(set(excl))[:30])}")
    red = {k: v for part in snap.get("redactions", {}).values() for k, v in part.items()}
    if red:
        w(f"- Credential patterns redacted before sending: {red}")
    for warn in snap.get("warnings", []):
        w(f"- ⚠ {warn}")
    w("")

    if params.get('development_verification'):
        evidence = params['development_verification']
        if evidence['reviewed_head'] != snap['head_commit']:
            raise ValueError('development verification packet targets a different revision')
        integration = evidence.get('integration') or {}
        if (integration.get('relation') == 'current' and integration.get('tree')
                and integration['tree'] != snap['head_tree']):
            raise ValueError('development verification tree differs from reviewed snapshot')
        w('## Engine verification records (complete, revision-bound)\n')
        w('Current means the exact reviewed commit; historical failures remain evidence of their own revisions. '
          'Passing checks are not an approval. Missing evidence supplies no verification. '
          'Full logs below are embedded so no access to host log paths is required.\n')
        w(json.dumps(evidence, indent=2, ensure_ascii=False))
        w('')

    w("## Coverage\n")
    if params.get('competing_summary'):
        from .competing import contributions
        w(params['competing_summary'])
        w('Ordinary blind review slots below are not used for this competing-implementation job.\n')
        for label, source, raw in contributions(job):
            w(f"### Original competing contribution: {label} ({source['actual_model']})\n")
            w(raw)
            w('')
    req = len(slots)
    w(f"- Requested independent Nicks: **{req}** · usable responses: **{len(usable)}** · "
      f"distinct model families among them: **{len(fam_counts)}**")
    if len(usable) < req:
        unfilled = [f"{_label(s)} ({v.get('note') or v['status']})" for s, v in slots.items() if v["status"] != "done"]
        w(f"- ⚠ **Degraded coverage**: only {len(usable)} of {req} requested reviews were obtained. "
          f"Unfilled slots: {', '.join(unfilled)}. Missing reviews are not votes either way.")
    models: dict[str, list[str]] = {}
    for a in usable:
        if a["diversity_key"] == "unknown":
            continue
        identity = a.get("actual_model") or a["route"].split("/", 1)[-1]
        identity = identity.rsplit("/", 1)[-1].lower().removesuffix(":free").removesuffix("-free")
        models.setdefault(identity, []).append(_label(a["slot"]))
    w(f"- Independent executions: **{len(usable)}** · distinct models: **{len(models)}** ("
      + "; ".join(f"`{m}` x{len(v)}" for m, v in sorted(models.items())) + ")" if usable else
      "- Independent executions: **0**")
    subs = [a for a in usable if a["basis"] == "subscription"]
    if "free_quota_pools" in summ:
        pools = summ["free_quota_pools"]
        w(f"- Live free cloud quota pools in this job: **{len(pools)}**: " + ", ".join(f"`{p}`" for p in pools))
        w(f"- Undisclosed underlying identities: **{summ.get('unknown_identities', 0)}** (not counted as diversity).")
    if subs:
        w(f"- Subscription workers (Council members, no vote): {', '.join(_label(a['slot']) for a in subs)}; "
          f"free/local workers: {len(usable) - len(subs)}")
    if dup_fams:
        dups = "; ".join(k + ": " + ", ".join(v) for k, v in dup_fams.items())
        w(f"- ⚠ Same model family used by more than one Nick (not independent diversity): {dups}")
    w(f"- Attempts: {len(attempts)} total, {summ.get('failed_attempts', 0)} failed "
      f"({summ.get('injected_faults', 0)} deliberately injected) · reported inference cost: {summ.get('total_cost', 0)}")
    w("")

    w("## Manifest of usable Nicks\n")
    w("| Nick | Route (provider/model) | Actual model | Family (reported) | Basis | Duration | Tokens in/out | Tool calls | Response | sha256 |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for a, p, text in responses:
        dur = (a["finished_at"] or 0) - a["started_at"]
        w(f"| {_label(a['slot'])} | `{a['route']}` | {_cell(a.get('actual_model') or ('unknown' if a['diversity_key'] == 'unknown' else '(as routed)'))} | "
          f"{_cell(a['family'] or 'undisclosed')} | {a['basis']} | {dur:.0f}s | "
          f"{a['tokens_in']}/{a['tokens_out']} | {a['tool_calls']} | `{p}` ({a['response_bytes']} B) | `{(a['response_sha256'] or '')[:12]}` |")
    w("")

    w("## Attempt history (every route tried, including failovers)\n")
    w("| # | Slot | Route | Outcome | Class | Detail | Router action | Duration |")
    w("|---|---|---|---|---|---|---|---|")
    for i, a in enumerate(attempts, 1):
        dur = f"{(a['finished_at'] or a['started_at']) - a['started_at']:.0f}s" if a["finished_at"] else "-"
        outcome = a["status"] + (" (INJECTED)" if a["injected"] else "")
        w(f"| {i} | {_label(a['slot'])} | `{a['route']}` | {outcome} | {_cell(a['error_class'])} | "
          f"{_cell((a['error'] or '')[:180])} | {_cell((a['action'] or '')[:200])} | {dur} |")
    w("")

    w("## Original Nick responses\n")
    w("Each response is reproduced **verbatim** from the worker. Formats differ by Nick and carry no weight: "
      "judge substance and evidence only. Nothing below has been merged, filtered, or reworded.\n")
    if not responses:
        w("_No usable responses were obtained._\n")
    for a, p, text in responses:
        lab = _label(a["slot"])
        if inline:
            w(f"<<<NICK {lab} BEGIN {boundary}>>> route={a['route']} family={a['family'] or 'undisclosed'} attempt={a['id']}")
            w(text)
            w(f"<<<NICK {lab} END {boundary}>>>\n")
        else:
            w(f"- Nick {lab} (`{a['route']}`): read `{p}` ({a['response_bytes']} bytes); omitted inline because the "
              f"total ({total} B) exceeds inline_limit_bytes={settings.inline_limit_bytes}.")
    failed_with_text = [a for a in attempts if a["status"] != "ok" and (a["response_bytes"] or 0) > 0]
    if failed_with_text:
        w("\nPartial output from failed or interrupted attempts (not counted as reviews; kept for diagnosis):")
        for a in failed_with_text:
            w(f"- `{Path(a['dir']) / 'response.txt'}` ({a['route']}, {a['error_class']})")
    w("")

    attack_runs = db.adversary_runs(job_id, "attack")
    attack_ok = db.latest_ok_run(job_id, "attack")
    if attack_runs:
        w("## Evil Nick, Stage A: blind attack (adversary, not a free Nick)\n")
        if attack_ok:
            atext = (Path(attack_ok["dir"]) / "response.txt").read_text(encoding="utf-8")
            ab = secrets.token_hex(4)
            while ab in atext:
                ab = secrets.token_hex(4)
            w(f"Run `{attack_ok['id']}` · {attack_ok['engine']} · raw events `{Path(attack_ok['dir']) / 'events.jsonl'}`. "
              "Written without seeing the free Nicks or Good Nick. Verbatim, unedited; not counted in coverage.\n")
            w(f"<<<EVIL NICK ATTACK BEGIN {ab}>>>")
            w(atext.rstrip("\n"))
            w(f"<<<EVIL NICK ATTACK END {ab}>>>\n")
        else:
            last = attack_runs[-1]
            w(f"Evil Nick's blind attack did not succeed ({last['status']}, {last['error_class']}: {last['error']}).\n")

    neutral_runs = db.adversary_runs(job_id, "review", member="neutral")
    if neutral_runs:
        w("## Neutral Nick: independent blind review (Nickquisition member, not a Council Nick)\n")
        sels = db.neutral_selections(job_id)
        for sel in sels:
            w(f"- Draw #{sel['id']} ({sel['reason']}): `{sel['provider']}/{sel['model']}` via {sel['random_source']}"
              + (f"; replaced run `{sel['replaces_run']}` after {sel['failure']}" if sel["replaces_run"] else "")
              + (f"; {sel['notes']}" if sel["notes"] else ""))
        nok = [r for r in neutral_runs if r["status"] == "ok"]
        if nok:
            r = nok[-1]
            ntext = (Path(r["dir"]) / "response.txt").read_text(encoding="utf-8")
            nb = secrets.token_hex(4)
            while nb in ntext:
                nb = secrets.token_hex(4)
            w(f"\nRun `{r['id']}` · {r['engine']} · actual model **{r['model']}** · raw events "
              f"`{Path(r['dir']) / 'events.jsonl'}`. Written blind (diff + snapshot only). Verbatim, unedited; "
              "not counted in coverage.\n")
            w(f"<<<NEUTRAL NICK BEGIN {nb}>>>")
            w(ntext.rstrip("\n"))
            w(f"<<<NEUTRAL NICK END {nb}>>>\n")
        else:
            last = neutral_runs[-1]
            w(f"\nNeutral Nick has no successful review ({last['status']}, {last['error_class']}: {last['error']}).\n")

    w("## Arbiter reminder\n")
    w("You (Claude) are the arbiter. Follow the arbitration protocol in the `/nicks` skill: verify claims against the "
      "diff and source, group materially equivalent findings yourself, keep minority findings, distinguish agreement "
      "from validity, and report coverage honestly. The engine has made **no** semantic judgement. Record a provisional "
      "decision with `nicks verdict`, let Evil Nick challenge it (`nicks evil challenge`), then answer every objection "
      "in the final decision.")

    md = d / "packet.md"
    md.write_text("\n".join(L) + "\n", encoding="utf-8")

    manifest = {
        "job_id": job_id, "status": job["status"], "snapshot": snap, "summary": summ, "params": params,
        "packet_sha256": hashlib.sha256(md.read_bytes()).hexdigest(),
        "responses_inline": inline,
        "nicks": [
            {"label": _label(a["slot"]), "attempt_id": a["id"], "route": a["route"], "provider": a["provider"],
             "family": a["family"], "diversity_key": a["diversity_key"], "basis": a["basis"],
             "response_path": str(p), "response_sha256": a["response_sha256"], "raw_events_path": str(Path(a["dir"]) / "events.jsonl"),
             "raw_sha256": a["raw_sha256"], "tokens_in": a["tokens_in"], "tokens_out": a["tokens_out"], "cost": a["cost"]}
            for a, p, _ in responses
        ],
        "attempts": attempts,
        "execution_policy": ep,
        "adversary_runs": db.adversary_runs(job_id),
        "neutral_selections": db.neutral_selections(job_id),
    }
    (d / "packet.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return md


def reconciliation_input(job: dict) -> str:
    """Deliver the existing packet and full frozen diff to a snapshot-only arbiter."""
    from .snapshot import sha256_bytes, tree_digest
    snap = job['snapshot']
    if tree_digest(Path(snap['snapshot_dir'])) != snap['snapshot_digest']:
        raise ValueError('reconciliation snapshot changed after review')
    diff = Path(snap['diff_path']).read_bytes()
    if sha256_bytes(diff) != snap['diff_sha256']:
        raise ValueError('reconciliation diff changed after review')
    directory = job_dir(job['id'])
    packet = (directory / 'packet.md').read_text(encoding='utf-8')
    # Large packets list original responses by path; those paths are not mounted
    # into the arbiter's jail. Expand only omitted originals, without summarizing.
    manifest = json.loads((directory / 'packet.json').read_text(encoding='utf-8'))
    if manifest.get('packet_sha256'):
        if sha256_bytes(packet.encode('utf-8')) != manifest['packet_sha256']:
            raise ValueError('reconciliation packet changed after review')
    else:
        packet += '\nEvidence limitation: legacy packet has no recorded delivery digest.\n'
    if manifest['snapshot'] != snap or manifest['params'] != job['params']:
        raise ValueError('reconciliation packet does not match the review job')
    for nick in manifest['nicks']:
        raw = Path(nick['response_path']).read_bytes()
        if sha256_bytes(raw) != nick['response_sha256']:
            raise ValueError('reconciliation response changed after review')
        text = raw.decode('utf-8')
        if not manifest.get('responses_inline', text in packet):
            packet += '\nOriginal Nick ' + nick['label'] + ' response (complete):\n' + text + '\n'
    identity = {k: snap[k] for k in ('head_commit', 'head_tree', 'snapshot_digest', 'diff_sha256')}
    return (packet + '\nExact current revision identity:\n' + json.dumps(identity, indent=2)
            + '\nComplete frozen diff (same filtering as the blind reviews):\n' + diff.decode('utf-8'))
