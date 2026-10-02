#!/usr/bin/env python3
"""Focused tests for update_actions_runner.py."""

from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

import update_actions_runner as updater


DOCKERFILE = """# syntax=docker/dockerfile:1.18
FROM debian:13.6-slim
ARG RUNNER_VERSION=2.336.0
ARG RUNNER_SHA256_AMD64=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
ARG RUNNER_SHA256_ARM64=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
"""
COMPOSE = """services:
  runner-image:
    build:
      args:
        RUNNER_VERSION: ${CI_FLEET_RUNNER_VERSION:-2.336.0}
"""


def asset(version: str, architecture: str, size: int = 123) -> dict[str, object]:
    return {
        "name": f"actions-runner-linux-{architecture}-{version}.tar.gz",
        "browser_download_url": updater.expected_asset_url(version, architecture),
        "size": size,
        "state": "uploaded",
    }


def release(version: str, *, draft: bool = False, prerelease: bool = False) -> dict[str, object]:
    return {
        "tag_name": f"v{version}",
        "draft": draft,
        "prerelease": prerelease,
        "assets": [asset(version, "x64"), asset(version, "arm64")],
    }


def selected_release(version: str = "2.337.0") -> updater.Release:
    return updater.select_latest_stable([release(version)])


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, url: str, *, content_length: int | None = None) -> None:
        super().__init__(data)
        self.status = 200
        self._url = url
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def runner_archive() -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name in updater.ARCHIVE_MARKERS:
            content = f"fixture for {name}\n".encode()
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


