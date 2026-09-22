"""Read-only ARM64 artifact and post-restart gateway verification."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import struct
import time
from typing import Callable, cast

from hermes_cli.main_desktop import (
    _desktop_build_needed,
    _desktop_packaged_executable,
)


_EM_AARCH64 = 183
_READY_PLATFORM_STATES = {"connected", "running", "ok"}
_READY_GATEWAY_STATES = {"running"}


class ArtifactVerificationError(RuntimeError):
    """Raised when a packaged Desktop receipt is missing, stale, or wrong-arch."""


class GatewayVerificationError(RuntimeError):
    """Raised when the restarted gateway cannot prove current platform readiness."""


@dataclass(frozen=True)
class ArtifactReceipt:
    """Paths that passed the ARM64 packaged-artifact gates."""

    executable: Path
    node_pty: Path


def _elf_machine(path: Path) -> int:
    """Validate a bounded, structurally plausible ELF64 image and return e_machine."""
    try:
        file_size = path.stat().st_size
        with path.open("rb") as stream:
            header = stream.read(64)
            if len(header) != 64:
                raise ArtifactVerificationError(
                    f"packaged ELF header is truncated: {path}"
                )
            ident = header[:16]
            if ident[:4] != b"\x7fELF" or ident[4] != 2:
                raise ArtifactVerificationError(
                    f"packaged artifact is not a 64-bit ELF file: {path}"
                )
            if ident[5] == 1:
                order = "<"
            elif ident[5] == 2:
                order = ">"
            else:
                raise ArtifactVerificationError(
                    f"packaged ELF has an invalid byte order: {path}"
                )
            if ident[6] != 1:
                raise ArtifactVerificationError(
                    f"packaged ELF has an invalid identification version: {path}"
                )
            (
                _ident,
                file_type,
                machine,
                version,
                _entry,
                program_offset,
                section_offset,
                _flags,
                header_size,
                program_entry_size,
                program_count,
                section_entry_size,
                section_count,
                section_names_index,
            ) = struct.unpack(f"{order}16sHHIQQQIHHHHHH", header)
            if file_type not in {2, 3} or version != 1 or header_size != 64:
                raise ArtifactVerificationError(
                    f"packaged ELF has an invalid complete header: {path}"
                )
            if program_count <= 0 or program_entry_size != 56:
                raise ArtifactVerificationError(
                    f"packaged ELF has an invalid program header table: {path}"
                )
            program_end = program_offset + program_entry_size * program_count
            if program_offset < header_size or program_end > file_size:
                raise ArtifactVerificationError(
                    f"packaged ELF program header table is outside file bounds: {path}"
                )
            if section_count:
                section_end = section_offset + section_entry_size * section_count
                if (
                    section_offset < header_size
                    or section_entry_size != 64
                    or section_end > file_size
                    or section_names_index >= section_count
                ):
                    raise ArtifactVerificationError(
                        f"packaged ELF section header table is outside file bounds: {path}"
                    )
            elif section_offset != 0 or section_names_index != 0:
                raise ArtifactVerificationError(
                    f"packaged ELF has inconsistent section structure: {path}"
                )

            has_load_segment = False
            for index in range(program_count):
                stream.seek(program_offset + index * program_entry_size)
                raw_program = stream.read(program_entry_size)
                if len(raw_program) != program_entry_size:
                    raise ArtifactVerificationError(
                        f"packaged ELF program header is truncated: {path}"
                    )
                (
                    segment_type,
                    _segment_flags,
                    segment_offset,
                    _virtual_address,
                    _physical_address,
                    file_bytes,
                    memory_bytes,
                    alignment,
                ) = struct.unpack(f"{order}IIQQQQQQ", raw_program)
                if segment_offset > file_size or file_bytes > file_size - segment_offset:
                    raise ArtifactVerificationError(
                        f"packaged ELF segment extends outside file bounds: {path}"
                    )
                if memory_bytes < file_bytes:
                    raise ArtifactVerificationError(
                        f"packaged ELF segment has invalid memory bounds: {path}"
                    )
                if alignment not in {0, 1} and alignment & (alignment - 1):
                    raise ArtifactVerificationError(
                        f"packaged ELF segment has invalid alignment: {path}"
                    )
                if segment_type == 1:
                    has_load_segment = True
            if not has_load_segment:
                raise ArtifactVerificationError(
                    f"packaged ELF has no loadable segment: {path}"
                )
            return machine
    except ArtifactVerificationError:
        raise
    except (OSError, struct.error) as exc:
        raise ArtifactVerificationError(f"cannot validate packaged ELF {path}: {exc}") from exc


def _require_aarch64(path: Path, label: str) -> None:
    if _elf_machine(path) != _EM_AARCH64:
        raise ArtifactVerificationError(f"packaged {label} is not ARM64 aarch64 ELF: {path}")


def _node_pty_candidates(executable: Path) -> tuple[Path, ...]:
    root = (
        executable.parent
        / "resources"
        / "app.asar.unpacked"
        / "dist"
        / "node_modules"
        / "node-pty"
    )
    return (
        root / "prebuilds" / "linux-arm64" / "pty.node",
        root / "build" / "Release" / "pty.node",
    )


def verify_arm64_update_artifacts(project_root: Path) -> ArtifactReceipt:
    """Require a current Desktop stamp plus ARM64 app and packaged node-pty."""
    root = project_root.resolve()
    desktop = root / "apps" / "desktop"
    executable = _desktop_packaged_executable(desktop)
    if executable is None or not executable.is_file():
        raise ArtifactVerificationError("packaged ARM64 Desktop application is missing")
    if _desktop_build_needed(desktop, root, source_mode=False):
        raise ArtifactVerificationError("Desktop build stamp is stale, missing, or incomplete")
    _require_aarch64(executable, "application")

    node_pty = next((candidate for candidate in _node_pty_candidates(executable) if candidate.is_file()), None)
    if node_pty is None:
        raise ArtifactVerificationError("packaged node-pty native module is missing")
    _require_aarch64(node_pty, "node-pty")
    return ArtifactReceipt(executable=executable, node_pty=node_pty)


def _platform_ready(value: object, *, pid: object, start_time: object) -> bool:
    if not isinstance(value, dict):
        return False
    platform = cast(dict[str, object], value)
    return (
        str(platform.get("state") or platform.get("status") or "").lower()
        in _READY_PLATFORM_STATES
        and platform.get("writer_pid") == pid
        and platform.get("writer_start_time") == start_time
    )


def _gateway_receipt_error(
    payload: object,
    expected_sha: str,
    *,
    process_start_time: Callable[[int], int | None],
    start_times_match: Callable[[object, object], bool],
) -> str | None:
    if not isinstance(payload, dict):
        return "gateway runtime receipt is not an object"
    receipt = cast(dict[str, object], payload)
    if receipt.get("code_sha") != expected_sha:
        return "gateway runtime receipt does not report the expected revision"
    if receipt.get("gateway_state") not in _READY_GATEWAY_STATES:
        return "gateway runtime receipt is not running"
    pid = receipt.get("pid")
    recorded_start = receipt.get("start_time")
    if not isinstance(pid, int) or pid <= 0 or recorded_start is None:
        return "gateway runtime receipt has no valid process identity"
    live_start = process_start_time(pid)
    if live_start is None:
        return "gateway process is not alive or its live process identity is unavailable"
    try:
        if not start_times_match(recorded_start, live_start):
            return "gateway process identity does not match the recorded start time"
    except (TypeError, ValueError, OverflowError):
        return "gateway runtime receipt has a malformed process start time"
    platforms = receipt.get("platforms")
    if not isinstance(platforms, dict) or not any(
        _platform_ready(value, pid=pid, start_time=recorded_start)
        for value in platforms.values()
    ):
        return "gateway platform-ready writer identity is missing or stale"
    return None


def _default_process_start_time(pid: int) -> int | None:
    from gateway.status import get_process_start_time

    return get_process_start_time(pid)


def _default_start_times_match(recorded: object, current: object) -> bool:
    from gateway.status import start_time_fingerprints_match

    return start_time_fingerprints_match(recorded, current)


def verify_gateway_ready(
    expected_sha: str,
    *,
    state_path: Path | None = None,
    attempts: int = 12,
    interval: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    process_start_time: Callable[[int], int | None] = _default_process_start_time,
    start_times_match: Callable[[object, object], bool] = _default_start_times_match,
) -> None:
    """Poll for the intended revision, live process identity, and a ready platform."""
    if state_path is None:
        from hermes_constants import get_hermes_home

        state_path = get_hermes_home() / "gateway_state.json"
    last_error = "gateway runtime receipt is missing"
    for attempt in range(max(1, attempts)):
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            last_error = "gateway runtime receipt is missing"
        except (OSError, json.JSONDecodeError) as exc:
            last_error = f"gateway runtime receipt is unreadable: {exc}"
        else:
            last_error = (
                _gateway_receipt_error(
                    payload,
                    expected_sha,
                    process_start_time=process_start_time,
                    start_times_match=start_times_match,
                )
                or ""
            )
            if not last_error:
                return
        if attempt + 1 < max(1, attempts):
            sleep(interval)
    raise GatewayVerificationError(last_error)
