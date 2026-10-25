#!/usr/bin/env python3
"""Offline identity/transport fixtures. No Docker process or daemon is used."""

import argparse
import base64
from contextlib import contextmanager
import copy
import io
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import managed_image_identity as identity

ENGINE = "a" * 40
ARCH = "amd64"
SECRET = "fixture-config-env-must-not-appear-in-proof"


def encode(value):
    return (json.dumps(value, indent=1) + "\n").encode()


def desc(raw, media):
    return {"digest": identity.digest(raw), "size": len(raw), "mediaType": media}


def archive(members, end=True):
    result = bytearray()
    for name, raw, kind in members:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.size = len(raw)
        result.extend(member.tobuf(format=tarfile.USTAR_FORMAT))
        result.extend(raw)
        result.extend(bytes((-len(raw)) % 512))
    if end:
        result.extend(bytes(1024))
    return bytes(result)


class Image:
    def __init__(self, *, classic=False, legacy=False, direct=False, architecture=ARCH, config_engine=ENGINE, salt="", index_children=None):
        self.classic, self.legacy = classic, legacy
        layer = b"uncompressed-layer-" + salt.encode() if classic else b"gzip-representation-" + salt.encode()
        self.layer = layer
        self.config_raw = encode({"architecture": architecture, "os": "linux", "config": {
            "Labels": {identity.REVISION_LABEL: config_engine}, "Env": [SECRET], "WorkingDir": "/" + salt},
            "rootfs": {"type": "layers", "diff_ids": [identity.digest(layer) if classic else "sha256:" + "1" * 64]}})
        self.config = identity.digest(self.config_raw)
        self.config_desc = desc(self.config_raw, "application/vnd.oci.image.config.v1+json")
        manifest_raw = encode({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                               "config": self.config_desc, "layers": [desc(layer, "application/vnd.oci.image.layer.v1.tar" if classic else "application/vnd.oci.image.layer.v1.tar+gzip")]})
        self.manifest = desc(manifest_raw, "application/vnd.oci.image.manifest.v1+json")
        child = {**self.manifest, "platform": {"os": "linux", "architecture": architecture}}
        index_raw = encode({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                            "manifests": index_children if index_children is not None else [child]})
        self.root = self.manifest if direct else desc(index_raw, "application/vnd.oci.image.index.v1+json")
        self.runtime = self.config if classic else self.root["digest"]
        wrapper = encode({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [self.root]})
        self.files = [(identity.blob_path(self.config), self.config_raw, tarfile.REGTYPE),
                      (identity.blob_path(identity.digest(layer)), layer, tarfile.REGTYPE),
                      (identity.blob_path(self.manifest["digest"]), manifest_raw, tarfile.REGTYPE)]
        if not direct:
            self.files.append((identity.blob_path(self.root["digest"]), index_raw, tarfile.REGTYPE))
        self.files += [("index.json", wrapper, tarfile.REGTYPE), ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}', tarfile.REGTYPE)]
        if classic:
            config_path = self.config[7:] + ".json" if legacy else identity.blob_path(self.config)
            layer_path = "b" * 64 + "/layer.tar" if legacy else identity.blob_path(identity.digest(layer))
            if legacy:
                self.files = [(config_path, self.config_raw, tarfile.REGTYPE), (layer_path, layer, tarfile.REGTYPE)]
            self.files.append(("manifest.json", encode([{"Config": config_path, "RepoTags": ["fixture:staged"], "Layers": [layer_path]}]), tarfile.REGTYPE))
        self.inspect = {"Id": self.runtime, "Os": "linux", "Architecture": ARCH,
                        "Config": {"Labels": {identity.REVISION_LABEL: ENGINE}}}
        if not classic:
            self.inspect.update(Descriptor=self.root, Manifests=[{"ID": self.manifest["digest"], "Descriptor": self.manifest,
                "Available": True, "Kind": "image", "ImageData": {"Platform": {"os": "linux", "architecture": ARCH}}}])

    def bytes(self):
        return archive(self.files)

    def proof(self):
        return identity.prove_export(io.BytesIO(self.bytes()), self.runtime, self.config, ENGINE, ARCH,
                                     None if self.classic else self.root, None if self.classic else self.manifest)

    def replace(self, name, raw):
        self.files = [(path, raw if path == name else previous, kind) for path, previous, kind in self.files]


def daemon():
    return {"id": "fixture-daemon", "root": "/fixture/docker", "driver": "overlayfs", "server_version": "29.fixture",
            "version": "29.fixture", "api_version": "1.56", "git_commit": "fixture-commit",
            "driver_status": [["driver-type", "io.containerd.snapshotter.v1"]],
            "components": [{"name": "Engine", "version": "29.fixture", "details": {"GitCommit": "fixture-commit"}}]}


def binding(controller=None, runner=None):
    return {"schema_version": 1, "mode": identity.CAPABILITY, "engine": ENGINE, "architecture": ARCH,
            "daemon": daemon(), "roles": {"controller": (controller or Image()).proof(), "runner": (runner or Image(salt="runner")).proof()}}


