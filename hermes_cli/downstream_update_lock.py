"""Fail-closed single-writer lock for downstream update transactions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from types import TracebackType
from typing import Self


_LOCK_DIRECTORY = "hermes-downstream-update"


class DownstreamUpdateLockError(RuntimeError):
    """Raised when exclusive updater ownership cannot be proven."""


def _default_runtime_dir() -> Path:
    configured = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if configured:
        runtime = Path(configured)
        if runtime.is_absolute():
            return runtime
    if not hasattr(os, "getuid"):
        raise DownstreamUpdateLockError("cannot determine an owner-only runtime directory")
    return Path("/run/user") / str(os.getuid())


def transaction_lock_path(repo: Path, *, runtime_dir: Path | None = None) -> Path:
    """Return the deterministic per-repository lock path for this OS user."""
    identity = str(repo.resolve()).encode("utf-8", "surrogateescape")
    digest = hashlib.sha256(identity).hexdigest()[:24]
    root = (runtime_dir or _default_runtime_dir()).resolve()
    return root / _LOCK_DIRECTORY / f"{digest}.lock"


class UpdaterTransactionLock:
    """Advisory owner-only lock held from updater entry through readiness verification."""

    def __init__(self, *, path: Path, repair_permissions: bool = True) -> None:
        self.path = path
        self.repair_permissions = repair_permissions
        self._fd: int | None = None

    def _prepare_directory(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            metadata = self.path.parent.stat()
        except OSError as exc:
            raise DownstreamUpdateLockError(
                f"cannot prepare updater lock directory {self.path.parent}: {exc}"
            ) from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise DownstreamUpdateLockError("updater lock parent is not a directory")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise DownstreamUpdateLockError("updater lock directory is not owned by this user")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode != 0o700:
            if not self.repair_permissions:
                raise DownstreamUpdateLockError(
                    f"updater lock directory permissions are not owner-only: {mode:o}"
                )
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError as exc:
                raise DownstreamUpdateLockError(
                    "cannot enforce owner-only updater lock directory permissions"
                ) from exc

    def acquire(self) -> None:
        """Acquire the non-blocking transaction lock or fail closed."""
        if self._fd is not None:
            raise DownstreamUpdateLockError("updater transaction lock is already acquired")
        self._prepare_directory()
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise DownstreamUpdateLockError(f"cannot open updater transaction lock: {exc}") from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise DownstreamUpdateLockError("updater transaction lock is not a regular file")
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise DownstreamUpdateLockError("updater transaction lock is not owned by this user")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                if not self.repair_permissions:
                    raise DownstreamUpdateLockError(
                        "updater transaction lock permissions are not owner-only"
                    )
                os.fchmod(fd, 0o600)

            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                raise DownstreamUpdateLockError(
                    "downstream update transaction lock is held by another process"
                ) from exc

            owner: dict[str, int | None] = {"pid": os.getpid()}
            try:
                from gateway.status import get_process_start_time

                owner["start_time"] = get_process_start_time(os.getpid())
            except Exception:
                owner["start_time"] = None
            payload = (json.dumps(owner, sort_keys=True) + "\n").encode("utf-8")
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload)
            os.fsync(fd)
        except Exception:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        """Release ownership while retaining the harmless stale metadata file."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.release()
