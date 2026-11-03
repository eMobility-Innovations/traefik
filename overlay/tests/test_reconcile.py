"""Checks the producer's release selection and fail-closed configuration boundary."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("producer", Path(__file__).parents[1] / "reconcile.py")
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)


class ProducerTests(unittest.TestCase):
    def test_newest_stable_excludes_preview_and_peeled_tags(self):
        rows = "a refs/tags/v3.7.9\nb refs/tags/v3.7.17\nc refs/tags/v3.8.0-rc1\nd refs/tags/v3.7.17^{}"
        self.assertEqual(producer.latest_release(rows), ((3, 7, 17), "b", "v3.7.17"))

    def test_empty_upstream_read_refuses_build(self):
        with self.assertRaises(ValueError):
            producer.latest_release("")

    def test_failed_build_keeps_last_good_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            manifest = state / "promotion-manifest.json"
            manifest.write_text("last-good")
            config = {"source_checkout": "/source", "overlay_commit": "d" * 40, "patch_commits": ["a" * 40],
                      "build_image": "build", "runtime_image": "runtime", "webui_image": "webui",
                      "gate_command": "/gate"}

            def execute(*args, **_kwargs):
                if args[:2] == ("git", "ls-remote"):
                    return "b" * 40 + " refs/tags/v3.7.17"
                if args[:2] == ("git", "rev-parse"):
                    return "c" * 40
                if args[0] == "/gate":
                    raise RuntimeError("candidate gate failed")
                return ""

            with patch.object(producer, "inputs", return_value=config), patch.object(producer, "reviewed_source", return_value=Path("/source")), patch.object(producer, "install_overlay"), patch.object(producer, "fetch_release", return_value="b" * 40), patch.object(producer, "run", side_effect=execute), self.assertRaisesRegex(RuntimeError, "candidate gate failed"):
                producer.reconcile(state / "config.json", state)
            self.assertEqual(manifest.read_text(), "last-good")
            self.assertFalse((state / "built-inputs.json").exists())

    def test_wrong_remote_refuses_source(self):
        with patch.object(producer, "run", return_value="https://example.com/traefik.git"), self.assertRaisesRegex(ValueError, "canonical org fork"):
            producer.reviewed_source({"source_checkout": "/source", "overlay_commit": "d" * 40,
                                      "patch_commits": ["a" * 40]})

    def test_unreviewed_patch_refuses_source(self):
        def execute(*args, **_kwargs):
            if args[:3] == ("git", "remote", "get-url"):
                return "https://github.com/eMobility-Innovations/traefik.git"
            if args[:2] == ("git", "merge-base"):
                raise producer.subprocess.CalledProcessError(1, args)
            return ""
        with patch.object(producer, "run", side_effect=execute), self.assertRaises(producer.subprocess.CalledProcessError):
            producer.reviewed_source({"source_checkout": "/source", "overlay_commit": "d" * 40,
                                      "patch_commits": ["a" * 40]})

    def test_missing_fixture_refuses_overlay(self):
        with patch.object(producer, "run", return_value="overlay/Dockerfile\nverify.sh"), self.assertRaisesRegex(ValueError, "complete overlay"):
            producer.install_overlay(Path("/candidate"), "d" * 40)

    def test_changed_official_tag_refuses_source(self):
        with patch.object(producer, "run", side_effect=["", "different-object"]), self.assertRaisesRegex(ValueError, "differs from advertised"):
            producer.fetch_release(Path("/source"), "v3.7.17", "advertised-object")

    def test_trust_failure_keeps_last_good_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            manifest = state / "promotion-manifest.json"
            manifest.write_text("last-good")
            with patch.object(producer, "inputs", return_value={}), patch.object(producer, "reviewed_source", side_effect=ValueError("untrusted source")), self.assertRaisesRegex(ValueError, "untrusted source"):
                producer.reconcile(state / "config.json", state)
            self.assertEqual(manifest.read_text(), "last-good")
            self.assertFalse((state / "built-inputs.json").exists())

    def test_missing_inputs_and_mutable_images_refused(self):
        value = {"source_checkout": "/src", "overlay_commit": "d" * 40, "patch_commits": ["a" * 40],
                 "build_image": "golang@sha256:" + "b" * 64,
                 "runtime_image": "traefik@sha256:" + "c" * 64,
                 "webui_image": "node@sha256:" + "d" * 64,
                 "gate_command": "/usr/local/libexec/traefik-gate"}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(value))
            self.assertEqual(producer.inputs(path), value)
            for key in value:
                malformed = value.copy()
                del malformed[key]
                path.write_text(json.dumps(malformed))
                with self.assertRaises(ValueError):
                    producer.inputs(path)
            value["runtime_image"] = "traefik:latest"
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                producer.inputs(path)


if __name__ == "__main__":
    unittest.main()
