"""Is a successful model invocation actually a usable review?

Deterministic and deliberately narrow: this is NOT a semantic judgement of the review and imposes no schema.
A review with no findings ("No actionable issues.") or in any format (Markdown, plain text, JSON) is usable.
Only two structural failure modes observed in real runs are rejected:

1. **Hallucinated tool calls only.** Tool-less reviewers emitted tool-call markup (`<invoke name="Read">`,
   `<invoke/read>`, `<function_calls>`, ...) instead of a review. The markup is removed (only for this check; the
   original output is always preserved verbatim) and what remains must contain an actual statement.
2. **Only requests for inaccessible material.** The remaining text is short and its only statements are
   "let me read X" / "please provide Y" sentences (fragments under three words do not count as statements).

Anything else, including long, odd or partly degenerate output that still contains review text, is usable; the
judges weigh it, the engine does not.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Whole tool-call blocks first (non-greedy, across lines), then any stray opening/closing tags.
_BLOCKS = [
    re.compile(r"<(?:antml:)?function_calls\b.*?</(?:antml:)?function_calls\s*>", re.S | re.I),
    re.compile(r"<(?:antml:)?invoke\b[^>]*>.*?</(?:antml:)?invoke\s*>", re.S | re.I),
    re.compile(r"<(?:antml:)?(?:tool_use|tool_call|tool_result|function_results)\b.*?</(?:antml:)?"
               r"(?:tool_use|tool_call|tool_result|function_results)\s*>", re.S | re.I),
]
_TAGS = re.compile(r"</?(?:antml:)?(?:invoke|parameter|function_calls|function_results|tool_use|tool_call|tool_result)"
                   r"(?:/[\w.-]+)?\b[^>]*>", re.I)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'_-]*")
# A sentence that only announces an action the reviewer cannot take, or asks for material it was not given.
_REQUEST = re.compile(
    r"^(?:(?:ok(?:ay)?|now|first|next|then|so|great|sure)[,:]?\s+)*"
    r"(?:let me|let's|i(?:'ll| will| need to| would need to| have to| must| should| am going to|'m going to| want to)"
    r"|please (?:provide|share|send|paste|give|attach)|(?:could|can|would) you (?:please )?(?:provide|share|send|paste|give)"
    r"|i (?:cannot|can't|can not|could not|couldn't) (?:see|access|read|open|view|find)"
    r"|i (?:don't|do not) have (?:access|the (?:file|source|content))"
    r"|(?:i'll|i will) (?:start|begin) by)\b", re.I)
MIN_WORDS = 3
REQUEST_ONLY_MAX_CHARS = 600


@dataclass
class Assessment:
    usable: bool
    reason: str | None
    tool_markup_blocks: int
    residual_words: int

    def note(self) -> str | None:
        if self.usable and self.tool_markup_blocks:
            return (f"output contains {self.tool_markup_blocks} hallucinated tool-call block(s) (no tools were "
                    "available); the review text around them was used")
        return None


def strip_tool_markup(text: str) -> tuple[str, int]:
    n = 0
    for rx in _BLOCKS:
        text, k = rx.subn(" ", text)
        n += k
    text, k = _TAGS.subn(" ", text)
    return text, n + k


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?:])\s+|\n+", text)
    return [p.strip(" \t-*#>`") for p in parts if _WORD.search(p or "")]


def assess(text: str) -> Assessment:
    residual, blocks = strip_tool_markup(text or "")
    words = _WORD.findall(residual)
    if not (text or "").strip():
        return Assessment(False, "empty output", 0, 0)
    if blocks and len(words) < MIN_WORDS:
        return Assessment(False, f"only hallucinated tool calls ({blocks} block(s)), no review text", blocks, len(words))
    sents = _sentences(residual)
    requests = [x for x in sents if _REQUEST.match(x)]
    # Statements of >= MIN_WORDS words that are not requests; shorter debris (e.g. "odeexpected") is ignored.
    statements = [x for x in sents if not _REQUEST.match(x) and len(_WORD.findall(x)) >= MIN_WORDS]
    if requests and not statements and len(residual.strip()) <= REQUEST_ONLY_MAX_CHARS:
        return Assessment(False, "only requests to inspect material the reviewer cannot access"
                          + (f" (plus {blocks} hallucinated tool-call block(s))" if blocks else ""), blocks, len(words))
    return Assessment(True, None, blocks, len(words))
