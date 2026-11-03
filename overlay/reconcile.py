#!/usr/bin/env python3
"""Build the latest stable release plus reviewed patches from an existing fleet tick."""

import argparse
import configparser
import fcntl
import gzip
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tarfile
import tempfile
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path

REPOSITORY = "https://github.com/traefik/traefik.git"
ORG_REPOSITORY = "https://github.com/eMobility-Innovations/traefik.git"
OVERLAY_PATHS = ("overlay", "verify.sh", "REPO-CONTRACT.toml", "script/validate-shell-script.sh")
REQUIRED_OVERLAY = {"overlay/Dockerfile", "overlay/build-image.sh", "overlay/build-inputs.lock",
                    "overlay/emit-promotion-manifest.sh", "overlay/verify_image_startup.py",
                    "overlay/promotion-manifest.schema.json", "overlay/reconcile.py",
                    "overlay/tests/test_reconcile.py", "overlay/tests/test_producer_security.py",
                    "overlay/traefik-readiness-gate", "overlay/verify_promotion_artifact.py",
                    "overlay/tests/test_promotion_artifact.py", "overlay/tests/test_promotion_manifest.sh",
                    "overlay/tests/test_shell_detection.sh", "script/validate-shell-script.sh",
                    "verify.sh", "REPO-CONTRACT.toml"}
PROMOTION_TAG = "registry.fiszu.com/emobility/traefik:promotion-linux-amd64"
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


def _redact_gate_output(value):
    value = value or ""
    value = re.sub(r"(?im)^([^\n:=]*(?:password|token|authorization|auth)[^\n:=]*)\s*[:=].*$", r"\1=<redacted>", value)
    value = re.sub(r"(?i)(https?://)[^/\s:@]+:[^@\s/]+@", r"\1<redacted>@", value)
    if len(value) <= 4096:
        return value
    marker = "\n[... truncated ...]\n"
    remaining = 4096 - len(marker)
    head = remaining // 2
    return value[:head] + marker + value[-(remaining - head):]


def run_gate(checkout, state=None, source_commit=None):
    validate_root_path(GATE_COMMAND)
    if not GATE_COMMAND.is_file() or not os.access(GATE_COMMAND, os.X_OK):
        raise ValueError("fixed gate must be an executable regular file")
    try:
        run(str(GATE_COMMAND), str(checkout), cwd=checkout)
    except subprocess.CalledProcessError as error:
        if state is None or source_commit is None:
            raise
        receipt = {"phase": "gate", "tool": "traefik-readiness-gate", "source_commit": source_commit,
                   "exit_code": error.returncode, "stdout": _redact_gate_output(error.output),
                   "stderr": _redact_gate_output(error.stderr)}
        pending = state / "gate-failure.json.new"
        pending.write_text(json.dumps(receipt, sort_keys=True))
        os.chmod(pending, 0o600)
        os.replace(pending, state / "gate-failure.json")
        raise


def candidate_tag(version, fingerprint):
    identity = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
    return f"{IMAGE}:{version}-{identity}-{uuid.uuid4().hex}"


def require_unused_tag(tag):
    try:
        run("docker", "manifest", "inspect", tag)
    except subprocess.CalledProcessError as error:
        message = (error.stderr or "").lower()
        if "manifest unknown" in message or "no such manifest" in message:
            return
        raise
    raise ValueError("candidate tag already exists; refusing to overwrite it")


def local_config_digest(tag):
    metadata = json.loads(run("docker", "image", "inspect", "--platform", "linux/amd64", "--format", "{{json .}}", tag))
    # Modern Docker IDs identify manifests or indexes, never raw configuration.
    if metadata.get("Descriptor") is not None:
        return saved_config_digest(tag)
    digest = metadata.get("Id")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("Docker did not expose an authoritative local configuration digest")
    return digest


