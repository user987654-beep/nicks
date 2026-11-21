"""Freeze the review target: exact base/head, the diff, and a read-only source snapshot.

The user's working tree, index and refs are never modified. Reviewing uncommitted work
uses a temporary index file (GIT_INDEX_FILE) to synthesise a tree object; this only adds
loose objects to the object store, which is harmless and garbage-collectable.

Secret-looking files are excluded from both the diff and the snapshot, and well-known
credential patterns are redacted from whatever text is sent to workers.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


class SnapshotError(RuntimeError):
    pass


class NothingToReview(SnapshotError):
    pass


SECRET_GLOBS = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".npmrc", ".pypirc", ".netrc",
    ".git-credentials", ".htpasswd", "credentials", "credentials.json", "*credentials*.json",
    "service-account*.json", "secrets.*", "*.secret", "*.secrets", "*.tfstate", "*.tfstate.*", "*.tfvars",
]
SECRET_ALLOW = {".env.example", ".env.sample", ".env.template", ".env.dist"}

REDACTIONS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S)),
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("sk-api-key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
]


def is_secret_path(path: str) -> bool:
    name = path.rstrip("/").rsplit("/", 1)[-1]
    if name in SECRET_ALLOW:
        return False
    return any(fnmatch.fnmatch(name, g) for g in SECRET_GLOBS)


def redact(text: str) -> tuple[str, dict]:
    counts: dict[str, int] = {}
    for kind, rx in REDACTIONS:
        text, n = rx.subn(f"[REDACTED-BY-NICKS:{kind}]", text)
        if n:
            counts[kind] = counts.get(kind, 0) + n
    return text, counts


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git(args: list[str], cwd: str | Path, env: dict | None = None, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True)
    if check and proc.returncode != 0:
        raise SnapshotError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout.decode("utf-8", errors="replace")


def _resolve_commit(root: str, ref: str) -> str | None:
    proc = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=root, capture_output=True)
    return proc.stdout.decode().strip() if proc.returncode == 0 else None


def default_base(root: str, head_commit: str | None) -> str:
    candidates = []
    sym = subprocess.run(["git", "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], cwd=root, capture_output=True)
    if sym.returncode == 0 and sym.stdout.strip():
        candidates.append(sym.stdout.decode().strip())
    candidates += ["origin/main", "origin/master", "main", "master"]
    for c in candidates:
        sha = _resolve_commit(root, c)
        if sha and sha != head_commit:
            return c
    raise SnapshotError("could not infer a base branch different from HEAD; pass --base <ref> (or --uncommitted)")


def worktree_tree(root: str) -> str:
    """Tree object for the current working tree (tracked + untracked, honouring .gitignore)."""
    with tempfile.TemporaryDirectory(prefix="nicks-index-") as td:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(td) / "index")}
        if _resolve_commit(root, "HEAD"):
            git(["read-tree", "HEAD"], root, env)
        git(["add", "-A", "--", "."], root, env)
        return git(["write-tree"], root, env).strip()


def _exclude_pathspecs() -> list[str]:
    specs = [f":(glob,exclude)**/{g}" for g in SECRET_GLOBS]
    return ["--", ".", *specs]


def _numstat(root: str, base: str, tree: str) -> tuple[int, int, int, list[str]]:
    out = git(["diff", "--numstat", "--no-renames", base, tree, *_exclude_pathspecs()], root)
    files, adds, dels, names = 0, 0, 0, []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        files += 1
        names.append(parts[2])
        adds += int(parts[0]) if parts[0].isdigit() else 0
        dels += int(parts[1]) if parts[1].isdigit() else 0
    return files, adds, dels, names


def _extract_tree(root: str, tree: str, dest: Path) -> tuple[list[str], int]:
    dest.mkdir(parents=True, exist_ok=True)
    excluded, count = [], 0
    proc = subprocess.Popen(["git", "archive", "--format=tar", tree], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    has_filter = hasattr(tarfile, "data_filter")
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
            for member in tf:
                if is_secret_path(member.name):
                    excluded.append(member.name)
                    continue
                try:
                    if has_filter:
                        tf.extract(member, dest, filter="data")
                    elif not (member.issym() or member.islnk()):
                        tf.extract(member, dest)
                    else:
                        excluded.append(member.name + " (link, unsupported on this Python)")
                        continue
                except (tarfile.TarError, OSError) as exc:
                    excluded.append(f"{member.name} (not extracted: {exc})")
                    continue
                if member.isfile():
                    count += 1
    finally:
        proc.stdout.close()
        err = proc.stderr.read().decode(errors="replace")
        proc.stderr.close()
        if proc.wait() != 0:
            raise SnapshotError(f"git archive failed: {err.strip()}")
    return excluded, count


def _redact_tree(dest: Path, limit: int = 2_000_000) -> dict:
    counts: dict[str, int] = {}
    for p in dest.rglob("*"):
        if not p.is_file() or p.is_symlink() or p.stat().st_size > limit:
            continue
        data = p.read_bytes()
        if b"\0" in data[:8192]:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        new, c = redact(text)
        if c:
            p.write_text(new, encoding="utf-8")
            for k, v in c.items():
                counts[k] = counts.get(k, 0) + v
    return counts


def tree_digest(dest: Path) -> str:
    """Content digest of a directory tree (paths, file bytes, symlink targets)."""
    h = hashlib.sha256()
    for p in sorted(dest.rglob("*"), key=lambda x: x.as_posix()):
        rel = p.relative_to(dest).as_posix()
        if p.is_symlink():
            h.update(f"L {rel} -> {os.readlink(p)}\n".encode())
        elif p.is_file():
            h.update(f"F {rel} {sha256_file(p)}\n".encode())
        elif p.is_dir():
            h.update(f"D {rel}\n".encode())
    return h.hexdigest()


def make_readonly(dest: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(dest, topdown=False):
        for f in filenames:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                execute_bits = stat.S_IMODE(os.stat(p).st_mode) & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                os.chmod(p, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH | execute_bits)
        os.chmod(dirpath, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)


def make_writable(dest: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(dest):
        os.chmod(dirpath, stat.S_IRWXU)
        for f in filenames:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)


def capture(
    repo: str,
    job_dir: Path,
    *,
    base: str | None = None,
    head: str | None = None,
    uncommitted: bool = False,
    diff_file: str | None = None,
    merge_base: bool = True,
    max_diff_bytes: int = 400_000,
) -> dict:
    try:
        root = git(["rev-parse", "--show-toplevel"], repo).strip()
    except SnapshotError as exc:
        raise SnapshotError(f"{repo} is not inside a Git repository") from exc
    warnings: list[str] = []
    head_commit = _resolve_commit(root, head or "HEAD")
    if head and not head_commit:
        raise SnapshotError(f"cannot resolve head ref {head!r}")
    if uncommitted and head:
        raise SnapshotError("--uncommitted reviews the working tree; it cannot be combined with --head")

    base_ref, base_sha, base_commit, base_mode = base, None, None, None
    if diff_file:
        head_kind = "patch"
        raw = sys.stdin.buffer.read() if diff_file == "-" else Path(diff_file).read_bytes()
        diff_text = raw.decode("utf-8", errors="replace")
        tree = git(["rev-parse", f"{head_commit}^{{tree}}"], root).strip() if head_commit else None
        files = sum(1 for l in diff_text.splitlines() if l.startswith("diff --git ")) or diff_text.count("\n+++ ")
        adds = sum(1 for l in diff_text.splitlines() if l.startswith("+") and not l.startswith("+++"))
        dels = sum(1 for l in diff_text.splitlines() if l.startswith("-") and not l.startswith("---"))
        names = re.findall(r"^\+\+\+ b/(.+)$", diff_text, re.M)
        excluded_from_diff = [n for n in names if is_secret_path(n)]
        if excluded_from_diff:
            raise SnapshotError(f"supplied patch touches secret-like files {excluded_from_diff}; remove them first")
        warnings.append("patch supplied directly; the snapshot is the repository at HEAD and may not contain the patch applied")
    else:
        if uncommitted:
            head_kind = "worktree"
            tree = worktree_tree(root)
            base_ref = base or "HEAD"
        else:
            head_kind = "commit"
            if not head_commit:
                raise SnapshotError("repository has no commits; use --uncommitted")
            tree = git(["rev-parse", f"{head_commit}^{{tree}}"], root).strip()
            base_ref = base or default_base(root, head_commit)
        base_commit = _resolve_commit(root, base_ref)
        if not base_commit:
            raise SnapshotError(f"cannot resolve base ref {base_ref!r}")
        base_sha, base_mode = base_commit, "direct"
        if merge_base and head_commit:
            mb = git(["merge-base", base_commit, head_commit], root, check=False).strip()
            if mb:
                base_sha, base_mode = mb, "merge-base"
            else:
                warnings.append("no merge-base between base and head; diffing directly against base")
        diff_text = git(["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--find-renames", base_sha, tree,
                         *_exclude_pathspecs()], root)
        files, adds, dels, names = _numstat(root, base_sha, tree)
        all_changed = git(["diff", "--name-only", "--no-renames", base_sha, tree], root).splitlines()
        excluded_from_diff = [n for n in all_changed if is_secret_path(n)]
        if head_kind == "commit" and (head or "HEAD") == "HEAD":
            dirty = git(["status", "--porcelain", "--untracked-files=no"], root).strip()
            if dirty:
                warnings.append("working tree has uncommitted changes that are NOT part of this review (use --uncommitted to include them)")

    if not diff_text.strip():
        raise NothingToReview("the diff is empty; nothing to review")
    diff_text, diff_redactions = redact(diff_text)
    diff_bytes = diff_text.encode("utf-8")
    if len(diff_bytes) > max_diff_bytes:
        raise SnapshotError(
            f"diff is {len(diff_bytes)} bytes (> max_diff_bytes={max_diff_bytes}); narrow the review range for now"
        )
    job_dir.mkdir(parents=True, exist_ok=True)
    diff_path = job_dir / "diff.patch"
    diff_path.write_bytes(diff_bytes)

    snap_dir = job_dir / "snapshot"
    excluded_snapshot, nfiles, snap_redactions = [], 0, {}
    snap_dir.mkdir(parents=True, exist_ok=True)
    if tree:
        excluded_snapshot, nfiles = _extract_tree(root, tree, snap_dir)
        snap_redactions = _redact_tree(snap_dir)
    digest = tree_digest(snap_dir)
    make_readonly(snap_dir)

    return {
        "repo": root,
        "head_kind": head_kind,
        "head_ref": head or ("WORKTREE" if head_kind == "worktree" else "HEAD"),
        "head_commit": head_commit,
        "head_tree": tree,
        "base_ref": base_ref,
        "base_commit": base_commit,
        "base_sha": base_sha,
        "base_mode": base_mode,
        "diff_path": str(diff_path),
        "diff_sha256": sha256_bytes(diff_bytes),
        "diff_bytes": len(diff_bytes),
        "files_changed": files,
        "insertions": adds,
        "deletions": dels,
        "changed_files": names[:500],
        "excluded_from_diff": excluded_from_diff,
        "excluded_from_snapshot": excluded_snapshot,
        "redactions": {"diff": diff_redactions, "snapshot": snap_redactions},
        "snapshot_dir": str(snap_dir),
        "snapshot_files": nfiles,
        "snapshot_digest": digest,
        "warnings": warnings,
    }
