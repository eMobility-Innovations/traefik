"""The Docker acceptance trust chain must satisfy strict X.509 validation."""
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


class StartupFixtureCertificateTest(unittest.TestCase):
    def test_generated_chain_is_valid_under_strict_verification(self):
        source = Path(__file__).resolve().parents[1] / "verify_image_startup.py"
        spec = importlib.util.spec_from_file_location("startup_fixture", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module.make_certificates(root)
            result = subprocess.run(
                ["openssl", "verify", "-x509_strict", "-CAfile", str(root / "ca.pem"), str(root / "leaf.pem")],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
