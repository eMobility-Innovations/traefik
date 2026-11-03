#!/usr/bin/env bash
# Reconcile the dedicated builder from protected source and native machine inputs.
set -euo pipefail

ORG_REPOSITORY=https://github.com/eMobility-Innovations/traefik.git
SOURCE_COMMIT='' PRODUCER_INPUT='' PUBLISHER_INPUT='' DEST_ROOT=/
BASE='' MIRROR='' RELEASES='' TOOLS='' STATE='' CONFIG='' STAGING=''

_die() { printf 'builder install refused: %s\n' "$1" >&2; exit 1; }
_pending() { printf '{"status":"PENDING","installed":false,"reason":"%s"}\n' "$1"; }
_safe_path() {
  local path=$1 mode
  [[ $path == /* ]] || _die 'absolute path required'
  while :; do
    [[ -e $path && ! -L $path ]] || _die 'missing or symbolic trusted path'
    [[ $(stat -c %u "$path") == 0 ]] || _die 'trusted path must belong to root'
    mode=$(stat -c %a "$path")
    (( (8#$mode & 022) == 0 )) || _die 'trusted path permits non-root writes'
    [[ $path != / ]] || break
    path=$(dirname "$path")
  done
}
_private_file() {
  _safe_path "$1"
  [[ -f $1 && $(stat -c %a "$1") == 600 ]] || _die 'native JSON input must be a regular root-only file'
}
_private_dir() {
  if [[ ! -e $1 ]]; then _safe_path "$(dirname "$1")"; mkdir -m 700 "$1"; fi
  _safe_path "$1"
  [[ -d $1 ]] || _die 'private directory required'
  chmod 700 "$1"
}
_git() {
  env -i PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin LANG=C \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null \
    GIT_TERMINAL_PROMPT=0 GIT_NO_REPLACE_OBJECTS=1 GIT_GRAFT_FILE=/dev/null \
    git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.attributesFile=/dev/null \
    -c protocol.ext.allow=never "$@"
}
_write_mirror_config() {
  cat >"$1" <<'CONFIG'
[core]
    repositoryformatversion = 0
    filemode = true
    bare = true
[remote "origin"]
    url = https://github.com/eMobility-Innovations/traefik.git
    fetch = +refs/heads/main:refs/remotes/origin/main
CONFIG
  chmod 600 "$1"
}
_materialize_source() {
  local release=$RELEASES/$SOURCE_COMMIT
  if [[ ! -e $MIRROR ]]; then
    _git init --bare "$MIRROR" >/dev/null || _die 'cannot initialize controlled source mirror'
    _write_mirror_config "$MIRROR/config"
  fi
  _safe_path "$MIRROR"
  [[ -z $(find "$MIRROR" \( -type l -o ! -user root -o -perm /022 \) -printf x -quit) ]] || _die 'unsafe source mirror contents'
  _write_mirror_config "$STAGING/expected-mirror-config"
  cmp -s "$MIRROR/config" "$STAGING/expected-mirror-config" || _die 'source mirror configuration differs from the controlled policy'
  _git --git-dir="$MIRROR" fetch --no-tags "$ORG_REPOSITORY" \
    +refs/heads/main:refs/remotes/origin/main >/dev/null || _die 'cannot fetch protected organization source'
  _git --git-dir="$MIRROR" merge-base --is-ancestor "$SOURCE_COMMIT" refs/remotes/origin/main || _die 'source is not on protected organization main'
  if [[ ! -e $release ]]; then
    mkdir -m 700 "$STAGING/source"
    _git --git-dir="$MIRROR" archive "$SOURCE_COMMIT" overlay verify.sh REPO-CONTRACT.toml \
      .github/workflows/validate.yaml | tar -xf - -C "$STAGING/source" || _die 'cannot materialize protected source'
    chmod -R go-w "$STAGING/source"
    mv "$STAGING/source" "$release"
  fi
  _safe_path "$release"
  printf '%s\n' "$release"
}
_lock_value() {
  local value
  value=$(awk -F= -v name="$2" '$1 == name {print $2}' "$1/overlay/build-inputs.lock")
  [[ $value != *$'\n'* && -n $value ]] || _die 'missing or duplicate compiler lock value'
  printf '%s\n' "$value"
}
_download_go() {
  curl --fail --silent --show-error --location --proto '=https' \
    "https://go.dev/dl/$1.linux-amd64.tar.gz" --output "$2"
}
_install_go() {
  local version checksum destination archive
  version=$(_lock_value "$1" GO_VERSION)
  checksum=$(_lock_value "$1" GO_LINUX_AMD64_SHA256)
  [[ $version =~ ^go[0-9]+\.[0-9]+\.[0-9]+$ && $checksum =~ ^[a-f0-9]{64}$ ]] || _die 'invalid official compiler lock'
  destination=$TOOLS/$version
  if [[ -e $destination ]]; then
    _safe_path "$destination/bin/go"
    [[ $(GOTOOLCHAIN=local "$destination/bin/go" version) == "go version $version linux/amd64" ]] || _die 'installed dedicated compiler does not match its lock'
    [[ -f $destination/archive.sha256 && $(cat "$destination/archive.sha256") == "$checksum" ]] || _die 'installed compiler lacks matching archive attribution'
    return
  fi
  archive=$STAGING/go.tar.gz
  _download_go "$version" "$archive" || _die 'official compiler archive is unavailable'
  printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --status || _die 'official compiler archive checksum failed'
  mkdir -m 700 "$STAGING/compiler"
  tar -xzf "$archive" -C "$STAGING/compiler" || _die 'cannot unpack verified compiler'
  [[ $(GOTOOLCHAIN=local "$STAGING/compiler/go/bin/go" version) == "go version $version linux/amd64" ]] || _die 'verified compiler has an unexpected platform or version'
  printf '%s\n' "$checksum" >"$STAGING/compiler/go/archive.sha256"
  chmod -R go-w "$STAGING/compiler/go"
  mv "$STAGING/compiler/go" "$destination"
}
_validate_inputs() {
  local source=$1
  for name in reconcile.py build-inputs.lock traefik-readiness-gate validate.sh; do
    _safe_path "$source/overlay/$name"
  done
  _safe_path "$source/.github/workflows/validate.yaml"
  python3 - "$source" "$PRODUCER_INPUT" "$MIRROR" "$SOURCE_COMMIT" <<'PY'
import importlib.util
from pathlib import Path
import sys
source, config, mirror, commit = sys.argv[1:]
spec = importlib.util.spec_from_file_location("producer", Path(source) / "overlay/reconcile.py")
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)
value = producer.inputs(Path(config))
if value["source_checkout"] != mirror or value["overlay_commit"] != commit:
    raise ValueError("native producer input must identify the installed mirror and source commit")
PY
}
_atomic_file() {
  local temporary
  temporary=$(mktemp "$(dirname "$2")/.builder-input.XXXXXX")
  install -m "$3" "$1" "$temporary"
  mv -fT "$temporary" "$2"
}
_activate() {
  local source=$1 version=$2 sbin libexec
  sbin=${DEST_ROOT%/}/usr/local/sbin
  libexec=${DEST_ROOT%/}/usr/local/libexec
  mkdir -p "$sbin" "$libexec"
  _safe_path "$sbin"; _safe_path "$libexec"
  sed -e "s|@SOURCE_ROOT@|$source|g" -e "s|@TOOLS_ROOT@|$TOOLS|g" \
    -e "s|@MIRROR_ROOT@|$MIRROR|g" "$source/overlay/traefik-readiness-gate" >"$STAGING/gate"
  cat >"$STAGING/producer" <<WRAPPER
#!/bin/sh
set -eu
[ "\$#" -eq 0 ] || exit 64
PATH='$TOOLS/$version/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'
DOCKER_CONFIG='$STATE/docker'
export PATH DOCKER_CONFIG
exec /usr/bin/python3 '$source/overlay/reconcile.py' --config '$CONFIG/producer.json' --state '$STATE'
WRAPPER
  _atomic_file "$PRODUCER_INPUT" "$CONFIG/producer.json" 600
  _atomic_file "$PUBLISHER_INPUT" "$STATE/docker/config.json" 600
  _atomic_file "$STAGING/gate" "$libexec/traefik-readiness-gate" 555
  _atomic_file "$STAGING/producer" "$sbin/traefik-readiness-reconcile" 555
  printf '{"schema_version":1,"source_commit":"%s","go_version":"%s"}\n' "$SOURCE_COMMIT" "$version" >"$STAGING/receipt"
  _atomic_file "$STAGING/receipt" "$STATE/builder-installed.json" 600
}
main() {
  local source version argument
  while [[ $# -gt 0 ]]; do
    argument=$1; [[ $# -ge 2 ]] || _die 'installer arguments require explicit values'
    case $argument in
      --source-commit) SOURCE_COMMIT=$2 ;;
      --producer-config) PRODUCER_INPUT=$2 ;;
      --publisher-docker-config) PUBLISHER_INPUT=$2 ;;
      --dest-root)
        [[ ${BASH_SOURCE[0]} != "$0" && $2 == /root/traefik-builder-test-*/target ]] || _die 'destination override is private to the root fixture'
        DEST_ROOT=$2 ;;
      *) _die 'unknown installer argument' ;;
    esac
    shift 2
  done
  [[ $SOURCE_COMMIT =~ ^[a-f0-9]{40}$ ]] || _die 'a full protected source commit is required'
  if [[ ! -f $PRODUCER_INPUT ]]; then _pending producer-config-missing; return; fi
  if [[ ! -f $PUBLISHER_INPUT ]]; then _pending publisher-docker-config-missing; return; fi
  [[ $EUID == 0 ]] || _die 'root is required for the dedicated builder install'
  [[ $DEST_ROOT =~ ^/[A-Za-z0-9._/-]*$ ]] || _die 'unsafe destination root'
  case "$DEST_ROOT/" in */../*|*/./*) _die 'destination root must be canonical' ;; esac
  umask 077
  _safe_path "$DEST_ROOT"; _private_file "$PRODUCER_INPUT"; _private_file "$PUBLISHER_INPUT"
  BASE=${DEST_ROOT%/}/opt/traefik-readiness
  MIRROR=$BASE/source.git; RELEASES=$BASE/releases; TOOLS=$BASE/tools
  STATE=${DEST_ROOT%/}/var/lib/traefik-readiness; CONFIG=${DEST_ROOT%/}/etc/traefik-readiness
  mkdir -p "$(dirname "$BASE")" "$(dirname "$STATE")" "$(dirname "$CONFIG")"
  _private_dir "$BASE"; _private_dir "$RELEASES"; _private_dir "$TOOLS"
  _private_dir "$STATE"; _private_dir "$STATE/docker"; _private_dir "$CONFIG"
  if [[ -e $STATE/reconcile.lock || -L $STATE/reconcile.lock ]]; then _private_file "$STATE/reconcile.lock"; fi
  exec {builder_lock}>"$STATE/reconcile.lock"
  if ! flock -n "$builder_lock"; then _pending producer-active; return; fi
  STAGING=$(mktemp -d "$BASE/.install.XXXXXX")
  trap 'rm -rf "$STAGING"' EXIT
  source=$(_materialize_source) || _die 'protected source preparation failed'
  _validate_inputs "$source" || _die 'native producer input validation failed'
  version=$(_lock_value "$source" GO_VERSION)
  _install_go "$source" || _die 'compiler preparation failed'
  _activate "$source" "$version"
  printf '{"status":"INSTALLED","installed":true,"source_commit":"%s"}\n' "$SOURCE_COMMIT"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
