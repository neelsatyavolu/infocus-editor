#!/bin/bash
# Everything that doesn't need an admin. install.sh runs it; the auto-updater and the
# Portal's Repair command run it with --update --unattended (no prompts, no linking).
#   install/user-setup.sh [--home <data dir>] [--update] [--unattended] [--progress]
#                         [--package-src <dir>] [--portal <url>] [--personal]
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=install/lib.sh
. "$REPO/install/lib.sh"
install_traps

DATA_DIR="${INFOCUS_EDITOR_HOME:-$HOME/InFocusEditor}"
UPDATE=0
PERSONAL=""
PACKAGE_SRC=""
PORTAL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --home) DATA_DIR="${2:?--home needs a folder}"; shift 2 ;;
    --update) UPDATE=1; shift ;;
    --unattended) UNATTENDED=1; shift ;;
    --progress) PROGRESS=1; shift ;;
    --package-src) PACKAGE_SRC="${2:?--package-src needs a folder}"; shift 2 ;;
    --portal) PORTAL="${2:?--portal needs a url}"; shift 2 ;;
    --personal) PERSONAL=1; shift ;;
    *) die "unknown option $1" ;;
  esac
done
[ "$(id -u)" -ne 0 ] || die "run as the editor's own user, not root"
[ "$UPDATE" = 1 ] && UNATTENDED=1

[ -x "$BREW" ] || die "Homebrew isn't installed for this user yet. Open InFocus Editor Setup; an admin needs to approve once."
export PATH="$PATH:$BREW_PREFIX/bin:$BREW_PREFIX/sbin:$HOME/.local/bin"
# Never upgrade what's already installed (a personal computer's own tools stay as they are).
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1 HOMEBREW_NO_ENV_HINTS=1
export HOMEBREW_NO_INSTALL_UPGRADE=1 HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK=1 HOMEBREW_BUNDLE_NO_UPGRADE=1
export INFOCUS_EDITOR_HOME="$DATA_DIR"

PACKAGE_DIR="$DATA_DIR/package"
WHISPER_DIR="$HOME/.cache/whisper-cpp"
WHISPER_FILE="$WHISPER_DIR/ggml-large-v3-turbo.bin"
WHISPER_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
WHISPER_SHA256="1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69"
WHISPER_SIZE=1624555275

# Personal mode comes from the flag on a fresh install and from config.toml afterwards.
if [ -z "$PERSONAL" ]; then
  PERSONAL=0
  if [ "$UPDATE" = 1 ] && grep -q '^machine_kind[[:space:]]*=[[:space:]]*"personal"' "$DATA_DIR/config.toml" 2>/dev/null; then
    PERSONAL=1
  fi
fi
if [ "$PERSONAL" = 1 ]; then
  check_brew_owner
fi

editor() { (cd "$REPO" && uv run --quiet --project "$REPO" infocus-editor "$@"); }

config_value() {  # config_value <key>: the quoted string value from config.toml, if any
  sed -n "s/^$1[[:space:]]*=[[:space:]]*\"\(.*\)\"[[:space:]]*\$/\1/p" "$DATA_DIR/config.toml" 2>/dev/null | head -n 1
}

set_config() {  # set_config <key> <value>: replace or add a `key = "..."` line
  local tmp
  tmp=$(mktemp)
  if grep -q "^$1[[:space:]]*=" "$DATA_DIR/config.toml" 2>/dev/null; then
    awk -v k="$1" -v v="$2" '$0 ~ "^"k"[[:space:]]*=" {print k " = \"" v "\""; next} {print}' \
      "$DATA_DIR/config.toml" > "$tmp"
  else
    { cat "$DATA_DIR/config.toml" 2>/dev/null || true; printf '%s = "%s"\n' "$1" "$2"; } > "$tmp"
  fi
  mv "$tmp" "$DATA_DIR/config.toml"
}

# Only what isn't there yet, judged by the command each formula provides. A tool installed some
# other way (e.g. uv's own installer into /opt/homebrew/bin) is used as is, never fought over.
FORMULA_COMMANDS="git:git uv:uv ffmpeg:ffmpeg node:node whisper-cpp:whisper-cli"

