"""Artifact and runtime receipts for the Silverblue downstream update."""

from __future__ import annotations

import json
from pathlib import Path
import struct
from typing import Any

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


_PRE_RESTART_UPDATED_AT = "2026-09-22T18:59:59+00:00"
_RESTART_THRESHOLD = 1_795_474_800.0  # 2026-11-23T23:00:00+00:00
_FRESH_UPDATED_AT = "2026-11-23T23:00:01+00:00"
_GATEWAY_ARGV = ["python", "-m", "hermes_cli.main", "gateway", "run"]


def _gateway_payload(
    *,
    pid: int = 8,
    start_time: int = 22,
    updated_at: object = _FRESH_UPDATED_AT,
    code_sha: str = "abc123",
    gateway_state: str = "running",
    kind: str = "hermes-gateway",
    argv: object = None,
    platform_state: str = "connected",
    writer_pid: int | None = None,
    writer_start_time: int | None = None,
) -> dict[str, object]:
    return {
        "gateway_state": gateway_state,
        "code_sha": code_sha,
        "kind": kind,
        "argv": _GATEWAY_ARGV if argv is None else argv,
        "pid": pid,
        "start_time": start_time,
        "updated_at": updated_at,
        "platforms": {
            "telegram": {
                "state": platform_state,
                "writer_pid": pid if writer_pid is None else writer_pid,
                "writer_start_time": (
                    start_time if writer_start_time is None else writer_start_time
                ),
            }
        },
    }


def _write_gateway_payload(path: Path, **overrides: Any) -> None:
    path.write_text(json.dumps(_gateway_payload(**overrides)), encoding="utf-8")


def _no_pre_restart_gateway() -> verify.GatewayReceiptIdentity:
    return verify.GatewayReceiptIdentity(
        pid=None,
        start_time=None,
        updated_at=None,
        live_incarnation=None,
    )


def _process_identity_matches(receipt: dict[str, object], pid: int, _home: Path) -> bool:
    return (
        pid == receipt.get("pid")
        and receipt.get("kind") == "hermes-gateway"
        and receipt.get("argv") == _GATEWAY_ARGV
    )


def _verify_gateway_once(
    state: Path,
    *,
    pre_restart: verify.GatewayReceiptIdentity | None = None,
    process_start_time=lambda pid: 22 if pid == 8 else None,
    process_identity_matches=_process_identity_matches,
) -> None:
    verify.verify_gateway_ready(
        "abc123",
        restart_started_at=_RESTART_THRESHOLD,
        pre_restart=pre_restart or _no_pre_restart_gateway(),
        state_path=state,
        attempts=1,
        interval=0,
        process_start_time=process_start_time,
        process_identity_matches=process_identity_matches,
    )


def test_capture_gateway_receipt_records_prior_identity_and_live_incarnation(tmp_path):
    state = tmp_path / "gateway_state.json"
    _write_gateway_payload(
        state,
        pid=7,
        start_time=11,
        updated_at=_PRE_RESTART_UPDATED_AT,
        writer_pid=7,
        writer_start_time=11,
    )

    identity = verify.capture_gateway_receipt_identity(
        state_path=state,
        process_start_time=lambda pid: 11 if pid == 7 else None,
        process_identity_matches=_process_identity_matches,
    )

    assert identity.pid == 7
    assert identity.start_time == 11
    assert identity.updated_at == _PRE_RESTART_UPDATED_AT
    assert identity.live_incarnation == (7, 11)


def test_capture_gateway_receipt_identity_rejects_malformed_existing_receipt(tmp_path):
    receipt = tmp_path / "gateway_state.json"
    receipt.write_text("{not-json", encoding="utf-8")

    with pytest.raises(
        verify.GatewayVerificationError, match="cannot capture pre-restart gateway receipt"
    ):
        verify.capture_gateway_receipt_identity(state_path=receipt)


def test_capture_gateway_receipt_identity_finds_live_gateway_without_receipt(tmp_path):
    captured = verify.capture_gateway_receipt_identity(
        state_path=tmp_path / "gateway_state.json",
        live_gateway_pid=lambda _home: 7,
        process_start_time=lambda pid: 17 if pid == 7 else None,
    )

    assert captured.live_incarnation == (7, 17)


