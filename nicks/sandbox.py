"""Filesystem isolation for workers using bubblewrap (bwrap).

Each worker runs in fresh user/mount/pid/ipc/uts namespaces whose filesystem is built from an
allowlist, so anything not listed simply does not exist for the worker:

* read-only: /usr (+ the /bin, /lib, /sbin links), a handful of /etc files needed for DNS/TLS,
  the OpenCode executable itself, and the review snapshot (also the working directory);
* read-write: a private, empty, per-attempt directory mounted over $HOME (OpenCode's own
  db/cache/logs go there and are deleted afterwards) and a private tmpfs /tmp;
* network is shared (models are remote or on a loopback port).

The user's home directory, repositories, SSH keys, Claude/OpenCode configuration, the Nicks
state directory and other reviewers' outputs are not mounted. This is enforced by the kernel,
independently of OpenCode's own permission rules, which remain as a second layer.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class SandboxUnavailable(RuntimeError):
    pass


ETC_ENTRIES = ("resolv.conf", "hosts", "nsswitch.conf", "host.conf", "gai.conf", "passwd", "group",
               "localtime", "ssl", "ca-certificates", "pki", "crypto-policies",
               # Debian/Ubuntu: awk, which, cc, vi, pager, ... are /usr/bin -> /etc/alternatives -> /usr/... symlinks;
               # without this read-only directory of symlinks they are "not found" in every jail (found by V1D).
               "alternatives")


def bwrap_path() -> str | None:
    return shutil.which("bwrap")


def wrap(cmd: list[str], *, snapshot: Path, home: Path, binary: str,
         extra_ro: tuple | list = (), extra_rw: tuple | list = (), network: bool = True,
         writable: bool = False, ro_overlays: tuple | list = ()) -> list[str]:
    """Reviewers get `snapshot` read-only (the default). Implementation workers (nicks/dev.py) pass
    writable=True for their own disposable worktree, and `ro_overlays` re-mounts paths inside or next to it
    read-only on top (its `.git` pointer file and private git directory), so the worktree is the only
    writable host location besides the private, discarded $HOME."""
    bw = bwrap_path()
    if not bw:
        raise SandboxUnavailable("bubblewrap (bwrap) is not installed")
    args = [bw, "--unshare-all", *(["--share-net"] if network else []), "--die-with-parent", "--new-session",
            "--ro-bind", "/usr", "/usr"]
    for d in ("/bin", "/sbin", "/lib", "/lib32", "/lib64"):
        if os.path.islink(d):
            args += ["--symlink", os.readlink(d), d]
        elif os.path.isdir(d):
            args += ["--ro-bind", d, d]
    for name in ETC_ENTRIES:
        src = os.path.realpath(f"/etc/{name}")  # e.g. resolv.conf -> /run/systemd/resolve/...
        if os.path.exists(src):
            args += ["--ro-bind", src, f"/etc/{name}"]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    # Order matters: later mounts are placed on top of earlier ones.
    args += ["--bind", str(home), str(Path.home())]
    real_bin = os.path.realpath(binary)          # only the executable itself, not its directory
    args += ["--ro-bind", real_bin, real_bin]
    for p in extra_ro:
        args += ["--ro-bind", str(p), str(p)]
    for p in extra_rw:
        args += ["--bind", str(p), str(p)]
    args += ["--bind" if writable else "--ro-bind", str(snapshot), str(snapshot)]
    for p in ro_overlays:
        args += ["--ro-bind", str(p), str(p)]
    args += ["--chdir", str(snapshot), "--"]
    return args + list(cmd)


def self_test(binary: str, canary_dir: Path) -> tuple[bool, str]:
    """Run a shell inside the exact worker sandbox and check what it can and cannot touch."""
    if not bwrap_path():
        return False, "bwrap not installed"
    with tempfile.TemporaryDirectory(prefix="nicks-sbx-", dir=canary_dir) as td:
        root = Path(td)
        snap, home = root / "snapshot", root / "home"
        snap.mkdir()
        home.mkdir()
        (snap / "inside.txt").write_text("inside\n")
        canary = root / "canary.txt"           # outside the snapshot, under the real $HOME
        canary.write_text("secret\n")
        user_cfg = Path.home() / ".config"
        script = (
            f'cat inside.txt >/dev/null || exit 11; '
            f'test -e "{canary}" && exit 12; '
            f'test -e "{user_cfg}" && exit 13; '
            f'(echo x > inside.txt) 2>/dev/null && exit 14; '
            f'(echo x > new.txt) 2>/dev/null && exit 15; '
            f'exit 0'
        )
        args = wrap(["/bin/sh", "-c", script], snapshot=snap, home=home, binary=binary)
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"bwrap failed to start: {exc}"
        reasons = {11: "snapshot not readable", 12: "file outside the snapshot is visible",
                   13: "user's ~/.config is visible", 14: "snapshot is writable", 15: "snapshot dir is writable"}
        if p.returncode == 0:
            return True, "bwrap: snapshot readable and read-only; $HOME and other host paths invisible"
        return False, reasons.get(p.returncode, f"bwrap exited {p.returncode}: {p.stderr.strip()[:200]}")
