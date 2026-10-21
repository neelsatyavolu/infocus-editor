#!/bin/bash
# Developer ID signing and notarization helpers for setup-app/build.sh.
# Source it; never execute. Credentials come only from 1Password (see
# ~/Documents/GitHub/APPLE_SIGNING.md) and live in a temp dir and a temp keychain
# that signing_cleanup removes. Nothing secret is printed.

# Machine-specific values (1Password account, identity, loader path) go in the
# gitignored setup-app/signing.local.sh, or the environment.
# shellcheck disable=SC1091
[ -f "$(dirname "${BASH_SOURCE[0]}")/../signing.local.sh" ] && . "$(dirname "${BASH_SOURCE[0]}")/../signing.local.sh"
SIGN_LOADER="${AGMUX_APPLE_CREDS_LOADER:-$HOME/Documents/GitHub/agmux/scripts/load-apple-creds.sh}"
SIGN_KEYCHAIN=""
SIGN_PREV_KEYCHAINS=""  # gitleaks:allow (empty variable, not a secret)

# Loads the p12 and notary key from 1Password and imports them into a temp keychain.
signing_load() {
  [ -n "${OP_ACCOUNT:-}" ] || { echo "error: set OP_ACCOUNT (1Password account) in setup-app/signing.local.sh" >&2; return 1; }
  export OP_ACCOUNT
  if ! op whoami >/dev/null 2>&1; then
    op signin --account "$OP_ACCOUNT" >/dev/null || {
      echo "error: 1Password isn't signed in. Unlock the 1Password app (or run: op signin --account $OP_ACCOUNT), then re-run." >&2
      return 1
    }
  fi
  [ -f "$SIGN_LOADER" ] || { echo "error: shared Apple creds loader not found: $SIGN_LOADER" >&2; return 1; }
  # shellcheck disable=SC1090
  source "$SIGN_LOADER" || return 1
  agmux_require_developer_id || return 1
  SIGN_IDENTITY="${APPLE_SIGNING_IDENTITY:?the creds loader must set APPLE_SIGNING_IDENTITY (or set it in signing.local.sh)}"
  _signing_keychain
}

_signing_fetch_cas() {
  local dest="$1" f
  mkdir -p "$dest"
  for f in DeveloperIDG2CA.cer DeveloperIDCA.cer AppleRootCA-G2.cer AppleRootCA-G3.cer; do
    [ -s "$dest/$f" ] || curl -fsSL -o "$dest/$f" "https://www.apple.com/certificateauthority/$f"
  done
  [ -s "$dest/AppleIncRootCertificate.cer" ] || \
    curl -fsSL -o "$dest/AppleIncRootCertificate.cer" "https://www.apple.com/appleca/AppleIncRootCertificate.cer"
}

