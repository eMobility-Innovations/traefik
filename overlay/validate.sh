#!/usr/bin/env bash
# Keep vendor validation dependencies private to this invocation.
set -euo pipefail
umask 077
repo=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo"
command -v go >/dev/null
command -v shellcheck >/dev/null
pins="$repo/.github/workflows/validate.yaml"
lint_version=$(awk '$1 == "GOLANGCI_LINT_VERSION:" {print $2}' "$pins")
spell_version=$(awk '$1 == "MISSPELL_VERSION:" {print $2}' "$pins")
for version in "$lint_version" "$spell_version"; do
    if [[ ! $version =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo 'Vendor validation tool version must be an exact stable tag.' >&2
        exit 2
    fi
done
validation_dir=$(mktemp -d "${TMPDIR:-/tmp}/traefik-validate.XXXXXXXX")
trap 'rm -r -- "$validation_dir"' EXIT
lint_bin="$validation_dir/golangci-lint-$lint_version/bin"
spell_bin="$validation_dir/misspell-$spell_version/bin"
mkdir -p "$lint_bin" "$spell_bin"
# Module checksums remain mandatory even when a caller has private-module settings.
GOBIN="$lint_bin" GOSUMDB=sum.golang.org GONOSUMDB= GOPRIVATE= GOTOOLCHAIN=local \
    go install -p 4 "github.com/golangci/golangci-lint/v2/cmd/golangci-lint@$lint_version"
GOBIN="$spell_bin" GOSUMDB=sum.golang.org GONOSUMDB= GOPRIVATE= GOTOOLCHAIN=local \
    go install -p 4 "github.com/golangci/misspell/cmd/misspell@$spell_version"
PATH="$lint_bin:$spell_bin:$PATH" make validate
