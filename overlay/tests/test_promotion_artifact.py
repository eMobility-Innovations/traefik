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


def valid_promotion(image):
    return {"schema_version":1, "source_repo":producer.ORG_REPOSITORY.removesuffix(".git"),
            "upstream_version":"v3.7.17", "upstream_commit":"b"*40, "patch_commit":"c"*40,
            "image_ref":image, "platform":"linux/amd64", "tests_passed":True,
            "tls_canary_passed":True, "provenance":{"build_recipe_sha256":"d"*64,
                                                      "build_timestamp_utc":"2026-11-03T00:00:00Z"}}


def archive(path, names, payload=b'{"test":"fixture"}', wrong_diff_id=False, architecture="amd64"):
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as tar:
        for name, kind in names:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(payload) if kind == tarfile.REGTYPE else 0
            tar.addfile(info, io.BytesIO(payload) if info.size else None)
    layer_bytes = layer.getvalue()
    config = json.dumps({"architecture":architecture, "os":"linux", "rootfs":{"type":"layers", "diff_ids":["sha256:" + ("0"*64 if wrong_diff_id else hashlib.sha256(layer_bytes).hexdigest())]}}).encode()
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

    def test_wrong_platform_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.tar"
            identity = archive(path, [("promotion.json", tarfile.REGTYPE)], architecture="arm64")
            with self.assertRaisesRegex(ValueError, "linux/amd64"):
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
            pending.write_text(json.dumps(valid_promotion("wrong")))
            with patch.object(producer, "run") as run, self.assertRaises(ValueError):
                producer.build_promotion_artifact(pending, producer.IMAGE + "@sha256:" + "a"*64)
            run.assert_not_called()

    def test_real_build_context_is_scratch_and_validated_before_publish(self):
        image = producer.IMAGE + "@sha256:" + "a" * 64
        value = valid_promotion(image)
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
                    identity = archive(Path(args[args.index("--output") + 1]), [("promotion.json", tarfile.REGTYPE)], pending.read_bytes())
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

    def test_published_artifact_is_pulled_by_digest_and_fully_checked(self):
        image = producer.IMAGE + "@sha256:" + "a" * 64
        reference = producer.IMAGE + "@sha256:" + "b" * 64
        value = valid_promotion(image)
        for corrupted in (False, "payload", "diff_id"):
            with self.subTest(corrupted=corrupted), tempfile.TemporaryDirectory() as temporary:
                pending = Path(temporary) / "pending.json"
                pending.write_text(json.dumps(value))
                identity = None

                def execute(*args, _corrupted=corrupted, _pending=pending, **_kwargs):
                    nonlocal identity
                    if args[:3] == ("docker", "image", "save"):
                        payload = b"wrong remote layer" if _corrupted == "payload" else _pending.read_bytes()
                        identity = archive(Path(args[args.index("--output") + 1]),
                                           [("promotion.json",tarfile.REGTYPE)], payload,
                                           wrong_diff_id=_corrupted == "diff_id")
                    if args[:3] == ("docker", "image", "inspect"):
                        return json.dumps({"Id":identity})
                    return ""

                with patch.object(producer, "run", side_effect=execute) as run:
                    if corrupted:
                        with self.assertRaises(ValueError):
                            producer.verify_published_promotion(reference, pending, image)
                    else:
                        producer.verify_published_promotion(reference, pending, image)
                run.assert_any_call("docker", "pull", "--platform", "linux/amd64", reference)
                self.assertFalse(any(call.args[:2] == ("docker", "run") for call in run.call_args_list))

    def test_publisher_requires_remote_readback_before_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            pending = Path(temporary) / "pending.json"
            pending.write_text("candidate")
            with patch.object(producer, "build_promotion_artifact", return_value="candidate"), patch.object(producer, "run"), patch.object(producer, "publish", return_value="immutable-ref"), patch.object(producer, "verify_published_promotion", side_effect=ValueError("remote layer differs")), self.assertRaisesRegex(ValueError,"remote layer differs"):
                producer.publish_promotion(pending, "verified-image")

    def test_native_tick_retry_after_remote_failure_preserves_and_then_advances_state(self):
        image = producer.IMAGE + "@sha256:" + "a" * 64
        artifact = producer.IMAGE + "@sha256:" + "b" * 64
        config = {"source_checkout":"/source", "overlay_commit":"d"*40, "patch_commits":["a"*40],
                  "build_image":"build", "runtime_image":"runtime", "webui_image":"webui"}
        published_tags = set()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            final, marker = state / "promotion-manifest.json", state / "built-inputs.json"
            final.write_text("last-good")
            marker.write_text('{"last_good":true}')

            def execute(*args, **_kwargs):
                if args[:2] == ("git", "ls-remote"):
                    return "b"*40 + " refs/tags/v3.7.17"
                if args[:2] == ("git", "rev-parse"):
                    return "c"*40
                if args[:2] == ("bash", "overlay/emit-promotion-manifest.sh"):
                    Path(args[args.index("--output")+1]).write_text("candidate")
                return ""

            def unused(tag):
                if tag in published_tags:
                    raise ValueError("existing immutable candidate cannot be overwritten")

            def publish(tag):
                if tag == producer.PROMOTION_TAG:
                    return artifact
                published_tags.add(tag)
                return image

            with patch.object(producer, "validate_root_path"), patch.object(producer, "inputs", return_value=config), patch.object(producer, "reviewed_source", return_value=Path("/source")), patch.object(producer, "fetch_release", return_value="b"*40), patch.object(producer, "install_overlay"), patch.object(producer, "run_gate") as gate, patch.object(producer, "require_unused_tag", side_effect=unused), patch.object(producer, "publish", side_effect=publish), patch.object(producer, "build_promotion_artifact", return_value="artifact-candidate"), patch.object(producer, "verify_published_promotion", side_effect=[ValueError("wrong remote layer"), None]), patch.object(producer, "run", side_effect=execute):
                with self.assertRaisesRegex(ValueError, "wrong remote layer"):
                    producer.reconcile(state / "config.json", state)
                self.assertEqual(final.read_text(), "last-good")
                self.assertEqual(marker.read_text(), '{"last_good":true}')
                result = producer.reconcile(state / "config.json", state)
            self.assertEqual(len(published_tags), 2)
            self.assertEqual(gate.call_count, 2)
            self.assertEqual(result["promotion_artifact_ref"], artifact)
            self.assertEqual(final.read_text(), "candidate")
            self.assertNotEqual(marker.read_text(), '{"last_good":true}')

    def test_full_manifest_schema_and_provenance_are_required(self):
        image = producer.IMAGE + "@sha256:" + "a"*64
        cases = [("schema_version",True), ("upstream_version","latest"), ("upstream_commit","short"),
                 ("patch_commit","A"*40), ("tests_passed",1), ("tls_canary_passed",False),
                 ("source_repo","https://example.com/traefik"), ("platform","linux/arm64"),
                 ("image_ref",producer.IMAGE + ":latest"), ("extra","unexpected")]
        provenance = [{"build_recipe_sha256":"short", "build_timestamp_utc":"2026-11-03T00:00:00Z"},
                      {"build_recipe_sha256":"d"*64, "build_timestamp_utc":"2026-02-30T00:00:00Z"},
                      {"build_recipe_sha256":"d"*64, "build_timestamp_utc":"2026-11-03"},
                      {"build_recipe_sha256":"d"*64, "build_timestamp_utc":"2026-11-03T00:00:00+00:00"},
                      {"build_recipe_sha256":"d"*64, "build_timestamp_utc":"2026-11-03T00:00:00Z", "extra":True}]
        with tempfile.TemporaryDirectory() as temporary:
            pending = Path(temporary) / "pending.json"
            pending.write_text(json.dumps(valid_promotion(image)))
            self.assertEqual(producer.promotion_payload(pending,image), pending.read_bytes())
            for field, value in cases:
                candidate = valid_promotion(image)
                candidate[field] = value
                pending.write_text(json.dumps(candidate))
                with self.subTest(field=field), self.assertRaises(ValueError):
                    producer.promotion_payload(pending,image)
            for value in provenance:
                candidate = valid_promotion(image)
                candidate["provenance"] = value
                pending.write_text(json.dumps(candidate))
                with self.subTest(provenance=value), self.assertRaises(ValueError):
                    producer.promotion_payload(pending,image)
            for field in valid_promotion(image):
                candidate = valid_promotion(image)
                del candidate[field]
                pending.write_text(json.dumps(candidate))
                with self.subTest(missing=field), self.assertRaises(ValueError):
                    producer.promotion_payload(pending,image)

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
