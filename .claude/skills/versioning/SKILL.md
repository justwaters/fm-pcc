---
name: versioning
description: How fm-pcc ships a change. Covers version numbering (fixes-only releases take a patch number like v0.47.1, anything New or Improved takes the next minor like v0.48), the per-change workflow (test, reinstall, README, version bump, commit, push, release), and the GitHub release format and release notes (install command first, then New/Improved/Fixed sections). Use whenever finishing a change to fm-pcc, bumping __version__, committing, pushing, tagging, publishing a GitHub release, or writing release notes.
---

# Versioning and releases for fm-pcc

fm-pcc's in-app update check (the statusline **Update** button and `/update`) reads the **latest GitHub release**. It doesn't read the default branch. A version that's committed and pushed but never released is invisible to it. So every version bump ends in a GitHub release, made by `scripts/release.sh`.

## Version numbers

The version lives in one place: `__version__` in `src/fm_pcc/__init__.py`. The git tag and release title are `v` plus that value.

- **Only fixes** → bump the **patch** number: `0.47` → `0.47.1` → `0.47.2`.
- **Anything New or Improved** (even alongside fixes) → bump the **minor** number and drop the patch: `0.47.1` → `0.48`.
- Never reuse a version, and never skip one.
- To decide, look at the release notes you're about to write. If they have only a `## Fixed` section, it's a patch release. If they have `## New` or `## Improved`, it's a minor release.

Three-part versions compare correctly in the update check (`_version_tuple` / `is_newer`): 0.47 < 0.47.1 < 0.48.

## The per-change workflow

Every finished change goes through all of these steps, in this order:

1. **Implement**, then **test**: the real backends plus the headless Textual `run_test()` harness. Add regression tests to `tests/fast/` (mocked, offline) or `tests/slow/` (real on-device model), never to `/tmp`. Run `tests/run.sh fast`, and `tests/run.sh slow` when the change touches `/task`, `/ask`, or anything else model-driven.
2. **Reinstall and smoke-test.** The user's `fm-pcc` is an editable install of this checkout:
   ```
   find . -name "__pycache__" -exec rm -rf {} +; uv tool install -e . --force
   fm-pcc respond -m on-device "say hi"
   ```
3. **Update `README.md`** for anything user-visible.
4. **Bump `__version__`** per the rules above.
5. **Commit** with a detailed message: what changed and why, and what was measured or verified. **No attribution lines** (no `Co-Authored-By`, no "Generated with"). The pre-commit hook (`scripts/git-hooks/pre-commit`, enabled with `git config core.hooksPath scripts/git-hooks`) runs the fast suite and blocks the commit on any failure. Don't bypass it; fix the failure.
6. **Push**: `git push`.
7. **Release**: write the release notes to a file (use `$CLAUDE_JOB_DIR/tmp/` or another scratch location, not the repo), then run:
   ```
   ./scripts/release.sh <notes-file>
   ```

Changes that don't touch the app (docs-only edits, repo tooling like this skill) are committed and pushed without a version bump or release.

## What `scripts/release.sh` does

Always use the script. Don't hand-roll `git tag` or `gh release create`: the script is the source of truth for the format, and ad-hoc commands drift from it. It:

1. Reads `__version__` and refuses if tag `v<version>` already exists.
2. Runs the **full** test suite (`tests/run.sh all`, fast and slow) and stops before tagging if anything fails.
3. Builds the notes: an install header, then the notes file (or the last commit message if no file is given; that's only acceptable for a release that's genuinely one simple commit).
4. Creates the annotated tag `v<version>`, pushes it, and runs `gh release create v<version> --verify-tag --title v<version>`.

## Release format

- **Title** is just the tag, e.g. `v0.48`, with no subject suffix. The script sets it.
- **Body** starts with the pinned install command, which the script adds:
  ```
  ## Install this version

      uv tool install "git+https://github.com/justwaters/fm-pcc@v0.48"
  ```
- **Then the notes**, in `## New`, `## Improved` and `## Fixed` sections, in that order, each a bulleted list. **Include only the sections that apply**; never add an empty section. Sorting changes into these sections is a judgment call you make for each release. Don't derive it mechanically from commit messages.
  - **New**: a capability that didn't exist before (a command, an action, a mode).
  - **Improved**: something that already worked, now working better (more reliable, faster, clearer, broader).
  - **Fixed**: something that was broken or wrong.

## Writing the notes

Release notes are for people using fm-pcc, not for whoever wrote the code:

- Describe what changed for the user, in plain words. Use command names and quoted phrasings a user would type (`/export`, "you do it"), not function names or internals.
- One idea per bullet. Lead with the change, then add the reason or the effect if it helps.
- Give concrete numbers when they exist ("went from 7 of 24 to 24 of 24").
- For a Fixed bullet, say what used to happen ("failed with 'HTTP Error 403: rate limit exceeded'") and what happens now.
- Keep implementation detail in the commit message, not the release notes.

Example (a minor release, since it has New and Improved sections):

```
## New

- `/export` saves the session transcript as Markdown, plain text, or JSON, or copies it with `/export copy`.

## Improved

- Chat hands clear change requests to `/task` instead of saying it can't edit files.

## Fixed

- `/push` no longer fails on a branch that's never been pushed; it publishes it to your remote.
```

## Changing a published release

Leave history alone. Earlier releases have older formats (titles like `v0.30 — subject`, and no install header on v0.1.0–v0.36); don't rewrite them.

Only renumber or delete a published release when the user asks. It happened once: the fixes-only v0.48 became v0.47.1. When it's asked for:
1. Set `__version__` to the new number, then commit and push.
2. `gh release delete <old-tag> --cleanup-tag --yes`. This also removes the tag remotely; delete a leftover local tag with `git tag -d <old-tag>` only if it still exists.
3. Run `./scripts/release.sh` with the same notes file.
4. Check the result with `gh release list`.
