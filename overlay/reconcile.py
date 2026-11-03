#!/usr/bin/env python3
"""Build the latest stable release plus reviewed patches from an existing fleet tick."""

import argparse
import configparser
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

REPOSITORY = "https://github.com/traefik/traefik.git"
ORG_REPOSITORY = "https://github.com/eMobility-Innovations/traefik.git"
OVERLAY_PATHS = ("overlay", "verify.sh", "REPO-CONTRACT.toml")
REQUIRED_OVERLAY = {"overlay/Dockerfile", "overlay/build-image.sh", "overlay/build-inputs.lock",
                    "overlay/emit-promotion-manifest.sh", "overlay/verify_image_startup.py",
                    "overlay/promotion-manifest.schema.json", "overlay/reconcile.py",
                    "overlay/tests/test_reconcile.py", "overlay/tests/test_producer_security.py",
                    "overlay/traefik-readiness-gate", "overlay/tests/test_promotion_manifest.sh",
                    "verify.sh", "REPO-CONTRACT.toml"}
GATE_COMMAND = Path("/usr/local/libexec/traefik-readiness-gate")
IMAGE = "registry.fiszu.com/emobility/traefik"
VERSION = re.compile(r"refs/tags/v(\d+)\.(\d+)\.(\d+)$")
DIGEST = re.compile(r"[a-z0-9][a-z0-9./_-]*@sha256:[a-f0-9]{64}$")


def run(*args, cwd=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null",
               GIT_ATTR_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
               GIT_NO_REPLACE_OBJECTS="1", GIT_GRAFT_FILE="/dev/null")
    if args[0] == "git":
        args = ("git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                "-c", "core.attributesFile=/dev/null", "-c", "protocol.ext.allow=never",
                "-c", "user.name=Traefik Readiness Automation",
                "-c", "user.email=traefik-readiness@fiszu.com", *args[1:])
    return subprocess.run(args, cwd=cwd, check=True, text=True, env=env,
                          capture_output=True).stdout.strip()


def validate_root_path(path):
    if not path.is_absolute():
        raise ValueError("trusted paths must be absolute")
    for member in (path, *path.parents):
        info = member.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f"unsafe root-owned path: {member}")


def validate_mirror_config(text):
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(text)
    except configparser.Error as error:
        raise ValueError("invalid controlled mirror configuration") from error
    expected = {"core": {"repositoryformatversion": "0", "filemode": "true", "bare": "true"},
                'remote "origin"': {"url": ORG_REPOSITORY,
                                    "fetch": "+refs/heads/main:refs/remotes/origin/main"}}
    actual = {section: dict(parser[section]) for section in parser.sections()}
    if parser.defaults() or actual != expected:
        raise ValueError("source must be a controlled bare mirror without hooks, filters or includes")


def validate_mirror(source):
    validate_root_path(source)
    for root, directories, files in os.walk(source, followlinks=False):
        for name in [*directories, *files]:
            validate_root_path(Path(root) / name)
    validate_mirror_config((source / "config").read_text())


def trusted_build_inputs():
    lock = Path(__file__).absolute().parent / "build-inputs.lock"
    validate_root_path(lock)
    values = dict(line.split("=", 1) for line in lock.read_text().splitlines()
                  if line and not line.startswith("#"))
    return {name: values[name.upper()] for name in ("build_image", "runtime_image", "webui_image")}


def validate_build_inputs(config):
    trusted = trusted_build_inputs()
    if any(config[name] != value for name, value in trusted.items()):
        raise ValueError("producer images must equal the trusted build lock")


def run_gate(checkout):
    validate_root_path(GATE_COMMAND)
    if not GATE_COMMAND.is_file() or not os.access(GATE_COMMAND, os.X_OK):
        raise ValueError("fixed gate must be an executable regular file")
    run(str(GATE_COMMAND), str(checkout), cwd=checkout)


def candidate_tag(version, fingerprint):
    identity = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
    return f"{IMAGE}:{version}-{identity}"


def require_unused_tag(tag):
    try:
        run("docker", "manifest", "inspect", tag)
    except subprocess.CalledProcessError as error:
        message = (error.stderr or "").lower()
        if "manifest unknown" in message or "no such manifest" in message:
            return
        raise
    raise ValueError("candidate tag already exists; refusing to overwrite it")


def publish(tag):
    digest = run("docker", "push", "--quiet", tag)
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("registry push did not return an authoritative digest")
    reference = IMAGE + "@" + digest
    manifest = json.loads(run("docker", "buildx", "imagetools", "inspect", "--raw", reference))
    current = run("docker", "image", "inspect", "--format", "{{.Id}}", tag)
    if manifest.get("config", {}).get("digest") != current:
        raise ValueError("pushed registry manifest does not match the current candidate")
    return reference


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
    validate_root_path(path)
    config = json.loads(path.read_text())
    required = {"source_checkout", "overlay_commit", "patch_commits", "build_image", "runtime_image", "webui_image"}
    if set(config) != required or not config["patch_commits"]:
        raise ValueError("producer configuration must declare all build inputs and patches")
    for key in ("build_image", "runtime_image", "webui_image"):
        if not DIGEST.fullmatch(config[key]):
            raise ValueError("build inputs must be immutable image references")
    if not all(re.fullmatch(r"[a-f0-9]{40}", item) for item in [config["overlay_commit"], *config["patch_commits"]]):
        raise ValueError("patches must name full reviewed commit IDs")
    validate_build_inputs(config)
    return config


def reviewed_source(config):
    source = Path(config["source_checkout"])
    validate_mirror(source)
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
    validate_root_path(state.parent)
    state.mkdir(mode=0o700, exist_ok=True)
    validate_root_path(state)
    descriptor = os.open(state / "reconcile.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
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
                run_gate(checkout)
                tag = candidate_tag(version, fingerprint)
                require_unused_tag(tag)
                run("bash", "overlay/build-image.sh", "--build-image", config["build_image"],
                    "--runtime-image", config["runtime_image"], "--webui-image", config["webui_image"],
                    "--image", tag, "--upstream-version", version, "--source-commit", patch_commit, cwd=checkout)
                run("python3", "overlay/verify_image_startup.py", "--image", tag, cwd=checkout)
                digest = publish(tag)
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
