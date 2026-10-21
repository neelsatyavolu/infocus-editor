#!/bin/bash
# Remove the editor's launchd agent, then (one admin dialog) the sudoers rule
# and the root helper. Leaves the data folder (~/InFocusEditor: house rules,
# job folders, config) and the user's Homebrew; delete those by hand if wanted.
#   install/uninstall.sh
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=install/lib.sh
. "$SRC_DIR/install/lib.sh"
[ "$(id -u)" -ne 0 ] || die "run this as the editor's own user, not root"

PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
say "Removed the launchd agent"

if [ -e "$SUDOERS" ] || [ -e "$ROOT_DIR" ]; then
  say "An admin needs to approve removing the helper and its sudoers rule."
  run_as_admin /bin/rm -rf "$SUDOERS" "$ROOT_DIR"
  say "Removed $SUDOERS and $ROOT_DIR"
fi
say "Done. The data folder and Homebrew in /opt/homebrew were left in place."
