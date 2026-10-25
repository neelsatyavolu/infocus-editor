#!/bin/bash
# Builds "InFocus Editor Setup.app" (docs/setup-app.md).
#   setup-app/build.sh [--sign | --release] [--portal URL] [--package DIR]
#     (none)     unsigned local build in setup-app/build/
#     --sign     Developer ID signature (1Password), no notarization
#     --release  sign, notarize and staple the app and dist/InFocus Editor Setup.dmg
# The package comes from --package or $INFOCUS_PACKAGE (the full "InFocus 2026 Package").
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
MODE=unsigned
PORTAL="https://infocusportal.com"
PACKAGE="${INFOCUS_PACKAGE:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --sign) MODE=sign; shift ;;
    --release) MODE=release; shift ;;
    --portal) PORTAL="${2:?--portal needs a url}"; shift 2 ;;
    --package) PACKAGE="${2:?--package needs a folder}"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
case "$PORTAL" in https://*|http://localhost*) ;; *) echo "error: --portal must be an https URL" >&2; exit 2 ;; esac
[ -n "$PACKAGE" ] && [ -d "$PACKAGE" ] || {
  echo "error: pass --package <InFocus 2026 Package folder> (or set INFOCUS_PACKAGE)" >&2; exit 2; }

NAME="InFocus Editor Setup"
BUNDLE_ID="org.infocus.editor-setup"
VERSION="$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$REPO/pyproject.toml" | head -n 1)"
BUILD="$HERE/build"
APP="$BUILD/$NAME.app"
DIST="$HERE/dist"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/infocus-setup.XXXXXX")"

if [ "$MODE" != unsigned ]; then
  # shellcheck source=scripts/signing.sh
  . "$HERE/scripts/signing.sh"
fi
cleanup() {
  [ "$MODE" != unsigned ] && signing_cleanup
  rm -rf "$WORK"
}
trap cleanup EXIT

say() { printf '==> %s\n' "$*"; }

# 1. Compile.
say "Compiling the app (arm64, release)"
(cd "$HERE" && swift build -c release --arch arm64 >/dev/null)
BIN="$(cd "$HERE" && swift build -c release --arch arm64 --show-bin-path)/InFocusSetup"

# 2. App bundle skeleton.
rm -rf "$APP" && mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/Fonts"
cp "$BIN" "$APP/Contents/MacOS/InFocusSetup"
sed -e "s|@VERSION@|$VERSION|g" -e "s|@PORTAL@|$PORTAL|g" -e "s|@BUNDLE_ID@|$BUNDLE_ID|g" \
  "$HERE/support/Info.plist.in" > "$APP/Contents/Info.plist"
plutil -lint "$APP/Contents/Info.plist" >/dev/null

RES="$APP/Contents/Resources"

# 3. The editor source (tracked + untracked, never ignored files) for the installer scripts.
say "Copying the editor source"
mkdir -p "$RES/infocus-editor"
(cd "$REPO" && git ls-files -z --cached --others --exclude-standard -- . ':!setup-app' \
  | rsync -a --from0 --files-from=- ./ "$RES/infocus-editor/")
# The setup app itself isn't needed on the Mac, but keep the docs consistent.
chmod +x "$RES/infocus-editor/install/"*.sh "$RES/infocus-editor/install/infocus-root" 2>/dev/null || true

# 4. The editor as a git bundle. A release build comes from a tagged commit, so the
#    installed Mac shares history with GitHub and later releases fetch as fast-forwards.
say "Building infocus-editor.bundle"
head_tag="$(git -C "$REPO" describe --tags --exact-match --match 'v*' HEAD 2>/dev/null || true)"
dirty="$(git -C "$REPO" status --porcelain 2>/dev/null || echo dirty)"
if [ -n "$head_tag" ] && [ -z "$dirty" ]; then
  git -C "$REPO" bundle create -q "$RES/infocus-editor.bundle" HEAD "refs/tags/$head_tag"
  say "  bundle from the real repo at $head_tag"
else
  cat >&2 <<EOF
WARNING: this build is for TESTING ONLY. The repo isn't a clean checkout of a v* tag,
         so the bundle is a single fresh commit tagged v$VERSION with no shared history.
         A release build must come from a clean, tagged commit (git tag v$VERSION).
EOF
  rsync -a "$RES/infocus-editor/" "$WORK/src/"
  (cd "$WORK/src" && git init -q -b main && git add -A \
    && git -c user.name="InFocus" -c user.email="noreply@example.org" commit -qm "infocus-editor v$VERSION (test build)" \
    && git tag "v$VERSION" && git bundle create -q "$RES/infocus-editor.bundle" --all)