class ReleaseTests(unittest.TestCase):
    def test_selects_highest_stable_and_ignores_draft_and_prerelease(self) -> None:
        payload = [
            release("2.339.0", prerelease=True),
            release("2.338.0", draft=True),
            release("2.336.0"),
            release("2.337.0"),
        ]

        selected = updater.select_latest_stable(payload)

        self.assertEqual(selected.version, "2.337.0")
        self.assertEqual([item.architecture for item in selected.assets], ["x64", "arm64"])

    def test_rejects_untrusted_tag_text(self) -> None:
        malformed = release("2.337.0")
        malformed["tag_name"] = "v2.337.0; touch /tmp/pwned"

        with self.assertRaisesRegex(updater.UpdateError, "malformed release tag"):
            updater.select_latest_stable([malformed])

    def test_rejects_mismatched_asset_url(self) -> None:
        malformed = release("2.337.0")
        assets = malformed["assets"]
        assert isinstance(assets, list)
        assets[0]["browser_download_url"] = updater.expected_asset_url("2.336.0", "x64")

        with self.assertRaisesRegex(updater.UpdateError, "official URL"):
            updater.select_latest_stable([malformed])

    def test_rejects_missing_architecture(self) -> None:
        malformed = release("2.337.0")
        malformed["assets"] = [asset("2.337.0", "x64")]

        with self.assertRaisesRegex(updater.UpdateError, "arm64"):
            updater.select_latest_stable([malformed])

    def test_fetch_rejects_malformed_json(self) -> None:
        def opener(_request: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse(b"not json", updater.RELEASES_URL)

        with self.assertRaisesRegex(updater.UpdateError, "valid JSON"):
            updater.fetch_releases(opener=opener)


class DownloadTests(unittest.TestCase):
    def test_downloads_valid_archive_and_calculates_sha256(self) -> None:
        data = runner_archive()
        selected_asset = updater.Asset(
            architecture="x64",
            name="actions-runner-linux-x64-2.337.0.tar.gz",
            url=updater.expected_asset_url("2.337.0", "x64"),
            size=len(data),
        )

        def opener(_request: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse(data, selected_asset.url, content_length=len(data))

        with tempfile.TemporaryDirectory() as temporary_directory:
            digest = updater.download_and_hash(
                selected_asset,
                Path(temporary_directory) / selected_asset.name,
                opener=opener,
            )

        self.assertEqual(digest, hashlib.sha256(data).hexdigest())

    def test_rejects_download_size_mismatch(self) -> None:
        data = runner_archive()
        selected_asset = updater.Asset(
            architecture="x64",
            name="actions-runner-linux-x64-2.337.0.tar.gz",
            url=updater.expected_asset_url("2.337.0", "x64"),
            size=len(data) + 1,
        )

        def opener(_request: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse(data, selected_asset.url)

        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(updater.UpdateError, "size does not match"):
                updater.download_and_hash(
                    selected_asset,
                    Path(temporary_directory) / selected_asset.name,
                    opener=opener,
                )

    def test_rejects_malformed_archive(self) -> None:
        data = b"not a tar archive"
        selected_asset = updater.Asset(
            architecture="arm64",
            name="actions-runner-linux-arm64-2.337.0.tar.gz",
            url=updater.expected_asset_url("2.337.0", "arm64"),
            size=len(data),
        )

        def opener(_request: object, **_kwargs: object) -> FakeResponse:
            return FakeResponse(data, selected_asset.url)

        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(updater.UpdateError, "valid gzip-compressed tar"):
                updater.download_and_hash(
                    selected_asset,
                    Path(temporary_directory) / selected_asset.name,
                    opener=opener,
                )


class ReplacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "runner").mkdir()
        (self.root / "deploy").mkdir()
        (self.root / "runner" / "Dockerfile").write_text(DOCKERFILE)
        (self.root / "deploy" / "compose.yaml").write_text(COMPOSE)
        self.checksums = {"x64": "c" * 64, "arm64": "d" * 64}

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_updates_all_four_pins(self) -> None:
        result = updater.update_repository(self.root, selected_release(), self.checksums)

        self.assertTrue(result.changed)
        pins = updater.parse_pins(
            (self.root / "runner" / "Dockerfile").read_text(),
            (self.root / "deploy" / "compose.yaml").read_text(),
        )
        self.assertEqual(pins, updater.Pins(version="2.337.0", amd64="c" * 64, arm64="d" * 64))

    def test_no_op_when_all_pins_are_current(self) -> None:
        updater.update_repository(self.root, selected_release(), self.checksums)
        docker_before = (self.root / "runner" / "Dockerfile").read_bytes()
        compose_before = (self.root / "deploy" / "compose.yaml").read_bytes()

        result = updater.update_repository(self.root, selected_release(), self.checksums)

        self.assertFalse(result.changed)
        self.assertEqual((self.root / "runner" / "Dockerfile").read_bytes(), docker_before)
        self.assertEqual((self.root / "deploy" / "compose.yaml").read_bytes(), compose_before)

    def test_rejects_mismatched_existing_versions(self) -> None:
        compose_path = self.root / "deploy" / "compose.yaml"
        compose_path.write_text(COMPOSE.replace("2.336.0", "2.335.0"))

        with self.assertRaisesRegex(updater.UpdateError, "versions are mismatched"):
            updater.update_repository(self.root, selected_release(), self.checksums)

    def test_rejects_malformed_existing_checksum(self) -> None:
        docker_path = self.root / "runner" / "Dockerfile"
        docker_path.write_text(DOCKERFILE.replace("a" * 64, "not-a-sha256"))

        with self.assertRaisesRegex(updater.UpdateError, "checksum is malformed"):
            updater.update_repository(self.root, selected_release(), self.checksums)

    def test_rejects_duplicate_field(self) -> None:
        docker_path = self.root / "runner" / "Dockerfile"
        docker_path.write_text(DOCKERFILE + "ARG RUNNER_VERSION=2.336.0\n")

        with self.assertRaisesRegex(updater.UpdateError, "exactly one Dockerfile runner version token"):
            updater.update_repository(self.root, selected_release(), self.checksums)

    def test_rejects_downgrade(self) -> None:
        updater.update_repository(self.root, selected_release("2.337.0"), self.checksums)

        with self.assertRaisesRegex(updater.UpdateError, "refusing to downgrade"):
            updater.update_repository(self.root, selected_release("2.336.0"), self.checksums)


if __name__ == "__main__":
    unittest.main()