missing_formulae() {
  local pair
  for pair in $FORMULA_COMMANDS; do
    command -v "${pair#*:}" >/dev/null 2>&1 || printf '%s ' "${pair%%:*}"
  done
}

tools() {
  step tools running "Installing ffmpeg, node, whisper and uv"
  local missing
  missing=$(missing_formulae)
  if [ -z "$missing" ]; then
    step tools skipped "Using the tools already installed"
    return
  fi
  # shellcheck disable=SC2086 # one formula per word
  "$BREW" install $missing || die "Homebrew couldn't install: ${missing% }. See Show details."
  step tools "done" "Installed ${missing% }"
}

ai_tools() {
  step ai running "Installing the Codex and Grok command-line tools"
  if ! command -v codex >/dev/null 2>&1; then
    "$BREW" bundle --no-upgrade --file "$REPO/install/Brewfile.ai" \
      || die "Homebrew couldn't install the Codex CLI."
  fi
  # The Grok CLI has no Homebrew formula; xAI's official installer puts it in ~/.grok/bin
  # and links ~/.local/bin/grok, both inside this user's home (no admin).
  if command -v grok >/dev/null 2>&1; then
    step ai "done" "Codex and Grok ready"
    return
  fi
  local script
  script=$(mktemp)
  if curl -fsSL https://x.ai/cli/install.sh -o "$script" && bash "$script"; then
    step ai "done" "Codex and Grok ready"
  else
    step ai "done" "Codex ready; the Grok CLI didn't install (Grok stays unavailable)"
  fi
  rm -f "$script"
}

whisper_model() {
  step model running "Downloading the speech model (1.6 GB)"
  if [ -f "$WHISPER_FILE" ] && [ "$(stat -f %z "$WHISPER_FILE")" = "$WHISPER_SIZE" ]; then
    step model skipped "Already downloaded"
    return
  fi
  mkdir -p "$WHISPER_DIR"
  curl -fL --retry 3 --continue-at - -o "$WHISPER_FILE.part" "$WHISPER_URL" \
    || die "Couldn't download the speech model. Check the internet connection and try again."
  echo "$WHISPER_SHA256  $WHISPER_FILE.part" | shasum -a 256 -c - >/dev/null \
    || { rm -f "$WHISPER_FILE.part"; die "The speech model download was damaged (checksum mismatch). Try again."; }
  mv "$WHISPER_FILE.part" "$WHISPER_FILE"
  step model "done" "Speech model ready"
}

app() {
  step app running "Setting up the editor program"
  mkdir -p "$DATA_DIR/jobs"
  if [ -n "$PORTAL" ]; then
    case "$PORTAL" in https://*|http://localhost*|http://127.0.0.1*) ;; *) die "The Portal address must start with https://" ;; esac
    plain_path "$PORTAL" || die "Unexpected characters in the Portal address."
    set_config portal_url "${PORTAL%/}"
  elif [ -z "$(config_value portal_url)" ]; then
    [ "$UNATTENDED" = 0 ] && [ -t 0 ] || die "No Portal address: pass --portal."
    local value
    read -r -p "Portal address (https://…): " value
    case "$value" in https://*) ;; *) die "The Portal address must start with https://" ;; esac
    plain_path "$value" || die "Unexpected characters in the Portal address."
    set_config portal_url "${value%/}"
  fi
  if [ "$UPDATE" = 0 ]; then
    if [ "$PERSONAL" = 1 ]; then set_config machine_kind personal; else set_config machine_kind dedicated; fi
  fi
  (cd "$REPO" && uv sync --frozen --quiet) || die "Couldn't set up the editor's Python environment."
  step app "done" "Editor program ready"
}

