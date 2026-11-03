#!/usr/bin/env bash
set -euo pipefail
export REAL_GO REAL_MAKE
REAL_GO=$(command -v go)
REAL_MAKE=$(command -v make)
repo=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -r -- "$work"' EXIT
mkdir -p "$work/repo/overlay" "$work/repo/.github/workflows" "$work/bin" "$work/tmp"
cp "$repo/overlay/validate.sh" "$work/repo/overlay/validate.sh"
cat > "$work/repo/.github/workflows/validate.yaml" <<'PINS'
env:
  GOLANGCI_LINT_VERSION: v2.14.0
  MISSPELL_VERSION: v0.7.0
PINS
cat > "$work/bin/go" <<'GO'
#!/usr/bin/env bash
set -euo pipefail
[[ $GOMAXPROCS == 4 && $GOFLAGS == -p=4 ]]
[[ $GOSUMDB == sum.golang.org && -z ${GONOSUMDB:-} && -z ${GOPRIVATE:-} && $GOTOOLCHAIN == local ]]
effective=$("$REAL_GO" env GONOSUMDB GOPRIVATE)
[[ -z $effective ]]
[[ $1 == install && $2 == -p && $3 == 4 ]]
case $4 in
 github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.14.0) tool=golangci-lint ;;
 github.com/golangci/misspell/cmd/misspell@v0.7.0) tool=misspell ;;
 *) exit 91 ;;
esac
[[ $GOBIN == "$TMPDIR"/traefik-validate.*/"$tool"-v*/bin ]]
[[ $(stat -c %a "$GOBIN") == 700 ]]
printf '#!/bin/sh\nexit 0\n' > "$GOBIN/$tool"
chmod 700 "$GOBIN/$tool"
printf '%s\n' "$4" >> "$AUDIT"
GO
cat > "$work/bin/make" <<'MAKE'
#!/usr/bin/env bash
set -euo pipefail
[[ $* == validate ]]
[[ $GOMAXPROCS == 4 && $GOFLAGS == -p=4 ]]
[[ $GOENV == $HOSTILE_GOENV ]]
[[ $PWD == $EXPECTED_REPO ]]

[[ $(command -v golangci-lint) == "$TMPDIR"/traefik-validate.*/golangci-lint-v2.14.0/bin/golangci-lint ]]
[[ $(command -v misspell) == "$TMPDIR"/traefik-validate.*/misspell-v0.7.0/bin/misspell ]]
if [[ ${MAKE_EXIT:-0} != 0 ]]; then
    exit "$MAKE_EXIT"
fi
"$REAL_MAKE" "$@"
MAKE
printf '#!/bin/sh\nexit 0\n' > "$work/bin/shellcheck"
chmod 700 "$work/bin/"*
cat > "$work/repo/Makefile" <<'MAKEFILE'
validate:
	@test "$$EXPECTED_MAKEFILE" = protected
	@printf 'make validate\n' >> "$$AUDIT"
MAKEFILE
export EXPECTED_REPO="$work/repo" EXPECTED_MAKEFILE=protected
export AUDIT="$work/audit" TMPDIR="$work/tmp"
export HOSTILE_GOENV="$work/hostile-go.env"
printf 'GOSUMDB=off\nGONOSUMDB=*\nGOPRIVATE=*\n' > "$HOSTILE_GOENV"
export GOENV="$HOSTILE_GOENV"
export GOMAXPROCS=99 GOFLAGS=-p=99
export GOSUMDB=off GOPRIVATE='*' GONOSUMDB='*'
PATH="$work/bin:$PATH" bash "$work/repo/overlay/validate.sh"
[[ $(wc -l < "$AUDIT") == 3 ]]
[[ -z $(find "$TMPDIR" -mindepth 1 -print -quit) ]]
mkdir -p "$work/candidate/.github/workflows"
cat > "$work/candidate/Makefile" <<'MAKEFILE'
validate:
	@test "$$EXPECTED_MAKEFILE" = candidate
	@printf 'make validate\n' >> "$$AUDIT"
MAKEFILE
printf 'env:\n  GOLANGCI_LINT_VERSION: v99.0.0\n  MISSPELL_VERSION: v99.0.0\n' > "$work/candidate/.github/workflows/validate.yaml"
PATH="$work/bin:$PATH" EXPECTED_REPO="$work/candidate" EXPECTED_MAKEFILE=candidate \
    bash "$work/repo/overlay/validate.sh" "$work/candidate"
before=$(wc -l < "$AUDIT")
for args in missing extra; do
    if [[ $args == missing ]]; then
        set -- "$work/not-a-directory"
    else
        set -- "$work/candidate" "$work/candidate"
    fi
    if PATH="$work/bin:$PATH" bash "$work/repo/overlay/validate.sh" "$@"; then
        echo 'invalid candidate arguments accepted' >&2; exit 1
    fi
    [[ $(wc -l < "$AUDIT") == "$before" ]]
done

if PATH="$work/bin:$PATH" MAKE_EXIT=37 bash "$work/repo/overlay/validate.sh"; then
 echo 'make failure was swallowed' >&2;exit 1
else
 [[ $? == 37 ]]
fi
[[ -z $(find "$TMPDIR" -mindepth 1 -print -quit) ]]
printf 'env:\n  GOLANGCI_LINT_VERSION: latest\n  MISSPELL_VERSION: v0.7.0\n' > "$work/repo/.github/workflows/validate.yaml"
before=$(wc -l < "$AUDIT")
if PATH="$work/bin:$PATH" bash "$work/repo/overlay/validate.sh"; then
 echo 'unpinned tool version accepted' >&2;exit 1
fi
[[ $(wc -l < "$AUDIT") == "$before" ]]
echo 'validation bootstrap tests passed'