# Temp keychain: Apple CAs first (G2 also into login), then the leaf p12.
_signing_keychain() {
  local login="$HOME/Library/Keychains/login.keychain-db" kc_pass cas cer p12
  p12="${AGMUX_APPLE_CREDS_DIR:?}/certificate.p12"
  [ -f "$p12" ] || { echo "error: no Developer ID .p12 was loaded" >&2; return 1; }
  SIGN_PREV_KEYCHAINS="$(security list-keychains -d user | sed 's/^[[:space:]]*"//;s/"$//' | tr '\n' ' ')"
  SIGN_KEYCHAIN="$AGMUX_APPLE_CREDS_DIR/infocus-setup.keychain-db"
  kc_pass="$(/usr/bin/openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
  cas="$AGMUX_APPLE_CREDS_DIR/apple-cas"
  _signing_fetch_cas "$cas" || { echo "error: couldn't download Apple's intermediate certificates" >&2; return 1; }

  security delete-keychain "$SIGN_KEYCHAIN" >/dev/null 2>&1 || true
  security create-keychain -p "$kc_pass" "$SIGN_KEYCHAIN"
  security set-keychain-settings -lut 21600 "$SIGN_KEYCHAIN"
  security unlock-keychain -p "$kc_pass" "$SIGN_KEYCHAIN"
  for cer in "$cas"/*.cer; do
    security import "$cer" -k "$SIGN_KEYCHAIN" -T /usr/bin/codesign -T /usr/bin/security >/dev/null 2>&1 || true
  done
  for cer in "$cas/DeveloperIDG2CA.cer" "$cas/DeveloperIDCA.cer"; do
    security import "$cer" -k "$login" -T /usr/bin/codesign -T /usr/bin/security >/dev/null 2>&1 || true
  done
  security import "$p12" -k "$SIGN_KEYCHAIN" -P "${APPLE_CERTIFICATE_PASSWORD:?}" \
    -T /usr/bin/codesign -T /usr/bin/security -T /usr/bin/productbuild >/dev/null
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$kc_pass" "$SIGN_KEYCHAIN" >/dev/null
  security list-keychains -d user -s "$SIGN_KEYCHAIN" "$login"
  security unlock-keychain -p "$kc_pass" "$SIGN_KEYCHAIN"
  unset APPLE_CERTIFICATE APPLE_CERTIFICATE_PASSWORD

  # find-identity can lie with stale keychains around; prove codesign works on a tiny binary.
  local probe="$AGMUX_APPLE_CREDS_DIR/probe"
  printf 'int main(void){return 0;}\n' > "$probe.c" && cc -o "$probe" "$probe.c"
  codesign --force --sign "$SIGN_IDENTITY" --keychain "$SIGN_KEYCHAIN" --timestamp --options runtime "$probe" \
    >/dev/null 2>&1 || { echo "error: codesign smoke test failed with the imported identity" >&2; return 1; }
  echo "  signing: Developer ID ready ($SIGN_IDENTITY)"
}

sign_path() {
  codesign --force --sign "$SIGN_IDENTITY" --keychain "$SIGN_KEYCHAIN" --timestamp --options runtime "$@"
}

notarize() {  # notarize <file>: submit and wait; prints Apple's status lines only
  [ -n "${APPLE_API_KEY_PATH:-}" ] && [ -f "$APPLE_API_KEY_PATH" ] || {
    echo "error: no notarization API key loaded from 1Password" >&2; return 1; }
  local out
  out="$(xcrun notarytool submit "$1" --key "$APPLE_API_KEY_PATH" --key-id "${APPLE_API_KEY:?}" \
    --issuer "${APPLE_API_ISSUER:?}" --wait 2>&1)" || true
  printf '%s\n' "$out" | grep -E '^\s*(id|status):|Processing complete' || true
  if ! printf '%s\n' "$out" | grep -q 'status: Accepted'; then
    local id
    id="$(printf '%s\n' "$out" | awk '/^ *id:/ {print $2; exit}')"
    [ -n "$id" ] && xcrun notarytool log "$id" --key "$APPLE_API_KEY_PATH" --key-id "$APPLE_API_KEY" \
      --issuer "$APPLE_API_ISSUER" 2>&1 | head -60
    echo "error: notarization was not accepted" >&2
    return 1
  fi
}

signing_cleanup() {
  local login="$HOME/Library/Keychains/login.keychain-db"
  # Restore the previous search list, minus temp keychains that no longer exist
  # (stale entries break later codesign runs; see APPLE_SIGNING.md).
  local keep=() kc
  for kc in $SIGN_PREV_KEYCHAINS; do
    [ -e "$kc" ] && [ "$kc" != "$SIGN_KEYCHAIN" ] && keep+=("$kc")
  done
  [ ${#keep[@]} -gt 0 ] && security list-keychains -d user -s "${keep[@]}" >/dev/null 2>&1 || true
  if ! security list-keychains -d user 2>/dev/null | grep -q 'login.keychain' \
     || security list-keychains -d user 2>/dev/null | grep -q 'infocus-setup.keychain'; then
    security list-keychains -d user -s "$login" >/dev/null 2>&1 || true
  fi
  [ -n "$SIGN_KEYCHAIN" ] && security delete-keychain "$SIGN_KEYCHAIN" >/dev/null 2>&1 || true
  [ -n "${AGMUX_APPLE_CREDS_DIR:-}" ] && rm -rf "$AGMUX_APPLE_CREDS_DIR"
  unset APPLE_CERTIFICATE APPLE_CERTIFICATE_PASSWORD APPLE_API_KEY APPLE_API_ISSUER \
        APPLE_API_KEY_PATH APPLE_SIGNING_IDENTITY APPLE_TEAM_ID AGMUX_APPLE_CREDS_DIR 2>/dev/null || true
}
