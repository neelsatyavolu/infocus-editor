#!/bin/bash
# One-time root setup, run by install.sh through the macOS admin dialog:
#   bootstrap-root.sh <editor user> <repo dir> [--personal]
# It installs the Command Line Tools, gives the editor user its own Homebrew
# (so brew never needs admin again), applies power settings, installs the
# infocus-root helper and a sudoers rule for that one helper only, and writes
# a root-owned marker. Re-running is safe. --personal (someone's own computer, possibly an
# admin account) keeps the normal sleep settings and reuses only a Homebrew that account owns.
# Progress for the setup app goes to $ROOT_DIR/bootstrap.progress (root-owned, world-readable),
# which install.sh tails while this runs inside the admin dialog.
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
[ $# -ge 2 ] || die "usage: bootstrap-root.sh <user> <repo dir> [--personal]"
USER_NAME="$1"
REPO_DIR="$2"
shift 2
PERSONAL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --personal) PERSONAL=1; shift ;;
    *) die "unknown option $1" ;;
  esac
done
PROGRESS_FILE="$ROOT_DIR/bootstrap.progress"
progress() { printf '@@step %s\n' "$*" >> "$PROGRESS_FILE"; }
[[ "$USER_NAME" =~ ^[a-z_][a-z0-9_.-]{0,31}$ ]] || die "invalid user name"
USER_UID=$(id -u "$USER_NAME" 2>/dev/null) || die "no such user: $USER_NAME"
[ "$USER_UID" -ge 501 ] || die "refusing a system account"
[ -f "$REPO_DIR/install/infocus-root" ] || die "no install/infocus-root in $REPO_DIR"

install_clt() {
  local dev
  # Xcode or the standalone tools both provide git and swiftc; never replace a working selection.
  if dev=$(xcode-select -p 2>/dev/null) && [ -x "$dev/usr/bin/git" ]; then
    log "developer tools present ($dev)"
    progress clt skipped "Already installed"
    return
  fi
  log "installing the Command Line Tools (this takes a few minutes)"
  progress clt running "Installing Apple command-line tools (a few minutes)"
  local flag=/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
  touch "$flag"
  local label
  label=$(softwareupdate --list 2>/dev/null | sed -n 's/^[[:space:]]*\* Label: \(Command Line Tools.*\)$/\1/p' | tail -n 1)
  [ -n "$label" ] || { rm -f "$flag"; die "softwareupdate offered no Command Line Tools"; }
  softwareupdate --install "$label" --verbose
  rm -f "$flag"
  xcode-select --switch /Library/Developer/CommandLineTools
  progress clt "done" "Installed"
}

install_homebrew() {
  if [ -x "$BREW_PREFIX/bin/brew" ] && [ "$(stat -f %Su "$BREW_PREFIX")" = "$USER_NAME" ]; then
    log "Homebrew present and owned by $USER_NAME"
    progress homebrew skipped "Already installed"
    return
  fi
  progress homebrew running "Setting up Homebrew"
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
  progress homebrew "done" "Installed"
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

mkdir -p "$ROOT_DIR"
chown root:wheel "$ROOT_DIR"
chmod 755 "$ROOT_DIR"
rm -f "$PROGRESS_FILE"
install -o root -g wheel -m 644 /dev/null "$PROGRESS_FILE"
progress admin "done" "Approved"
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
