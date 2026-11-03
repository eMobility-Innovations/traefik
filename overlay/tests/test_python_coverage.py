"""The owned-source gate includes undiscovered helpers and refuses empty proofs."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("coverage_gate", Path(__file__).with_name("check_python_coverage.py"))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class CoverageGateTests(unittest.TestCase):
    def test_floor_is_eighty_percent_without_rounding_bypass(self):
        self.assertFalse(gate.passes({"covered_lines":79,"executable_lines":100}))
        self.assertTrue(gate.passes({"covered_lines":80,"executable_lines":100}))
        self.assertFalse(gate.passes({"covered_lines":7999,"executable_lines":10000}))
        self.assertFalse(gate.passes({"covered_lines":0,"executable_lines":0}))

    def test_unimported_production_helpers_count_against_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            overlay = root / "overlay"
            overlay.mkdir()
            production = overlay / "reconcile.py"
            production.write_text("covered = 1\n")
            (overlay / "new_helper.py").write_text("uncovered = 1\n")
            (overlay / "verify_image_startup.py").write_text("separate_docker_acceptance = 1\n")
            (overlay / "tests").mkdir()
            (overlay / "tests" / "test_fixture.py").write_text("fixture = 1\n")
            report = gate.coverage_report(root, {(str(production),1):1})
            self.assertEqual(set(report["files"]), {"overlay/reconcile.py","overlay/new_helper.py"})
            self.assertEqual(report["covered_lines"],1)
            self.assertEqual(report["executable_lines"],2)
            self.assertFalse(gate.passes(report))


if __name__ == "__main__":
    unittest.main()
