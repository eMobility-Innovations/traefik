#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -r -- "$work"' EXIT
mkdir -p "$work/script"
cp "$repo/script/validate-shell-script.sh" "$work/script/validate-shell-script.sh"
# Model a job-table query after all fast children have already completed.
mkdir -p "$work/bin"
cat > "$work/bin/shellcheck" <<'SHIM'
#!/usr/bin/env bash
printf 'checked\n' >> "$SHELLCHECK_AUDIT"
exit 37
SHIM
chmod 700 "$work/bin/shellcheck"
cat > "$work/job-table.bashenv" <<'ENV'
jobs() {
    builtin wait
    # A completed-jobs snapshot is permitted to be empty.
    :
}
ENV
assert_fast_failure() {
    local status=0
    PATH="$work/bin:$PATH" BASH_ENV="$work/job-table.bashenv" \
        SHELLCHECK_AUDIT="$work/checks" bash "$1" || status=$?
    if [[ $status != 37 ]]; then
        echo 'completed shellcheck failure was lost' >&2
        return 1
    fi
    [[ -s $work/checks ]]
}
assert_fast_failure "$work/script/validate-shell-script.sh"
# Returning to a late job-table lookup must lose the completed child's failure.
sed "s/for p in .*/for p in \$(jobs -p)/" \
    "$work/script/validate-shell-script.sh" > "$work/script/untracked.sh"
if assert_fast_failure "$work/script/untracked.sh"; then
    echo 'removed PID tracking was not detected' >&2
    exit 1
fi
rm "$work/script/untracked.sh"
cat > "$work/fixture.py" <<'PY'
"""Embedded shell fixture is Python, not a shell program."""
fixture = "#!/bin/sh\necho hello\n"
PY
printf '#!/usr/bin/env bash\ntrue\n' > "$work/valid.sh"
bash "$work/script/validate-shell-script.sh"
printf '#!/bin/sh\nif then\n' > "$work/invalid.sh"
if bash "$work/script/validate-shell-script.sh"; then
    echo 'invalid real shell script was accepted' >&2
    exit 1
fi
rm "$work/invalid.sh"
# Removing the first-line guard must recreate the embedded-shebang false positive.
sed '/# First-line guard begin/,/# First-line guard end/d' \
    "$work/script/validate-shell-script.sh" > "$work/script/mutated.sh"
if bash "$work/script/mutated.sh"; then
    echo 'removed first-line guard was not detected' >&2
    exit 1
fi
echo 'shell detection regression and mutation checks passed'
