#!/usr/bin/env python3
"""Build the latest stable release plus reviewed patches from an existing fleet tick."""

import argparse
import fcntl
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

REPOSITORY = "https://github.com/traefik/traefik.git"
ORG_REPOSITORY = "https://github.com/eMobility-Innovations/traefik.git"
OVERLAY_PATHS = ("overlay", "verify.sh", "REPO-CONTRACT.toml")
REQUIRED_OVERLAY = {"overlay/Dockerfile", "overlay/build-image.sh", "overlay/build-inputs.lock",
                    "overlay/emit-promotion-manifest.sh", "overlay/verify_image_startup.py",
                    "overlay/promotion-manifest.schema.json", "overlay/reconcile.py",
                    "overlay/tests/test_reconcile.py", "overlay/tests/test_promotion_manifest.sh",
                    "verify.sh", "REPO-CONTRACT.toml"}
IMAGE = "registry.fiszu.com/emobility/traefik"
VERSION = re.compile(r"refs/tags/v(\d+)\.(\d+)\.(\d+)$")
DIGEST = re.compile(r"[a-z0-9][a-z0-9./_-]*@sha256:[a-f0-9]{64}$")


def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE).stdout.strip()


def latest_release(refs):
    releases = []
    for row in refs.splitlines():
        commit, ref = row.split()
        match = VERSION.fullmatch(ref)
        if match:
            releases.append((tuple(map(int, match.groups())), commit, ref.removeprefix("refs/tags/")))
    if not releases:
        raise ValueError("upstream returned no complete stable release tags")
    return max(releases)


def inputs(path):
    config = json.loads(path.read_text())
    required = {"source_checkout", "overlay_commit", "patch_commits", "build_image", "runtime_image", "webui_image", "gate_command"}
    if set(config) != required or not config["patch_commits"]:
        raise ValueError("producer configuration must declare all build inputs and patches")
    for key in ("build_image", "runtime_image", "webui_image"):
        if not DIGEST.fullmatch(config[key]):
            raise ValueError("build inputs must be immutable image references")
    if not all(re.fullmatch(r"[a-f0-9]{40}", item) for item in [config["overlay_commit"], *config["patch_commits"]]):
        raise ValueError("patches must name full reviewed commit IDs")
    if not isinstance(config["gate_command"], str) or not Path(config["gate_command"]).is_absolute():
        raise ValueError("gate command must be an absolute installed executable")
    return config


def reviewed_source(config):
    source = Path(config["source_checkout"])
    if run("git", "remote", "get-url", "origin", cwd=source) != ORG_REPOSITORY:
        raise ValueError("source checkout must use the canonical org fork")
    run("git", "fetch", "--no-tags", ORG_REPOSITORY,
        "+refs/heads/main:refs/remotes/origin/main", cwd=source)
    for commit in [config["overlay_commit"], *config["patch_commits"]]:
        run("git", "merge-base", "--is-ancestor", commit, "refs/remotes/origin/main", cwd=source)
    return source


def fetch_release(source, version, advertised_object):
    run("git", "fetch", "--no-tags", REPOSITORY, f"refs/tags/{version}", cwd=source)
    if run("git", "rev-parse", "FETCH_HEAD", cwd=source) != advertised_object:
        raise ValueError("fetched official release differs from advertised tag")
    return run("git", "rev-parse", "FETCH_HEAD^{commit}", cwd=source)


def install_overlay(checkout, commit):
    files = set(run("git", "ls-tree", "-r", "--name-only", commit, "--", *OVERLAY_PATHS,
                    cwd=checkout).splitlines())
    if not REQUIRED_OVERLAY <= files:
        raise ValueError("reviewed commit must contain the complete overlay and image fixture")
    run("git", "checkout", commit, "--", *OVERLAY_PATHS, cwd=checkout)
    run("git", "commit", "-m", f"build: compose reviewed fleet overlay {commit}\n\nAssisted-by: OpenAI Codex",
        cwd=checkout)


def reconcile(config_path, state):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / "reconcile.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already-running"}
        config = inputs(config_path)
        source = reviewed_source(config)
        _, upstream_object, version = latest_release(run("git", "ls-remote", "--tags", "--refs", REPOSITORY))
        upstream_commit = fetch_release(source, version, upstream_object)
        fingerprint = {"upstream_commit": upstream_commit, "inputs": config}
        marker = state / "built-inputs.json"
        if marker.exists() and json.loads(marker.read_text()) == fingerprint:
            return {"status": "unchanged", "version": version}
        with tempfile.TemporaryDirectory(prefix="traefik-build-", dir=state) as temporary:
            checkout = Path(temporary) / "source"
            run("git", "worktree", "add", "--detach", str(checkout), upstream_commit, cwd=source)
            try:
                run("git", "cherry-pick", *config["patch_commits"], cwd=checkout)
                install_overlay(checkout, config["overlay_commit"])
                patch_commit = run("git", "rev-parse", "HEAD", cwd=checkout)
                # This executable is installed by the build owner and must run make
                # validate and ./verify.sh; its nonzero exit retains the prior manifest.
                run(config["gate_command"], str(checkout), cwd=checkout)
                tag = f"{IMAGE}:{version}-{patch_commit[:12]}"
                run("bash", "overlay/build-image.sh", "--build-image", config["build_image"],
                    "--runtime-image", config["runtime_image"], "--webui-image", config["webui_image"],
                    "--image", tag, "--upstream-version", version, "--source-commit", patch_commit, cwd=checkout)
                run("python3", "overlay/verify_image_startup.py", "--image", tag, cwd=checkout)
                run("docker", "push", tag)
                refs = json.loads(run("docker", "image", "inspect", "--format", "{{json .RepoDigests}}", tag))
                digest = next(ref for ref in refs if ref.startswith(IMAGE + "@sha256:"))
                run("bash", "overlay/emit-promotion-manifest.sh", "--image-ref", digest,
                    "--upstream-version", version, "--upstream-commit", upstream_commit,
                    "--patch-commit", patch_commit, "--output", str(state / "promotion-manifest.json"), cwd=checkout)
                temporary_marker = state / "built-inputs.json.new"
                temporary_marker.write_text(json.dumps(fingerprint, sort_keys=True))
                os.replace(temporary_marker, marker)
                return {"status": "published", "version": version, "image_ref": digest}
            finally:
                run("git", "worktree", "remove", "--force", str(checkout), cwd=source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/traefik-readiness/producer.json"))
    parser.add_argument("--state", type=Path, default=Path("/var/lib/traefik-readiness"))
    args = parser.parse_args()
    os.umask(0o077)
    print(json.dumps(reconcile(args.config, args.state), sort_keys=True))


if __name__ == "__main__":
    main()
