#!/usr/bin/env bash
# Build only. Publishing and promotion deliberately belong to the organization build
# identity and fleet control respectively.
set -euo pipefail

usage() {
  echo "usage: $0 --build-image REF@sha256:... --runtime-image REF@sha256:... --webui-image REF@sha256:... --image TAG --upstream-version vX.Y.Z --source-commit SHA" >&2
  exit 64
}

build_image='' runtime_image='' webui_image='' image='' upstream_version='' source_commit=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    --build-image) build_image=${2-}; shift 2 ;;
    --runtime-image) runtime_image=${2-}; shift 2 ;;
    --webui-image) webui_image=${2-}; shift 2 ;;
    --image) image=${2-}; shift 2 ;;
    --upstream-version) upstream_version=${2-}; shift 2 ;;
    --source-commit) source_commit=${2-}; shift 2 ;;
    *) usage ;;
  esac
done

for value in "$build_image" "$runtime_image" "$webui_image" "$image" "$upstream_version" "$source_commit"; do
  [ -n "$value" ] || usage
done
case "$build_image,$runtime_image,$webui_image" in
  *@sha256:*,*@sha256:*,*@sha256:*) ;;
  *) echo "build and runtime images must be immutable digests" >&2; exit 65 ;;
esac
if ! [[ "$source_commit" =~ ^[0-9a-f]{40}$ ]]; then
  echo "source commit must be 40 lowercase hexadecimal characters" >&2
  exit 65
fi

docker buildx build --platform linux/amd64 --load \
  --build-arg "BUILD_IMAGE=$build_image" \
  --build-arg "RUNTIME_IMAGE=$runtime_image" \
  --build-arg "WEBUI_IMAGE=$webui_image" \
  --build-arg "SOURCE_COMMIT=$source_commit" \
  --build-arg "UPSTREAM_VERSION=$upstream_version" \
  --tag "$image" --file overlay/Dockerfile .
