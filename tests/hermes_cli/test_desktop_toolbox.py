"""Persistent Fedora Toolbx lifecycle for native Desktop builds."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.desktop_toolbox import (
    REQUIRED_PACKAGES,
    TOOLBOX_NAME,
    ToolboxProvisionError,
    ensure_desktop_toolbox,
)


class _ToolboxRunner:
    def __init__(
        self,
        *,
        container_exists: bool,
        container_release: str = "44",
        packages_installed: bool = True,
    ) -> None:
        self.container_exists = container_exists
        self.container_release = container_release
        self.packages_installed = packages_installed
        self.commands: list[tuple[str, ...]] = []

    def run(self, command, **_kwargs):
        argv = tuple(str(part) for part in command)
        self.commands.append(argv)
        if argv[1:3] == ("list", "--containers"):
            row = f"abc123  {TOOLBOX_NAME}  running\n" if self.container_exists else ""
            return SimpleNamespace(returncode=0, stdout=f"CONTAINER ID  NAME  STATUS\n{row}", stderr="")
        if argv[1] == "create":
            self.container_exists = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[-2:] == ("$ID", '"$VERSION_ID"'):
            raise AssertionError("release probe must be a single shell command")
        if ". /etc/os-release" in argv[-1]:
            return SimpleNamespace(
                returncode=0,
                stdout=f"fedora\n{self.container_release}\n",
                stderr="",
            )
        if argv[-2:-1] == ("-lc",) and "rpm -q --whatprovides --quiet" in argv[-1]:
            return SimpleNamespace(
                returncode=0 if self.packages_installed else 1,
                stdout="",
                stderr="",
            )
        if "rpm" in argv and "--quiet" in argv:
            return SimpleNamespace(
                returncode=0 if self.packages_installed else 1,
                stdout="",
                stderr="",
            )
        if "dnf" in argv and "install" in argv:
            self.packages_installed = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[-2:-1] == ("-lc",) and "command -v make" in argv[-1]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "test" in argv and "-d" in argv:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {argv}")


def _commands_with(runner: _ToolboxRunner, token: str) -> list[tuple[str, ...]]:
    return [command for command in runner.commands if token in command]


def test_missing_release_matched_toolbox_is_created_provisioned_and_verified(tmp_path):
    runner = _ToolboxRunner(container_exists=False, packages_installed=False)

    name = ensure_desktop_toolbox(
        runner=runner,
        project_root=tmp_path,
        host_release="44",
        toolbox_executable="toolbox",
    )

    assert name == TOOLBOX_NAME
    assert (
        "toolbox",
        "create",
        "--distro",
        "fedora",
        "--release",
        "44",
        TOOLBOX_NAME,
    ) in runner.commands
    installs = _commands_with(runner, "dnf")
    assert len(installs) == 1
    assert installs[0][-len(REQUIRED_PACKAGES) :] == REQUIRED_PACKAGES
    assert runner.commands[-1][-3:] == ("test", "-d", str(tmp_path))


def test_existing_verified_toolbox_is_idempotent(tmp_path):
    runner = _ToolboxRunner(container_exists=True, packages_installed=True)

    ensure_desktop_toolbox(
        runner=runner,
        project_root=tmp_path,
        host_release="44",
        toolbox_executable="toolbox",
    )

    assert not _commands_with(runner, "create")
    assert not _commands_with(runner, "dnf")


def test_package_verification_accepts_virtual_capability_providers(tmp_path):
    runner = _ToolboxRunner(container_exists=True, packages_installed=True)

    ensure_desktop_toolbox(
        runner=runner,
        project_root=tmp_path,
        host_release="44",
        toolbox_executable="toolbox",
    )

    provider_probes = [
        command[-1]
        for command in runner.commands
        if command[-2:-1] == ("-lc",) and "rpm -q --whatprovides --quiet" in command[-1]
    ]
    assert len(provider_probes) == 1
    for capability in REQUIRED_PACKAGES:
        assert f"rpm -q --whatprovides --quiet {capability}" in provider_probes[0]
    assert not any(command[4:7] == ("rpm", "-q", "--quiet") for command in runner.commands)


def test_wrong_release_toolbox_fails_closed_without_installing_packages(tmp_path):
    runner = _ToolboxRunner(
        container_exists=True,
        container_release="43",
        packages_installed=False,
    )

    with pytest.raises(ToolboxProvisionError, match="release 43.*host release 44"):
        ensure_desktop_toolbox(
            runner=runner,
            project_root=tmp_path,
            host_release="44",
            toolbox_executable="toolbox",
        )

    assert not _commands_with(runner, "dnf")


def test_checkout_outside_shared_home_fails_closed(tmp_path):
    class _MissingCheckoutRunner(_ToolboxRunner):
        def run(self, command, **kwargs):
            result = super().run(command, **kwargs)
            if "test" in command and "-d" in command:
                return SimpleNamespace(returncode=1, stdout="", stderr="missing")
            return result

    runner = _MissingCheckoutRunner(container_exists=True, packages_installed=True)

    with pytest.raises(ToolboxProvisionError, match="checkout is not visible"):
        ensure_desktop_toolbox(
            runner=runner,
            project_root=Path(tmp_path),
            host_release="44",
            toolbox_executable="toolbox",
        )
