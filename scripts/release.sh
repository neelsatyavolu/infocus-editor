#!/bin/bash
# Publish an update: every installed editor moves to it within ~15 minutes, unless the
# Portal pins another version (SHOW_EDITOR_TARGET_VERSION / the panel's update pin).
#   scripts/release.sh <version>      e.g. 0.2.0 or v0.2.0
# Needs a clean tree on main, uv, and an authenticated `gh`.
set -euo pipefail

cd "$(dirname "$0")/.."
die() { printf 'release: %s\n' "$*" >&2; exit 1; }

[ $# -eq 1 ] || die "usage: scripts/release.sh <version>"
VERSION="${1#v}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.]+)?$ ]] || die "not a version: $1"
TAG="v$VERSION"

[ -z "$(git status --porcelain)" ] || die "the working tree has changes; commit or stash them first"
[ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] || die "release from main"
git fetch --quiet --tags origin
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "$TAG already exists"
command -v gh >/dev/null || die "the GitHub CLI (gh) is needed"

# Bump the version, refresh the lock, run the tests that need no private fixtures or AI account.
sed -i '' -E "s/^version = \"[^\"]+\"/version = \"$VERSION\"/" pyproject.toml
grep -q "^version = \"$VERSION\"" pyproject.toml || die "couldn't set the version in pyproject.toml"
uv lock --quiet
uv run pytest -q -m "not golden and not fixtures"

git add pyproject.toml uv.lock
git commit -q -m "release $TAG"
if git config user.signingkey >/dev/null; then
  git tag -s "$TAG" -m "$TAG"
else
  git tag -a "$TAG" -m "$TAG"
fi
git push --quiet origin HEAD "$TAG"
gh release create "$TAG" --generate-notes --verify-tag
echo "Released $TAG. Editors update within about 15 minutes (or pin a version in the Portal)."