class FakeDocker:
    def __init__(self, images=None):
        self.images = images or {"controller:canonical": Image(), "runner:canonical": Image(salt="runner")}
        self.by_id = {image.runtime: image for image in self.images.values()}
        self.inspects, self.exports = [], []
        self.daemon = daemon()
        self.export_error = None

    def inspect(self, reference):
        self.inspects.append(reference)
        image = self.images.get(reference) or self.by_id.get(reference)
        if image is None:
            raise identity.IdentityError("fixture image missing")
        return copy.deepcopy(image.inspect)

    def identity(self):
        return copy.deepcopy(self.daemon)

    @contextmanager
    def export(self, runtime):
        self.exports.append(runtime)
        if self.export_error:
            raise self.export_error
        yield io.BytesIO(self.by_id[runtime].bytes())


class ParserTests(unittest.TestCase):
    def test_portable_original_index_is_distinct_from_wrapper_config_and_manifest(self):
        image = Image()
        proof = image.proof()
        self.assertEqual(proof["runtime_image_id"], image.runtime)
        self.assertEqual(proof["config_digest"], image.config)
        self.assertEqual(proof["manifest_digest"], image.manifest["digest"])
        self.assertEqual(len({image.runtime, image.config, image.manifest["digest"]}), 3)
        self.assertNotEqual(identity.digest(dict((n, r) for n, r, _ in image.files)["index.json"]), image.runtime)
        self.assertNotIn(SECRET, json.dumps(proof))
        self.assertNotIn(base64.b64encode(image.config_raw).decode(), json.dumps(proof))
        identity.validate_role(proof, ENGINE, ARCH)

    def test_direct_manifest_root(self):
        image = Image(direct=True)
        proof = image.proof()
        self.assertEqual(proof["runtime_image_id"], proof["manifest_digest"])
        identity.validate_role(proof, ENGINE, ARCH)

    def test_classic_modern_and_legacy_actual_layouts(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                image = Image(classic=True, legacy=legacy)
                proof = image.proof()
                self.assertEqual(proof["runtime_image_id"], proof["config_digest"])
                self.assertIsNone(proof["manifest_digest"])
                identity.validate_role(proof, ENGINE, ARCH)

    def test_arbitrary_blob_order_and_directories(self):
        image = Image()
        image.files = [("blobs/", b"", tarfile.DIRTYPE), ("blobs/sha256/", b"", tarfile.DIRTYPE)] + list(reversed(image.files))
        self.assertEqual(image.proof()["config_digest"], image.config)

    def assert_bad(self, image, message=None):
        with self.assertRaises(identity.IdentityError) as caught:
            image.proof()
        if message:
            self.assertIn(message, str(caught.exception))

    def test_wrong_reviewed_config_with_same_revision(self):
        image = Image()
        image.config = "sha256:" + "f" * 64
        self.assert_bad(image, "different config")

    def test_wrong_original_source_and_platform(self):
        for values in ({"config_engine": "b" * 40}, {"architecture": "arm64"}):
            with self.subTest(values=values):
                self.assert_bad(Image(**values))

    def test_all_digest_addressed_blobs_are_hashed_including_unselected_layers(self):
        image = Image()
        image.files.append((identity.blob_path("sha256:" + "e" * 64), b"arbitrary-extra-layer", tarfile.REGTYPE))
        self.assert_bad(image, "hash mismatch")

    def test_corruption_of_root_manifest_config_and_layer(self):
        original = Image()
        for name, raw, _ in original.files[:4]:
            with self.subTest(name=name):
                image = Image()
                image.replace(name, raw + b" ")
                self.assert_bad(image, "hash mismatch")

    def test_selected_full_closure_missing(self):
        original = Image()
        for name, _, _ in original.files[:4]:
            with self.subTest(name=name):
                image = Image()
                image.files = [member for member in image.files if member[0] != name]
                self.assert_bad(image)

    def test_wrapper_is_never_substitute_for_missing_original_root(self):
        image = Image()
        image.files = [member for member in image.files if member[0] != identity.blob_path(image.runtime)]
        self.assert_bad(image, "missing")

    def test_wrapper_must_name_exact_requested_root(self):
        image = Image()
        image.replace("index.json", encode({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [image.manifest]}))
        self.assert_bad(image, "wrapper")

    def test_wrapper_without_media_type_is_outside_pinned_export_protocol(self):
        # Pinned containerd exporter ociIndexRecord explicitly sets MediaType;
        # classic saveSession.save does too. No absent-type variant is assumed.
        image = Image()
        image.replace("index.json", encode({"schemaVersion": 2, "manifests": [image.root]}))
        self.assert_bad(image, "metadata type")

    def test_original_index_rejects_ambiguous_wrong_platform_and_nesting(self):
        image = Image()
        for children in ([{**image.manifest, "platform": {"os": "linux", "architecture": ARCH}}] * 2,
                         [{**image.manifest, "platform": {"os": "linux", "architecture": "arm64"}}],
                         [{**image.root, "platform": {"os": "linux", "architecture": ARCH}}]):
            with self.subTest(children=children):
                self.assert_bad(Image(index_children=children))

    def test_bad_descriptor_sizes_and_media_types(self):
        for field, value in (("size", True), ("size", -1), ("size", identity.MAX_OBJECT + 1),
                             ("mediaType", "application/unknown"), ("digest", "sha256:short"), ("urls", ["https://fixture"])):
            with self.subTest(field=field, value=value):
                image = Image()
                image.root[field] = value
                self.assert_bad(image)

    def test_truncated_header_body_padding_and_end(self):
        raw = Image().bytes()
        for cut in (0, 1, 511, 513, 1023, len(raw) - 1024, len(raw) - 512, len(raw) - 1):
            with self.subTest(cut=cut), self.assertRaises(identity.IdentityError):
                image = Image()
                identity.prove_export(io.BytesIO(raw[:cut]), image.runtime, image.config, ENGINE, ARCH, image.root, image.manifest)

    def test_end_requires_full_zero_blocks_and_complete_response(self):
        image = Image()
        for tail in (b'{"message":"late export error"}', b"\0", bytes(identity.MAX_PADDING + 512), bytes(511) + b"x"):
            with self.subTest(tail_size=len(tail)), self.assertRaises(identity.IdentityError):
                identity.prove_export(io.BytesIO(image.bytes() + tail), image.runtime, image.config, ENGINE, ARCH, image.root, image.manifest)
        self.assertEqual(identity.prove_export(io.BytesIO(image.bytes() + bytes(1024)), image.runtime, image.config, ENGINE, ARCH, image.root, image.manifest)["config_digest"], image.config)

    def test_zero_block_followed_by_nonzero_header(self):
        image = Image()
        raw = archive(image.files, end=False) + bytes(512) + archive([("index.json", b"{}", tarfile.REGTYPE)])
        with self.assertRaisesRegex(identity.IdentityError, "two zero"):
            identity.prove_export(io.BytesIO(raw), image.runtime, image.config, ENGINE, ARCH, image.root, image.manifest)

    def test_checksum_and_nonzero_member_padding(self):
        image = Image()
        raw = bytearray(image.bytes())
        raw[0] ^= 1
        with self.assertRaisesRegex(identity.IdentityError, "checksum"):
            identity.prove_export(io.BytesIO(raw), image.runtime, image.config, ENGINE, ARCH, image.root, image.manifest)
        raw = bytearray(image.bytes())
        raw[512 + len(image.config_raw)] = 1
        with self.assertRaisesRegex(identity.IdentityError, "padding"):
            identity.prove_export(io.BytesIO(raw), image.runtime, image.config, ENGINE, ARCH, image.root, image.manifest)

    def test_duplicates_links_special_files_extensions_and_unsafe_paths(self):
        for name in ("/escape", "../escape", "a/../escape", "./index.json", "a//b", "a\\b", "a/./b", "a b"):
            with self.subTest(name=name):
                image = Image()
                image.files.insert(0, (name, b"", tarfile.REGTYPE))
                self.assert_bad(image)
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE, tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_SPARSE):
            with self.subTest(kind=kind):
                image = Image()
                image.files.insert(0, ("unsupported", b"", kind))
                self.assert_bad(image)
        image = Image()
        image.files.append(image.files[0])
        self.assert_bad(image, "duplicate")
        image = Image()
        image.files[:0] = [("blobs", b"", tarfile.DIRTYPE), ("blobs/", b"", tarfile.DIRTYPE)]
        self.assert_bad(image, "duplicate")

    def test_directory_payload_and_unexpected_members(self):
        for member in (("blobs", b"x", tarfile.DIRTYPE), ("unexpected", b"x", tarfile.REGTYPE), ("other/", b"", tarfile.DIRTYPE)):
            with self.subTest(member=member):
                image = Image()
                image.files.insert(0, member)
                self.assert_bad(image)

    def test_member_object_retained_and_stream_bounds(self):
        for constant, limit in (("MAX_MEMBERS", 2), ("MAX_OBJECT", 64), ("MAX_METADATA", 64), ("MAX_STREAM", 1024)):
            with self.subTest(constant=constant), patch.object(identity, constant, limit), self.assertRaises(identity.IdentityError):
                Image().proof()

    def test_duplicate_json_keys_nonfinite_and_depth(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":{"x":0,"x":1}}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":1e9999}', b'[' * 33 + b'0' + b']' * 33):
            with self.subTest(raw=raw), self.assertRaises(identity.IdentityError):
                identity.json_value(raw)
        self.assertEqual(identity.json_value(b'{"quoted":"[[[\\\""}'), {"quoted": '[[["'})

    def test_original_config_duplicate_key(self):
        image = Image(classic=True, legacy=True)
        raw = image.config_raw.replace(b'"architecture": "amd64"', b'"architecture": "amd64", "architecture": "amd64"')
        previous = image.config
        image.config = image.runtime = identity.digest(raw)
        image.files = [(image.config[7:] + ".json", raw, tarfile.REGTYPE),
                       ("b" * 64 + "/layer.tar", image.layer, tarfile.REGTYPE),
                       ("manifest.json", encode([{"Config": image.config[7:] + ".json", "Layers": ["b" * 64 + "/layer.tar"]}]), tarfile.REGTYPE)]
        self.assertNotEqual(previous, image.config)
        self.assert_bad(image, "duplicate")

    def test_classic_does_not_accept_wrong_layer_order_or_opaque_runtime(self):
        image = Image(classic=True, legacy=True)
        image.replace("b" * 64 + "/layer.tar", b"different-layer")
        self.assert_bad(image, "diff ID")
        image = Image(classic=True)
        image.runtime = "sha256:" + "9" * 64
        self.assert_bad(image, "must equal")

    def test_stream_read_failure_after_valid_archive_invalidates_proof(self):
        class FailedEnding(io.BytesIO):
            def read(self, count):
                result = super().read(count)
                if not result:
                    raise OSError("fixture failed export transport")
                return result
        image = Image()
        with self.assertRaises(OSError):
            identity.prove_export(FailedEnding(image.bytes()), image.runtime, image.config, ENGINE, ARCH, image.root, image.manifest)

    def test_unused_compatibility_metadata_still_rejects_duplicate_json_keys(self):
        image = Image()
        image.files.append(("manifest.json", b'{"ignored":0,"ignored":1}', tarfile.REGTYPE))
        self.assert_bad(image, "duplicate")

    def test_selected_layer_descriptor_size_mismatch(self):
        image = Image()
        manifest_raw = next(raw for name, raw, _ in image.files if name == identity.blob_path(image.manifest["digest"]))
        manifest = json.loads(manifest_raw)
        manifest["layers"][0]["size"] += 1
        manifest_raw = encode(manifest)
        old_manifest = image.manifest
        image.manifest = desc(manifest_raw, old_manifest["mediaType"])
        image.root = image.manifest
        image.runtime = image.manifest["digest"]
        image.files = [member for member in image.files if member[0] not in {
            identity.blob_path(old_manifest["digest"]), "index.json"}]
        image.files += [(identity.blob_path(image.manifest["digest"]), manifest_raw, tarfile.REGTYPE),
                        ("index.json", encode({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [image.root]}), tarfile.REGTYPE)]
        self.assert_bad(image, "size mismatched")

    def test_fragmented_body_uses_bounded_reads(self):
        image = Image()
        class Fragments(io.BytesIO):
            def read(self, count):
                self_case.assertLessEqual(count, identity.CHUNK)
                return super().read(min(count, 17))
        self_case = self
        result = identity.prove_export(Fragments(image.bytes()), image.runtime, image.config, ENGINE, ARCH, image.root, image.manifest)
        self.assertEqual(result["config_digest"], image.config)


