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
    ident = bytearray(16)
    ident[:4] = b"\x7fELF"
    ident[4] = 2  # ELFCLASS64
    ident[5] = 1  # little endian
    ident[6] = 1  # current ELF version
    file_size = 64 + 56
    header = struct.pack(
        "<16sHHIQQQIHHHHHH",
        bytes(ident),
        3,  # ET_DYN (Electron and native modules are PIE/shared objects)
        machine,
        1,
        0,
        64,
        0,
        0,
        64,
        56,
        1,
        0,
        0,
        0,
    )
    load_segment = struct.pack(
        "<IIQQQQQQ",
        1,  # PT_LOAD
        5,
        0,
        0,
        0,
        file_size,
        file_size,
        0x1000,
    )
    path.write_bytes(header + load_segment)


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


def test_truncated_elf64_header_fails_closed(tmp_path):
    artifact = tmp_path / "truncated"
    artifact.write_bytes(b"\x7fELF\x02\x01" + bytes(14))

    with pytest.raises(verify.ArtifactVerificationError, match="truncated|header"):
        verify._elf_machine(artifact)


@pytest.mark.parametrize("corruption", ["program-table", "load-segment"])
def test_out_of_bounds_elf64_structure_fails_closed(tmp_path, corruption):
    artifact = tmp_path / corruption
    _write_elf(artifact, _AARCH64)
    payload = bytearray(artifact.read_bytes())
    if corruption == "program-table":
        payload[32:40] = struct.pack("<Q", len(payload) + 1)
    else:
        # First program header p_filesz (offset 32 inside Elf64_Phdr).
        payload[64 + 32 : 64 + 40] = struct.pack("<Q", len(payload) + 1)
    artifact.write_bytes(payload)

    with pytest.raises(verify.ArtifactVerificationError, match="bounds|outside|truncated"):
        verify._elf_machine(artifact)


def test_gateway_receipt_requires_expected_revision_and_connected_platform(tmp_path):
    state = tmp_path / "gateway_state.json"
    state.write_text(
        '{"gateway_state":"running","code_sha":"abc123","pid":7,"start_time":11,'
        '"platforms":{"telegram":{"state":"connected","writer_pid":7,'
        '"writer_start_time":11}}}',
        encoding="utf-8",
    )

    verify.verify_gateway_ready(
        "abc123",
        state_path=state,
        attempts=1,
        interval=0,
        process_start_time=lambda pid: 11 if pid == 7 else None,
    )


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
        verify.verify_gateway_ready(
            "abc123",
            state_path=state,
            attempts=1,
            interval=0,
            process_start_time=lambda pid: 11 if pid == 7 else None,
        )


@pytest.mark.parametrize(
    ("live_start", "message"),
    [
        (None, "not alive|live process"),
        (99999, "identity|start time"),
    ],
)
def test_gateway_receipt_rejects_dead_or_reused_pid(tmp_path, live_start, message):
    state = tmp_path / "gateway_state.json"
    state.write_text(
        '{"gateway_state":"running","code_sha":"abc123","pid":7,"start_time":11,'
        '"platforms":{"telegram":{"state":"connected","writer_pid":7,'
        '"writer_start_time":11}}}',
        encoding="utf-8",
    )

    with pytest.raises(verify.GatewayVerificationError, match=message):
        verify.verify_gateway_ready(
            "abc123",
            state_path=state,
            attempts=1,
            interval=0,
            process_start_time=lambda _pid: live_start,
        )


def test_gateway_receipt_rejects_platform_writer_from_other_incarnation(tmp_path):
    state = tmp_path / "gateway_state.json"
    state.write_text(
        '{"gateway_state":"running","code_sha":"abc123","pid":7,"start_time":11,'
        '"platforms":{"telegram":{"state":"connected","writer_pid":7,'
        '"writer_start_time":99999}}}',
        encoding="utf-8",
    )

    with pytest.raises(verify.GatewayVerificationError, match="platform-ready|writer"):
        verify.verify_gateway_ready(
            "abc123",
            state_path=state,
            attempts=1,
            interval=0,
            process_start_time=lambda _pid: 11,
        )