package() {
  step package running "Setting up the graphics package"
  if [ ! -d "$PACKAGE_DIR" ]; then
    if [ -n "$PACKAGE_SRC" ]; then
      [ -d "$PACKAGE_SRC" ] || die "No graphics package at $PACKAGE_SRC."
      ditto "$PACKAGE_SRC" "$PACKAGE_DIR" || die "Couldn't copy the graphics package."
    elif [ "$UNATTENDED" = 0 ] && [ -t 0 ]; then
      local value
      read -r -p "Folder holding the graphics package: " value
      [ -d "${value/#\~/$HOME}" ] || die "No folder at $value."
      ditto "${value/#\~/$HOME}" "$PACKAGE_DIR" || die "Couldn't copy the graphics package."
    else
      die "No graphics package: pass --package-src."
    fi
  fi
  local skill="$REPO/.claude/skills/infocus-show" gfx="$PACKAGE_DIR/_Working Files/Graphics Source" version=""
  if [ -f "$skill/package.json" ]; then
    if [ -f "$skill/package-lock.json" ]; then npm ci --prefix "$skill"; else npm install --prefix "$skill"; fi \
      || die "Couldn't install the skill's node packages."
  fi
  if [ -f "$gfx/package.json" ]; then
    if [ -f "$gfx/package-lock.json" ]; then npm ci --prefix "$gfx"; else npm install --prefix "$gfx"; fi \
      || die "Couldn't install the graphics package's node packages."
    version=$(node -p 'require(process.argv[1]).version' "$gfx/node_modules/playwright-core/package.json" 2>/dev/null || true)
  fi
  npx --yes "playwright@${version:-latest}" install chromium || die "Couldn't install Playwright's Chromium."
  step package "done" "Graphics package ready"
}

link() {
  step link running "Linking this Mac to the Portal"
  if editor link --check >/dev/null 2>&1; then
    step link skipped "Already linked"
    return
  fi
  local flags=()
  [ "$PROGRESS" = 1 ] && flags+=(--progress)
  editor link ${flags[@]+"${flags[@]}"} || die "Linking to the Portal didn't finish. Try again."
  step link "done" "Linked"
}

# A personal computer runs without the permanent caffeinate (the lines marked <!-- dedicated -->);
# the runner keeps it awake only while a job runs.
render_plist() {
  local drop='^@@never@@$' uv_bin
  [ "$PERSONAL" = 1 ] && drop='<!-- dedicated -->'
  uv_bin=$(command -v uv || echo /opt/homebrew/bin/uv)
  sed -e "/$drop/d" -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" -e "s|__UV__|$uv_bin|g" \
      -e "s|__DATA__|$DATA_DIR|g" "$REPO/launchd/$LABEL.plist"
}

service() {
  step service running "Starting the background service"
  local dest="$HOME/Library/LaunchAgents/$LABEL.plist" domain
  domain="gui/$(id -u)"
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  render_plist > "$dest"
  plutil -lint "$dest" >/dev/null || die "The background service definition is invalid."
  launchctl bootout "$domain/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$domain" "$dest" || die "macOS wouldn't start the background service."
  launchctl kickstart -k "$domain/$LABEL" || true
  step service "done" "Running"
}

# On an update: install a changed launchd definition and leave a flag for the updater,
# which reloads the agent after it exits (a running agent can't reload itself).
service_definition_update() {
  local dest="$HOME/Library/LaunchAgents/$LABEL.plist" tmp
  [ -f "$dest" ] || return 0
  tmp=$(mktemp)
  render_plist > "$tmp"
  if ! cmp -s "$tmp" "$dest"; then
    plutil -lint "$tmp" >/dev/null || die "The new background service definition is invalid."
    mv "$tmp" "$dest"
    touch "$DATA_DIR/.plist-changed"
  else
    rm -f "$tmp"
  fi
}

health() {
  step health running "Checking everything works"
  local report
  report=$(editor doctor 2>&1) || true
  printf '%s\n' "$report"
  step health "done" "$(printf '%s\n' "$report" | tail -n 1)"
}

tools
ai_tools
whisper_model
app
package
if [ "$UPDATE" = 0 ]; then
  link
  service
  health
else
  service_definition_update
fi
finish
