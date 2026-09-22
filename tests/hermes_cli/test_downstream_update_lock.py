"""Single-writer lock coverage for the downstream update transaction."""

from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

from hermes_cli import downstream_update_lock as lock_module
from hermes_cli.downstream_update_lock import (
    DownstreamUpdateLockError,
    UpdaterTransactionLock,
    transaction_lock_path,
)

pytestmark = pytest.mark.linux_only


def _runtime_dir(tmp_path: Path, *, mode: int = 0o700) -> Path:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=mode)
    os.chmod(runtime, mode)
    return runtime


def _lock_path(tmp_path: Path, *, runtime_mode: int = 0o700) -> Path:
    runtime = _runtime_dir(tmp_path, mode=runtime_mode)
    return transaction_lock_path(tmp_path / "checkout", runtime_dir=runtime)


def test_transaction_lock_path_is_deterministic_and_scoped_to_runtime_dir(tmp_path):
    repo = tmp_path / "checkout"
    runtime = _runtime_dir(tmp_path)

    first = transaction_lock_path(repo, runtime_dir=runtime)
    second = transaction_lock_path(repo, runtime_dir=runtime)

    assert first == second
    assert first.parent == runtime / "hermes-downstream-update"
    assert first.name.endswith(".lock")


def test_relative_xdg_runtime_override_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "relative/runtime")

    with pytest.raises(DownstreamUpdateLockError, match="absolute"):
        transaction_lock_path(tmp_path / "checkout")


def test_transaction_lock_is_owner_only_and_blocks_concurrent_holder(tmp_path):
    path = _lock_path(tmp_path)

    with UpdaterTransactionLock(path=path):
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with pytest.raises(DownstreamUpdateLockError, match="already running|held"):
            with UpdaterTransactionLock(path=path):
                pass


def test_stale_unlocked_file_is_reclaimed_without_deleting_lock_path(tmp_path):
    path = _lock_path(tmp_path)
    path.parent.mkdir(mode=0o700)
    path.write_text("stale owner metadata\n", encoding="utf-8")
    os.chmod(path, 0o600)

    with UpdaterTransactionLock(path=path):
        contents = path.read_text(encoding="utf-8")
        assert f'"pid": {os.getpid()}' in contents
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    assert path.exists()


def test_default_lock_rejects_unsafe_preexisting_directory_without_chmod(tmp_path):
    path = _lock_path(tmp_path)
    path.parent.mkdir(mode=0o755)
    os.chmod(path.parent, 0o755)

    with pytest.raises(DownstreamUpdateLockError, match="owner-only|permissions"):
        with UpdaterTransactionLock(path=path):
            pass

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o755
    assert not path.exists()


def test_default_lock_rejects_unsafe_preexisting_file_without_chmod(tmp_path):
    path = _lock_path(tmp_path)
    path.parent.mkdir(mode=0o700)
    path.write_text("pre-existing\n", encoding="utf-8")
    os.chmod(path, 0o644)

    with pytest.raises(DownstreamUpdateLockError, match="owner-only|permissions"):
        with UpdaterTransactionLock(path=path):
            pass

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_symlinked_lock_directory_is_rejected_without_chmodding_target(tmp_path):
    runtime = _runtime_dir(tmp_path)
    target = tmp_path / "unrelated"
    target.mkdir(mode=0o755)
    os.chmod(target, 0o755)
    (runtime / "hermes-downstream-update").symlink_to(target, target_is_directory=True)
    path = transaction_lock_path(tmp_path / "checkout", runtime_dir=runtime)

    with pytest.raises(DownstreamUpdateLockError, match="directory|symlink"):
        UpdaterTransactionLock(path=path).acquire()

    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert not (target / path.name).exists()


def test_symlinked_runtime_root_ancestor_is_rejected(tmp_path):
    actual_parent = tmp_path / "actual-parent"
    actual_parent.mkdir()
    runtime = actual_parent / "runtime"
    runtime.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(actual_parent, target_is_directory=True)
    path = transaction_lock_path(
        tmp_path / "checkout", runtime_dir=alias / "runtime"
    )

    with pytest.raises(DownstreamUpdateLockError, match="runtime root|symlink|open"):
        UpdaterTransactionLock(path=path).acquire()

    assert not (runtime / "hermes-downstream-update").exists()


def test_unsafe_runtime_root_mode_is_rejected_without_repair(tmp_path):
    path = _lock_path(tmp_path, runtime_mode=0o755)

    with pytest.raises(DownstreamUpdateLockError, match="runtime root.*owner-only"):
        UpdaterTransactionLock(path=path).acquire()

    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o755
    assert not path.parent.exists()


def test_runtime_root_owned_by_another_user_is_rejected_before_lock_dir_creation(
    tmp_path, monkeypatch
):
    path = _lock_path(tmp_path)
    actual_uid = os.getuid()
    monkeypatch.setattr(lock_module.os, "getuid", lambda: actual_uid + 1)

    with pytest.raises(DownstreamUpdateLockError, match="runtime root.*owned"):
        UpdaterTransactionLock(path=path).acquire()

    assert not path.parent.exists()


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_acquisition_base_exception_closes_fd_and_releases_flock(
    tmp_path, monkeypatch, interrupt_type
):
    path = _lock_path(tmp_path)
    lock = UpdaterTransactionLock(path=path)

    def interrupt_write(_fd, _payload):
        raise interrupt_type("injected during acquisition")

    with monkeypatch.context() as context:
        context.setattr(lock_module.os, "write", interrupt_write)
        with pytest.raises(interrupt_type, match="injected during acquisition"):
            lock.acquire()

    assert lock._fd is None
    with UpdaterTransactionLock(path=path):
        assert path.exists()


def test_lock_release_after_base_exception_allows_next_transaction(tmp_path):
    path = _lock_path(tmp_path)

    with pytest.raises(KeyboardInterrupt):
        with UpdaterTransactionLock(path=path):
            raise KeyboardInterrupt

    with UpdaterTransactionLock(path=path):
        assert path.exists()
