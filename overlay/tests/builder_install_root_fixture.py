"""The builder install stays private and preserves its prior runtime on refusal."""
import fcntl
import json
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

OVERLAY = Path(__file__).resolve().parents[1]
INSTALLER = OVERLAY / "install-builder.sh"
COMMIT = "a" * 40


class BuilderInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="traefik-builder-test-", dir="/root")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.prefix = self.root / "target"
        self.prefix.mkdir(mode=0o700)
        self.source = self.root / "protected"
        (self.source / "overlay").mkdir(parents=True)
        for name in ("reconcile.py", "build-inputs.lock", "traefik-readiness-gate"):
            shutil.copyfile(OVERLAY / name, self.source / "overlay" / name)
        (self.source / ".github/workflows").mkdir(parents=True)
        (self.source / ".github/workflows/validate.yaml").write_text("fixture-pins: protected\n")
        (self.source / "overlay/validate.sh").write_text('#!/bin/sh\nprintf "bootstrap\\n" >>"$1/calls"\ngo version >"$1/compiler"\n')
        (self.source / "overlay/validate.sh").chmod(0o755)
        self.config = self.root / "producer.json"
        lock = dict(line.split("=",1) for line in (OVERLAY / "build-inputs.lock").read_text().splitlines() if line and not line.startswith("#"))
        value = {"source_checkout":str(self.prefix / "opt/traefik-readiness/source.git"),
                 "overlay_commit":COMMIT,"patch_commits":["b"*40]}
        value.update({key:lock[key.upper()] for key in ("build_image","runtime_image","webui_image")})
        self.config.write_text(json.dumps(value))
        self.config.chmod(0o600)
        self.publisher = self.root / "publisher.json"
        self.publisher.write_text('{"fixture":"not a real credential"}')
        self.publisher.chmod(0o600)

    def invoke(self, failure=False, checksum_failure=False):
        code = r'''
source "$1"
fixture_source=$2
_materialize_source() { printf '%s\n' "$fixture_source"; }
_fixture_install_go() {
  [[ $fixture_failure == 0 ]] || return 1
  mkdir -p "$TOOLS/go1.27.3/bin"
  printf '#!/bin/sh\necho go version go1.27.3 linux/amd64\n' > "$TOOLS/go1.27.3/bin/go"
  chmod 755 "$TOOLS/go1.27.3/bin/go"
}
fixture_failure=$3
if [[ $fixture_failure == checksum ]]; then
  _download_go() { printf corrupted >"$2"; }
else
  _install_go() { _fixture_install_go "$@"; }
fi
shift 3
main "$@"
'''
        return subprocess.run(["bash","-c",code,"fixture",str(INSTALLER),str(self.source),("checksum" if checksum_failure else str(int(failure))),
                               "--dest-root",str(self.prefix),"--source-commit",COMMIT,
                               "--producer-config",str(self.config),"--publisher-docker-config",str(self.publisher)],
                              text=True,capture_output=True,check=False)

    def test_missing_native_publisher_is_pending_without_installing_defaults(self):
        self.publisher.unlink()
        result = self.invoke()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"],"PENDING")
        self.assertFalse((self.prefix / "opt").exists())

    def test_readable_or_symlink_publisher_is_refused(self):
        self.publisher.chmod(0o644)
        self.assertNotEqual(self.invoke().returncode,0)
        self.publisher.unlink()
        self.publisher.symlink_to(self.config)
        self.assertNotEqual(self.invoke().returncode,0)
        self.assertFalse((self.prefix / "usr/local/sbin/traefik-readiness-reconcile").exists())

    def test_compiler_failure_preserves_the_installed_runtime(self):
        entry = self.prefix / "usr/local/sbin/traefik-readiness-reconcile"
        entry.parent.mkdir(parents=True)
        entry.write_text("last-good-runtime")
        receipt = self.prefix / "var/lib/traefik-readiness/builder-installed.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text('{"source_commit":"last-good"}')
        receipt.chmod(0o600)
        result = self.invoke(failure=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(entry.read_text(),"last-good-runtime")
        self.assertEqual(receipt.read_text(),'{"source_commit":"last-good"}')

    def test_bad_compiler_archive_preserves_the_installed_runtime(self):
        entry = self.prefix / "usr/local/sbin/traefik-readiness-reconcile"
        entry.parent.mkdir(parents=True)
        entry.write_text("last-good-runtime")
        result = self.invoke(checksum_failure=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("checksum failed",result.stderr)
        self.assertEqual(entry.read_text(),"last-good-runtime")

    def test_active_producer_returns_pending_without_replacing_runtime(self):
        state = self.prefix / "var/lib/traefik-readiness"
        state.mkdir(parents=True)
        lock = state / "reconcile.lock"
        with lock.open("w") as handle:
            lock.chmod(0o600)
            fcntl.flock(handle,fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.invoke()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)["reason"],"producer-active")
        self.assertFalse((self.prefix / "usr/local/sbin/traefik-readiness-reconcile").exists())

    def test_fixed_gate_uses_private_compiler_and_protected_candidate_bootstrap(self):
        result = self.invoke()
        self.assertEqual(result.returncode,0,result.stderr)
        mirror = self.prefix / "opt/traefik-readiness/source.git"
        candidate = self.root / "candidate"
        code = r'''source "$1"
mirror=$2
candidate=$3
_git init --bare "$mirror" >/dev/null
_write_mirror_config "$mirror/config"
tree=$(_git --git-dir="$mirror" mktree </dev/null)
commit=$(printf fixture | _git --git-dir="$mirror" -c user.name=Fixture -c user.email=fixture@invalid commit-tree "$tree")
_git --git-dir="$mirror" worktree add --detach "$candidate" "$commit" >/dev/null
'''
        prepared = subprocess.run(["bash","-c",code,"fixture",str(INSTALLER),str(mirror),str(candidate)],
                                  check=False,capture_output=True,text=True)
        self.assertEqual(prepared.returncode,0,prepared.stderr)
        (candidate / "verify.sh").write_text('#!/bin/sh\nprintf "verify\\n" >>calls\n')
        (candidate / "verify.sh").chmod(0o755)
        gate = self.prefix / "usr/local/libexec/traefik-readiness-gate"
        checked = subprocess.run([str(gate),str(candidate)],check=False,capture_output=True,text=True)
        self.assertEqual(checked.returncode,0,checked.stderr)
        self.assertEqual((candidate / "calls").read_text(),"bootstrap\nverify\n")
        self.assertEqual((candidate / "compiler").read_text().strip(),"go version go1.27.3 linux/amd64")

    def test_install_private_inputs_and_fixed_noargument_entrypoint(self):
        result = self.invoke()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"],"INSTALLED")
        entry = self.prefix / "usr/local/sbin/traefik-readiness-reconcile"
        gate = self.prefix / "usr/local/libexec/traefik-readiness-gate"
        for path in (entry,gate):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o555)
            self.assertEqual(path.stat().st_uid,0)
        auth = self.prefix / "var/lib/traefik-readiness/docker/config.json"
        self.assertEqual(stat.S_IMODE(auth.stat().st_mode),0o600)
        self.assertEqual(auth.read_bytes(),self.publisher.read_bytes())
        self.assertFalse((self.prefix / "root/.docker").exists())
        receipt = self.prefix / "var/lib/traefik-readiness/builder-installed.json"
        self.assertEqual(json.loads(receipt.read_text()),
                         {"schema_version":1,"source_commit":COMMIT,"go_version":"go1.27.3"})
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode),0o600)
        self.assertEqual(receipt.stat().st_uid,0)
        rejected = subprocess.run([str(entry),"unexpected"],capture_output=True,check=False)
        self.assertEqual(rejected.returncode,64)


if __name__ == "__main__":
    unittest.main()