def registry_descriptor_digest(descriptor):
    if not isinstance(descriptor, dict):
        raise ValueError("registry descriptor must describe exactly one image manifest")
    if type(descriptor.get("size")) is not int or not 0 < descriptor["size"] <= 1024 * 1024:
        raise ValueError("registry descriptor has an invalid manifest size")
    digest = descriptor.get("digest")
    if not matches(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("registry descriptor did not expose an authoritative digest")
    if "schemaVersion" in descriptor and (type(descriptor["schemaVersion"]) is not int or descriptor["schemaVersion"] != 2):
        raise ValueError("registry descriptor must be a schema v2 image manifest")
    if descriptor.get("mediaType") not in ("application/vnd.docker.distribution.manifest.v2+json",
                                          "application/vnd.oci.image.manifest.v1+json"):
        raise ValueError("registry descriptor must name a single image manifest")
    return digest


def publish(tag):
    # Docker 29 prints a platform banner and the tag, never the registry digest,
    # so push output is not an identity source.
    run("docker", "push", "--platform", "linux/amd64", "--quiet", tag)
    descriptor = json.loads(run("docker", "buildx", "imagetools", "inspect",
                                "--format", "{{json .Manifest}}", tag))
    digest = registry_descriptor_digest(descriptor)
    reference = IMAGE + "@" + digest
    manifest = json.loads(run("docker", "buildx", "imagetools", "inspect", "--raw", reference))
    if (not isinstance(manifest, dict) or type(manifest.get("schemaVersion")) is not int
            or manifest["schemaVersion"] != 2 or manifest.get("mediaType") != descriptor["mediaType"]
            or "manifests" in manifest):
        raise ValueError("pushed registry manifest does not match the current candidate")
    current = local_config_digest(tag)
    if manifest.get("config", {}).get("digest") != current:
        raise ValueError("pushed registry manifest does not match the current candidate")
    return reference


def sha256(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def archive_configuration(archive):
    manifest = json.load(archive.extractfile("manifest.json"))
    if not isinstance(manifest, list) or len(manifest) != 1:
        raise ValueError("selected platform archive must contain exactly one image")
    entry = manifest[0]
    member = archive.getmember(entry["Config"])
    if not member.isreg():
        raise ValueError("saved image configuration must be a regular file")
    config = archive.extractfile(member).read()
    metadata = json.loads(config)
    if not isinstance(metadata, dict) or metadata.get("architecture") != "amd64" or metadata.get("os") != "linux":
        raise ValueError("saved image platform must be linux/amd64")
    return entry, config, metadata


def saved_config_digest(tag):
    try:
        with tempfile.TemporaryDirectory(prefix="traefik-config-identity-") as temporary:
            path = Path(temporary) / "image.tar"
            run("docker", "image", "save", "--platform", "linux/amd64", "--output", str(path), tag)
            with tarfile.open(path, "r:*") as archive:
                _, config, _ = archive_configuration(archive)
            return sha256(config)
    except (OSError, tarfile.TarError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("cannot verify the selected local configuration bytes") from error


def verify_artifact_archive(path, image_id, payload):
    with tarfile.open(path, "r:*") as archive:
        entry, config, metadata = archive_configuration(archive)
        if len(entry["Layers"]) != 1:
            raise ValueError("promotion artifact must have one image and one layer")
        if sha256(config) != image_id:
            raise ValueError("promotion artifact configuration digest differs from image")
        layer = archive.extractfile(entry["Layers"][0]).read()
    if layer.startswith(b"\x1f\x8b"):
        layer = gzip.decompress(layer)
    if metadata.get("rootfs", {}).get("diff_ids") != [sha256(layer)]:
        raise ValueError("promotion artifact layer digest differs from configuration")
    with tarfile.open(fileobj=io.BytesIO(layer), mode="r:") as archive:
        members = archive.getmembers()
        if len(members) != 1 or members[0].name not in ("promotion.json", "/promotion.json") or not members[0].isreg():
            raise ValueError("promotion layer must contain only regular promotion.json")
        if archive.extractfile(members[0]).read() != payload:
            raise ValueError("promotion artifact payload differs from verified manifest")


def matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def validate_provenance(value):
    if not isinstance(value, dict) or set(value) != {"build_recipe_sha256", "build_timestamp_utc"}:
        raise ValueError("promotion provenance fields do not match the canonical schema")
    if not matches(r"[a-f0-9]{64}", value["build_recipe_sha256"]):
        raise ValueError("promotion recipe identity must be a SHA256 digest")
    timestamp = value["build_timestamp_utc"]
    if not matches(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", timestamp):
        raise ValueError("promotion build timestamp must be an RFC3339 UTC timestamp")
    datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def validate_promotion(value, verified_image):
    fields = {"schema_version", "source_repo", "upstream_version", "upstream_commit", "patch_commit",
              "image_ref", "platform", "tests_passed", "tls_canary_passed", "provenance"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("promotion fields do not match the canonical schema")
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["source_repo"] != ORG_REPOSITORY.removesuffix(".git")
            or value["platform"] != "linux/amd64" or value["tests_passed"] is not True
            or value["tls_canary_passed"] is not True):
        raise ValueError("promotion authority, platform or checks are invalid")
    patterns = {"upstream_version": r"v[0-9]+\.[0-9]+\.[0-9]+",
                "upstream_commit": r"[a-f0-9]{40}", "patch_commit": r"[a-f0-9]{40}"}
    if not all(matches(pattern, value[field]) for field, pattern in patterns.items()):
        raise ValueError("promotion release and commit identities must be complete")
    if (not matches(re.escape(IMAGE) + r"@sha256:[a-f0-9]{64}", verified_image)
            or value["image_ref"] != verified_image):
        raise ValueError("promotion payload must describe the verified candidate image")
    validate_provenance(value["provenance"])


def promotion_payload(path, verified_image):
    payload = path.read_bytes()
    validate_promotion(json.loads(payload), verified_image)
    return payload


def build_promotion_artifact(pending, verified_image):
    payload = promotion_payload(pending, verified_image)
    tag = IMAGE + ":promotion-candidate-" + hashlib.sha256(payload).hexdigest()
    with tempfile.TemporaryDirectory(prefix="traefik-promotion-", dir=pending.parent) as temporary:
        context = Path(temporary)
        (context / "promotion.json").write_bytes(payload)
        (context / "Dockerfile").write_text("FROM scratch\nCOPY promotion.json /promotion.json\n")
        try:
            run("docker", "buildx", "build", "--platform", "linux/amd64", "--load", "--provenance=false",
                "--sbom=false", "--network=none", "--tag", tag, str(context))
            run("docker", "image", "save", "--platform", "linux/amd64", "--output", str(context / "artifact.tar"), tag)
            verify_artifact_archive(context / "artifact.tar", local_config_digest(tag), payload)
        except BaseException:
            with suppress(subprocess.CalledProcessError):
                run("docker", "image", "rm", tag)
            raise
    return tag


def verify_published_promotion(reference, pending, verified_image):
    if not DIGEST.fullmatch(reference) or not reference.startswith(IMAGE + "@sha256:"):
        raise ValueError("promotion readback requires an immutable approved repository digest")
    payload = promotion_payload(pending, verified_image)
    with tempfile.TemporaryDirectory(prefix="traefik-promotion-readback-", dir=pending.parent) as temporary:
        archive = Path(temporary) / "artifact.tar"
        run("docker", "pull", "--platform", "linux/amd64", reference)
        run("docker", "image", "save", "--platform", "linux/amd64", "--output", str(archive), reference)
        verify_artifact_archive(archive, local_config_digest(reference), payload)


def publish_promotion(pending, verified_image):
    tag = build_promotion_artifact(pending, verified_image)
    try:
        run("docker", "tag", tag, PROMOTION_TAG)
        reference = publish(PROMOTION_TAG)
        verify_published_promotion(reference, pending, verified_image)
        return reference
    finally:
        run("docker", "image", "rm", tag)


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
                run_gate(checkout, state, config["overlay_commit"])
                tag = candidate_tag(version, fingerprint)
                require_unused_tag(tag)
                run("bash", "overlay/build-image.sh", "--build-image", config["build_image"],
                    "--runtime-image", config["runtime_image"], "--webui-image", config["webui_image"],
                    "--image", tag, "--upstream-version", version, "--source-commit", patch_commit, cwd=checkout)
                run("python3", "overlay/verify_image_startup.py", "--image", tag, cwd=checkout)
                digest = publish(tag)
                pending = Path(temporary) / "promotion-manifest.json"
                run("bash", "overlay/emit-promotion-manifest.sh", "--image-ref", digest,
                    "--upstream-version", version, "--upstream-commit", upstream_commit,
                    "--patch-commit", patch_commit, "--output", str(pending), cwd=checkout)
                artifact_ref = publish_promotion(pending, digest)
                os.replace(pending, state / "promotion-manifest.json")
                temporary_marker = state / "built-inputs.json.new"
                temporary_marker.write_text(json.dumps(fingerprint, sort_keys=True))
                os.replace(temporary_marker, marker)
                return {"status": "published", "version": version, "image_ref": digest,
                        "promotion_artifact_ref": artifact_ref}
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
