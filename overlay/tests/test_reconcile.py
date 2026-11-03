"""Checks the producer's release selection and fail-closed configuration boundary."""
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("producer", Path(__file__).parents[1] / "reconcile.py")
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)


class ProducerTests(unittest.TestCase):
    def test_gate_failure_is_private_bounded_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            secret = 'registry-password=do-not-leak'
            bearer = 'Authorization: Bearer do-not-leak'
            token = 'token: Bearer do-not-leak-too'
            failure = subprocess.CalledProcessError(
                1, [str(producer.GATE_COMMAND)], output='missing shellcheck', stderr='\n'.join((secret, bearer, token)))
            with patch.object(producer, "validate_root_path"), patch.object(producer.os, "access", return_value=True), patch.object(Path, "is_file", return_value=True), patch.object(producer, "run", side_effect=failure), self.assertRaises(subprocess.CalledProcessError):
                producer.run_gate(Path("/candidate"), state, "a" * 40)
            receipt = json.loads((state / "gate-failure.json").read_text())
            self.assertEqual((state / "gate-failure.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(receipt["phase"], "gate")
            self.assertEqual(receipt["tool"], "traefik-readiness-gate")
            self.assertEqual(receipt["source_commit"], "a" * 40)
            self.assertIn("missing shellcheck", receipt["stdout"])
            self.assertNotIn(secret, json.dumps(receipt))
            self.assertNotIn('do-not-leak', json.dumps(receipt))

    def test_gate_output_preserves_beginning_and_terminal_cause(self):
        output = "starting gate\n" + "downloading dependency\n" * 400 + "linter cache: permission denied"
        result = producer._redact_gate_output(output)
        self.assertTrue(result.startswith("starting gate\n"))
        self.assertTrue(result.endswith("linter cache: permission denied"))
        self.assertIn("[... truncated ...]", result)
        self.assertLessEqual(len(result), 4096)

    def test_gate_output_length_boundaries(self):
        for size in (0, 1, 4095, 4096, 4097, 10000):
            with self.subTest(size=size):
                output = "é" * size
                result = producer._redact_gate_output(output)
                self.assertLessEqual(len(result), 4096)
                if size <= 4096:
                    self.assertEqual(result, output)
                else:
                    self.assertIn("[... truncated ...]", result)
                    self.assertTrue(result.startswith("é"))
                    self.assertTrue(result.endswith("é"))
        self.assertEqual(producer._redact_gate_output(None), "")

    def test_gate_output_redacts_whole_sensitive_lines_before_trimming(self):
        for padding in (0, 2000, 4095, 4096, 4097, 8000):
            with self.subTest(padding=padding):
                output = ("public start\n" + "x" * padding + "\n"
                          + "Authorization: Bearer " + "s" * 10000 + "tail-secret\n"
                          + "y" * 8000 + "\nterminal cause")
                result = producer._redact_gate_output(output)
                self.assertNotIn("Bearer", result)
                self.assertNotIn("tail-secret", result)
                self.assertNotIn("s" * 20, result)
                self.assertTrue(result.endswith("terminal cause"))
                self.assertLessEqual(len(result), 4096)

    def test_gate_output_never_exposes_sensitive_line_tail_fragments(self):
        for header in ("Authorization: Bearer ", "token=", "password: "):
            for padding in (0, 2038, 4096):
                with self.subTest(header=header, padding=padding):
                    output = ("public start\n" + "chatter\n" * 1000
                              + "x" * padding + "\n" + header
                              + "s" * 10000 + "tail-secret\nterminal cause")
                    result = producer._redact_gate_output(output)
                    self.assertNotIn("Bearer", result)
                    self.assertNotIn("tail-secret", result)
                    self.assertNotIn("s" * 20, result)
                    self.assertTrue(result.endswith("terminal cause"))
                    self.assertLessEqual(len(result), 4096)

    def test_gate_output_redacts_url_credentials_in_retained_tail(self):
        output = "starting\n" + "chatter\n" * 1000 + "failed https://user:secret@example.com/cache"
        result = producer._redact_gate_output(output)
        self.assertTrue(result.endswith("failed https://<redacted>@example.com/cache"))
        self.assertNotIn("user:secret", result)
        self.assertEqual(producer._redact_gate_output("short diagnostic\n"), "short diagnostic\n")

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
                      "build_image": "build", "runtime_image": "runtime", "webui_image": "webui"}

            def execute(*args, **_kwargs):
                if args[:2] == ("git", "ls-remote"):
                    return "b" * 40 + " refs/tags/v3.7.17"
                if args[:2] == ("git", "rev-parse"):
                    return "c" * 40
                if args[0] == str(producer.GATE_COMMAND):
                    raise RuntimeError("candidate gate failed")
                return ""

            with patch.object(producer, "validate_root_path"), patch.object(producer.os, "access", return_value=True), patch.object(Path, "is_file", return_value=True), patch.object(producer, "inputs", return_value=config), patch.object(producer, "reviewed_source", return_value=Path("/source")), patch.object(producer, "install_overlay"), patch.object(producer, "fetch_release", return_value="b" * 40), patch.object(producer, "run", side_effect=execute), self.assertRaisesRegex(RuntimeError, "candidate gate failed"):
                producer.reconcile(state / "config.json", state)
            self.assertEqual(manifest.read_text(), "last-good")
            self.assertFalse((state / "built-inputs.json").exists())

    def test_wrong_remote_refuses_source(self):
        with patch.object(producer, "validate_mirror"), patch.object(producer, "run", return_value="https://example.com/traefik.git"), self.assertRaisesRegex(ValueError, "canonical org fork"):
            producer.reviewed_source({"source_checkout": "/source", "overlay_commit": "d" * 40,
                                      "patch_commits": ["a" * 40]})

    def test_unreviewed_patch_refuses_source(self):
        def execute(*args, **_kwargs):
            if args[:3] == ("git", "remote", "get-url"):
                return "https://github.com/eMobility-Innovations/traefik.git"
            if args[:2] == ("git", "merge-base"):
                raise producer.subprocess.CalledProcessError(1, args)
            return ""
        with patch.object(producer, "validate_mirror"), patch.object(producer, "run", side_effect=execute), self.assertRaises(producer.subprocess.CalledProcessError):
            producer.reviewed_source({"source_checkout": "/source", "overlay_commit": "d" * 40,
                                      "patch_commits": ["a" * 40]})

    def test_missing_fixture_refuses_overlay(self):
        with patch.object(producer, "run", return_value="overlay/Dockerfile\nverify.sh"), self.assertRaisesRegex(ValueError, "complete overlay"):
            producer.install_overlay(Path("/candidate"), "d" * 40)

    def test_composed_git_candidate_runs_reviewed_validation(self):
        root = Path(__file__).resolve().parents[2]
        validator = "script/validate-shell-script.sh"
        regression = "overlay/tests/test_shell_detection.sh"
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary)
            producer.run("git", "init", cwd=candidate)
            (candidate / "script").mkdir()
            shutil.copyfile(root / "overlay/tests/fixtures/validate-shell-script-stock.sh",
                            candidate / validator)
            producer.run("git", "add", ".", cwd=candidate)
            producer.run("git", "commit", "-m", "stock upstream", cwd=candidate)
            base = producer.run("git", "rev-parse", "HEAD", cwd=candidate)
            for relative in producer.REQUIRED_OVERLAY | {validator, regression}:
                target = candidate / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / relative, target)
            producer.run("git", "add", ".", cwd=candidate)
            producer.run("git", "commit", "-m", "reviewed overlay", cwd=candidate)
            reviewed = producer.run("git", "rev-parse", "HEAD", cwd=candidate)
            producer.run("git", "checkout", "--detach", base, cwd=candidate)
            producer.install_overlay(candidate, reviewed)
            result = producer.run("bash", regression, cwd=candidate)
            self.assertIn("shell detection regression and mutation checks passed", result)
            self.assertEqual((candidate / validator).read_bytes(), (root / validator).read_bytes())

    def test_changed_official_tag_refuses_source(self):
        with patch.object(producer, "run", side_effect=["", "different-object"]), self.assertRaisesRegex(ValueError, "differs from advertised"):
            producer.fetch_release(Path("/source"), "v3.7.17", "advertised-object")

    def test_trust_failure_keeps_last_good_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            manifest = state / "promotion-manifest.json"
            manifest.write_text("last-good")
            with patch.object(producer, "validate_root_path"), patch.object(producer, "inputs", return_value={}), patch.object(producer, "reviewed_source", side_effect=ValueError("untrusted source")), self.assertRaisesRegex(ValueError, "untrusted source"):
                producer.reconcile(state / "config.json", state)
            self.assertEqual(manifest.read_text(), "last-good")
            self.assertFalse((state / "built-inputs.json").exists())

    def test_missing_inputs_and_mutable_images_refused(self):
        value = {"source_checkout": "/src", "overlay_commit": "d" * 40, "patch_commits": ["a" * 40],
                 "build_image": "golang@sha256:" + "b" * 64,
                 "runtime_image": "traefik@sha256:" + "c" * 64,
                 "webui_image": "node@sha256:" + "d" * 64}
        trusted = {key: value[key] for key in ("build_image", "runtime_image", "webui_image")}
        with tempfile.TemporaryDirectory() as temporary, patch.object(producer, "validate_root_path"), patch.object(producer, "trusted_build_inputs", return_value=trusted):
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
