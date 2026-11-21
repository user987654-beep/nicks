#!/usr/bin/env bash
# Install (or uninstall) the `nicks` CLI and the Claude Code and Codex skills as symlinks into this checkout.
#   ./install.sh            install
#   ./install.sh uninstall  remove the symlinks (only if they point into this checkout)
# Env overrides: NICKS_BIN_DIR (default ~/.local/bin), CLAUDE_SKILLS_DIR (default ~/.claude/skills)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${NICKS_BIN_DIR:-$HOME/.local/bin}"
SKILL_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
CODEX_SKILL_DIR="${CODEX_SKILLS_DIR:-$HOME/.agents/skills}"

link() {  # link <target> <linkpath>
  if [ -e "$2" ] && [ ! -L "$2" ]; then
    echo "refusing to replace non-symlink $2" >&2; exit 1
  fi
  ln -sfn "$1" "$2"
  echo "linked $2 -> $1"
}

unlink_if_ours() {
  if [ -L "$1" ] && [[ "$(readlink "$1")" == "$ROOT"/* ]]; then rm "$1" && echo "removed $1"; fi
}

case "${1:-install}" in
  install)
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
      || { echo "Python >= 3.10 is required" >&2; exit 1; }
    mkdir -p "$BIN_DIR" "$SKILL_DIR" "$CODEX_SKILL_DIR"
    link "$ROOT/bin/nicks" "$BIN_DIR/nicks"
    link "$ROOT/skill/nicks" "$SKILL_DIR/nicks"
    link "$ROOT/skill/codex-nicks" "$CODEX_SKILL_DIR/nicks"
    command -v opencode >/dev/null || [ -x "$HOME/.opencode/bin/opencode" ] \
      || echo "warning: OpenCode not found; install it from https://opencode.ai or set NICKS_OPENCODE_BIN" >&2
    case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "note: add $BIN_DIR to PATH" >&2 ;; esac
    echo "Done. Run nicks (or nicks init) for automatic setup, then nicks doctor and nicks models."
    echo "Anonymous free providers and available local servers are discovered automatically; no provider account is required."
    ;;
  uninstall)
    unlink_if_ours "$BIN_DIR/nicks"
    unlink_if_ours "$SKILL_DIR/nicks"
    unlink_if_ours "$CODEX_SKILL_DIR/nicks"
    ;;
  *) echo "usage: $0 [install|uninstall]" >&2; exit 2 ;;
esac
