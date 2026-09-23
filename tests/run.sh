#!/bin/sh
# Run fm-pcc's test suites.
#
# Usage: tests/run.sh [fast|slow|all]   (default: fast)
#
#   fast  mocked/offline tests (tests/fast/) -- seconds; run on every commit
#         by scripts/git-hooks/pre-commit
#   slow  real on-device model runs (tests/slow/) -- minutes; run by
#         scripts/release.sh before tagging, or on demand
#   all   both
#
# Every test file is a standalone script that exits non-zero on failure.
# All of them run even if an earlier one fails, so one run shows every
# failure; the exit status is non-zero if any failed.
set -u

cd "$(dirname "$0")/.."

suite=${1:-fast}
case "$suite" in
    fast) dirs="tests/fast" ;;
    slow) dirs="tests/slow" ;;
    all)  dirs="tests/fast tests/slow" ;;
    *) echo "usage: tests/run.sh [fast|slow|all]" >&2; exit 2 ;;
esac

log=$(mktemp)
trap 'rm -f "$log"' EXIT

passed=0
failed=""
for dir in $dirs; do
    for test in "$dir"/test_*.py; do
        start=$(date +%s)
        # A fresh FM_PCC_HOME per test keeps tests away from the real
        # ~/.fm-pcc (saved sessions, and state.json's remembered
        # unavailable tiers, which would otherwise change outcomes) and
        # from each other.
        FM_PCC_HOME=$(mktemp -d)
        export FM_PCC_HOME
        if uv run --quiet --with textual --with rich python3 "$test" >"$log" 2>&1; then
            passed=$((passed + 1))
            echo "  ok    $test ($(( $(date +%s) - start ))s)"
        else
            failed="$failed $test"
            echo "  FAIL  $test ($(( $(date +%s) - start ))s)"
            sed 's/^/        /' "$log" | tail -n 30
        fi
        rm -rf "$FM_PCC_HOME"
    done
done

if [ -n "$failed" ]; then
    echo "$suite: $passed passed, $(echo $failed | wc -w | tr -d ' ') failed:$failed"
    exit 1
fi
echo "$suite: all $passed passed"
