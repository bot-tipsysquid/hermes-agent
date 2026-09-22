"""Artifact and runtime receipts for the Silverblue downstream update."""

from __future__ import annotations

from pathlib import Path
import struct

import pytest

from hermes_cli import downstream_update_verify as verify


_AARCH64 = 183
_X86_64 = 62


def _write_elf(path: Path, machine: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = 2  # ELFCLASS64
    header[5] = 1  # little endian
    header[18:20] = struct.pack("<H", machine)
    path.write_bytes(header)


@pytest.fixture
def arm64_bundle(tmp_path, monkeypatch):
    desktop = tmp_path / "apps" / "desktop"
    executable = desktop / "release" / "linux-arm64-unpacked" / "hermes"
    node_pty = (
        executable.parent
        / "resources"
        / "app.asar.unpacked"
        / "dist"
        / "node_modules"
        / "node-pty"
        / "prebuilds"
        / "linux-arm64"
        / "pty.node"
    )
    _write_elf(executable, _AARCH64)
    _write_elf(node_pty, _AARCH64)
    monkeypatch.setattr(verify, "_desktop_packaged_executable", lambda _desktop: executable)
    monkeypatch.setattr(
        verify,
        "_desktop_build_needed",
        lambda _desktop, _root, *, source_mode: False,
    )
    return tmp_path, executable, node_pty


def test_arm64_app_current_stamp_and_packaged_node_pty_pass(arm64_bundle):
    root, executable, node_pty = arm64_bundle

    receipt = verify.verify_arm64_update_artifacts(root)

    assert receipt.executable == executable
    assert receipt.node_pty == node_pty


@pytest.mark.parametrize(("target", "message"), [("app", "application"), ("pty", "node-pty")])
def test_wrong_architecture_fails_closed(arm64_bundle, target, message):
    root, executable, node_pty = arm64_bundle
    _write_elf(executable if target == "app" else node_pty, _X86_64)

    with pytest.raises(verify.ArtifactVerificationError, match=f"{message}.*ARM64"):
        verify.verify_arm64_update_artifacts(root)


def test_stale_or_missing_desktop_stamp_fails_closed(arm64_bundle, monkeypatch):
    root, _, _ = arm64_bundle
    monkeypatch.setattr(
        verify,
        "_desktop_build_needed",
        lambda _desktop, _root, *, source_mode: True,
    )

    with pytest.raises(verify.ArtifactVerificationError, match="build stamp"):
        verify.verify_arm64_update_artifacts(root)


def test_missing_packaged_node_pty_fails_closed(arm64_bundle):
    root, _, node_pty = arm64_bundle
    node_pty.unlink()

    with pytest.raises(verify.ArtifactVerificationError, match="node-pty.*missing"):
        verify.verify_arm64_update_artifacts(root)


def test_gateway_receipt_requires_expected_revision_and_connected_platform(tmp_path):
    state = tmp_path / "gateway_state.json"
    state.write_text(
        '{"gateway_state":"running","code_sha":"abc123","pid":7,"start_time":11,'
        '"platforms":{"telegram":{"state":"connected","writer_pid":7,'
        '"writer_start_time":11}}}',
        encoding="utf-8",
    )

    verify.verify_gateway_ready("abc123", state_path=state, attempts=1, interval=0)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            '{"gateway_state":"running","code_sha":"old","pid":7,"start_time":11,'
            '"platforms":{"telegram":{"state":"connected","writer_pid":7,'
            '"writer_start_time":11}}}',
            "expected revision",
        ),
        (
            '{"gateway_state":"running","code_sha":"abc123","pid":7,"start_time":11,'
            '"platforms":{"telegram":{"state":"retrying","writer_pid":7,'
            '"writer_start_time":11}}}',
            "platform-ready",
        ),
        (
            '{"gateway_state":"draining","code_sha":"abc123","pid":7,"start_time":11,'
            '"platforms":{"telegram":{"state":"connected","writer_pid":7,'
            '"writer_start_time":11}}}',
            "not running",
        ),
    ],
)
def test_gateway_receipt_fails_closed_without_current_platform_ready_marker(
    tmp_path, payload, message
):
    state = tmp_path / "gateway_state.json"
    state.write_text(payload, encoding="utf-8")

    with pytest.raises(verify.GatewayVerificationError, match=message):
        verify.verify_gateway_ready("abc123", state_path=state, attempts=1, interval=0)
