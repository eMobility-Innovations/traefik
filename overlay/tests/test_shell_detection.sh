#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -r -- "$work"' EXIT
mkdir -p "$work/script"
cp "$repo/script/validate-shell-script.sh" "$work/script/validate-shell-script.sh"
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
