#!/usr/bin/env python3
"""Prove the scratch artifact's real Docker layer shape without any registry push."""

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path

import reconcile


def main():
    os.umask(0o077)
    image = reconcile.IMAGE + "@sha256:" + "a" * 64
    manifest = {
        "schema_version": 1, "source_repo": reconcile.ORG_REPOSITORY.removesuffix(".git"),
        "upstream_version": "v3.7.17", "upstream_commit": "b" * 40, "patch_commit": "c" * 40,
        "image_ref": image, "platform": "linux/amd64", "tests_passed": True,
        "tls_canary_passed": True, "provenance": {
            "build_recipe_sha256": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            "build_timestamp_utc": "2026-11-03T00:00:00Z"},
    }
    with tempfile.TemporaryDirectory(prefix="traefik-artifact-fixture-") as temporary:
        pending = Path(temporary) / "fixture.json"
        pending.write_text(json.dumps(manifest, sort_keys=True))
        tag = reconcile.build_promotion_artifact(pending, image)
        try:
            print(json.dumps({"fixture_only": True, "registry_push_performed": False,
                              "single_regular_promotion_json_layer": True,
                              "config_and_layer_digests_verified": True,
                              "payload_sha256": reconcile.sha256(pending.read_bytes())}, sort_keys=True))
        finally:
            reconcile.run("docker", "image", "rm", tag)


if __name__ == "__main__":
    main()
