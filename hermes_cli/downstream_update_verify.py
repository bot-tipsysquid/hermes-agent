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
    try:
        with path.open("rb") as stream:
            header = stream.read(20)
    except OSError as exc:
        raise ArtifactVerificationError(f"cannot read packaged artifact {path}: {exc}") from exc
    if len(header) < 20 or header[:4] != b"\x7fELF" or header[4] != 2:
        raise ArtifactVerificationError(f"packaged artifact is not a 64-bit ELF file: {path}")
    if header[5] == 1:
        order = "<"
    elif header[5] == 2:
        order = ">"
    else:
        raise ArtifactVerificationError(f"packaged ELF has an invalid byte order: {path}")
    return struct.unpack(f"{order}H", header[18:20])[0]


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


def _gateway_receipt_error(payload: object, expected_sha: str) -> str | None:
    if not isinstance(payload, dict):
        return "gateway runtime receipt is not an object"
    receipt = cast(dict[str, object], payload)
    if receipt.get("code_sha") != expected_sha:
        return "gateway runtime receipt does not report the expected revision"
    if receipt.get("gateway_state") not in _READY_GATEWAY_STATES:
        return "gateway runtime receipt is not running"
    pid = receipt.get("pid")
    start_time = receipt.get("start_time")
    if pid is None or start_time is None:
        return "gateway runtime receipt has no process identity"
    platforms = receipt.get("platforms")
    if not isinstance(platforms, dict) or not any(
        _platform_ready(value, pid=pid, start_time=start_time) for value in platforms.values()
    ):
        return "gateway platform-ready marker is missing"
    return None


def verify_gateway_ready(
    expected_sha: str,
    *,
    state_path: Path | None = None,
    attempts: int = 12,
    interval: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Poll the runtime receipt for the intended revision and a ready platform."""
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
            last_error = _gateway_receipt_error(payload, expected_sha) or ""
            if not last_error:
                return
        if attempt + 1 < max(1, attempts):
            sleep(interval)
    raise GatewayVerificationError(last_error)
