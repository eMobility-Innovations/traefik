"""Unprivileged installer parsing and controlled Git policy checks."""
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

OVERLAY = Path(__file__).resolve().parents[1]
INSTALLER = OVERLAY / "install-builder.sh"
spec = importlib.util.spec_from_file_location("producer", OVERLAY / "reconcile.py")
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)


class BuilderInstallContractTests(unittest.TestCase):
    def test_missing_bundle_is_pending_before_any_privileged_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = subprocess.run(["bash",str(INSTALLER),"--source-commit","a"*40,
                                     "--producer-config",str(root / "missing.json")],
                                    check=False,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"],"PENDING")
            self.assertEqual(list(root.iterdir()),[])

    def test_commit_and_argument_validation_cannot_be_omitted(self):
        for args in ([],["--source-commit","short"],["--unknown","value"]):
            result = subprocess.run(["bash",str(INSTALLER),*args],check=False,capture_output=True)
            self.assertNotEqual(result.returncode,0)

    def test_production_entrypoint_refuses_a_fixture_destination(self):
        result = subprocess.run(["bash",str(INSTALLER),"--source-commit","a"*40,
                                 "--dest-root","/root/traefik-builder-test-not-real/target"],
                                check=False,capture_output=True)
        self.assertNotEqual(result.returncode,0)

    def test_generated_mirror_policy_is_accepted_by_the_real_producer(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config"
            result = subprocess.run(["bash","-c",'source "$1"; _write_mirror_config "$2"',
                                     "fixture",str(INSTALLER),str(config)],check=False,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            producer.validate_mirror_config(config.read_text())
            self.assertEqual(config.stat().st_mode & 0o777,0o600)

    def test_git_ignores_inherited_configuration_injection(self):
        result = subprocess.run(["bash","-c",'source "$1"; export GIT_CONFIG_COUNT=999; _git config --get core.hooksPath',
                                 "fixture",str(INSTALLER)],check=False,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),"/dev/null")


if __name__ == "__main__":
    unittest.main()
