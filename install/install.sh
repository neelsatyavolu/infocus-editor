#!/bin/bash
# Install or repair the InFocus editor. Run it as the editor's (standard) macOS user:
#   install/install.sh [--unattended] [--progress] [--repo <git url or .bundle>]
#                      [--home <data dir>] [--package-src <dir>] [--portal <url>] [--personal]
# The setup app runs it with --unattended --progress (docs/setup-app.md). The first run
# asks an admin to approve one macOS password dialog; later runs skip it unless the
# root helper changed.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=install/lib.sh
. "$SRC_DIR/install/lib.sh"
install_traps

REPO_URL=""
PERSONAL=0
DATA_DIR="$HOME/InFocusEditor"
PASS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO_URL="${2:?--repo needs a git url}"; shift 2 ;;
    --home) DATA_DIR="${2:?--home needs a folder}"; shift 2 ;;
    --package-src) PASS+=(--package-src "${2:?--package-src needs a folder}"); shift 2 ;;
    --portal) PASS+=(--portal "${2:?--portal needs a url}"); shift 2 ;;
    --unattended) PASS+=(--unattended); shift ;;
    --progress) PROGRESS=1; PASS+=(--progress); shift ;;
    --personal) PERSONAL=1; PASS+=(--personal); shift ;;
    -h|--help) sed -n '2,7p' "$0"; FINISHED=1; exit 0 ;;
    *) die "unknown option $1" ;;
  esac
done
[ "$(id -u)" -ne 0 ] || die "run this as the editor's own user, not root (it asks for an admin when needed)"
REPO_URL="${REPO_URL:-$DEFAULT_REPO_URL}"
APP_DIR="$DATA_DIR/app"
export PATH="$PATH:$BREW_PREFIX/bin:$BREW_PREFIX/sbin:$HOME/.local/bin"

# A repair moves the existing checkout to the newest release first, so the admin step
# below installs that release's root helper.
HELPER_SRC="$SRC_DIR/install/infocus-root"
if [ -d "$APP_DIR/.git" ] && command -v git >/dev/null 2>&1; then
  refresh_app "$APP_DIR"
  HELPER_SRC="$APP_DIR/install/infocus-root"
fi

ROOT_PROGRESS_FILE="$ROOT_DIR/bootstrap.progress"
# 1. The admin step, once (or again when the root helper changed).
step admin running "Waiting for an admin to approve"
if [ "$PERSONAL" = 1 ]; then
  check_brew_owner
fi
BOOTSTRAP_ARGS=("$(id -un)" "$(dirname "$(dirname "$HELPER_SRC")")")
if [ "$PERSONAL" = 1 ]; then
  BOOTSTRAP_ARGS+=(--personal)
fi
had_clt=0; dev_dir=$(xcode-select -p 2>/dev/null) && [ -x "$dev_dir/usr/bin/git" ] && had_clt=1
had_brew=0; [ -x "$BREW" ] && had_brew=1
if root_step_ok "$HELPER_SRC"; then
  step admin skipped "Already approved"
else
  say "An admin needs to approve a one-time setup step (a macOS password dialog will appear)."
  # The admin step runs inside the dialog's shell and reports its own steps (clt, homebrew)
  # through a root-owned progress file; relay it to the setup app while it runs.
  TAIL_PID=""
  if [ "$PROGRESS" = 1 ]; then
    tail -n 0 -F "$ROOT_PROGRESS_FILE" 2>/dev/null &
    TAIL_PID=$!
  fi
  admin_ok=1
  run_as_admin /bin/bash "$SRC_DIR/install/bootstrap-root.sh" "${BOOTSTRAP_ARGS[@]}" || admin_ok=0
  if [ -n "$TAIL_PID" ]; then
    sleep 1
    kill "$TAIL_PID" 2>/dev/null || true
    wait "$TAIL_PID" 2>/dev/null || true
  fi
  [ "$admin_ok" = 1 ] || die "The admin step didn't finish (the password dialog was cancelled or failed). Try again."
  root_step_ok "$HELPER_SRC" || die "The admin step ran but the root helper check still fails. Try again."
  step admin "done" "Approved"
fi
if [ "$had_clt" = 1 ]; then step clt skipped "Already installed"; else step clt "done" "Installed"; fi
if [ "$had_brew" = 1 ]; then step homebrew skipped "Already installed"; else step homebrew "done" "Installed"; fi
[ -x "$BREW" ] || die "Homebrew isn't installed for this user; run setup again."

# 2. The production checkout.
say "Getting the editor program"
mkdir -p "$DATA_DIR"
if [ ! -d "$APP_DIR/.git" ]; then
  clone_app "$REPO_URL" "$APP_DIR" || die "Couldn't get the editor program from $REPO_URL."
fi

# 3. Everything else, as this user (user-setup.sh finishes the app step and prints @@done).
[ -x "$APP_DIR/install/user-setup.sh" ] || die "The editor program in $APP_DIR is incomplete; run setup again."
FINISHED=1  # exec hands over; user-setup.sh owns the final line
exec "$APP_DIR/install/user-setup.sh" --home "$DATA_DIR" ${PASS[@]+"${PASS[@]}"}
