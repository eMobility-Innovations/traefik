"""The data artifact has one regular file and never promotes unverified images."""
import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("producer", Path(__file__).parents[1] / "reconcile.py")
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)


def archive(path, names, payload=b'{"test":"fixture"}'):
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as tar:
        for name, kind in names:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(payload) if kind == tarfile.REGTYPE else 0
            tar.addfile(info, io.BytesIO(payload) if info.size else None)
    layer_bytes = layer.getvalue()
    config = json.dumps({"rootfs":{"type":"layers", "diff_ids":["sha256:" + hashlib.sha256(layer_bytes).hexdigest()]}}).encode()
    identity = hashlib.sha256(config).hexdigest()
    manifest = json.dumps([{"Config":identity + ".json", "Layers":["layer.tar"]}]).encode()
    with tarfile.open(path, "w") as tar:
        for name, data in (("manifest.json",manifest), (identity + ".json",config), ("layer.tar",layer_bytes)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return "sha256:" + identity


class PromotionArtifactTests(unittest.TestCase):
    def test_single_regular_payload_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.tar"
            identity = archive(path, [("promotion.json", tarfile.REGTYPE)])
            producer.verify_artifact_archive(path, identity, b'{"test":"fixture"}')

    def test_noncanonical_layers_are_refused(self):
        cases = [[("other.json", tarfile.REGTYPE)], [("../promotion.json",tarfile.REGTYPE)],
                 [("promotion.json",tarfile.SYMTYPE)], [("promotion.json",tarfile.DIRTYPE)],
                 [("promotion.json",tarfile.REGTYPE), ("extra",tarfile.REGTYPE)]]
        for names in cases:
            with self.subTest(names=names), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "image.tar"
                identity = archive(path, names)
                with self.assertRaises(ValueError):
                    producer.verify_artifact_archive(path, identity, b'{"test":"fixture"}')

    def test_config_digest_and_payload_are_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.tar"
            identity = archive(path, [("promotion.json", tarfile.REGTYPE)])
            with self.assertRaises(ValueError):
                producer.verify_artifact_archive(path, "sha256:" + "0"*64, b'{"test":"fixture"}')
            with self.assertRaises(ValueError):
                producer.verify_artifact_archive(path, identity, b"different")

    def test_mismatched_image_never_builds_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            pending = Path(temporary) / "pending.json"
            pending.write_text(json.dumps({"image_ref":"wrong"}))
            with patch.object(producer, "run") as run, self.assertRaises(ValueError):
                producer.build_promotion_artifact(pending, producer.IMAGE + "@sha256:" + "a"*64)
            run.assert_not_called()

    def test_real_build_context_is_scratch_and_validated_before_publish(self):
        image = producer.IMAGE + "@sha256:" + "a" * 64
        value = {"image_ref": image, "source_repo": producer.ORG_REPOSITORY.removesuffix(".git"),
                 "platform": "linux/amd64", "schema_version": 1, "tests_passed": True,
                 "tls_canary_passed": True}
        identity = None
        with tempfile.TemporaryDirectory() as temporary:
            pending = Path(temporary) / "pending.json"
            pending.write_text(json.dumps(value))

            def execute(*args, **_kwargs):
                nonlocal identity
                if args[:3] == ("docker", "buildx", "build"):
                    self.assertEqual((Path(args[-1]) / "Dockerfile").read_text(),
                                     "FROM scratch\nCOPY promotion.json /promotion.json\n")
                if args[:3] == ("docker", "image", "save"):
                    identity = archive(Path(args[4]), [("promotion.json", tarfile.REGTYPE)], pending.read_bytes())
                if args[:3] == ("docker", "image", "inspect"):
                    return json.dumps({"Id": identity})
                return ""

            with patch.object(producer, "run", side_effect=execute) as run:
                tag = producer.build_promotion_artifact(pending, image)
            self.assertTrue(tag.startswith(producer.IMAGE + ":promotion-candidate-"))
            self.assertFalse(any("push" in call.args for call in run.call_args_list))

    def test_reconcile_artifact_failure_keeps_both_last_good_files(self):
        image = producer.IMAGE + "@sha256:" + "a" * 64
        config = {"source_checkout": "/source", "overlay_commit": "d" * 40,
                  "patch_commits": ["a" * 40], "build_image": "build",
                  "runtime_image": "runtime", "webui_image": "webui"}
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            final = state / "promotion-manifest.json"
            marker = state / "built-inputs.json"
            final.write_text("last-good")
            marker.write_text('{"last_good":true}')

            def execute(*args, **_kwargs):
                if args[:2] == ("git", "ls-remote"):
                    return "b" * 40 + " refs/tags/v3.7.17"
                if args[:2] == ("git", "rev-parse"):
                    return "c" * 40
                if args[:2] == ("bash", "overlay/emit-promotion-manifest.sh"):
                    Path(args[args.index("--output") + 1]).write_text("candidate")
                return ""

            with patch.object(producer, "validate_root_path"), patch.object(producer, "inputs", return_value=config), patch.object(producer, "reviewed_source", return_value=Path("/source")), patch.object(producer, "fetch_release", return_value="b" * 40), patch.object(producer, "install_overlay"), patch.object(producer, "run_gate"), patch.object(producer, "require_unused_tag"), patch.object(producer, "publish", return_value=image), patch.object(producer, "publish_promotion", side_effect=RuntimeError("artifact unavailable")), patch.object(producer, "run", side_effect=execute), self.assertRaisesRegex(RuntimeError, "artifact unavailable"):
                producer.reconcile(state / "config.json", state)
            self.assertEqual(final.read_text(), "last-good")
            self.assertEqual(marker.read_text(), '{"last_good":true}')

    def test_failed_artifact_push_keeps_prior_local_promotion(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            pending = state / "pending.json"
            pending.write_text("new")
            final = state / "promotion-manifest.json"
            final.write_text("old")
            with patch.object(producer, "build_promotion_artifact", return_value="candidate"), patch.object(producer, "run"), patch.object(producer, "publish", side_effect=RuntimeError("push failed")), self.assertRaisesRegex(RuntimeError,"push failed"):
                producer.publish_promotion(pending, "verified-image")
            self.assertEqual(final.read_text(), "old")


if __name__ == "__main__":
    unittest.main()
