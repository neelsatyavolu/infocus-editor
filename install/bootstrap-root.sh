#!/bin/bash
# One-time root setup, run by install.sh through the macOS admin dialog:
#   bootstrap-root.sh <editor user> <repo dir> [--personal]
# It installs the Command Line Tools, gives the editor user its own Homebrew
# (so brew never needs admin again), applies power settings, installs the
# infocus-root helper and a sudoers rule for that one helper only, and writes
# a root-owned marker. Re-running is safe. --personal (someone's own computer, possibly an
# admin account) keeps the normal sleep settings and reuses only a Homebrew that account owns.
set -euo pipefail
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
umask 022

ROOT_DIR=/Library/InFocusEditor
HELPER="$ROOT_DIR/bin/infocus-root"
SUDOERS=/etc/sudoers.d/infocus-editor
MARKER="$ROOT_DIR/bootstrap.done"
BREW_PREFIX=/opt/homebrew

die() { echo "bootstrap: $*" >&2; exit 1; }
log() { echo "bootstrap: $*"; }

[ "$(id -u)" -eq 0 ] || die "must run as root"
[ $# -eq 2 ] || [ $# -eq 3 ] || die "usage: bootstrap-root.sh <user> <repo dir> [--personal]"
USER_NAME="$1"
REPO_DIR="$2"
PERSONAL=0
if [ $# -eq 3 ]; then
  [ "$3" = "--personal" ] || die "unknown option $3"
  PERSONAL=1
fi
[[ "$USER_NAME" =~ ^[a-z_][a-z0-9_.-]{0,31}$ ]] || die "invalid user name"
USER_UID=$(id -u "$USER_NAME" 2>/dev/null) || die "no such user: $USER_NAME"
[ "$USER_UID" -ge 501 ] || die "refusing a system account"
[ -f "$REPO_DIR/install/infocus-root" ] || die "no install/infocus-root in $REPO_DIR"

install_clt() {
  if xcode-select -p >/dev/null 2>&1 && [ -x /Library/Developer/CommandLineTools/usr/bin/git ]; then
    log "Command Line Tools present"
    return
  fi
  log "installing the Command Line Tools (this takes a few minutes)"
  local flag=/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
  touch "$flag"
  local label
  label=$(softwareupdate --list 2>/dev/null | sed -n 's/^[[:space:]]*\* Label: \(Command Line Tools.*\)$/\1/p' | tail -n 1)
  [ -n "$label" ] || { rm -f "$flag"; die "softwareupdate offered no Command Line Tools"; }
  softwareupdate --install "$label" --verbose
  rm -f "$flag"
  xcode-select --switch /Library/Developer/CommandLineTools
}

install_homebrew() {
  if [ -x "$BREW_PREFIX/bin/brew" ] && [ "$(stat -f %Su "$BREW_PREFIX")" = "$USER_NAME" ]; then
    log "Homebrew present and owned by $USER_NAME"
    return
  fi
  if [ -e "$BREW_PREFIX" ] && [ "$PERSONAL" = 1 ] && [ "$(stat -f %Su "$BREW_PREFIX")" != "$USER_NAME" ]; then
    die "Homebrew on this Mac belongs to another account ($(stat -f %Su "$BREW_PREFIX")). Run setup from that account, or ask an admin."
  fi
  mkdir -p "$BREW_PREFIX"
  # A dedicated editing Mac: the editor account owns Homebrew so it never needs admin.
  chown -R "$USER_NAME":staff "$BREW_PREFIX"
  if [ ! -x "$BREW_PREFIX/bin/brew" ]; then
    log "installing Homebrew into $BREW_PREFIX as $USER_NAME"
    sudo -u "$USER_NAME" -H /usr/bin/git clone --depth=1 https://github.com/Homebrew/brew "$BREW_PREFIX"
  fi
  sudo -u "$USER_NAME" -H "$BREW_PREFIX/bin/brew" update --force --quiet || log "brew update failed; user-setup will retry"
}

install_helper() {
  mkdir -p "$ROOT_DIR/bin"
  chown root:wheel "$ROOT_DIR" "$ROOT_DIR/bin"
  chmod 755 "$ROOT_DIR" "$ROOT_DIR/bin"
  install -o root -g wheel -m 755 "$REPO_DIR/install/infocus-root" "$HELPER"
  log "installed $HELPER"
}

install_sudoers() {
  local tmp
  tmp=$(mktemp /tmp/infocus-sudoers.XXXXXX)
  printf '# Managed by infocus-editor install. Lets %s run only the infocus-root helper.\n%s ALL=(root) NOPASSWD: %s\n' \
    "$USER_NAME" "$USER_NAME" "$HELPER" > "$tmp"
  if ! visudo -cf "$tmp" >/dev/null; then
    rm -f "$tmp"
    die "the sudoers rule failed validation; nothing was changed"
  fi
  install -o root -g wheel -m 440 "$tmp" "$SUDOERS"
  rm -f "$tmp"
  log "installed $SUDOERS"
}

write_marker() {
  local version
  version=$(sed -n 's/^HELPER_VERSION=\([0-9][0-9]*\)$/\1/p' "$HELPER")
  printf 'helper_version=%s\nuser=%s\ndate=%s\n' "$version" "$USER_NAME" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$MARKER.tmp"
  chown root:wheel "$MARKER.tmp"
  chmod 644 "$MARKER.tmp"
  mv "$MARKER.tmp" "$MARKER"
}

install_clt
install_homebrew
install_helper
if [ "$PERSONAL" = 1 ]; then
  log "personal computer: leaving the power settings alone"
else
  "$HELPER" apply-power
fi
install_sudoers
write_marker
log "done"
