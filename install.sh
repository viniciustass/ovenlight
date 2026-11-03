#!/usr/bin/env bash
# Ovenlight installer: a session explorer for Claude Code.
#
#   curl -fsSL https://raw.githubusercontent.com/viniciustass/ovenlight/main/install.sh | bash
#
# Options (environment variables):
#   OVENLIGHT_VERSION=v0.1.0          install a specific release (default: latest)
#   OVENLIGHT_INSTALL_DIR=~/bin       where to put the `ovenlight` command (default: ~/.local/bin)
#   OVENLIGHT_NO_MODIFY_PATH=1        never touch your shell profile
#
# What it does: checks macOS/Linux, Python 3.9+ and SQLite FTS5, downloads ovenlight.pyz from the GitHub
# release, verifies its SHA-256, installs it as `ovenlight`, and tells you how to start. Nothing else.
# Ovenlight is an independent project, not affiliated with or endorsed by Anthropic.
set -euo pipefail

REPO="viniciustass/ovenlight"
VERSION="${OVENLIGHT_VERSION:-latest}"
DIR="${OVENLIGHT_INSTALL_DIR:-$HOME/.local/bin}"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  O=$'\033[38;2;217;119;87m'; G=$'\033[32m'; R=$'\033[31m'; D=$'\033[2m'; B=$'\033[1m'; N=$'\033[0m'
else
  O=""; G=""; R=""; D=""; B=""; N=""
fi
step() { printf "  %s⏺%s %s\n" "$G" "$N" "$1"; }
info() { printf "    %s⎿  %s%s\n" "$D" "$1" "$N"; }
fail() { printf "  %s✕ %s%s\n" "$R" "$1" "$N" >&2; exit 1; }

printf "\n  %s✻%s %sovenlight%s %s· session explorer for Claude Code%s\n\n" "$O" "$N" "$B" "$N" "$D" "$N"

# --- platform
case "$(uname -s)" in
  Darwin) OS="macOS" ;;
  Linux)  OS="Linux" ;;
  *) fail "Ovenlight supports macOS and Linux only (found $(uname -s))." ;;
esac
step "Platform: $OS ($(uname -m))"

# --- python 3.9+ with sqlite fts5
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
[ -n "$PY" ] || fail "Python 3.9+ not found. macOS: brew install python · Debian/Ubuntu: sudo apt install python3"
step "Python: $("$PY" -c 'import platform; print(platform.python_version())') ($PY)"
"$PY" -c "import sqlite3; sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)')" 2>/dev/null \
  || fail "Your Python's SQLite has no FTS5 (full-text search). Install a Python built with FTS5 (Homebrew or python.org builds have it)."
info "SQLite FTS5 available"

if command -v claude >/dev/null 2>&1; then
  info "Claude Code found: $(claude --version 2>/dev/null | head -1)"
else
  info "Claude Code not found on PATH (install it to reopen and talk to sessions; search works without it)"
fi

# --- download + verify
if [ -n "${OVENLIGHT_BASE_URL:-}" ]; then  # testing / mirrors
  BASE="$OVENLIGHT_BASE_URL"
elif [ "$VERSION" = "latest" ]; then
  BASE="https://github.com/$REPO/releases/latest/download"
else
  BASE="https://github.com/$REPO/releases/download/$VERSION"
fi
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
step "Downloading ovenlight.pyz ($VERSION)"
curl -fsSL --retry 2 -o "$TMP/ovenlight.pyz" "$BASE/ovenlight.pyz" || fail "Download failed: $BASE/ovenlight.pyz"
if curl -fsSL --retry 2 -o "$TMP/ovenlight.pyz.sha256" "$BASE/ovenlight.pyz.sha256" 2>/dev/null; then
  want="$(awk '{print $1}' "$TMP/ovenlight.pyz.sha256")"
  if command -v shasum >/dev/null 2>&1; then got="$(shasum -a 256 "$TMP/ovenlight.pyz" | awk '{print $1}')"
  else got="$(sha256sum "$TMP/ovenlight.pyz" | awk '{print $1}')"; fi
  [ "$want" = "$got" ] || fail "Checksum mismatch (expected $want, got $got). Not installing."
  info "SHA-256 verified"
else
  info "no checksum published for this release; skipped verification"
fi
"$PY" "$TMP/ovenlight.pyz" --version >/dev/null 2>&1 || fail "The downloaded file does not run with $PY."

# --- install
mkdir -p "$DIR"
chmod +x "$TMP/ovenlight.pyz"
mv -f "$TMP/ovenlight.pyz" "$DIR/ovenlight"
step "Installed $("$PY" "$DIR/ovenlight" --version) → $DIR/ovenlight"

# --- PATH
case ":$PATH:" in
  *":$DIR:"*) ON_PATH=1 ;;
  *) ON_PATH=0 ;;
esac
if [ "$ON_PATH" = 0 ]; then
  LINE="export PATH=\"$DIR:\$PATH\""
  case "${SHELL##*/}" in
    zsh)  RC="$HOME/.zshrc" ;;
    bash) if [ "$OS" = "macOS" ]; then RC="$HOME/.bash_profile"; else RC="$HOME/.bashrc"; fi ;;
    fish) RC=""; LINE="fish_add_path $DIR" ;;
    *)    RC="$HOME/.profile" ;;
  esac
  if [ -z "${OVENLIGHT_NO_MODIFY_PATH:-}" ] && [ -n "$RC" ]; then
    if ! grep -qsF "$DIR" "$RC"; then
      printf '\n# added by the ovenlight installer\n%s\n' "$LINE" >> "$RC"
      step "Added $DIR to your PATH in $RC"
    fi
    info "open a new terminal, or run: $LINE"
  else
    step "$DIR is not on your PATH. Add it with:"
    info "$LINE"
  fi
fi

printf "\n  %s✻ Done.%s Next:\n\n" "$O" "$N"
printf "    %sovenlight doctor%s          %scheck the environment%s\n" "$B" "$N" "$D" "$N"
printf "    %sovenlight index --backup%s  %sindex your Claude Code sessions%s\n" "$B" "$N" "$D" "$N"
printf "    %sovenlight web --open%s      %sopen the web UI%s\n\n" "$B" "$N" "$D" "$N"
printf "  %sDocs: https://github.com/%s · not affiliated with Anthropic%s\n\n" "$D" "$REPO" "$N"
