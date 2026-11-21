"""Acceptance Check Preflight (V1D.1): validate a task's verification commands in the real verification environment
BEFORE any worker inference is spent.

V1D Round 1 showed checks that could never pass (`git diff ... | awk ...`: no .git in the exported tree, and `awk` was a
dangling Debian alternatives symlink) and checks that could never fail (`test -z "$(git diff ...)"` prints nothing, so it
passes). A worker must never be blamed for those. For every verify command of a task the preflight gathers evidence:

1. static: does it need git metadata (verification runs on an export without .git)? is a pipeline's exit status the
   last stage's (no pipefail)?
2. baseline: run it on the tree the task would be built on (unmodified), network-less, exactly like verification:
   missing executables / broken symlinks (exit 126/127, "not found"), git metadata, shell syntax errors;
3. negative control: run it on the same tree with every path in the task's permitted scope REMOVED. A command that
   passes both on the baseline and without the task's files cannot detect anything about the task's work.

Per command: `broken` (cannot run here), `discriminating` (fails on the baseline, i.e. checks new behaviour, or fails
without the task's files, i.e. depends on them), `non-discriminating` (passes either way: a legitimate regression or
static check, but no evidence about the task), `uncertain` (timed out / could not be established). The TASK is rejected
only on demonstrated problems: a broken command, or no discriminating command at all. Everything else is recorded,
never claimed as validated beyond what was observed.
"""
from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

from .sandbox import wrap

GIT_USE = re.compile(r"(^|[;&|(`]|\$\(|\s)git\s+\w")
NOT_FOUND = re.compile(r"(^|\n)(/bin/)?(ba|da)?sh: (\d+: |line \d+: )?([^:\n]+): (command )?not found")
SYNTAX = re.compile(r"(^|\n)(/bin/)?(ba|da)?sh: (\d+: |line \d+: )?Syntax error", re.I)


def _export(gitdir: Path, rev: str, dest: Path) -> None:
    dest.mkdir(parents=True)
    arch = subprocess.run(["git", f"--git-dir={gitdir}", "archive", rev], capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=arch.stdout, check=True)


def _ablate(tree: Path, scope: list[str]) -> list[str]:
    """Remove every path the task may change; returns what was removed (relative)."""
    removed = []
    for f in sorted(tree.rglob("*")):
        if not f.exists() or f.is_dir():
            continue
        rel = f.relative_to(tree).as_posix()
        if any(fnmatch.fnmatch(rel, pat) or rel.startswith(pat.rstrip("/") + "/") for pat in scope):
            f.unlink()
            removed.append(rel)
    return removed


def _run(tree: Path, home: Path, cmd: str, timeout: float) -> dict:
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1", "PWD": str(tree), "TMPDIR": "/tmp"}
    argv = wrap(["/bin/sh", "-c", cmd], snapshot=tree, home=home, binary="/bin/sh", network=False, writable=True)
    t0 = time.time()
    try:
        p = subprocess.run(argv, cwd=str(tree), env=env, capture_output=True, timeout=timeout)
        rc, out = p.returncode, (p.stdout + p.stderr).decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        rc, out = None, f"timed out after {timeout:.0f}s"
    return {"exit_code": rc, "seconds": round(time.time() - t0, 2), "output_tail": out[-2000:]}


def static_findings(cmd: str) -> tuple[list[str], list[str]]:
    """(broken reasons, warnings) derivable from the command text alone."""
    broken, warn = [], []
    if GIT_USE.search(cmd):
        broken.append("uses git; verification runs on an exported tree without .git metadata")
    stripped = cmd.replace("||", "")
    if "|" in stripped and "pipefail" not in cmd:
        warn.append("pipeline without `set -o pipefail`: its exit status is the LAST stage's; failures of earlier "
                    "stages are ignored")
    if re.search(r"\btest\s+-z\s+\"?\$\(|\[\s+-z\s+\"?\$\(", cmd):
        warn.append("`test -z \"$(...)\"` passes whenever the inner command prints nothing on stdout, including when "
                    "it fails (errors go to stderr)")
    return broken, warn


def runtime_broken(r: dict) -> str | None:
    out = r["output_tail"]
    if "not a git repository" in out.lower():
        return "needs git metadata (not a git repository in the verification tree)"
    m = NOT_FOUND.search(out)
    if m:
        return f"executable not found or a broken symlink: `{m.group(5).strip()}`"
    if SYNTAX.search(out):
        return "shell syntax error"
    if r["exit_code"] in (126, 127):
        return f"exit {r['exit_code']}: a command is not executable or not found"
    return None


def preflight_task(gitdir: Path, rev: str, spec: dict, timeout: float, out_dir: Path | None = None) -> dict:
    """Evidence for one task's verify commands on revision `rev` (the head it would be dispatched on)."""
    results = []
    with tempfile.TemporaryDirectory(prefix="nicks-preflight-") as td:
        base, abl, home = Path(td) / "base", Path(td) / "ablated", Path(td) / "home"
        _export(gitdir, rev, base)
        _export(gitdir, rev, abl)
        home.mkdir()
        removed = _ablate(abl, spec["scope"])
        for cmd in spec["verify"]:
            broken, warn = static_findings(cmd)
            b = _run(base, home, cmd, timeout)
            why = runtime_broken(b)
            if why and why not in broken:
                broken.append(why)
            a = _run(abl, home, cmd, timeout) if not broken else None
            if broken:
                verdict = "broken"
            elif b["exit_code"] is None or (a and a["exit_code"] is None):
                verdict = "uncertain"
                warn.append("timed out during preflight; discriminating power not established")
            elif b["exit_code"] != 0:
                verdict = "discriminating"            # fails before the task: it checks behaviour the task must add
            elif a and a["exit_code"] != 0:
                verdict = "discriminating"            # passes now but fails without the task's files: depends on them
            else:
                verdict = "non-discriminating"
                warn.append("passes on the unmodified tree AND with every file in the task's scope removed: it cannot "
                            "detect anything about the task's work (fine as a regression/static check, not as evidence)")
            results.append({"command": cmd, "verdict": verdict, "broken": broken, "warnings": warn,
                            "baseline": b, "without_scope": a})
    task_reasons = [f"`{r['command'][:160]}`: {'; '.join(r['broken'])}" for r in results if r["verdict"] == "broken"]
    if results and not any(r["verdict"] in ("discriminating", "uncertain") for r in results):
        task_reasons.append("no verification command can detect incorrect behaviour: every command passes on the "
                            "unmodified tree and without the task's files")
    out = {"rev": rev, "status": "rejected" if task_reasons else "ok", "reasons": task_reasons,
           "removed_for_negative_control": removed, "commands": results, "at": time.time()}
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"preflight-{rev[:12]}.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def summary(pf: dict) -> str:
    counts: dict[str, int] = {}
    for r in pf["commands"]:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    return (f"{pf['status'].upper()}: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
            + (" | " + " | ".join(pf["reasons"]) if pf["reasons"] else ""))