fi
git bundle verify -q "$RES/infocus-editor.bundle" >/dev/null 2>&1 || git bundle verify "$RES/infocus-editor.bundle"

# 5. The trimmed graphics package (only what the skill reads).
say "Copying the trimmed graphics package"
mkdir -p "$RES/package/_Working Files"
for item in "README.md" "01 Logos" "02 Fonts" "03 Intro & Outro" "04 Virtual Sets" "08 Follow InFocus" "09 TV Standby"; do
  [ -e "$PACKAGE/$item" ] && cp -R "$PACKAGE/$item" "$RES/package/"
done
[ -d "$PACKAGE/_Working Files/Graphics Source" ] && cp -R "$PACKAGE/_Working Files/Graphics Source" "$RES/package/_Working Files/"
[ -f "$PACKAGE/_Working Files/NOTES.md" ] && cp "$PACKAGE/_Working Files/NOTES.md" "$RES/package/_Working Files/"
for need in "03 Intro & Outro" "02 Fonts" "_Working Files/Graphics Source"; do
  [ -e "$RES/package/$need" ] || { echo "error: the package is missing '$need'" >&2; exit 1; }
done
cp "$PACKAGE/02 Fonts/"Lexend-*.ttf "$RES/Fonts/" 2>/dev/null || true

# 6. Icon from the package's channel avatar.
icon_src="$PACKAGE/01 Logos/Icon/infocus-channel-avatar-1800.png"
if [ -f "$icon_src" ]; then
  set_dir="$WORK/AppIcon.iconset"; mkdir -p "$set_dir"
  for s in 16 32 128 256 512; do
    sips -z "$s" "$s" "$icon_src" --out "$set_dir/icon_${s}x${s}.png" >/dev/null
    sips -z $((s * 2)) $((s * 2)) "$icon_src" --out "$set_dir/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$set_dir" -o "$RES/AppIcon.icns"
fi

find "$APP" \( -name .DS_Store -o -name __pycache__ \) -prune -exec rm -rf {} +
xattr -cr "$APP"

if [ "$MODE" = unsigned ]; then
  codesign --force --sign - "$APP" >/dev/null 2>&1 || true   # ad-hoc, so it launches locally
  say "Built (unsigned): $APP"
  exit 0
fi

# 7. Sign inside out: nested Mach-O files and executable scripts, then the app.
say "Loading the Developer ID from 1Password"
signing_load
say "Signing nested code"
nested=0
while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q 'Mach-O' || head -c 2 "$f" | grep -q '#!'; then
    sign_path "$f" >/dev/null 2>&1 || { echo "error: couldn't sign ${f#"$APP"/}" >&2; exit 1; }
    nested=$((nested + 1))
  fi
done < <(find "$RES" -type f -perm -u+x -print0)
say "  signed $nested nested files"
sign_path --identifier "$BUNDLE_ID" "$APP"
codesign --verify --strict --deep --verbose=2 "$APP"
codesign -dv --verbose=2 "$APP" 2>&1 | grep -E '^(Identifier|Authority|TeamIdentifier|Timestamp|Runtime|Format)'

if [ "$MODE" = sign ]; then
  say "Signed (not notarized): $APP"
  exit 0
fi

# 8. Notarize and staple the app, then the DMG that carries it.
say "Notarizing the app"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$WORK/app.zip"
notarize "$WORK/app.zip"
xcrun stapler staple -q "$APP"
xcrun stapler validate -q "$APP"

say "Building the DMG"
mkdir -p "$DIST" "$WORK/dmg"
DMG="$DIST/$NAME.dmg"
rm -f "$DMG"
cp -R "$APP" "$WORK/dmg/"
ln -s /Applications "$WORK/dmg/Applications"
hdiutil create -quiet -volname "$NAME" -srcfolder "$WORK/dmg" -ov -format UDZO "$DMG"
codesign --force --sign "$SIGN_IDENTITY" --keychain "$SIGN_KEYCHAIN" --timestamp "$DMG"

say "Notarizing the DMG"
notarize "$DMG"
xcrun stapler staple -q "$DMG"
xcrun stapler validate -q "$DMG"

say "Gatekeeper checks"
spctl --assess --type execute --verbose=2 "$APP" 2>&1
spctl -a -t open --context context:primary-signature -v "$DMG" 2>&1
say "Release ready: $DMG"
