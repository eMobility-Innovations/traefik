"""Producer trust controls must refuse unsafe sources before publishing."""
import hashlib
import importlib.util
import io
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("producer", Path(__file__).parents[1] / "reconcile.py")
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)


class SecurityTests(unittest.TestCase):
    def test_configurable_success_gate_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(producer, "validate_root_path"):
            config = Path(temporary) / "config.json"
            config.write_text('{"gate_command":"/bin/true"}')
            with self.assertRaises(ValueError):
                producer.inputs(config)
        self.assertEqual(producer.GATE_COMMAND, Path("/usr/local/libexec/traefik-readiness-gate"))

    def test_gate_is_checked_before_execution(self):
        with patch.object(producer, "validate_root_path", side_effect=ValueError("unsafe gate")), patch.object(producer, "run") as run, self.assertRaisesRegex(ValueError, "unsafe gate"):
            producer.run_gate(Path("/candidate"))
        run.assert_not_called()

    def test_local_hook_or_filter_configuration_is_refused(self):
        for extra in ('[include]\npath=/tmp/evil\n', '[filter "evil"]\nsmudge=/tmp/evil\n', '[core]\nhooksPath=/tmp/evil\n'):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                producer.validate_mirror_config(extra)

    def test_hermetic_git_drops_environment_configuration(self):
        with patch.dict(producer.os.environ, {"GIT_CONFIG_COUNT":"1", "GIT_CONFIG_KEY_0":"core.hooksPath", "GIT_CONFIG_VALUE_0":"/evil", "GIT_EXEC_PATH":"/evil"}), patch.object(producer.subprocess, "run") as command:
            command.return_value.stdout = ""
            producer.run("git", "status")
            args = command.call_args
        self.assertEqual(args.kwargs["env"]["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(args.kwargs["env"]["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(args.kwargs["env"]["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(args.kwargs["env"]["GIT_GRAFT_FILE"], "/dev/null")
        self.assertIn("user.name=Traefik Readiness Automation", args.args[0])
        self.assertNotIn("GIT_CONFIG_COUNT", args.kwargs["env"])
        self.assertNotIn("GIT_EXEC_PATH", args.kwargs["env"])
        self.assertIn("core.hooksPath=/dev/null", args.args[0])
        self.assertIn("core.fsmonitor=false", args.args[0])

    def test_root_path_rejects_symlink_foreign_owner_and_writable_parent(self):
        safe = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        for unsafe in (SimpleNamespace(st_mode=stat.S_IFLNK | 0o755, st_uid=0),
                       SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=1000),
                       SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=0)):
            with self.subTest(unsafe=unsafe), patch.object(Path, "lstat", side_effect=[safe, unsafe]), self.assertRaisesRegex(ValueError, "unsafe root-owned path"):
                producer.validate_root_path(Path("/safe/source"))

    def test_existing_candidate_tag_is_never_overwritten(self):
        with patch.object(producer, "run", return_value="existing"), self.assertRaisesRegex(ValueError, "already exists"):
            producer.require_unused_tag("candidate")

    def test_registry_transport_error_is_not_treated_as_missing(self):
        error = producer.subprocess.CalledProcessError(1, ["docker"], stderr="connection refused")
        with patch.object(producer, "run", side_effect=error), self.assertRaises(producer.subprocess.CalledProcessError):
            producer.require_unused_tag("candidate")

    def test_arbitrary_immutable_build_input_is_refused(self):
        with patch.object(producer, "trusted_build_inputs", return_value={"build_image":"trusted", "runtime_image":"trusted", "webui_image":"trusted"}), self.assertRaisesRegex(ValueError, "trusted build lock"):
            producer.validate_build_inputs({"build_image":"evil@sha256:" + "a"*64,
                                            "runtime_image":"trusted", "webui_image":"trusted"})

    def test_tag_binds_entire_input_fingerprint(self):
        with patch.object(producer, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="f"*32)), create=True):
            self.assertNotEqual(producer.candidate_tag("v3.7.17", {"overlay":"a"}),
                                producer.candidate_tag("v3.7.17", {"overlay":"b"}))

    def test_same_fingerprint_retry_uses_a_fresh_immutable_tag(self):
        first = producer.candidate_tag("v3.7.17", {"overlay":"same"})
        second = producer.candidate_tag("v3.7.17", {"overlay":"same"})
        self.assertNotEqual(first, second)

    def test_modern_docker_manifest_id_is_not_the_config_id(self):
        metadata = {"Id": "sha256:" + "b" * 64, "Descriptor": {
            "digest": "sha256:" + "b" * 64,
            "annotations": {"config.digest": "sha256:" + "c" * 64}}}
        with patch.object(producer, "run", return_value=producer.json.dumps(metadata)), patch.object(producer, "saved_config_digest", return_value="sha256:"+"c"*64, create=True) as saved:
            self.assertEqual(producer.local_config_digest("candidate"), "sha256:" + "c" * 64)
        saved.assert_called_once_with("candidate")

    def test_descriptor_without_config_identity_is_refused(self):
        metadata = {"Id": "sha256:" + "b" * 64, "Descriptor": {"digest": "sha256:" + "b" * 64}}
        with patch.object(producer, "run", return_value=producer.json.dumps(metadata)), self.assertRaises(ValueError):
            producer.local_config_digest("candidate")

    def test_modern_index_without_annotations_uses_saved_platform_config(self):
        self.check_saved_config("amd64")

    def test_saved_configuration_refuses_the_wrong_platform(self):
        with self.assertRaisesRegex(ValueError,"linux/amd64"):
            self.check_saved_config("arm64")

    def check_saved_config(self, architecture):
        config = producer.json.dumps({"architecture":architecture,"os":"linux"}).encode()
        metadata = {"Id":"sha256:"+"b"*64,"Descriptor":{
            "digest":"sha256:"+"b"*64,"mediaType":"application/vnd.oci.image.index.v1+json"}}

        def execute(*args, **_kwargs):
            if args[:3] == ("docker","image","inspect"):
                return producer.json.dumps(metadata)
            if args[:3] == ("docker","image","save"):
                path = Path(args[args.index("--output")+1])
                manifest = producer.json.dumps([{"Config":"config.json","Layers":[]}]).encode()
                with tarfile.open(path,"w") as archive:
                    for name, payload in (("manifest.json",manifest),("config.json",config)):
                        member = tarfile.TarInfo(name)
                        member.size = len(payload)
                        archive.addfile(member,io.BytesIO(payload))
            return ""

        with patch.object(producer,"run",side_effect=execute) as run:
            self.assertEqual(producer.local_config_digest("candidate"),"sha256:"+hashlib.sha256(config).hexdigest())
        calls = [call.args for call in run.call_args_list if call.args[:3] == ("docker","image","save")]
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][3:5],("--platform","linux/amd64"))

    def test_docker29_banner_push_stdout_succeeds_via_registry_descriptor(self):
        for media_type in ("application/vnd.docker.distribution.manifest.v2+json",
                          "application/vnd.oci.image.manifest.v1+json"):
            digest = "sha256:" + "a" * 64
            config = "sha256:" + "c" * 64
            descriptor = producer.json.dumps({"mediaType": media_type, "size": 1382,
                                              "digest": digest, "schemaVersion": 2})
            readback = producer.json.dumps({"schemaVersion": 2, "mediaType": media_type,
                                            "config": {"mediaType": "application/vnd.docker.container.image.v1+json",
                                                       "size": 3286, "digest": config}})
            banner = "Info: Using --platform linux/amd64\ncandidate"
            with self.subTest(media_type=media_type), patch.object(producer, "run", side_effect=[banner, descriptor, readback, producer.json.dumps({"Id": config})]) as run:
                self.assertEqual(producer.publish("candidate"), producer.IMAGE + "@" + digest)
            run.assert_any_call("docker", "buildx", "imagetools", "inspect", "--format", "{{json .Manifest}}", "candidate")
            run.assert_any_call("docker", "buildx", "imagetools", "inspect", "--raw", producer.IMAGE + "@" + digest)
            self.assertFalse(any(".RepoDigests" in str(call) for call in run.call_args_list))

    def test_live_single_image_descriptor_without_schema_is_supported(self):
        digest = "sha256:" + "a" * 64
        config = "sha256:" + "c" * 64
        media = "application/vnd.oci.image.manifest.v1+json"
        descriptor = producer.json.dumps({"digest": digest, "mediaType": media, "size": 1246})
        readback = producer.json.dumps({"schemaVersion": 2, "mediaType": media, "config": {"digest": config}})
        with patch.object(producer, "run", side_effect=["Info -> --platform\ncandidate\n", descriptor,
                                                          readback, producer.json.dumps({"Id": config})]):
            self.assertEqual(producer.publish("candidate"), producer.IMAGE + "@" + digest)

    def test_misleading_push_stdout_digest_is_ignored(self):
        real = "sha256:" + "a" * 64
        fake = "sha256:" + "b" * 64
        config = "sha256:" + "c" * 64
        banner = "Info: Using --platform linux/amd64\n" + producer.IMAGE + ":v3.7.17-candidate\n" + fake
        descriptor = producer.json.dumps({"mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                                          "size": 1382, "digest": real, "schemaVersion": 2})
        readback = producer.json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.docker.distribution.manifest.v2+json", "config": {"digest": config}})
        with patch.object(producer, "run", side_effect=[banner, descriptor, readback, producer.json.dumps({"Id": config})]):
            self.assertEqual(producer.publish("candidate"), producer.IMAGE + "@" + real)

    def test_malformed_or_absent_descriptor_digest_is_refused(self):
        digest = "sha256:" + "a" * 64
        media = "application/vnd.docker.distribution.manifest.v2+json"
        cases = [{"mediaType": media, "size": 1, "schemaVersion": 2},
                 {"mediaType": media, "size": 1, "digest": "SHA256:" + "A" * 64, "schemaVersion": 2},
                 {"mediaType": media, "size": 1, "digest": "sha256:" + "a" * 63, "schemaVersion": 2},
                 {"mediaType": media, "size": 1, "digest": "sha256:" + "a" * 65, "schemaVersion": 2},
                 {"mediaType": media, "size": 1, "digest": "sha256:" + "g" * 64, "schemaVersion": 2},
                 {"mediaType": media, "size": 1, "digest": 1, "schemaVersion": 2},
                 None, [digest]]
        for value in cases:
            with self.subTest(value=value), patch.object(producer, "run", side_effect=["", producer.json.dumps(value)]), self.assertRaises(ValueError):
                producer.publish("candidate")

    def test_index_and_wrong_schema_descriptors_are_refused(self):
        digest = "sha256:" + "a" * 64
        media = "application/vnd.docker.distribution.manifest.v2+json"
        cases = [{"mediaType": "application/vnd.oci.image.index.v1+json", "size": 1, "digest": digest, "schemaVersion": 2},
                 {"mediaType": "application/vnd.docker.distribution.manifest.list.v2+json", "size": 1, "digest": digest, "schemaVersion": 2},
                 {"mediaType": media, "size": 1, "digest": digest, "schemaVersion": 1},
                 {"mediaType": media, "size": 1, "digest": digest, "schemaVersion": "2"},
                 {"mediaType": "application/vnd.unknown.image.v0+json", "size": 1, "digest": digest, "schemaVersion": 2}]
        for value in cases:
            with self.subTest(value=value), patch.object(producer, "run", side_effect=["", producer.json.dumps(value)]), self.assertRaises(ValueError):
                producer.publish("candidate")

    def test_push_selects_only_the_tested_linux_amd64_manifest(self):
        digest = "sha256:" + "a" * 64
        config = "sha256:" + "c" * 64
        descriptor = producer.json.dumps({"mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                                          "size": 1382, "digest": digest, "schemaVersion": 2})
        readback = producer.json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.docker.distribution.manifest.v2+json", "config": {"digest": config}})
        with patch.object(producer, "run", side_effect=["", descriptor, readback, producer.json.dumps({"Id": config})]) as run:
            producer.publish("candidate")
        run.assert_any_call("docker", "push", "--platform", "linux/amd64", "--quiet", "candidate")

    def test_unexpected_index_response_is_refused(self):
        digest = "sha256:" + "a" * 64
        index = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": []}
        descriptor = producer.json.dumps({"mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                                          "size": 1382, "digest": digest, "schemaVersion": 2})
        with patch.object(producer, "run", side_effect=["", descriptor, producer.json.dumps(index), producer.json.dumps({"Id": "sha256:" + "c" * 64})]), self.assertRaisesRegex(ValueError, "current candidate"):
            producer.publish("candidate")

    def test_immutable_readback_schema_is_validated_on_the_raw_manifest(self):
        digest = "sha256:" + "a" * 64
        config = "sha256:" + "c" * 64
        media = "application/vnd.oci.image.manifest.v1+json"
        descriptor = producer.json.dumps({"digest": digest, "mediaType": media, "size": 1246})
        for schema in (None, 1, True, 2.0, "2"):
            raw = producer.json.dumps({"schemaVersion": schema, "mediaType": media, "config": {"digest": config}})
            with self.subTest(schema=schema), patch.object(producer, "run", side_effect=["", descriptor, raw]), self.assertRaises(ValueError):
                producer.publish("candidate")

    def test_descriptor_size_is_a_bounded_positive_integer(self):
        for size in (None, 0, -1, True, 2.0, "1246", 1024 * 1024 + 1):
            descriptor = {"digest": "sha256:" + "a" * 64,
                          "mediaType": "application/vnd.oci.image.manifest.v1+json", "size": size}
            with self.subTest(size=size), self.assertRaises(ValueError):
                producer.registry_descriptor_digest(descriptor)

    def test_unavailable_tested_platform_cannot_advance_publication(self):
        error = producer.subprocess.CalledProcessError(1, ["docker", "push"], stderr="linux/amd64 platform unavailable")
        with patch.object(producer, "run", side_effect=error) as run, self.assertRaises(producer.subprocess.CalledProcessError):
            producer.publish("candidate")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args_list[0].args, ("docker", "push", "--platform", "linux/amd64", "--quiet", "candidate"))

    def test_registry_manifest_must_match_candidate_config(self):
        digest = "sha256:" + "a" * 64
        descriptor = producer.json.dumps({"mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                                          "size": 1382, "digest": digest, "schemaVersion": 2})
        with patch.object(producer, "run", side_effect=["", descriptor, producer.json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.docker.distribution.manifest.v2+json', 'config': {'digest': 'sha256:' + 'b' * 64}}), producer.json.dumps({'Id': 'sha256:' + 'c' * 64})]), self.assertRaisesRegex(ValueError, "current candidate"):
            producer.publish("candidate")


if __name__ == "__main__":
    unittest.main()