class FakeSocket:
    def __init__(self, raw, chunk=19):
        self.raw = io.BytesIO(raw)
        self.chunk = chunk
        self.timeouts = []

    def recv(self, count):
        return self.raw.read(min(count, self.chunk))

    def settimeout(self, timeout):
        self.timeouts.append(timeout)


def http_body(raw):
    return identity.HTTPBody(identity.SocketReader(FakeSocket(raw), identity.time.monotonic() + 10))


def read_all(body):
    result = bytearray()
    while True:
        chunk = body.read(identity.CHUNK)
        if not chunk:
            return bytes(result)
        result.extend(chunk)


class TransportTests(unittest.TestCase):
    def test_fixed_chunked_and_connection_close(self):
        for raw in (b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello",
                    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nhe\r\n3\r\nllo\r\n0\r\n\r\n",
                    b"HTTP/1.0 200 OK\r\n\r\nhello"):
            with self.subTest(raw=raw):
                self.assertEqual(read_all(http_body(raw)), b"hello")

    def test_http_rejects_short_extra_ambiguous_status_and_bad_chunks(self):
        cases = [b"HTTP/1.1 500 Failed\r\n\r\n", b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhi",
                 b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhierror",
                 b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\nhi",
                 b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n",
                 b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\n\r\n",
                 b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nh",
                 b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nhiXX",
                 b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\nerror",
                 b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nError: failed\r\n\r\n",
                 b"HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\n",
                 b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n"]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(identity.IdentityError):
                read_all(http_body(raw))

    def test_overall_deadline_rejects_trickling_response(self):
        reader = identity.SocketReader(FakeSocket(b"anything"), 10)
        with patch.object(identity.time, "monotonic", return_value=11), self.assertRaisesRegex(identity.IdentityError, "deadline"):
            reader.read(1)

    def test_export_request_is_full_runtime_and_has_no_platform_filter(self):
        client, image, paths = identity.Docker(), Image(), []
        @contextmanager
        def response(path):
            paths.append(path)
            yield io.BytesIO(image.bytes())
        with patch.object(client, "response", response), client.export(image.runtime) as stream:
            self.assertTrue(stream.read(1))
        self.assertEqual(paths, ["/images/get?names=" + image.runtime.replace(":", "%3A")])
        self.assertNotIn("platform", paths[0])

    def test_full_tar_http_combination_rejects_late_error_and_truncation(self):
        image = Image()
        raw = image.bytes()
        for framing in (b"Content-Length: " + str(len(raw) + 10).encode() + b"\r\n\r\n" + raw,
                        b"Content-Length: " + str(len(raw)).encode() + b"\r\n\r\n" + raw + b"late-error",
                        b"\r\n" + raw + b"late-error"):
            with self.subTest(framing=framing[:40]), self.assertRaises(identity.IdentityError):
                body = http_body(b"HTTP/1.1 200 OK\r\n" + framing)
                identity.prove_export(body, image.runtime, image.config, ENGINE, ARCH, image.root, image.manifest)


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.images = {"controller:canonical": Image(), "runner:canonical": Image(salt="runner")}
        self.client = FakeDocker(self.images)
        self.binding = binding(*self.images.values())
        self.values = {"CI_FLEET_ENGINE_REF": ENGINE}
        for name, image in zip(identity.ROLES, self.images.values()):
            self.values["CI_FLEET_" + name.upper() + "_IMAGE_DIGEST"] = image.config

    def test_warm_checks_rehash_original_descriptors_and_inspect_full_runtime_without_export(self):
        actual = identity.validate_bindings(self.binding, self.values, client=self.client)
        self.assertEqual(actual, self.binding)
        self.assertEqual(set(self.client.inspects), set(self.client.by_id))
        self.assertEqual(self.client.exports, [])
        self.assertEqual(identity.protected_references(actual), {role[field] for role in actual["roles"].values()
                          for field in ("config_digest", "runtime_image_id", "manifest_digest") if role[field]})

    def test_every_role_binding_field_is_validated(self):
        mutations = {"kind": "opaque", "config_digest": "sha256:" + "f" * 64, "runtime_image_id": "sha256:" + "e" * 64,
                     "manifest_digest": "sha256:" + "d" * 64, "config_size": True, "source_revision": "b" * 40,
                     "os": "windows", "architecture": "arm64", "root_descriptor": None, "descriptors": {}}
        for key, value in mutations.items():
            with self.subTest(key=key):
                candidate = copy.deepcopy(self.binding)
                candidate["roles"]["runner"][key] = value
                with self.assertRaises(identity.IdentityError):
                    identity.validate_bindings(candidate, self.values, client=self.client)

    def test_bad_version_unknown_fields_missing_role_and_opaque_mode(self):
        for key, value in (("schema_version", True), ("schema_version", 2), ("mode", "opaque"), ("roles", {}), ("extra", 1)):
            with self.subTest(key=key):
                candidate = copy.deepcopy(self.binding)
                candidate[key] = value
                with self.assertRaises(identity.IdentityError):
                    identity.validate_bindings(candidate)

    def test_daemon_store_and_software_changes_invalidate_warm_cache(self):
        for key in identity.DAEMON_FIELDS:
            with self.subTest(key=key):
                self.client.daemon = daemon()
                self.client.daemon[key] = [] if key in {"components", "driver_status"} else "different"
                with self.assertRaisesRegex(identity.IdentityError, "daemon/store/software"):
                    identity.validate_bindings(self.binding, client=self.client)

    def test_inspect_self_source_platform_manifest_availability_and_plan_are_required(self):
        mutations = [lambda v: v.update(Id="sha256:" + "e" * 64), lambda v: v.update(Os="windows"),
                     lambda v: v.update(Architecture="arm64"), lambda v: v["Config"]["Labels"].update({identity.REVISION_LABEL: "b" * 40}),
                     lambda v: v["Manifests"][0].update(Available=False), lambda v: v.update(Manifests=[]),
                     lambda v: v["Manifests"].append(v["Manifests"][0]), lambda v: v["Manifests"][0].update(Kind="attestation"),
                     lambda v: v["Manifests"][0].update(ID="sha256:" + "e" * 64),
                     lambda v: v["Manifests"][0]["Descriptor"].update(size=12)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                original = copy.deepcopy(self.images["controller:canonical"].inspect)
                try:
                    mutate(self.images["controller:canonical"].inspect)
                    with self.assertRaises(identity.IdentityError):
                        identity.validate_bindings(self.binding, client=self.client)
                finally:
                    self.images["controller:canonical"].inspect = original

    def test_retained_descriptor_rehash_and_no_raw_config(self):
        candidate = copy.deepcopy(self.binding)
        role = candidate["roles"]["runner"]
        role["descriptors"][role["runtime_image_id"]] = base64.b64encode(b"{}").decode()
        with self.assertRaisesRegex(identity.IdentityError, "hash"):
            identity.validate_bindings(candidate)
        candidate = copy.deepcopy(self.binding)
        candidate["roles"]["runner"]["raw_config"] = SECRET
        with self.assertRaises(identity.IdentityError):
            identity.validate_bindings(candidate)

    def test_policy_and_runtime_environment_must_match(self):
        for key, value in (("CI_FLEET_ENGINE_REF", "b" * 40), ("CI_FLEET_CONTROLLER_IMAGE_DIGEST", "sha256:" + "f" * 64),
                           ("CI_FLEET_RUNNER_RUNTIME_IMAGE_ID", "sha256:" + "e" * 64)):
            with self.subTest(key=key), self.assertRaises(identity.IdentityError):
                identity.validate_bindings(self.binding, {**self.values, key: value})

    def test_cold_export_uses_immutable_id_when_tag_drifts(self):
        runtime = self.images["controller:canonical"].runtime
        original_export = self.client.export
        @contextmanager
        def drift_export(reference):
            self.client.images["controller:canonical"] = Image(salt="wrong-tag-target")
            with original_export(reference) as stream:
                yield stream
        self.client.export = drift_export
        result = identity.cold_proof(self.client, "controller:canonical", self.binding["roles"]["controller"]["config_digest"], ENGINE, ARCH)
        self.assertEqual(result["runtime_image_id"], runtime)
        self.assertEqual(self.client.exports, [runtime])

    def test_direct_manifest_inspect_plan_and_cold_then_warm_proof(self):
        image = Image(direct=True)
        client = FakeDocker({"direct:stage": image})
        root, manifest = identity.inspect_plan(image.inspect, image.runtime, ENGINE, ARCH)
        self.assertEqual(root, manifest)
        role = identity.cold_proof(client, "direct:stage", image.config, ENGINE, ARCH)
        self.assertEqual(role["runtime_image_id"], role["manifest_digest"])
        client.exports.clear()
        proof = binding()
        proof["roles"] = {"controller": role, "runner": role}
        identity.validate_bindings(proof, client=client)
        self.assertEqual(client.exports, [])

    def test_daemon_fingerprint_uses_info_and_version_store_software_fields(self):
        client = identity.Docker()
        info = {"ID": "fixture-daemon", "DockerRootDir": "/fixture/docker", "Driver": "overlayfs", "ServerVersion": "29.fixture",
                "OSType": "linux", "DriverStatus": [["driver-type", "io.containerd.snapshotter.v1"]]}
        version = {"Version": "29.fixture", "ApiVersion": "1.56", "GitCommit": "fixture-commit", "Os": "linux",
                   "Components": [{"Name": "Engine", "Version": "29.fixture", "Details": {"GitCommit": "fixture-commit"}}]}
        with patch.object(client, "get", side_effect=[info, version]) as get:
            self.assertEqual(client.identity(), daemon())
        self.assertEqual([call.args for call in get.call_args_list], [("/info",), ("/version",)])

    def test_producer_discovers_config_in_two_containerd_exports_and_one_classic(self):
        for classic in (False, True):
            with self.subTest(classic=classic):
                image = Image(classic=classic)
                client = FakeDocker({"stage:producer": image})
                proof = identity.cold_proof(client, "stage:producer", None, ENGINE, ARCH)
                self.assertEqual(proof["config_digest"], image.config)
                self.assertEqual(client.exports, [image.runtime] * (1 if classic else 2))

    def test_invalid_daemon_field_shapes_and_unknown_root_descriptor_fields(self):
        changes = [("id", ""), ("root", "relative"), ("driver", []), ("server_version", "changed"),
                   ("components", []), ("components", [{"name": "Engine", "version": "v", "details": []}]),
                   ("driver_status", [["duplicate", "a"], ["duplicate", "b"]])]
        for field, value in changes:
            with self.subTest(field=field):
                candidate = copy.deepcopy(self.binding)
                candidate["daemon"][field] = value
                with self.assertRaises(identity.IdentityError):
                    identity.validate_bindings(candidate)
        candidate = copy.deepcopy(self.binding)
        candidate["roles"]["controller"]["root_descriptor"]["extra"] = True
        with self.assertRaises(identity.IdentityError):
            identity.validate_bindings(candidate)

    def test_invalid_json_types_fail_with_operator_error(self):
        for field, value in (("architecture", []), ("engine", {}), ("roles", []), ("daemon", False)):
            with self.subTest(field=field):
                candidate = copy.deepcopy(self.binding)
                candidate[field] = value
                with self.assertRaises(identity.IdentityError):
                    identity.validate_bindings(candidate)


class FileAndPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="managed-image-offline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Simulate trusted external ancestry without masking fixture modes.
        original_fstat = os.fstat
        temporary_ancestors = {(info.st_dev, info.st_ino)
                               for parent in self.root.parents
                               for info in (parent.stat(),)}
        def fstat(fd):
            result = original_fstat(fd)
            fields = list(result)
            fields[4] = 0
            if (result.st_dev, result.st_ino) in temporary_ancestors:
                fields[0] &= ~0o022
            return os.stat_result(fields)
        self.addCleanup(patch.stopall)
        patch.object(identity.os, "fstat", side_effect=fstat).start()
        self.images = {"controller:canonical": Image(), "runner:canonical": Image(salt="runner")}
        self.client = FakeDocker(self.images)
        self.env, self.metadata = self.root / "candidate.env", self.root / "candidate.json"
        self.release = self.root / "release"
        self.release.mkdir()
        self.values = {"CI_FLEET_ENGINE_REF": ENGINE}
        for name, image in zip(identity.ROLES, self.images.values()):
            self.values["CI_FLEET_" + name.upper() + "_IMAGE_DIGEST"] = image.config
            self.values["CI_FLEET_" + name.upper() + "_IMAGE"] = name + ":canonical"
        self.env.write_text(identity.render_env(self.values))
        self.metadata.write_bytes(encode({"architecture": ARCH, "engine_ref": ENGINE, "config_ref": "b" * 40}))
        (self.release / "engine-capabilities.json").write_bytes(encode({"schema_version": 1, "capabilities": {identity.CAPABILITY: True}}))
        self.args = argparse.Namespace(environment=self.env, metadata=self.metadata, release=self.release, state=None,
                                      runner_reference=None, controller_reference=None, check_only=False)
        patch.object(identity, "Docker", return_value=self.client).start()
        patch.object(identity.platform, "machine", return_value="x86_64").start()

    def run_prepare(self):
        identity.prepare(self.args)
        return json.loads(self.metadata.read_bytes())

    def test_both_roles_then_private_scratch_publication_preserves_policy(self):
        result = self.run_prepare()
        values = identity.parse_env(self.env, allow_unknown=True)
        for name, image in zip(identity.ROLES, self.images.values()):
            self.assertEqual(values["CI_FLEET_" + name.upper() + "_IMAGE_DIGEST"], image.config)
            self.assertEqual(values["CI_FLEET_" + name.upper() + "_RUNTIME_IMAGE_ID"], image.runtime)
        self.assertEqual(set(self.client.exports), set(self.client.by_id))
        self.assertEqual(stat.S_IMODE(self.metadata.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(identity.read_bindings(self.metadata, values), result["managed_image_bindings"])

    def test_second_role_failure_writes_neither_file(self):
        original = self.env.read_bytes(), self.metadata.read_bytes()
        self.images["runner:canonical"].replace(identity.blob_path(self.images["runner:canonical"].config), b"corrupt")
        with self.assertRaises(identity.IdentityError):
            self.run_prepare()
        self.assertEqual((self.env.read_bytes(), self.metadata.read_bytes()), original)
        self.assertEqual(len(self.client.exports), 2)

    def test_second_scratch_replace_failure_restores_both_original_bytes_and_modes(self):
        before = self.env.read_bytes(), self.metadata.read_bytes()
        modes = self.env.stat().st_mode, self.metadata.stat().st_mode
        original_replace = os.replace
        calls = 0
        def fail_second(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("fixture second replacement failure")
            return original_replace(source, destination)
        with patch.object(identity.os, "replace", side_effect=fail_second), self.assertRaises(OSError):
            self.run_prepare()
        self.assertEqual((self.env.read_bytes(), self.metadata.read_bytes()), before)
        self.assertEqual((self.env.stat().st_mode, self.metadata.stat().st_mode), modes)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["candidate.env", "candidate.json", "release"])

    def test_first_scratch_staging_failure_preserves_originals(self):
        before = self.env.read_bytes(), self.metadata.read_bytes()
        with patch.object(identity.tempfile, "mkstemp", side_effect=OSError("fixture no scratch space")), self.assertRaises(OSError):
            self.run_prepare()
        self.assertEqual((self.env.read_bytes(), self.metadata.read_bytes()), before)

    def test_check_only_rejects_preexisting_runtime_field_drift_without_hydration(self):
        self.run_prepare()
        values = identity.parse_env(self.env, allow_unknown=True)
        values["CI_FLEET_RUNNER_RUNTIME_IMAGE_ID"] = "sha256:" + "f" * 64
        self.env.write_text(identity.render_env(values))
        self.args.check_only = True
        self.client.exports.clear()
        before = self.env.read_bytes(), self.metadata.read_bytes()
        with self.assertRaisesRegex(identity.IdentityError, "rendered runtime"):
            self.run_prepare()
        self.assertEqual((self.env.read_bytes(), self.metadata.read_bytes()), before)
        self.assertEqual(self.client.exports, [])

    def test_changed_daemon_reproves_retained_roots_before_drifted_canonical_tags(self):
        self.run_prepare()
        roots = set(self.client.by_id)
        self.client.daemon["id"] = "new-daemon-context"
        self.client.images = {name: Image(salt="wrong-tag-" + name) for name in self.images}
        self.client.exports.clear()
        self.client.inspects.clear()
        self.run_prepare()
        self.assertEqual(set(self.client.exports), roots)
        self.assertTrue(all(reference in roots for reference in self.client.inspects))

    def test_warm_new_outer_config_ref_reuses_proof_and_retained_id_despite_tag_drift(self):
        self.run_prepare()
        state = self.root / "installed.json"
        state.write_bytes(self.metadata.read_bytes())
        state.chmod(0o600)
        self.metadata.write_bytes(encode({"architecture": ARCH, "engine_ref": ENGINE, "config_ref": "c" * 40}))
        self.env.write_text(identity.render_env(self.values))
        self.client.images = {key: Image(salt="drift-" + key) for key in self.images}
        self.client.exports.clear()
        self.client.inspects.clear()
        self.args.state = state
        self.args.check_only = True
        before = state.read_bytes()
        result = self.run_prepare()
        self.assertEqual(result["config_ref"], "c" * 40)
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual(self.client.exports, [])
        self.assertTrue(all(reference.startswith("sha256:") for reference in self.client.inspects))

    def test_check_only_missing_stale_daemon_or_unavailable_proof_never_exports_or_writes(self):
        for failure in ("missing", "daemon", "unavailable", "stale-config"):
            with self.subTest(failure=failure):
                self.set_candidate_binding(None if failure == "missing" else binding(*self.images.values()))
                self.client.daemon = daemon()
                self.client.by_id = {image.runtime: image for image in self.images.values()}
                if failure == "daemon":
                    self.client.daemon["id"] = "different"
                elif failure == "unavailable":
                    self.client.by_id = {}
                elif failure == "stale-config":
                    values = dict(self.values)
                    values["CI_FLEET_RUNNER_IMAGE_DIGEST"] = "sha256:" + "e" * 64
                    self.env.write_text(identity.render_env(values))
                self.args.check_only = True
                before = self.env.read_bytes(), self.metadata.read_bytes()
                with self.assertRaises(identity.IdentityError):
                    self.run_prepare()
                self.assertEqual((self.env.read_bytes(), self.metadata.read_bytes()), before)
                self.assertEqual(self.client.exports, [])

    def set_candidate_binding(self, value):
        self.env.write_text(identity.render_env(self.values))
        metadata = {"architecture": ARCH, "engine_ref": ENGINE}
        if value is not None:
            metadata["managed_image_bindings"] = value
        self.metadata.write_bytes(encode(metadata))

    def test_prepare_override_uses_prepared_reference(self):
        controller = self.images["controller:canonical"]
        self.client.images["controller:prepared"] = controller
        self.client.images["controller:canonical"] = Image(salt="bad-canonical")
        self.args.controller_reference = "controller:prepared"
        self.run_prepare()
        self.assertIn("controller:prepared", self.client.inspects)
        self.assertNotIn("controller:canonical", self.client.inspects)

    def test_runtime_capability_host_architecture_and_policy_are_mandatory(self):
        capability = self.release / "engine-capabilities.json"
        cases = [{"schema_version": 1, "capabilities": {}}, {"schema_version": 1, "capabilities": {identity.CAPABILITY: False}},
                 {"schema_version": 1, "capabilities": {identity.CAPABILITY: 1}}]
        for value in cases:
            with self.subTest(value=value):
                capability.write_bytes(encode(value))
                with self.assertRaises(identity.IdentityError):
                    self.run_prepare()
        capability.write_bytes(encode({"schema_version": 1, "capabilities": {identity.CAPABILITY: True}}))
        with patch.object(identity.platform, "machine", return_value="aarch64"), self.assertRaises(identity.IdentityError):
            self.run_prepare()
        with patch.object(identity.platform, "machine", return_value="unknown"), self.assertRaises(identity.IdentityError):
            self.run_prepare()
        self.env.write_text("CI_FLEET_ENGINE_REF=" + ENGINE + "\n")
        with self.assertRaises(identity.IdentityError):
            self.run_prepare()
        self.assertEqual(self.client.exports, [])

    def test_read_bindings_missing_duplicate_inconsistent_outer_and_private_mode(self):
        for raw in (b'{}', b'{"managed_image_bindings":{},"managed_image_bindings":{}}',
                    encode({"managed_image_bindings": binding(*self.images.values()), "engine_ref": "b" * 40}),
                    encode({"managed_image_bindings": binding(*self.images.values()), "architecture": "arm64"})):
            with self.subTest(raw_size=len(raw)):
                self.metadata.write_bytes(raw)
                self.metadata.chmod(0o600)
                with self.assertRaises(identity.IdentityError):
                    identity.read_bindings(self.metadata, check_daemon=False)
        self.metadata.write_bytes(encode({"managed_image_bindings": binding(*self.images.values())}))
        self.metadata.chmod(0o644)
        with self.assertRaisesRegex(identity.IdentityError, "private"):
            identity.read_bindings(self.metadata, check_daemon=False)

    def test_protected_reader_rejects_links_writable_ancestors_and_oversized_files(self):
        linked = self.root / "linked"
        linked.symlink_to(self.metadata)
        with self.assertRaises(OSError):
            identity.protected_read(linked)
        directory = self.root / "writable"
        directory.mkdir(mode=0o777)
        directory.chmod(0o777)
        (directory / "input").write_text("{}")
        with self.assertRaises(identity.IdentityError):
            identity.protected_read(directory / "input")
        linked_dir = self.root / "linked-dir"
        linked_dir.symlink_to(self.release, target_is_directory=True)
        with self.assertRaises(OSError):
            identity.protected_read(linked_dir / "engine-capabilities.json")
        with self.assertRaisesRegex(identity.IdentityError, "limit"):
            identity.protected_read(self.metadata, limit=1)
        self.metadata.chmod(0o666)
        with self.assertRaises(identity.IdentityError):
            identity.protected_read(self.metadata)

    def test_protected_reader_rejects_nonroot_owner_fifo_and_dotdot(self):
        metadata = os.stat(self.metadata)
        original_fstat = os.fstat
        def nonroot(fd):
            result = original_fstat(fd)
            if result.st_ino == metadata.st_ino:
                fields = list(result)
                fields[4] = 12345
                return os.stat_result(fields)
            return result
        with patch.object(identity.os, "fstat", side_effect=nonroot), self.assertRaises(identity.IdentityError):
            identity.protected_read(self.metadata)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(identity.IdentityError):
            identity.protected_read(fifo)
        with self.assertRaises(identity.IdentityError):
            identity.protected_read(self.root / "release/../candidate.json")

    def test_no_docker_process_is_started_and_describe_output_cannot_be_host_binding(self):
        output = io.StringIO()
        args = argparse.Namespace(engine=ENGINE, architecture=ARCH, reference="runner:canonical")
        with patch.object(identity.sys, "stdout", output):
            identity.describe(args)
        result = json.loads(output.getvalue())
        self.assertEqual(result["purpose"], "producer-description-only")
        self.assertNotIn("mode", result)
        with self.assertRaises(identity.IdentityError):
            identity.validate_bindings(result)


if __name__ == "__main__":
    unittest.main()
