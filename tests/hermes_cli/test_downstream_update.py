"""Fail-closed orchestration for the supported Silverblue downstream updater."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import downstream_update as updater
from hermes_cli.downstream_update import (
    DownstreamUpdateError,
    UpdateConfig,
    main,
    run_update,
)


_UPSTREAM_URL = "git@github.com:NousResearch/hermes-agent.git"
_FORK_URL = "git@github.com:bot-tipsysquid/hermes-agent.git"
_INITIAL_DOWNSTREAM = "1" * 40
_UPDATED_DOWNSTREAM = "2" * 40
_UPSTREAM_SHA = "a" * 40
_PRE_RESTART_GATEWAY = object()
_RESTART_THRESHOLD = 1234.5


def _result(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _GitRunner:
    def __init__(
        self,
        repo: Path,
        *,
        branch: str = "ken/downstream",
        dirty: bool = False,
        fail_ff: bool = False,
        merge_conflict: bool = False,
        fail_desktop_build: bool = False,
        push_mismatch: bool = False,
        main_push_mismatch: bool = False,
        local_main_diverged: bool = False,
        fork_main_diverged: bool = False,
        onecli_authenticated: bool = True,
        upstream_url: str = _UPSTREAM_URL,
        fork_url: str = _FORK_URL,
        fail_marker: str | None = None,
        interrupt_at: str | None = None,
        interrupt_type: type[BaseException] = KeyboardInterrupt,
    ) -> None:
        self.repo = repo.resolve()
        self.branch = branch
        self.dirty = dirty
        self.fail_ff = fail_ff
        self.merge_conflict = merge_conflict
        self.fail_desktop_build = fail_desktop_build
        self.push_mismatch = push_mismatch
        self.main_push_mismatch = main_push_mismatch
        self.local_main_diverged = local_main_diverged
        self.fork_main_diverged = fork_main_diverged
        self.onecli_authenticated = onecli_authenticated
        self.upstream_url = upstream_url
        self.fork_url = fork_url
        self.fail_marker = fail_marker
        self.interrupt_at = interrupt_at
        self.interrupt_type = interrupt_type
        self.main_sha = "0" * 40
        self.downstream_sha = _INITIAL_DOWNSTREAM
        self.remote_main_sha = "0" * 40
        self.remote_downstream_sha = _INITIAL_DOWNSTREAM
        self.commands: list[tuple[str, ...]] = []
        self.events: list[object] = []

    def _interrupt(self, point: str) -> None:
        if self.interrupt_at != point:
            return
        self.interrupt_at = None
        raise self.interrupt_type(point)

    def run(self, command, **_kwargs):
        argv = tuple(str(part) for part in command)
        self.commands.append(argv)
        self.events.append(argv)
        if self.fail_marker and any(self.fail_marker in part for part in argv):
            return _result(1, stderr=f"forced {self.fail_marker} failure")

        if argv[0] == "git":
            args = argv[1:]
            if args == ("rev-parse", "--show-toplevel"):
                return _result(stdout=f"{self.repo}\n")
            if args == ("branch", "--show-current"):
                self._interrupt("branch-probe")
                return _result(stdout=f"{self.branch}\n")
            if args == ("status", "--porcelain", "--untracked-files=all"):
                return _result(stdout=" M tracked.py\n" if self.dirty else "")
            if args[:2] == ("remote", "get-url"):
                remote = args[-1]
                url = self.upstream_url if remote == "upstream" else self.fork_url
                return _result(stdout=f"{url}\n")
            if args[0] == "fetch":
                return _result()
            if args[:2] == ("merge-base", "--is-ancestor"):
                refs = args[2:]
                if refs == ("main", "upstream/main") and self.local_main_diverged:
                    return _result(1, stderr="local main is not an ancestor")
                if refs == ("fork/main", "upstream/main") and self.fork_main_diverged:
                    return _result(1, stderr="fork main is not an ancestor")
                return _result()
            if args[:1] == ("checkout",):
                target = args[1]
                if target == "ken/downstream":
                    self._interrupt("checkout-downstream-before")
                self.branch = target
                self._interrupt(f"checkout-{target}-after")
                return _result()
            if args == ("merge", "--ff-only", "upstream/main"):
                self._interrupt("fast-forward-main")
                if self.fail_ff:
                    return _result(1, stderr="not possible to fast-forward")
                self.main_sha = _UPSTREAM_SHA
                return _result()
            if args == ("merge", "--no-edit", "main"):
                if self.merge_conflict:
                    return _result(1, stderr="CONFLICT")
                self.downstream_sha = _UPDATED_DOWNSTREAM
                return _result()
            if args[:1] == ("rev-parse",):
                ref = args[1]
                if ref == "main":
                    self._interrupt("read-local-main")
                values = {
                    "main": self.main_sha,
                    "upstream/main": _UPSTREAM_SHA,
                    "fork/main": self.remote_main_sha,
                    "ken/downstream": self.downstream_sha,
                    "HEAD": self.downstream_sha if self.branch == "ken/downstream" else self.main_sha,
                }
                return _result(stdout=f"{values[ref]}\n")
            if args[:1] == ("push",):
                destination = args[-1].split(":", 1)[-1]
                if destination == "refs/heads/main":
                    self._interrupt("push-main")
                    if not self.main_push_mismatch:
                        self.remote_main_sha = self.main_sha
                elif not self.push_mismatch:
                    self.remote_downstream_sha = self.downstream_sha
                return _result()
            if args[:1] == ("ls-remote",):
                ref = args[-1]
                if ref == "refs/heads/main":
                    self._interrupt("readback-main")
                    sha = self.remote_main_sha
                else:
                    sha = self.remote_downstream_sha
                return _result(stdout=f"{sha}\t{ref}\n")
            raise AssertionError(f"unexpected git command: {argv}")

        if argv[:3] == ("python", "-m", "hermes_cli.main") and "desktop" in argv:
            return _result(1, stderr="desktop build failed") if self.fail_desktop_build else _result()
        if argv[:3] == ("python", "-m", "hermes_cli.downstream_update"):
            return _result(
                stdout=json.dumps(
                    {
                        "checkout_root": str(self.repo),
                        "sha": self.downstream_sha,
                        "short_sha": self.downstream_sha[:8],
                        "source": "git",
                        "version": "0.test",
                    }
                )
            )
        if argv[:2] == ("onecli", "auth"):
            return _result(stdout=json.dumps({"authenticated": self.onecli_authenticated}))
        return _result()


def _config(repo: Path) -> UpdateConfig:
    return UpdateConfig(
        repo=repo,
        expected_repo=repo,
        python_executable="python",
        uv_executable="uv",
        toolbox_executable="toolbox",
        onecli_executable="onecli",
    )


def _provisioner(runner: _GitRunner, **_kwargs) -> str:
    runner.events.append("toolbox-ready")
    return "hermes-arm-build"


def _artifact_verifier(runner: _GitRunner, _repo: Path) -> None:
    runner.events.append("artifacts-verified")


def _gateway_verifier(
    runner: _GitRunner,
    expected_sha: str,
    pre_restart: object,
    restart_started_at: float,
) -> None:
    assert expected_sha == _UPDATED_DOWNSTREAM
    assert pre_restart is _PRE_RESTART_GATEWAY
    assert restart_started_at == _RESTART_THRESHOLD
    runner.events.append("gateway-ready")


def _run(runner: _GitRunner, **overrides):
    @contextmanager
    def transaction_lock(_config):
        runner.events.append("lock-acquired")
        try:
            yield
        finally:
            runner.events.append("lock-released")

    def host_preflight(_runner, config, provision_toolbox, output):
        runner.events.append("host-ready")
        return provision_toolbox(
            runner=_runner,
            project_root=config.repo,
            toolbox_executable=config.toolbox_executable,
            output=output,
        )

    def config_verifier(_runner, _config):
        runner.events.append("config-ready")

    def runtime_identity_verifier(_runner, _config, expected_sha):
        assert expected_sha == _UPDATED_DOWNSTREAM
        runner.events.append("runtime-identity-ready")

    def onecli_service_ready(_config):
        runner.events.append("onecli-service-ready")
        return True

    def capture_gateway_receipt(_runner):
        runner.events.append("gateway-receipt-captured")
        return _PRE_RESTART_GATEWAY

    def restart_clock():
        runner.events.append("gateway-restart-threshold")
        return _RESTART_THRESHOLD

    kwargs = {
        "runner": runner,
        "provision_toolbox": _provisioner,
        "verify_artifacts": _artifact_verifier,
        "verify_gateway": _gateway_verifier,
        "host_preflight": host_preflight,
        "transaction_lock": transaction_lock,
        "verify_config": config_verifier,
        "verify_runtime_identity": runtime_identity_verifier,
        "onecli_service_ready": onecli_service_ready,
        "capture_gateway_receipt": capture_gateway_receipt,
        "restart_clock": restart_clock,
        "output": lambda _line: None,
    }
    kwargs.update(overrides)
    return run_update(_config(runner.repo), **kwargs)


def _index(commands: list[tuple[str, ...]], expected: tuple[str, ...]) -> int:
    return commands.index(expected)


def _ran_uv_sync(runner: _GitRunner) -> bool:
    return any(command[:2] == ("uv", "sync") for command in runner.commands)


def _pushed_branch(runner: _GitRunner, branch: str) -> bool:
    suffix = f":refs/heads/{branch}"
    return any(
        command[1:2] == ("push",) and command[-1].endswith(suffix)
        for command in runner.commands
    )


def test_happy_path_publishes_pristine_main_restores_downstream_then_tests_builds_pushes_and_restarts(
    tmp_path,
):
    runner = _GitRunner(tmp_path)

    result = _run(runner)

    assert result == _UPDATED_DOWNSTREAM
    checkout_main = _index(runner.commands, ("git", "checkout", "main"))
    push_main = _index(
        runner.commands,
        ("git", "push", "fork", f"{_UPSTREAM_SHA}:refs/heads/main"),
    )
    readback_main = _index(
        runner.commands,
        ("git", "ls-remote", "--exit-code", "--heads", "fork", "refs/heads/main"),
    )
    checkout_downstream = _index(runner.commands, ("git", "checkout", "ken/downstream"))
    python_sync = _index(
        runner.commands,
        ("uv", "sync", "--locked", "--extra", "all", "--extra", "dev"),
    )
    test_run = next(
        i for i, command in enumerate(runner.commands) if command[0].endswith("run_tests.sh")
    )
    web_build = _index(
        runner.commands,
        (
            "toolbox",
            "run",
            "--container",
            "hermes-arm-build",
            "npm",
            "run",
            "build",
            "--workspace",
            "web",
        ),
    )
    desktop_build = next(
        i
        for i, command in enumerate(runner.commands)
        if command[:3] == ("python", "-m", "hermes_cli.main") and "desktop" in command
    )
    push_downstream = _index(
        runner.commands,
        ("git", "push", "fork", f"{_UPDATED_DOWNSTREAM}:refs/heads/ken/downstream"),
    )
    restart = _index(
        runner.commands,
        ("python", "-m", "hermes_cli.main", "gateway", "restart"),
    )

    assert (
        checkout_main
        < push_main
        < readback_main
        < checkout_downstream
        < python_sync
        < test_run
        < web_build
        < desktop_build
        < push_downstream
        < restart
    )
    assert runner.branch == "ken/downstream"
    assert runner.events[-2:] == ["gateway-ready", "lock-released"]
    assert runner.events.index("host-ready") < runner.events.index("toolbox-ready")
    assert runner.events.index("config-ready") < runner.events.index("runtime-identity-ready")
    assert runner.events.index("runtime-identity-ready") < runner.events.index(
        ("python", "-m", "hermes_cli.main", "gateway", "restart")
    )
    assert runner.events.index("gateway-receipt-captured") < runner.events.index(
        "gateway-restart-threshold"
    )
    assert runner.events.index("gateway-restart-threshold") < runner.events.index(
        ("python", "-m", "hermes_cli.main", "gateway", "restart")
    )


def test_fast_forward_failure_restores_downstream_and_stops_before_dependencies(tmp_path):
    runner = _GitRunner(tmp_path, fail_ff=True)

    with pytest.raises(DownstreamUpdateError, match="fast-forward local main"):
        _run(runner)

    assert runner.branch == "ken/downstream"
    assert not _ran_uv_sync(runner)
    assert not any(command[1:2] == ("push",) for command in runner.commands)
    assert not any(command[-2:] == ("gateway", "restart") for command in runner.commands)


def test_merge_conflict_is_left_visible_and_blocks_build_push_and_restart(tmp_path):
    runner = _GitRunner(tmp_path, merge_conflict=True)

    with pytest.raises(DownstreamUpdateError, match="merge main into ken/downstream"):
        _run(runner)

    flattened = [part for command in runner.commands for part in command]
    assert "--abort" not in flattened
    assert "reset" not in flattened
    assert not _ran_uv_sync(runner)
    assert _pushed_branch(runner, "main")
    assert not _pushed_branch(runner, "ken/downstream")
    assert not any(command[-2:] == ("gateway", "restart") for command in runner.commands)


def test_desktop_build_failure_prevents_push_and_restart(tmp_path):
    runner = _GitRunner(tmp_path, fail_desktop_build=True)

    with pytest.raises(DownstreamUpdateError, match="ARM64 Desktop build"):
        _run(runner)

    assert _pushed_branch(runner, "main")
    assert not _pushed_branch(runner, "ken/downstream")
    assert not any(command[-2:] == ("gateway", "restart") for command in runner.commands)


@pytest.mark.parametrize("marker", ["--locked", "ci", "run_tests.sh", "typecheck"])
def test_dependency_and_test_gate_failures_prevent_push_and_restart(tmp_path, marker):
    runner = _GitRunner(tmp_path, fail_marker=marker)

    with pytest.raises(DownstreamUpdateError):
        _run(runner)

    assert _pushed_branch(runner, "main")
    assert not _pushed_branch(runner, "ken/downstream")
    assert not any(command[-2:] == ("gateway", "restart") for command in runner.commands)


def test_push_mismatch_prevents_gateway_restart(tmp_path):
    runner = _GitRunner(tmp_path, push_mismatch=True)

    with pytest.raises(DownstreamUpdateError, match="fork/ken/downstream.*does not match"):
        _run(runner)

    assert any(command[1:2] == ("push",) for command in runner.commands)
    assert not any(command[-2:] == ("gateway", "restart") for command in runner.commands)


@pytest.mark.parametrize(
    ("runner_kwargs", "message"),
    [
        ({"dirty": True}, "working tree is not clean"),
        ({"branch": "main"}, "expected branch ken/downstream"),
    ],
)
def test_preflight_rejects_unsafe_checkout_before_fetch(tmp_path, runner_kwargs, message):
    runner = _GitRunner(tmp_path, **runner_kwargs)

    with pytest.raises(DownstreamUpdateError, match=message):
        _run(runner)

    assert not any(command[1:2] == ("fetch",) for command in runner.commands)


def test_preflight_rejects_wrong_remote_before_fetch(tmp_path):
    runner = _GitRunner(
        tmp_path,
        upstream_url="https://evil.example/github.com/NousResearch/hermes-agent.git",
    )

    with pytest.raises(DownstreamUpdateError, match="remote upstream has wrong URL"):
        _run(runner)

    assert not any(command[1:2] == ("fetch",) for command in runner.commands)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/NousResearch/hermes-agent.git",
        "git@github.com:NousResearch/hermes-agent.git",
        "ssh://git@github.com/NousResearch/hermes-agent.git",
    ],
)
def test_remote_transport_accepts_only_reviewed_canonical_forms(url):
    assert updater._remote_slug(url) == "NousResearch/hermes-agent"


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/NousResearch/hermes-agent.git",
        "git://github.com/NousResearch/hermes-agent.git",
        "ftp://github.com/NousResearch/hermes-agent.git",
        "https://user@github.com/NousResearch/hermes-agent.git",
        "https://github.com:443/NousResearch/hermes-agent.git",
        "https://github.com.evil.example/NousResearch/hermes-agent.git",
        "https://github.com/NousResearch/hermes-agent",
        "https://github.com/NousResearch/hermes-agent.git/",
        "https://github.com/NousResearch/hermes-agent.git?ref=main",
        "https://github.com/NousResearch/hermes-agent.git#main",
        "git@github.com:NousResearch/hermes-agent",
        "git@github.com:NousResearch/hermes-agent.git/extra",
        "root@github.com:NousResearch/hermes-agent.git",
        "ssh://github.com/NousResearch/hermes-agent.git",
        "ssh://root@github.com/NousResearch/hermes-agent.git",
        "ssh://git@github.com:22/NousResearch/hermes-agent.git",
        "ssh://git@evil.example/NousResearch/hermes-agent.git",
        "file:///NousResearch/hermes-agent.git",
        "/local/NousResearch/hermes-agent.git",
    ],
)
def test_remote_transport_rejects_insecure_or_ambiguous_forms(url):
    assert updater._remote_slug(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/NousResearch/hermes-agent.git",
        "https://user@github.com/NousResearch/hermes-agent.git",
        "ssh://git@github.com:2222/NousResearch/hermes-agent.git",
        "ssh://git@evil.example/NousResearch/hermes-agent.git",
        "https://github.com/Other/hermes-agent.git",
    ],
)
def test_invalid_remote_transport_stops_before_fetch_or_mutation(tmp_path, url):
    runner = _GitRunner(tmp_path, upstream_url=url)

    with pytest.raises(DownstreamUpdateError, match="remote upstream has wrong URL"):
        _run(runner)

    assert not any(command[1:2] in {("fetch",), ("checkout",), ("merge",)} for command in runner.commands)
    assert not _ran_uv_sync(runner)
    assert "host-ready" not in runner.events
    assert "toolbox-ready" not in runner.events


@pytest.mark.parametrize(
    ("machine", "os_release", "ostree", "message"),
    [
        ("x86_64", 'ID=fedora\nVERSION_ID="44"\nVARIANT_ID=silverblue\n', True, "aarch64"),
        ("aarch64", 'ID=fedora\nVERSION_ID="44"\nVARIANT_ID=workstation\n', True, "Silverblue"),
        ("aarch64", 'ID=fedora\nVERSION_ID="44"\nVARIANT_ID=silverblue\n', False, "OSTree"),
    ],
)
def test_host_preflight_rejects_unsupported_platform_before_toolbox(
    tmp_path, machine, os_release, ostree, message
):
    release_path = tmp_path / "os-release"
    release_path.write_text(os_release, encoding="utf-8")
    ostree_path = tmp_path / "ostree-booted"
    if ostree:
        ostree_path.touch()
    provisioned = []

    with pytest.raises(DownstreamUpdateError, match=message):
        updater.preflight_silverblue_host(
            _GitRunner(tmp_path),
            _config(tmp_path),
            lambda **kwargs: provisioned.append(kwargs) or "hermes-arm-build",
            lambda _line: None,
            machine=lambda: machine,
            os_release=release_path,
            ostree_booted=ostree_path,
        )

    assert provisioned == []


def test_host_preflight_proves_release_matched_persistent_toolbox_policy(tmp_path):
    release_path = tmp_path / "os-release"
    release_path.write_text(
        'ID=fedora\nVERSION_ID="44"\nVARIANT_ID=silverblue\n', encoding="utf-8"
    )
    ostree_path = tmp_path / "ostree-booted"
    ostree_path.touch()
    seen = {}

    def provision(**kwargs):
        seen.update(kwargs)
        return "hermes-arm-build"

    container = updater.preflight_silverblue_host(
        _GitRunner(tmp_path),
        _config(tmp_path),
        provision,
        lambda _line: None,
        machine=lambda: "aarch64",
        os_release=release_path,
        ostree_booted=ostree_path,
    )

    assert container == "hermes-arm-build"
    assert seen["host_release"] == "44"
    assert seen["project_root"] == tmp_path


def test_host_rejection_occurs_before_any_repository_or_environment_mutation(tmp_path):
    runner = _GitRunner(tmp_path)

    def reject_host(*_args):
        raise DownstreamUpdateError("unsupported host")

    with pytest.raises(DownstreamUpdateError, match="unsupported host"):
        _run(runner, host_preflight=reject_host)

    assert not any(
        command[1:2] in {("fetch",), ("checkout",), ("merge",), ("push",)}
        for command in runner.commands
    )
    assert not _ran_uv_sync(runner)
    assert "toolbox-ready" not in runner.events
    assert runner.events[0] == "lock-acquired"
    assert runner.events[-1] == "lock-released"


def test_transaction_lock_contention_stops_before_validation_or_mutation(tmp_path):
    runner = _GitRunner(tmp_path)

    @contextmanager
    def reject_lock(_config):
        raise DownstreamUpdateError("transaction lock is held")
        yield

    with pytest.raises(DownstreamUpdateError, match="transaction lock is held"):
        _run(runner, transaction_lock=reject_lock)

    assert runner.commands == []
    assert runner.events == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"local_main_diverged": True}, "local main.*fast-forward"),
        ({"fork_main_diverged": True}, "fork main.*fast-forward"),
        ({"main_push_mismatch": True}, "fork/main.*does not match"),
    ],
)
def test_pristine_main_rejects_local_ahead_divergence_or_readback_mismatch(
    tmp_path, kwargs, message
):
    runner = _GitRunner(tmp_path, **kwargs)

    with pytest.raises(DownstreamUpdateError, match=message):
        _run(runner)

    assert not _ran_uv_sync(runner)
    assert not any(command[-2:] == ("gateway", "restart") for command in runner.commands)
    assert runner.branch == "ken/downstream"


class _FatalInterrupt(BaseException):
    pass


@pytest.mark.parametrize(
    ("point", "interrupt_type"),
    [
        ("checkout-main-after", KeyboardInterrupt),
        ("fast-forward-main", SystemExit),
        ("read-local-main", _FatalInterrupt),
        ("push-main", KeyboardInterrupt),
        ("readback-main", SystemExit),
        ("checkout-downstream-before", _FatalInterrupt),
        ("checkout-ken/downstream-after", KeyboardInterrupt),
    ],
)
def test_every_main_transition_restores_downstream_on_base_exception(
    tmp_path, point, interrupt_type
):
    runner = _GitRunner(tmp_path, interrupt_at=point, interrupt_type=interrupt_type)

    with pytest.raises(interrupt_type):
        _run(runner)

    assert runner.branch == "ken/downstream"
    assert not _ran_uv_sync(runner)
    assert not any(command[-2:] == ("gateway", "restart") for command in runner.commands)


def test_restore_initial_branch_probe_interruption_restores_then_reraises(tmp_path):
    runner = _GitRunner(
        tmp_path,
        branch="main",
        interrupt_at="branch-probe",
        interrupt_type=KeyboardInterrupt,
    )

    with pytest.raises(KeyboardInterrupt, match="branch-probe"):
        updater._restore_live_branch(runner, _config(tmp_path))

    assert runner.branch == "ken/downstream"
    assert runner.commands[-1] == ("git", "checkout", "ken/downstream")


@pytest.mark.parametrize(
    "point", ["checkout-downstream-before", "checkout-ken/downstream-after"]
)
def test_restore_checkout_interruption_uses_one_unconditional_retry(tmp_path, point):
    runner = _GitRunner(
        tmp_path,
        branch="main",
        interrupt_at=point,
        interrupt_type=KeyboardInterrupt,
    )

    with pytest.raises(KeyboardInterrupt, match=point):
        updater._restore_live_branch(runner, _config(tmp_path))

    checkouts = [
        command
        for command in runner.commands
        if command == ("git", "checkout", "ken/downstream")
    ]
    assert len(checkouts) == 2
    assert runner.branch == "ken/downstream"


class _DoubleFailureRestoreRunner:
    def __init__(self, repo: Path, initial_location: str, restoration_error: BaseException):
        self.repo = repo
        self.initial_location = initial_location
        self.restoration_error = restoration_error
        self.commands: list[tuple[str, ...]] = []
        self.checkout_calls = 0

    def run(self, command, **_kwargs):
        argv = tuple(str(part) for part in command)
        self.commands.append(argv)
        if argv == ("git", "branch", "--show-current"):
            if self.initial_location == "probe":
                raise KeyboardInterrupt("initial branch probe interrupted")
            return _result(stdout="main\n")
        if argv == ("git", "checkout", "ken/downstream"):
            self.checkout_calls += 1
            if self.initial_location == "checkout" and self.checkout_calls == 1:
                raise KeyboardInterrupt("initial checkout interrupted")
            raise self.restoration_error
        raise AssertionError(f"unexpected command: {argv}")


@pytest.mark.parametrize(
    ("initial_location", "restoration_error"),
    [
        ("probe", RuntimeError("restoration command failed")),
        ("checkout", SystemExit("second restoration interrupted")),
    ],
)
def test_restore_double_failure_preserves_original_and_reports_restore_failure(
    tmp_path, initial_location, restoration_error
):
    runner = _DoubleFailureRestoreRunner(tmp_path, initial_location, restoration_error)

    with pytest.raises(KeyboardInterrupt) as excinfo:
        updater._restore_live_branch(runner, _config(tmp_path))

    assert excinfo.value.__cause__ is restoration_error
    notes = getattr(excinfo.value, "__notes__", [])
    assert any(type(restoration_error).__name__ in note for note in notes)
    assert runner.checkout_calls <= 2


@pytest.mark.parametrize("gate", ["config", "runtime-identity"])
def test_config_and_runtime_identity_failures_prevent_gateway_restart(tmp_path, gate):
    runner = _GitRunner(tmp_path)

    def fail(*_args):
        raise RuntimeError(f"{gate} rejected")

    override = {"verify_config" if gate == "config" else "verify_runtime_identity": fail}
    with pytest.raises(DownstreamUpdateError, match=gate):
        _run(runner, **override)

    assert not any(command[-2:] == ("gateway", "restart") for command in runner.commands)


def test_onecli_requires_local_service_readiness_before_authenticated_status(tmp_path):
    runner = _GitRunner(tmp_path)

    with pytest.raises(DownstreamUpdateError, match="local service.*not ready"):
        _run(runner, onecli_service_ready=lambda _config: False)

    assert not any(command[:2] == ("onecli", "auth") for command in runner.commands)
    assert not any("health" in part for command in runner.commands for part in command)
    assert not any(command[-2:] == ("gateway", "restart") for command in runner.commands)


def test_onecli_authentication_failure_prevents_gateway_restart(tmp_path):
    runner = _GitRunner(tmp_path, onecli_authenticated=False)

    with pytest.raises(DownstreamUpdateError, match="OneCLI authentication"):
        _run(runner)

    assert any(command[:3] == ("onecli", "auth", "status") for command in runner.commands)
    assert not any(command[-2:] == ("gateway", "restart") for command in runner.commands)


def test_locked_all_and_dev_extras_are_selected_before_focused_tests(tmp_path):
    runner = _GitRunner(tmp_path)

    _run(runner)

    sync = _index(
        runner.commands,
        ("uv", "sync", "--locked", "--extra", "all", "--extra", "dev"),
    )
    focused = next(
        i for i, command in enumerate(runner.commands) if command[0].endswith("run_tests.sh")
    )
    assert sync < focused


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required")
def test_locked_all_and_dev_extras_resolve_in_disposable_environment(tmp_path):
    disposable = tmp_path / "venv"
    result = subprocess.run(
        ["uv", "sync", "--locked", "--extra", "all", "--extra", "dev", "--dry-run"],
        cwd=updater.checkout_root(),
        env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(disposable)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    combined = f"{result.stdout}\n{result.stderr}".lower()
    assert result.returncode == 0, combined
    assert "debugpy==" in combined  # dev extra
    assert "uvloop==" in combined  # runtime all extra on Linux
    assert str(disposable) in combined


def _real_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _real_git_fixture(tmp_path: Path, *, conflict: bool) -> tuple[Path, UpdateConfig, str]:
    upstream = tmp_path / "upstream.git"
    fork = tmp_path / "fork.git"
    seed = tmp_path / "seed"
    checkout = tmp_path / "checkout"
    upstream.mkdir()
    fork.mkdir()
    seed.mkdir()
    _real_git(upstream, "init", "--bare")
    _real_git(fork, "init", "--bare")
    _real_git(seed, "init", "--initial-branch=main")
    _real_git(seed, "config", "user.name", "Updater Test")
    _real_git(seed, "config", "user.email", "updater@example.invalid")
    (seed / "shared.txt").write_text("base\n", encoding="utf-8")
    _real_git(seed, "add", "shared.txt")
    _real_git(seed, "commit", "-m", "base")
    _real_git(seed, "remote", "add", "upstream", str(upstream))
    _real_git(seed, "remote", "add", "fork", str(fork))
    _real_git(seed, "push", "upstream", "main")
    _real_git(seed, "push", "fork", "main")
    _real_git(seed, "checkout", "-b", "ken/downstream")
    if conflict:
        (seed / "shared.txt").write_text("downstream\n", encoding="utf-8")
        _real_git(seed, "add", "shared.txt")
    else:
        (seed / "downstream.txt").write_text("carried\n", encoding="utf-8")
        _real_git(seed, "add", "downstream.txt")
    _real_git(seed, "commit", "-m", "downstream")
    original_downstream = _real_git(seed, "rev-parse", "HEAD")
    _real_git(seed, "push", "fork", "ken/downstream")
    _real_git(tmp_path, "clone", "--branch", "ken/downstream", str(fork), str(checkout))
    _real_git(checkout, "remote", "rename", "origin", "fork")
    _real_git(checkout, "remote", "add", "upstream", str(upstream))
    _real_git(checkout, "branch", "main", "fork/main")
    _real_git(checkout, "config", "user.name", "Updater Test")
    _real_git(checkout, "config", "user.email", "updater@example.invalid")
    _real_git(seed, "checkout", "main")
    (seed / "shared.txt").write_text("upstream\n", encoding="utf-8")
    _real_git(seed, "add", "shared.txt")
    _real_git(seed, "commit", "-m", "upstream")
    upstream_sha = _real_git(seed, "rev-parse", "HEAD")
    _real_git(seed, "push", "upstream", "main")
    config = UpdateConfig(repo=checkout, expected_repo=checkout)
    assert original_downstream
    return checkout, config, upstream_sha


def test_real_git_pristine_main_publish_and_downstream_merge(tmp_path):
    checkout, config, upstream_sha = _real_git_fixture(tmp_path, conflict=False)

    updater._sync_source(updater.SubprocessRunner(), config)

    assert _real_git(checkout, "branch", "--show-current") == "ken/downstream"
    assert _real_git(checkout, "rev-parse", "main") == upstream_sha
    assert _real_git(checkout, "ls-remote", "fork", "refs/heads/main").split()[0] == upstream_sha
    assert _real_git(checkout, "merge-base", "--is-ancestor", upstream_sha, "HEAD") == ""
    assert _real_git(checkout, "status", "--porcelain") == ""


def test_real_git_conflict_stays_on_downstream_with_recovery_state_visible(tmp_path):
    checkout, config, _upstream_sha = _real_git_fixture(tmp_path, conflict=True)

    with pytest.raises(DownstreamUpdateError, match="merge main into ken/downstream"):
        updater._sync_source(updater.SubprocessRunner(), config)

    assert _real_git(checkout, "branch", "--show-current") == "ken/downstream"
    assert "UU shared.txt" in _real_git(checkout, "status", "--porcelain")


def test_supported_config_migrate_then_strict_validate_command_sequence(tmp_path):
    runner = _GitRunner(tmp_path)

    updater._verify_config(runner, _config(tmp_path))

    migrate = ("python", "-m", "hermes_cli.main", "config", "migrate")
    validate = ("python", "-m", "hermes_cli.main", "config", "validate")
    assert runner.commands.index(migrate) < runner.commands.index(validate)


def test_supported_config_migration_failure_stops_before_strict_validation(tmp_path):
    runner = _GitRunner(tmp_path, fail_marker="migrate")

    with pytest.raises(DownstreamUpdateError, match="configuration migration"):
        updater._verify_config(runner, _config(tmp_path))

    assert ("python", "-m", "hermes_cli.main", "config", "validate") not in runner.commands


def test_strict_config_validation_failure_stops_before_gateway_restart(tmp_path):
    runner = _GitRunner(tmp_path, fail_marker="validate")

    with pytest.raises(DownstreamUpdateError, match="strict configuration validation"):
        _run(runner, verify_config=updater._verify_config)

    assert not any(command[-2:] == ("gateway", "restart") for command in runner.commands)


def test_runtime_identity_gate_rejects_other_revision(tmp_path):
    runner = _GitRunner(tmp_path)
    runner.downstream_sha = "f" * 40

    with pytest.raises(DownstreamUpdateError, match="does not match"):
        updater._verify_runtime_identity(runner, _config(tmp_path), _UPDATED_DOWNSTREAM)


def test_runtime_identity_cli_reports_current_checkout_revision(capsys):
    assert main(["--runtime-identity-json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    expected_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=updater.checkout_root(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert Path(payload["checkout_root"]) == updater.checkout_root()
    assert payload["source"] == "git"
    assert payload["sha"] == expected_sha
    assert payload["version"]


def test_dry_run_prints_plan_without_touching_checkout(capsys):
    assert main(["--dry-run"]) == 0

    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "ken/downstream" in output
    assert "gateway restart" in output
    numbered_steps = [line for line in output.splitlines() if line[:2].strip().isdigit()]
    assert len(numbered_steps) == 14
    checkout_validation = next(
        index for index, line in enumerate(numbered_steps) if "validate clean expected" in line
    )
    host_preflight = next(
        index for index, line in enumerate(numbered_steps) if "native Fedora Silverblue" in line
    )
    assert checkout_validation < host_preflight
    assert "strictly validate config" in numbered_steps[11]
    assert "capture pre-restart" in numbered_steps[13]
