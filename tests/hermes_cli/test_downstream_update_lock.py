"""Single-writer lock coverage for the downstream update transaction."""

from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

from hermes_cli.downstream_update_lock import (
    DownstreamUpdateLockError,
    UpdaterTransactionLock,
    transaction_lock_path,
)


def test_transaction_lock_path_is_deterministic_and_scoped_to_runtime_dir(tmp_path):
    repo = tmp_path / "checkout"
    runtime = tmp_path / "runtime"

    first = transaction_lock_path(repo, runtime_dir=runtime)
    second = transaction_lock_path(repo, runtime_dir=runtime)

    assert first == second
    assert first.parent == runtime / "hermes-downstream-update"
    assert first.name.endswith(".lock")


def test_transaction_lock_is_owner_only_and_blocks_concurrent_holder(tmp_path):
    path = tmp_path / "locks" / "transaction.lock"

    with UpdaterTransactionLock(path=path):
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with pytest.raises(DownstreamUpdateLockError, match="already running|held"):
            with UpdaterTransactionLock(path=path):
                pass


def test_stale_unlocked_file_is_reclaimed_without_deleting_lock_path(tmp_path):
    path = tmp_path / "locks" / "transaction.lock"
    path.parent.mkdir(mode=0o700)
    path.write_text("stale owner metadata\n", encoding="utf-8")
    os.chmod(path, 0o600)

    with UpdaterTransactionLock(path=path):
        contents = path.read_text(encoding="utf-8")
        assert f'"pid": {os.getpid()}' in contents
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    assert path.exists()


def test_lock_fails_closed_when_owner_only_directory_cannot_be_proven(tmp_path):
    directory = tmp_path / "locks"
    directory.mkdir(mode=0o755)
    path = directory / "transaction.lock"

    with pytest.raises(DownstreamUpdateLockError, match="owner-only|permissions"):
        with UpdaterTransactionLock(path=path, repair_permissions=False):
            pass


def test_lock_release_after_base_exception_allows_next_transaction(tmp_path):
    path = tmp_path / "locks" / "transaction.lock"

    with pytest.raises(KeyboardInterrupt):
        with UpdaterTransactionLock(path=path):
            raise KeyboardInterrupt

    with UpdaterTransactionLock(path=path):
        assert path.exists()
