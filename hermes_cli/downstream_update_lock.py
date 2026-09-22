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
_OWNER_ONLY_DIRECTORY_MODE = 0o700
_OWNER_ONLY_FILE_MODE = 0o600


class DownstreamUpdateLockError(RuntimeError):
    """Raised when exclusive updater ownership cannot be proven."""


def _default_runtime_dir() -> Path:
    configured = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if configured:
        runtime = Path(configured)
        if not runtime.is_absolute():
            raise DownstreamUpdateLockError("XDG_RUNTIME_DIR must be an absolute path")
        return runtime
    if not hasattr(os, "getuid"):
        raise DownstreamUpdateLockError("cannot determine an owner-only runtime directory")
    return Path("/run/user") / str(os.getuid())


def transaction_lock_path(repo: Path, *, runtime_dir: Path | None = None) -> Path:
    """Return the deterministic per-repository lock path for this OS user."""
    identity = str(repo.resolve()).encode("utf-8", "surrogateescape")
    digest = hashlib.sha256(identity).hexdigest()[:24]
    root = runtime_dir or _default_runtime_dir()
    if not root.is_absolute():
        raise DownstreamUpdateLockError("updater runtime root must be an absolute path")
    root = Path(os.path.abspath(root))
    return root / _LOCK_DIRECTORY / f"{digest}.lock"


class UpdaterTransactionLock:
    """Advisory owner-only lock held from updater entry through readiness verification."""

    def __init__(self, *, path: Path, repair_permissions: bool = True) -> None:
        self.path = path
        self.repair_permissions = repair_permissions
        self._fd: int | None = None

    def _runtime_root(self) -> Path:
        if not self.path.is_absolute():
            raise DownstreamUpdateLockError("updater lock path must be absolute")
        if self.path.parent.name != _LOCK_DIRECTORY:
            raise DownstreamUpdateLockError("updater lock path has an unexpected directory")
        runtime_root = self.path.parent.parent
        if self.path.parent != runtime_root / _LOCK_DIRECTORY:
            raise DownstreamUpdateLockError("updater lock path is not anchored to its runtime root")
        return runtime_root

    @staticmethod
    def _require_owner(metadata: os.stat_result, label: str) -> None:
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise DownstreamUpdateLockError(f"{label} is not owned by this user")

    def _open_runtime_root(self) -> int:
        runtime_root = self._runtime_root()
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            runtime_fd = os.open("/", flags)
        except OSError as exc:
            raise DownstreamUpdateLockError(
                f"cannot open updater runtime root {runtime_root}: {exc}"
            ) from exc
        try:
            for component in runtime_root.parts[1:]:
                try:
                    next_fd = os.open(component, flags, dir_fd=runtime_fd)
                except OSError as exc:
                    raise DownstreamUpdateLockError(
                        "updater runtime root contains a symlink, non-directory, or unreadable component"
                    ) from exc
                os.close(runtime_fd)
                runtime_fd = next_fd
            opened = os.fstat(runtime_fd)
            if not stat.S_ISDIR(opened.st_mode):
                raise DownstreamUpdateLockError("updater runtime root is not a directory")
            self._require_owner(opened, "updater runtime root")
            if stat.S_IMODE(opened.st_mode) != _OWNER_ONLY_DIRECTORY_MODE:
                raise DownstreamUpdateLockError(
                    "updater runtime root permissions are not owner-only"
                )
        except BaseException:
            os.close(runtime_fd)
            raise
        return runtime_fd

    def _prepare_directory(self) -> int:
        """Return a no-follow fd for the dedicated lock directory."""
        runtime_fd = self._open_runtime_root()
        directory_fd: int | None = None
        try:
            try:
                os.mkdir(
                    _LOCK_DIRECTORY,
                    mode=_OWNER_ONLY_DIRECTORY_MODE,
                    dir_fd=runtime_fd,
                )
            except FileExistsError:
                pass
            except OSError as exc:
                raise DownstreamUpdateLockError(
                    f"cannot prepare updater lock directory {self.path.parent}: {exc}"
                ) from exc

            flags = os.O_RDONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                directory_fd = os.open(
                    _LOCK_DIRECTORY,
                    flags,
                    dir_fd=runtime_fd,
                )
            except OSError as exc:
                raise DownstreamUpdateLockError(
                    "updater lock directory is a symlink, is not a directory, or cannot be opened"
                ) from exc

            metadata = os.fstat(directory_fd)
            if not stat.S_ISDIR(metadata.st_mode):
                raise DownstreamUpdateLockError("updater lock parent is not a directory")
            self._require_owner(metadata, "updater lock directory")
            mode = stat.S_IMODE(metadata.st_mode)
            if mode != _OWNER_ONLY_DIRECTORY_MODE:
                if not self.repair_permissions:
                    raise DownstreamUpdateLockError(
                        f"updater lock directory permissions are not owner-only: {mode:o}"
                    )
                try:
                    os.fchmod(directory_fd, _OWNER_ONLY_DIRECTORY_MODE)
                except OSError as exc:
                    raise DownstreamUpdateLockError(
                        "cannot enforce owner-only updater lock directory permissions"
                    ) from exc
            prepared_fd, directory_fd = directory_fd, None
            return prepared_fd
        except BaseException:
            if directory_fd is not None:
                os.close(directory_fd)
            raise
        finally:
            os.close(runtime_fd)

    def acquire(self) -> None:
        """Acquire the non-blocking transaction lock or fail closed."""
        if self._fd is not None:
            raise DownstreamUpdateLockError("updater transaction lock is already acquired")

        directory_fd = self._prepare_directory()
        fd: int | None = None
        try:
            flags = os.O_RDWR | os.O_CREAT
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(
                    self.path.name,
                    flags,
                    _OWNER_ONLY_FILE_MODE,
                    dir_fd=directory_fd,
                )
            except OSError as exc:
                raise DownstreamUpdateLockError(
                    f"cannot open updater transaction lock: {exc}"
                ) from exc
            finally:
                os.close(directory_fd)

            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise DownstreamUpdateLockError("updater transaction lock is not a regular file")
            self._require_owner(metadata, "updater transaction lock")
            if stat.S_IMODE(metadata.st_mode) != _OWNER_ONLY_FILE_MODE:
                if not self.repair_permissions:
                    raise DownstreamUpdateLockError(
                        "updater transaction lock permissions are not owner-only"
                    )
                os.fchmod(fd, _OWNER_ONLY_FILE_MODE)

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
            if os.write(fd, payload) != len(payload):
                raise DownstreamUpdateLockError("cannot write complete updater lock metadata")
            os.fsync(fd)
        except BaseException:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
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
