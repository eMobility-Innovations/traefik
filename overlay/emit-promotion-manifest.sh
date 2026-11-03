#!/usr/bin/env bash
# Emits the candidate's evidence only after the build and TLS canary have passed.
# It deliberately cannot publish an image or overwrite fleet control's last-good file.
set -euo pipefail

usage() {
  echo "usage: $0 --image-ref REF@sha256:... --upstream-version vX.Y.Z --upstream-commit SHA --patch-commit SHA --output FILE" >&2
  exit 64
}

image_ref= upstream_version= upstream_commit= patch_commit= output=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --image-ref) image_ref=${2-}; shift 2 ;;
    --upstream-version) upstream_version=${2-}; shift 2 ;;
    --upstream-commit) upstream_commit=${2-}; shift 2 ;;
    --patch-commit) patch_commit=${2-}; shift 2 ;;
    --output) output=${2-}; shift 2 ;;
    *) usage ;;
  esac
done
for value in "$image_ref" "$upstream_version" "$upstream_commit" "$patch_commit" "$output"; do
  [ -n "$value" ] || usage
done
[[ "$image_ref" =~ ^[a-z0-9][a-z0-9./_-]*@sha256:[0-9a-f]{64}$ ]] || exit 65
[[ "$upstream_version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || exit 65
[[ "$upstream_commit" =~ ^[0-9a-f]{40}$ && "$patch_commit" =~ ^[0-9a-f]{40}$ ]] || exit 65

recipe_sha=$(sha256sum overlay/Dockerfile | awk '{print $1}')
umask 077
tmp=$(mktemp "${output}.tmp.XXXXXX")
trap 'rm -f "$tmp"' EXIT
printf '{\n  "schema_version": 1,\n  "source_repo": "https://github.com/eMobility-Innovations/traefik",\n  "upstream_version": "%s",\n  "upstream_commit": "%s",\n  "patch_commit": "%s",\n  "image_ref": "%s",\n  "platform": "linux/amd64",\n  "tests_passed": true,\n  "tls_canary_passed": true,\n  "provenance": {"build_recipe_sha256": "%s", "build_timestamp_utc": "%s"}\n}\n' \
  "$upstream_version" "$upstream_commit" "$patch_commit" "$image_ref" "$recipe_sha" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$tmp"
mv -f "$tmp" "$output"
trap - EXIT
