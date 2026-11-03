#!/usr/bin/env bash
set -euo pipefail
root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
schema="$root/overlay/promotion-manifest.schema.json"

python3 - "$schema" <<'PY'
import json
import re
import sys

schema = json.load(open(sys.argv[1], encoding="utf-8"))
props = schema["properties"]
valid = {
    "schema_version": 1,
    "source_repo": "https://github.com/eMobility-Innovations/traefik",
    "upstream_version": "v3.7.17",
    "upstream_commit": "04e8ccf8" + "0" * 32,
    "patch_commit": "1" * 40,
    "image_ref": "registry.fiszu.com/emobility/traefik@sha256:" + "2" * 64,
    "platform": "linux/amd64",
    "tests_passed": True,
    "tls_canary_passed": True,
    "provenance": {"build_recipe_sha256": "3" * 64, "build_timestamp_utc": "2026-11-03T00:00:00Z"},
}

def accepts(value):
    if set(value) != set(schema["required"]): return False
    for name, rule in props.items():
        item = value[name]
        if "const" in rule and item != rule["const"]: return False
        if "pattern" in rule and not re.fullmatch(rule["pattern"], item): return False
    provenance = value["provenance"]
    p = props["provenance"]
    return set(provenance) == set(p["required"]) and bool(re.fullmatch(p["properties"]["build_recipe_sha256"]["pattern"], provenance["build_recipe_sha256"]))

assert accepts(valid)
for key, value in (("image_ref", "registry.fiszu.com/emobility/traefik:v3.7.17"), ("platform", "linux/arm64"), ("tests_passed", False), ("tls_canary_passed", False), ("upstream_commit", "short")):
    mutated = valid.copy(); mutated[key] = value
    assert not accepts(mutated), key
PY
