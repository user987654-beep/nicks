"""Deterministic, bounded source context for reviewers that cannot read files themselves.

Tool-less reviewers (Claude Code runs with no tools: Haiku Council workers, Neutral Nick on Anthropic models)
only see what the prompt contains. Given just the diff and the changed files, they asked for (and, primed by a
tool-oriented system prompt, hallucinated calls to read) the unchanged modules the change depends on. This
module selects those files from the FROZEN snapshot by plain, explainable rules; no model is involved, and the
same snapshot always yields the same selection.

Tiers (in order; within a tier by path):
1. referenced by the change: the diff or the changed files reference the file's module (imports, includes,
   requires, qualified calls such as `wordstats.top_words`);
2. uses the changed code: the file references a changed file's module (callers and their tests);
3. tests of referenced modules: test files that reference a tier-1 module;

A bare module name counts as a reference only on an import/include/require-like line or in qualified access
(`wordstats.top_words`, `mod::f`); a path or dotted module path (`nicks/keyproxy`, `nicks.keyproxy`) counts
anywhere. Plain prose mentions do not.
4. the rest of a small repository: only if ALL remaining text files fit in the remaining budget.

Whole files only, within a per-file cap and a total budget; everything relevant that did not fit is listed by
name with the reason. The snapshot's secret filtering applies unchanged (excluded files are not in it).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
GENERIC = {"__init__", "index", "main", "setup", "conftest", "readme", "license", "changelog", "makefile", "test",
           "tests", "init", "mod", "lib", "src", "app", "package", "config", "settings"}
TEST_NAME = re.compile(r"(^|/)(tests?/|test_[^/]*$|[^/]*_test\.[^/]+$|[^/]*\.(spec|test)\.[^/]+$)")
SCAN_LIMIT = 256_000      # bytes read from a candidate when looking for references


def _text(p: Path, limit: int | None = None) -> str | None:
    try:
        data = p.read_bytes() if limit is None else p.open("rb").read(limit)
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def _stem(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    return name.split(".", 1)[0] if not name.startswith(".") else name


def _module_keys(rel: str) -> set[str]:
    """Names by which other code may refer to `rel`: its stem, and its path without extension (a/b/c, a.b.c)."""
    stem = _stem(rel)
    keys = set()
    if len(stem) >= 3 and stem.lower() not in GENERIC:
        keys.add(stem)
    noext = rel.rsplit(".", 1)[0] if "." in rel.rsplit("/", 1)[-1] else rel
    if "/" in noext:
        keys.add(noext)
        keys.add(noext.replace("/", "."))
    return keys


_IMPORT_LINE = re.compile(
    r"^[ \t]*(?:import\b|from\s+\S+\s+import\b|#\s*include\b|include\b|use\b|using\b|mod\b|require\b|load\b|source\b"
    r"|export\s.*\bfrom\b).*$|^.*\b(?:require|import)\s*\(.*$", re.M)
DOC_EXT = (".md", ".rst", ".txt", ".adoc")
_QUALIFIED = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)(?:\.|::|->)[A-Za-z_]")   # no spaces: not sentence ends


def _refs(text: str) -> set[str]:
    """Identifiers used as module references: on import/include/require-like lines, or qualified (`m.attr`)."""
    out = set()
    for line in _IMPORT_LINE.findall(text):
        out.update(_IDENT.findall(line))
    out.update(_QUALIFIED.findall(text))
    return out


def _mentions(text: str, keys: set[str], refs: set[str]) -> bool:
    """Bare module names count only as references (imports, qualified access); paths and dotted module paths
    count anywhere in the text."""
    return any((k in refs) if "/" not in k and "." not in k else (k in text) for k in keys)


def package(snapshot_dir: Path, changed: list[str], diff_text: str, budget: int, per_file: int) -> dict:
    root = Path(snapshot_dir)
    changed_set = set(changed)
    files = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and not p.is_symlink())
    candidates = [f for f in files if f not in changed_set]
    changed_text = diff_text + "\n" + "\n".join(t for t in (_text(root / c) for c in changed if (root / c).is_file()) if t)
    changed_refs = _refs(changed_text)
    changed_keys = set().union(*[_module_keys(c) for c in changed]) if changed else set()

    texts: dict[str, str] = {}
    tiers: dict[str, tuple[int, str]] = {}
    for f in candidates:
        t = _text(root / f, SCAN_LIMIT)
        if t is None:
            continue
        texts[f] = t
        keys = _module_keys(f)
        if keys and _mentions(changed_text, keys, changed_refs):
            tiers[f] = (1, "referenced by the change")
    tier1_keys = set().union(*[_module_keys(f) for f, (t, _) in tiers.items() if t == 1]) if tiers else set()
    for f, t in texts.items():
        if f in tiers:
            continue
        refs = _refs(t)
        if changed_keys and _mentions(t, changed_keys, refs):
            tiers[f] = (2, "uses the changed code")
        elif TEST_NAME.search(f) and tier1_keys and _mentions(t, tier1_keys, refs):
            tiers[f] = (3, "tests a referenced module")

    included, omitted, used = [], [], 0
    for f in sorted(tiers, key=lambda f: (tiers[f][0], f.lower().endswith(DOC_EXT), f)):   # code before docs
        size = (root / f).stat().st_size
        tier, why = tiers[f]
        if size > per_file:
            omitted.append({"path": f, "bytes": size, "tier": tier, "reason": f"{why}; larger than the {per_file}-byte per-file cap"})
        elif used + size > budget:
            omitted.append({"path": f, "bytes": size, "tier": tier, "reason": f"{why}; context budget exhausted"})
        else:
            used += size
            included.append({"path": f, "bytes": size, "tier": tier, "reason": why})
    rest = [f for f in texts if f not in tiers]
    rest_bytes = sum((root / f).stat().st_size for f in rest)
    small_repo = bool(rest) and used + rest_bytes <= budget and all((root / f).stat().st_size <= per_file for f in rest)
    if small_repo:
        for f in rest:
            size = (root / f).stat().st_size
            used += size
            included.append({"path": f, "bytes": size, "tier": 4, "reason": "rest of a small repository"})
    shown = {i["path"] for i in included} | {o["path"] for o in omitted}
    others = [f for f in candidates if f not in shown]
    return {"rules": "context.py tiers 1-4", "budget": budget, "per_file": per_file, "used": used,
            "included": included, "omitted": omitted, "others": others}


def render(pkg: dict, root: Path, boundary: str) -> str:
    if not pkg["included"] and not pkg["omitted"]:
        others = pkg["others"]
        return (f"\nOther files in the snapshot (names only, not provided): {', '.join(others[:300]) or '(none)'}\n"
                if others else "")
    parts = [f"--- {i['path']} ({i['reason']}) ---\n{(Path(root) / i['path']).read_text(encoding='utf-8', errors='replace')}"
             for i in pkg["included"]]
    if pkg["omitted"]:
        parts.append("Related but not included: " + "; ".join(f"{o['path']} ({o['reason']})" for o in pkg["omitted"]))
    body = "\n\n".join(parts)
    tail = (f"\nOther files in the snapshot (names only, not provided): {', '.join(pkg['others'][:300])}\n"
            if pkg["others"] else "")
    return (f"\n=== RELATED UNCHANGED FILES AT THE REVIEWED REVISION (selected by the engine) {boundary} START ===\n"
            f"{body}\n=== RELATED UNCHANGED FILES {boundary} END ===\n{tail}")


def for_job(snapshot: dict, diff_text: str, budget: int, per_file: int) -> dict:
    """The job's context selection, computed once and stored next to the snapshot (job_dir/context.json), so the
    Council prompt, Neutral Nick's prompt and the Nickquisition evidence all use the identical selection."""
    snap_dir = Path(snapshot.get("snapshot_dir") or "")
    store = snap_dir.parent / "context.json"
    if store.exists():
        return json.loads(store.read_text())
    if not snap_dir.is_dir():
        return {"included": [], "omitted": [], "others": [], "used": 0}
    pkg = package(snap_dir, list(snapshot.get("changed_files", [])), diff_text, budget, per_file)
    try:
        store.write_text(json.dumps(pkg, indent=2))
    except OSError:
        pass
    return pkg
