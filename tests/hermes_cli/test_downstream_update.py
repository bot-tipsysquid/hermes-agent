"""Fail-closed orchestration for the supported Silverblue downstream updater."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

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
        upstream_url: str = _UPSTREAM_URL,
        fork_url: str = _FORK_URL,
        fail_marker: str | None = None,
    ) -> None:
        self.repo = repo.resolve()
        self.branch = branch
        self.dirty = dirty
        self.fail_ff = fail_ff
        self.merge_conflict = merge_conflict
        self.fail_desktop_build = fail_desktop_build
        self.push_mismatch = push_mismatch
        self.upstream_url = upstream_url
        self.fork_url = fork_url
        self.fail_marker = fail_marker
        self.main_sha = "0" * 40
        self.downstream_sha = _INITIAL_DOWNSTREAM
        self.remote_downstream_sha = _INITIAL_DOWNSTREAM
        self.commands: list[tuple[str, ...]] = []
        self.events: list[object] = []

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
                return _result()
            if args[:1] == ("checkout",):
                self.branch = args[1]
                return _result()
            if args == ("merge", "--ff-only", "upstream/main"):
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
                values = {
                    "main": self.main_sha,
                    "upstream/main": _UPSTREAM_SHA,
                    "ken/downstream": self.downstream_sha,
                    "HEAD": self.downstream_sha if self.branch == "ken/downstream" else self.main_sha,
                }
                return _result(stdout=f"{values[ref]}\n")
            if args[:1] == ("push",):
                if not self.push_mismatch:
                    self.remote_downstream_sha = self.downstream_sha
                return _result()
            if args[:1] == ("ls-remote",):
                return _result(
                    stdout=f"{self.remote_downstream_sha}\trefs/heads/ken/downstream\n"
                )
            raise AssertionError(f"unexpected git command: {argv}")

        if argv[:3] == ("python", "-m", "hermes_cli.main") and "desktop" in argv:
            return _result(1, stderr="desktop build failed") if self.fail_desktop_build else _result()
        if argv[:2] == ("onecli", "auth"):
            return _result(stdout=json.dumps({"authenticated": True}))
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


def _gateway_verifier(runner: _GitRunner, expected_sha: str) -> None:
    assert expected_sha == _UPDATED_DOWNSTREAM
    runner.events.append("gateway-ready")


def _run(runner: _GitRunner):
    return run_update(
        _config(runner.repo),
        runner=runner,
        provision_toolbox=_provisioner,
        verify_artifacts=_artifact_verifier,
        verify_gateway=_gateway_verifier,
        output=lambda _line: None,
    )


def _index(commands: list[tuple[str, ...]], expected: tuple[str, ...]) -> int:
    return commands.index(expected)


def test_happy_path_restores_downstream_before_tests_builds_push_and_restart(tmp_path):
    runner = _GitRunner(tmp_path)

    result = _run(runner)

    assert result == _UPDATED_DOWNSTREAM
    checkout_downstream = _index(runner.commands, ("git", "checkout", "ken/downstream"))
    python_sync = _index(runner.commands, ("uv", "sync", "--locked"))
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
    push = next(i for i, command in enumerate(runner.commands) if command[1:2] == ("push",))
    restart = _index(
        runner.commands,
        ("python", "-m", "hermes_cli.main", "gateway", "restart"),
    )

    assert checkout_downstream < python_sync < test_run < web_build < desktop_build < push < restart
    assert (
        "git",
        "push",
        "fork",
        f"{_UPDATED_DOWNSTREAM}:refs/heads/ken/downstream",
    ) in runner.commands
    assert runner.branch == "ken/downstream"
    assert runner.events[-1] == "gateway-ready"


def test_fast_forward_failure_restores_downstream_and_stops_before_dependencies(tmp_path):
    runner = _GitRunner(tmp_path, fail_ff=True)

    with pytest.raises(DownstreamUpdateError, match="fast-forward local main"):
        _run(runner)

    assert runner.branch == "ken/downstream"
    assert ("uv", "sync", "--locked") not in runner.commands
    assert not any(command[1:2] == ("push",) for command in runner.commands)
    assert not any(command[-2:] == ("gateway", "restart") for command in runner.commands)


def test_merge_conflict_is_left_visible_and_blocks_build_push_and_restart(tmp_path):
    runner = _GitRunner(tmp_path, merge_conflict=True)

    with pytest.raises(DownstreamUpdateError, match="merge main into ken/downstream"):
        _run(runner)

    flattened = [part for command in runner.commands for part in command]
    assert "--abort" not in flattened
    assert "reset" not in flattened
    assert ("uv", "sync", "--locked") not in runner.commands
    assert not any(command[1:2] == ("push",) for command in runner.commands)
    assert not any(command[-2:] == ("gateway", "restart") for command in runner.commands)


def test_desktop_build_failure_prevents_push_and_restart(tmp_path):
    runner = _GitRunner(tmp_path, fail_desktop_build=True)

    with pytest.raises(DownstreamUpdateError, match="ARM64 Desktop build"):
        _run(runner)

    assert not any(command[1:2] == ("push",) for command in runner.commands)
    assert not any(command[-2:] == ("gateway", "restart") for command in runner.commands)


@pytest.mark.parametrize("marker", ["--locked", "ci", "run_tests.sh", "typecheck"])
def test_dependency_and_test_gate_failures_prevent_push_and_restart(tmp_path, marker):
    runner = _GitRunner(tmp_path, fail_marker=marker)

    with pytest.raises(DownstreamUpdateError):
        _run(runner)

    assert not any(command[1:2] == ("push",) for command in runner.commands)
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


def test_dry_run_prints_plan_without_touching_checkout(capsys):
    assert main(["--dry-run"]) == 0

    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "ken/downstream" in output
    assert "gateway restart" in output