@pytest.mark.parametrize(
    ("updated_at", "message"),
    [
        (None, "updated_at.*missing"),
        ("not-a-timestamp", "updated_at.*malformed"),
        ("2026-11-23T23:00:00+00:00", "newer than.*restart"),
        ("2026-11-23T22:59:59+00:00", "newer than.*restart"),
    ],
)
def test_gateway_receipt_rejects_missing_malformed_or_stale_updated_at(
    tmp_path, updated_at, message
):
    state = tmp_path / "gateway_state.json"
    _write_gateway_payload(state, updated_at=updated_at)

    with pytest.raises(verify.GatewayVerificationError, match=message):
        _verify_gateway_once(state)


def test_gateway_receipt_rejects_unchanged_pre_restart_timestamp_even_if_future_dated(tmp_path):
    state = tmp_path / "gateway_state.json"
    future_stamp = "2099-01-01T00:00:00+00:00"
    _write_gateway_payload(state, updated_at=future_stamp)
    pre_restart = verify.GatewayReceiptIdentity(
        pid=7,
        start_time=11,
        updated_at=future_stamp,
        live_incarnation=(7, 11),
    )

    with pytest.raises(verify.GatewayVerificationError, match="unchanged"):
        _verify_gateway_once(state, pre_restart=pre_restart)


def test_gateway_receipt_rejects_same_pre_restart_live_incarnation(tmp_path):
    state = tmp_path / "gateway_state.json"
    _write_gateway_payload(
        state,
        pid=7,
        start_time=11,
        writer_pid=7,
        writer_start_time=11,
    )
    pre_restart = verify.GatewayReceiptIdentity(
        pid=7,
        start_time=11,
        updated_at=_PRE_RESTART_UPDATED_AT,
        live_incarnation=(7, 11),
    )

    with pytest.raises(verify.GatewayVerificationError, match="pre-restart.*incarnation"):
        _verify_gateway_once(
            state,
            pre_restart=pre_restart,
            process_start_time=lambda pid: 11 if pid == 7 else None,
        )


def test_gateway_receipt_accepts_fresh_new_live_gateway_and_connected_writer(tmp_path):
    state = tmp_path / "gateway_state.json"
    _write_gateway_payload(state)
    pre_restart = verify.GatewayReceiptIdentity(
        pid=7,
        start_time=11,
        updated_at=_PRE_RESTART_UPDATED_AT,
        live_incarnation=(7, 11),
    )

    _verify_gateway_once(state, pre_restart=pre_restart)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"code_sha": "old"}, "expected revision"),
        ({"platform_state": "retrying"}, "platform-ready"),
        ({"gateway_state": "draining"}, "not running"),
        ({"kind": "other"}, "Hermes gateway"),
        (
            {"argv": ["python", "-m", "hermes_cli.main", "gateway", "status"]},
            "Hermes gateway",
        ),
    ],
)
def test_gateway_receipt_fails_closed_on_revision_readiness_or_receipt_identity(
    tmp_path, overrides, message
):
    state = tmp_path / "gateway_state.json"
    _write_gateway_payload(state, **overrides)

    with pytest.raises(verify.GatewayVerificationError, match=message):
        _verify_gateway_once(state)


@pytest.mark.parametrize(
    ("live_start", "message"),
    [
        (None, "not alive|live process"),
        (99999, "identity|start time"),
    ],
)
def test_gateway_receipt_rejects_dead_or_reused_pid(tmp_path, live_start, message):
    state = tmp_path / "gateway_state.json"
    _write_gateway_payload(state)

    with pytest.raises(verify.GatewayVerificationError, match=message):
        _verify_gateway_once(state, process_start_time=lambda _pid: live_start)


def test_gateway_receipt_rejects_live_pid_with_wrong_process_command(tmp_path):
    state = tmp_path / "gateway_state.json"
    _write_gateway_payload(state)

    with pytest.raises(verify.GatewayVerificationError, match="Hermes gateway"):
        _verify_gateway_once(state, process_identity_matches=lambda *_args: False)


def test_gateway_receipt_rejects_platform_writer_from_other_incarnation(tmp_path):
    state = tmp_path / "gateway_state.json"
    _write_gateway_payload(state, writer_start_time=99999)

    with pytest.raises(verify.GatewayVerificationError, match="platform-ready|writer"):
        _verify_gateway_once(state)
