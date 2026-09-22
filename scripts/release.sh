#!/bin/sh
# Tag HEAD with the current __version__ and publish a matching GitHub
# release. Run this right after committing and pushing a version bump --
# fm-pcc's in-app update check (/update, and the statusline button) reads
# GitHub's *latest release*, not the default branch, so a version that
# never gets tagged here is invisible to it.
set -e

cd "$(dirname "$0")/.."

version=$(python3 -c "import re; print(re.search(r'__version__ = \"([^\"]+)\"', open('src/fm_pcc/__init__.py').read()).group(1))")
tag="v${version}"

if git rev-parse "$tag" >/dev/null 2>&1; then
    echo "error: tag $tag already exists" >&2
    exit 1
fi

subject=$(git log -1 --format=%s)
notes_file=$(mktemp)
trap 'rm -f "$notes_file"' EXIT

{
    echo "## Install this version"
    echo ""
    echo "    uv tool install \"git+https://github.com/justwaters/fm-pcc@${tag}\""
    echo ""
    git log -1 --format=%B
} > "$notes_file"

git tag -a "$tag" -m "$subject"
git push origin "$tag"
gh release create "$tag" --verify-tag --title "${tag} — ${subject}" --notes-file "$notes_file"

echo "released ${tag}"
