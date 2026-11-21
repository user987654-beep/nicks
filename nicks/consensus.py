"""Decode explicit reconciliation decisions, without voting or adjudicating findings.

The assessment remains evidence in its entirety. This interface accepts the workflow's
Markdown heading, a standalone decision, a Decision: line, or a JSON decision object
(bare or fenced). It does not infer agreement from sentiment or reviewer counts.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tempfile

from .evil import DECISIONS


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate consensus field: {key}")
        result[key] = value
    return result


def parse_response(text: str) -> dict:
    """Require an explicit, unqualified decision; conflicting declarations fail closed."""
    declarations, representations = [], []
    body = text.strip()
    fences = list(re.finditer(r"^```(?:nicks-consensus|json)?[ \t]*\n(.*?)\n```[ \t]*$",
                              body, re.M | re.S))
    # Quoted/code examples must not become control declarations.
    if fences:
        if len(fences) != 1:
            raise ValueError("expected exactly one consensus control block")
        value = json.loads(fences[0][1], object_pairs_hook=_object)
        body = body[:fences[0].start()] + body[fences[0].end():]
        representations.append("fenced-json")
    else:
        # A bare object must occupy the whole response or finish the assessment.
        starts = list(re.finditer(r"^\{", body, re.M))
        if starts:
            if len(starts) != 1:
                raise ValueError("multiple consensus objects")
            candidate = body[starts[0].start():]
            value = json.loads(candidate, object_pairs_hook=_object)
            body = body[:-len(candidate)]
            representations.append("json")
        else:
            value = None
    if representations:
        if not isinstance(value, dict) or value.get("decision") not in DECISIONS:
            raise ValueError("consensus JSON needs an explicit supported decision")
        if value["decision"] == "approve" and (value.get("unresolved") or value.get("unresolved_findings")):
            raise ValueError("approval conflicts with declared unresolved findings")
        declarations.append(value["decision"])
    # Only author-level declarations count. Do not inspect blockquotes, reviewer
    # findings, or arbitrary prose for the word 'approve'. Preserve them verbatim.
    body = re.sub(r"^```[^\n]*\n.*?\n```[^\n]*$", "", body, flags=re.M | re.S)
    for line in body.splitlines():
        # A complete bold decision sentence may precede its rationale on the
        # same line. Historical run_length used exactly this non-approval form.
        # Do not turn inline, potentially qualified approval prose into approval.
        sentence = re.fullmatch(
            r"(?:#{1,6} )?\*\*(?:Final decision|Decision):\s*"
            r"(approve|request[ -]changes|needs[ -]investigation)\.\*\*(?:[ \t]+(.+))?",
            line.strip(), re.I)
        if sentence:
            decision = sentence[1].lower().replace(' ', '-')
            if sentence[2] and (decision == 'approve' or re.search(
                    r"\b(?:Final decision|Decision):", sentence[2], re.I)):
                raise ValueError("qualified or conflicting inline consensus declaration")
            declarations.append(decision)
            representations.append("markdown")
            continue
        match = re.fullmatch(
            r"(?:#{1,6} )?Council of Nicks review: (approve|request[ -]changes|needs[ -]investigation)"
            r"(?: \(provisional assessment, before the Nickquisition\))?", line.strip(), re.I)
        if not match:
            match = re.fullmatch(r"(?:#{1,6} )?(?:\*\*)?(?:Final decision|Decision)(?:\*\*)?:\s*"
                                 r"(?:\*\*)?(approve|request[ -]changes|needs[ -]investigation)(?:\*\*)?\.?",
                                 line.strip(), re.I)
        if match:
            declarations.append(match[1].lower().replace(' ', '-'))
            representations.append("markdown")
        elif re.match(r"^(?:#{1,6} )?(?:Council of Nicks review|(?:\*\*)?(?:Final decision|Decision)(?:\*\*)?):",
                      line.strip(), re.I):
            raise ValueError("qualified or unsupported consensus declaration")
    if body.lower().rstrip(".").replace(' ', '-') in DECISIONS:
        declarations.append(body.lower().rstrip(".").replace(' ', '-'))
        representations.append("plain-text")
    elif body.lower().rstrip(".") == "i approve":
        declarations.append("approve")
        representations.append("plain-text")
    if not declarations or len(set(declarations)) != 1:
        raise ValueError("missing, ambiguous or conflicting consensus decision")
    return {"decision": declarations[0], "representations": representations}


def record_response(directory: Path, text: str) -> dict:
    """Keep even invalid outputs before parsing; retries get separate immutable records.

    This is reconciliation evidence, never a Good/Evil/Neutral judicial ballot.
    """
    directory.mkdir(parents=True, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix="response-", dir=directory))
    raw = text.encode("utf-8")
    (attempt / "response.txt").write_bytes(raw)
    record = {"response": str(attempt / "response.txt"), "sha256": hashlib.sha256(raw).hexdigest()}
    try:
        record.update(parse_response(text), status="parsed")
    except ValueError as exc:
        record.update(status="invalid", error=str(exc))
        raise
    finally:
        (attempt / "decision.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record
