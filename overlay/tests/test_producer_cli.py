"""CLI boundaries preserve machine-readable output and fixture cleanup."""

import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("producer", ROOT / "reconcile.py")
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)


class ProducerCliTests(unittest.TestCase):
    def test_producer_arguments_and_result_remain_machine_readable(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["reconcile.py", "--config", "/fixture/config.json", "--state", "/fixture/state"]), patch.object(producer, "reconcile", return_value={"status":"unchanged"}) as reconcile, redirect_stdout(output):
            producer.main()
        reconcile.assert_called_once_with(Path("/fixture/config.json"), Path("/fixture/state"))
        self.assertEqual(json.loads(output.getvalue()), {"status":"unchanged"})

    def test_fixture_generates_valid_manifest_and_removes_only_its_image(self):
        fixture_spec = importlib.util.spec_from_file_location("fixture_cli", ROOT / "verify_promotion_artifact.py")
        fixture = importlib.util.module_from_spec(fixture_spec)
        with patch.dict(sys.modules, {"reconcile":producer}):
            fixture_spec.loader.exec_module(fixture)

        def build(pending, image):
            # Verify the fixture itself continues to satisfy the real schema gate.
            producer.promotion_payload(pending, image)
            return "owned-fixture-image"

        output = io.StringIO()
        with patch.object(producer, "build_promotion_artifact", side_effect=build), patch.object(producer, "run") as run, redirect_stdout(output):
            fixture.main()
        result = json.loads(output.getvalue())
        self.assertTrue(result["fixture_only"])
        self.assertFalse(result["registry_push_performed"])
        run.assert_called_once_with("docker", "image", "rm", "owned-fixture-image")


if __name__ == "__main__":
    unittest.main()
