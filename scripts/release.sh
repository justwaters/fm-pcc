#!/bin/sh
# Tag HEAD with the current __version__ and publish a matching GitHub
# release. Run this right after committing and pushing a version bump --
# fm-pcc's in-app update check (/update, and the statusline button) reads
# GitHub's *latest release*, not the default branch, so a version that
# never gets tagged here is invisible to it.
#
# Usage: scripts/release.sh [changelog-file]
#
# The release title is just "vX.XX" -- version history belongs on the
# releases page, not repeated in every title.
#
# changelog-file, if given, should already be formatted as one or more of:
#
#   ## New
#   - ...
#
#   ## Improved
#   - ...
#
#   ## Fixed
#   - ...
#
# only including the sections that actually apply, each bulleted. This is
# a judgment call (what's New vs. Improved vs. Fixed) that has to be made
# by whoever's cutting the release, not derived automatically -- so when
# changelog-file is omitted, this falls back to the latest commit's own
# message verbatim, which is fine for a release that's just one commit
# but is NOT a substitute for real categorization on a release bundling
# several.
set -e

cd "$(dirname "$0")/.."

version=$(python3 -c "import re; print(re.search(r'__version__ = \"([^\"]+)\"', open('src/fm_pcc/__init__.py').read()).group(1))")
tag="v${version}"

if git rev-parse "$tag" >/dev/null 2>&1; then
    echo "error: tag $tag already exists" >&2
    exit 1
fi

# Every commit already ran the fast suite (pre-commit hook); the slow
# real-model suite only gates releases, since it takes minutes.
echo "release: running full test suite before tagging ${tag}…"
tests/run.sh all

notes_file=$(mktemp)
trap 'rm -f "$notes_file"' EXIT

{
    echo "## Install this version"
    echo ""
    echo "    uv tool install \"git+https://github.com/justwaters/fm-pcc@${tag}\""
    echo ""
    if [ -n "$1" ]; then
        cat "$1"
    else
        git log -1 --format=%B
    fi
} > "$notes_file"

git tag -a "$tag" -m "$tag"
git push origin "$tag"
gh release create "$tag" --verify-tag --title "$tag" --notes-file "$notes_file"

echo "released ${tag}"
