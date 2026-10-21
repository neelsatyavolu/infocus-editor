#!/bin/bash
# Shared by install.sh, user-setup.sh and uninstall.sh. Source it; don't run it.
# shellcheck shell=bash
#
# Progress protocol (docs/setup-app.md), on stdout when --progress is given:
#   @@step <id> <running|done|skipped|failed> <message…>
#   @@link <userCode> <verificationUrl>     (printed by `infocus-editor link --progress`)
#   @@linked
#   @@done | @@error <message…>             (always the last line; exit 0 only after @@done)
#
# The INFOCUS_* overrides exist so the tests can run these scripts against fakes.
# They only affect these user-level scripts; bootstrap-root.sh ignores them.

ROOT_DIR="${INFOCUS_ROOT_DIR:-/Library/InFocusEditor}"
HELPER="$ROOT_DIR/bin/infocus-root"
SUDOERS="${INFOCUS_SUDOERS:-/etc/sudoers.d/infocus-editor}"
MARKER="$ROOT_DIR/bootstrap.done"
ROOT_OWNER="${INFOCUS_ROOT_OWNER:-root}"
BREW_PREFIX="${INFOCUS_BREW_PREFIX:-/opt/homebrew}"
# shellcheck disable=SC2034
BREW="${INFOCUS_BREW:-$BREW_PREFIX/bin/brew}"
# shellcheck disable=SC2034 # used by the scripts that source this file
LABEL=com.infocus.editor
# shellcheck disable=SC2034
DEFAULT_REPO_URL="${INFOCUS_DEFAULT_REPO_URL:-https://github.com/neelsatyavolu/infocus-editor.git}"

PROGRESS=0
# shellcheck disable=SC2034
UNATTENDED=0
CURRENT_STEP=""
LAST_ERROR=""
FINISHED=0

say() { printf '==> %s\n' "$*"; }

# step <id> <running|done|skipped|failed> <message…>
step() {
  local id="$1" state="$2"
  shift 2
  if [ "$state" = running ]; then
    CURRENT_STEP="$id"
  fi
  if [ "$PROGRESS" = 1 ]; then
    printf '@@step %s %s %s\n' "$id" "$state" "$*"
  elif [ "$state" != "done" ]; then
    say "$*"
  fi
}

die() {
  LAST_ERROR="$*"
  printf 'install: %s\n' "$*" >&2
  exit 1
}

# The last line: @@done (exit 0). Anything else ends through on_exit with @@error.
finish() {
  FINISHED=1
  if [ "$PROGRESS" = 1 ]; then
    printf '@@done\n'
  else
    say "Done."
  fi
}

on_exit() {
  local code=$?
  [ "$FINISHED" = 1 ] && return 0
  local message="${LAST_ERROR:-the ${CURRENT_STEP:-setup} step failed (exit $code)}"
  if [ "$PROGRESS" = 1 ]; then
    [ -n "$CURRENT_STEP" ] && printf '@@step %s failed %s\n' "$CURRENT_STEP" "$message"
    printf '@@error %s\n' "$message"
  fi
  [ "$code" -eq 0 ] && exit 1
  exit "$code"
}

on_err() {
  [ -n "$LAST_ERROR" ] || LAST_ERROR="command failed: $1"
}

# Call once at the top of a script: nothing fails silently.
install_traps() {
  set -E
  trap 'on_err "$BASH_COMMAND"' ERR
  trap on_exit EXIT
}

# Personal computers reuse a Homebrew this account owns; one owned by another account stops setup.
check_brew_owner() {
  local owner
  [ -e "$BREW_PREFIX" ] || return 0
  owner=$(stat -f %Su "$BREW_PREFIX")
  if [ "$owner" != "$(id -un)" ]; then
    die "Homebrew on this Mac belongs to another account ($owner). Run setup from that account, or ask an admin."
  fi
}

helper_version_of() {
  sed -n 's/^HELPER_VERSION=\([0-9][0-9]*\)$/\1/p' "$1" 2>/dev/null
}

# 0 when the admin step is complete and current for the helper in $1 (the repo's copy).
root_step_ok() {
  local wanted
  wanted=$(helper_version_of "$1")
  [ -f "$MARKER" ] && [ "$(stat -f %Su "$MARKER")" = "$ROOT_OWNER" ] || return 1
  [ -x "$HELPER" ] && [ "$(stat -f %Su "$HELPER")" = "$ROOT_OWNER" ] || return 1
  [ "$(helper_version_of "$HELPER")" = "$wanted" ] || return 1
  [ -e "$SUDOERS" ] || return 1
  sudo -n "$HELPER" status >/dev/null 2>&1
}

# Paths go inside an AppleScript string and a shell command: keep them plain.
plain_path() {
  case "$1" in
    *\'*|*\"*|*\\*|*$'\n'*) return 1 ;;
  esac
}

# Run "$@" as root through the standard macOS admin dialog. The password is
# typed into macOS, never seen or stored by these scripts.
run_as_admin() {
  local cmd="" arg
  for arg in "$@"; do
    plain_path "$arg" || die "can't pass '$arg' to the admin dialog (quotes or backslashes)"
    cmd="$cmd '$arg'"
  done
  osascript -e "do shell script \"$cmd\" with administrator privileges"
}

latest_tag() {
  git -C "$1" tag --list 'v*' --sort=-v:refname | head -n 1
}

# clone_app <repo url or .bundle> <app dir>: the production checkout, on the newest release tag.
clone_app() {
  local repo="$1" app="$2" latest
  git clone --quiet "$repo" "$app"
  # Installed from the setup app's offline bundle: future updates still come from GitHub.
  case "$repo" in
    *.bundle) git -C "$app" remote set-url origin "$DEFAULT_REPO_URL" ;;
  esac
  latest=$(latest_tag "$app")
  if [ -n "$latest" ]; then
    git -C "$app" checkout --quiet --detach "$latest"
  fi
}

# refresh_app <app dir>: on a repair, move an existing checkout to the newest release tag
# first, so the admin step installs that release's root helper.
refresh_app() {
  local app="$1" latest
  git -C "$app" fetch --quiet --tags --force origin || return 0
  latest=$(latest_tag "$app")
  if [ -n "$latest" ] && [ -z "$(git -C "$app" status --porcelain --untracked-files=no)" ]; then
    git -C "$app" checkout --quiet --detach "$latest"
  fi
}
