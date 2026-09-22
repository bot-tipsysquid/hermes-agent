"""Read-only ARM64 artifact and post-restart gateway verification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import stat
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


@dataclass(frozen=True)
class GatewayReceiptIdentity:
    """Pre-restart receipt fields plus its proven live process incarnation, if any."""

    pid: int | None
    start_time: object | None
    updated_at: object | None
    live_incarnation: tuple[int, object] | None


ProcessStartTime = Callable[[int], int | None]
StartTimesMatch = Callable[[object, object], bool]
ProcessIdentityMatches = Callable[[dict[str, object], int, Path], bool]
LiveGatewayPid = Callable[[Path], int | None]
OpenFile = Callable[..., int]


def _open_contained_artifact(
    path: Path,
    *,
    traversal_root: Path,
    containment_root: Path,
    open_file: OpenFile,
) -> int:
    artifact = Path(os.path.abspath(path))
    traversal = Path(os.path.abspath(traversal_root))
    containment = Path(os.path.abspath(containment_root))
    try:
        artifact.relative_to(containment)
        relative = artifact.relative_to(traversal)
    except ValueError as exc:
        raise ArtifactVerificationError(
            f"packaged artifact is outside the expected release root: {path}"
        ) from exc
    if not relative.parts:
        raise ArtifactVerificationError(f"packaged artifact path is invalid: {path}")

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | nofollow | getattr(os, "O_DIRECTORY", 0)
    artifact_flags = os.O_RDONLY | os.O_CLOEXEC | nofollow | getattr(os, "O_NONBLOCK", 0)
    directory_fd: int | None = None
    try:
        directory_fd = open_file(traversal, directory_flags)
        for component in relative.parts[:-1]:
            next_fd = open_file(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return open_file(relative.parts[-1], artifact_flags, dir_fd=directory_fd)
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _elf_machine(
    path: Path,
    *,
    traversal_root: Path | None = None,
    containment_root: Path | None = None,
    require_executable: bool = False,
    open_file: OpenFile = os.open,
) -> int:
    """Validate a bounded, structurally plausible ELF64 image and return e_machine."""
    root = traversal_root or path.parent
    allowed = containment_root or path.parent
    descriptor: int | None = None
    try:
        descriptor = _open_contained_artifact(
            path,
            traversal_root=root,
            containment_root=allowed,
            open_file=open_file,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ArtifactVerificationError(
                f"packaged artifact is not a regular file: {path}"
            )
        if require_executable and not stat.S_IMODE(metadata.st_mode) & 0o111:
            raise ArtifactVerificationError(
                f"packaged application has no executable mode: {path}"
            )
        file_size = metadata.st_size
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = None
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
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _require_aarch64(
    path: Path,
    label: str,
    *,
    traversal_root: Path,
    containment_root: Path,
    require_executable: bool = False,
    open_file: OpenFile = os.open,
) -> None:
    if (
        _elf_machine(
            path,
            traversal_root=traversal_root,
            containment_root=containment_root,
            require_executable=require_executable,
            open_file=open_file,
        )
        != _EM_AARCH64
    ):
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


def verify_arm64_update_artifacts(
    project_root: Path,
    *,
    open_file: OpenFile = os.open,
) -> ArtifactReceipt:
    """Require a current Desktop stamp plus ARM64 app and packaged node-pty."""
    root = project_root.resolve()
    desktop = root / "apps" / "desktop"
    release_root = desktop / "release"
    executable = _desktop_packaged_executable(desktop)
    if executable is None:
        raise ArtifactVerificationError("packaged ARM64 Desktop application is missing")
    if _desktop_build_needed(desktop, root, source_mode=False):
        raise ArtifactVerificationError("Desktop build stamp is stale, missing, or incomplete")
    _require_aarch64(
        executable,
        "application",
        traversal_root=root,
        containment_root=release_root,
        require_executable=True,
        open_file=open_file,
    )

    node_pty = None
    for candidate in _node_pty_candidates(executable):
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ArtifactVerificationError(
                f"cannot inspect packaged node-pty native module: {exc}"
            ) from exc
        node_pty = candidate
        break
    if node_pty is None:
        raise ArtifactVerificationError("packaged node-pty native module is missing")
    _require_aarch64(
        node_pty,
        "node-pty",
        traversal_root=root,
        containment_root=executable.parent,
        open_file=open_file,
    )
    return ArtifactReceipt(executable=executable, node_pty=node_pty)


def _default_process_start_time(pid: int) -> int | None:
    from gateway.status import get_process_start_time

    return get_process_start_time(pid)


def _default_start_times_match(recorded: object, current: object) -> bool:
    from gateway.status import start_time_fingerprints_match

    return start_time_fingerprints_match(recorded, current)


def _default_process_identity_matches(
    receipt: dict[str, object], pid: int, profile_home: Path
) -> bool:
    from gateway.status import (
        _record_looks_like_gateway,
        _record_matches_live_gateway_pid,
    )

    return _record_looks_like_gateway(receipt) and _record_matches_live_gateway_pid(
        receipt,
        pid,
        expected_home=profile_home,
    )


def _default_live_gateway_pid(profile_home: Path) -> int | None:
    from gateway.status import live_gateway_pid_for_home

    return live_gateway_pid_for_home(profile_home)


def _gateway_state_path(state_path: Path | None) -> Path:
    if state_path is not None:
        return state_path
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "gateway_state.json"


def _read_gateway_receipt(state_path: Path) -> object | None:
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def capture_gateway_receipt_identity(
    *,
    state_path: Path | None = None,
    process_start_time: ProcessStartTime = _default_process_start_time,
    live_gateway_pid: LiveGatewayPid = _default_live_gateway_pid,
) -> GatewayReceiptIdentity:
    """Capture receipt fields and the independently discovered live gateway incarnation."""
    path = _gateway_state_path(state_path)
    live_pid = live_gateway_pid(path.parent)
    live_incarnation: tuple[int, object] | None = None
    if live_pid is not None:
        live_start = process_start_time(live_pid)
        if live_start is None:
            raise GatewayVerificationError(
                "cannot capture pre-restart gateway process-start fingerprint"
            )
        live_incarnation = (live_pid, live_start)

    payload = _read_gateway_receipt(path)
    if not isinstance(payload, dict):
        if path.exists():
            raise GatewayVerificationError(
                f"cannot capture pre-restart gateway receipt: {path} is unreadable or malformed"
            )
        return GatewayReceiptIdentity(None, None, None, live_incarnation)

    receipt = cast(dict[str, object], payload)
    raw_pid = receipt.get("pid")
    pid = raw_pid if isinstance(raw_pid, int) and raw_pid > 0 else None
    return GatewayReceiptIdentity(
        pid=pid,
        start_time=receipt.get("start_time"),
        updated_at=receipt.get("updated_at"),
        live_incarnation=live_incarnation,
    )


def _updated_at_epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        epoch = parsed.timestamp()
    except (OverflowError, TypeError, ValueError):
        return None
    return epoch if math.isfinite(epoch) else None


def _platform_ready(
    value: object,
    *,
    pid: int,
    live_start: object,
    start_times_match: StartTimesMatch,
) -> bool:
    if not isinstance(value, dict):
        return False
    platform = cast(dict[str, object], value)
    if (
        str(platform.get("state") or platform.get("status") or "").lower()
        not in _READY_PLATFORM_STATES
        or platform.get("writer_pid") != pid
        or platform.get("writer_start_time") is None
    ):
        return False
    try:
        return start_times_match(platform["writer_start_time"], live_start)
    except (TypeError, ValueError, OverflowError):
        return False


def _same_incarnation(
    pid: int,
    live_start: object,
    pre_restart: GatewayReceiptIdentity,
    start_times_match: StartTimesMatch,
) -> bool:
    if pre_restart.live_incarnation is None:
        return False
    pre_pid, pre_start = pre_restart.live_incarnation
    if pid != pre_pid:
        return False
    try:
        return start_times_match(pre_start, live_start)
    except (TypeError, ValueError, OverflowError):
        return False


def _gateway_receipt_error(
    payload: object,
    expected_sha: str,
    *,
    restart_started_at: float,
    pre_restart: GatewayReceiptIdentity,
    state_path: Path,
    process_start_time: ProcessStartTime,
    start_times_match: StartTimesMatch,
    process_identity_matches: ProcessIdentityMatches,
) -> str | None:
    if not isinstance(payload, dict):
        return "gateway runtime receipt is not an object"
    receipt = cast(dict[str, object], payload)
    updated_at = receipt.get("updated_at")
    if updated_at is None or (isinstance(updated_at, str) and not updated_at.strip()):
        return "gateway runtime receipt updated_at is missing"
    updated_epoch = _updated_at_epoch(updated_at)
    if updated_epoch is None:
        return "gateway runtime receipt updated_at is malformed"
    if updated_at == pre_restart.updated_at:
        return "gateway runtime receipt updated_at is unchanged from before restart"
    if updated_epoch <= restart_started_at:
        return "gateway runtime receipt updated_at is not newer than the restart attempt"
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
    if not process_identity_matches(receipt, pid, state_path.parent):
        return "gateway live process does not identify as the Hermes gateway for this profile"
    if _same_incarnation(pid, live_start, pre_restart, start_times_match):
        return "gateway restart retained the pre-restart live process incarnation"
    platforms = receipt.get("platforms")
    if not isinstance(platforms, dict) or not any(
        _platform_ready(
            value,
            pid=pid,
            live_start=live_start,
            start_times_match=start_times_match,
        )
        for value in platforms.values()
    ):
        return "gateway platform-ready writer identity is missing or stale"
    return None


def verify_gateway_ready(
    expected_sha: str,
    *,
    restart_started_at: float,
    pre_restart: GatewayReceiptIdentity,
    state_path: Path | None = None,
    attempts: int = 12,
    interval: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    process_start_time: ProcessStartTime = _default_process_start_time,
    start_times_match: StartTimesMatch = _default_start_times_match,
    process_identity_matches: ProcessIdentityMatches = _default_process_identity_matches,
) -> None:
    """Poll for a fresh post-attempt receipt, new live gateway, and ready writer."""
    path = _gateway_state_path(state_path)
    last_error = "gateway runtime receipt is missing"
    for attempt in range(max(1, attempts)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            last_error = "gateway runtime receipt is missing"
        except (OSError, json.JSONDecodeError) as exc:
            last_error = f"gateway runtime receipt is unreadable: {exc}"
        else:
            last_error = (
                _gateway_receipt_error(
                    payload,
                    expected_sha,
                    restart_started_at=restart_started_at,
                    pre_restart=pre_restart,
                    state_path=path,
                    process_start_time=process_start_time,
                    start_times_match=start_times_match,
                    process_identity_matches=process_identity_matches,
                )
                or ""
            )
            if not last_error:
                return
        if attempt + 1 < max(1, attempts):
            sleep(interval)
    raise GatewayVerificationError(last_error)
