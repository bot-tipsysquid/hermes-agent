"""Deterministic Fedora Toolbx lifecycle for native Desktop builds."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable, Protocol, Sequence


TOOLBOX_NAME = "hermes-arm-build"
REQUIRED_PACKAGES = (
    "make",
    "gcc",
    "gcc-c++",
    "python3",
    "nodejs",
    "npm",
    "pkgconf-pkg-config",
)
_REQUIRED_COMMANDS = ("make", "gcc", "g++", "python3", "node", "npm", "pkg-config")
_RELEASE_PROBE = ". /etc/os-release; printf '%s\\n%s\\n' \"$ID\" \"$VERSION_ID\""
_COMMAND_PROBE = " && ".join(f"command -v {command} >/dev/null" for command in _REQUIRED_COMMANDS)


class ToolboxProvisionError(RuntimeError):
    """Raised when the named Desktop build Toolbx cannot be proven usable."""


class CommandRunner(Protocol):
    """Small injectable subprocess boundary used by provisioning and updater tests."""

    def run(self, command: Sequence[str], **kwargs: Any) -> Any:
        """Run one command and return an object with returncode/stdout/stderr."""


class SubprocessRunner:
    """Production command runner."""

    def run(self, command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.run(list(command), check=False, **kwargs)


def fedora_release(os_release: Path = Path("/etc/os-release")) -> str:
    """Return a Fedora host's VERSION_ID, failing closed for other or malformed hosts."""
    try:
        lines = os_release.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ToolboxProvisionError(f"cannot read Fedora release metadata: {exc}") from exc
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value.strip().strip('"')
    if values.get("ID") != "fedora" or not values.get("VERSION_ID"):
        raise ToolboxProvisionError("Desktop Toolbx provisioning requires a Fedora VERSION_ID")
    return values["VERSION_ID"]


def _run(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    timeout: int,
    cwd: Path | None = None,
):
    try:
        return runner.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolboxProvisionError(f"command could not run: {' '.join(command)}: {exc}") from exc


def _require_success(result, description: str) -> None:
    if result.returncode == 0:
        return
    detail = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
    suffix = f": {detail}" if detail else ""
    raise ToolboxProvisionError(f"{description} failed{suffix}")


def _container_names(output: str) -> set[str]:
    names: set[str] = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].upper() != "CONTAINER":
            names.add(parts[1])
    return names


def _inside(toolbox: str, *command: str) -> list[str]:
    return [toolbox, "run", "--container", TOOLBOX_NAME, *command]


def ensure_desktop_toolbox(
    *,
    runner: CommandRunner | None = None,
    project_root: Path,
    host_release: str | None = None,
    toolbox_executable: str | None = None,
    output: Callable[[str], None] = print,
) -> str:
    """Create, provision, and verify the persistent release-matched Desktop Toolbx.

    Existing containers are never deleted or silently replaced. A named container
    from the wrong Fedora release is an explicit repair condition.
    """
    command_runner: CommandRunner = runner if runner is not None else SubprocessRunner()
    release = host_release or fedora_release()
    toolbox = toolbox_executable or shutil.which("toolbox")
    if not toolbox:
        raise ToolboxProvisionError("toolbox is required for Silverblue native Desktop builds")

    output(f"→ Verifying Fedora {release} Toolbx '{TOOLBOX_NAME}'")
    listed = _run(command_runner, [toolbox, "list", "--containers"], timeout=15)
    _require_success(listed, "Toolbx inventory")
    if TOOLBOX_NAME not in _container_names(listed.stdout or ""):
        output(f"  → Creating release-matched Toolbx '{TOOLBOX_NAME}'")
        created = _run(
            command_runner,
            [
                toolbox,
                "create",
                "--distro",
                "fedora",
                "--release",
                release,
                TOOLBOX_NAME,
            ],
            timeout=900,
        )
        _require_success(created, f"Toolbx creation for Fedora {release}")

    release_result = _run(
        command_runner,
        _inside(toolbox, "sh", "-lc", _RELEASE_PROBE),
        timeout=30,
    )
    _require_success(release_result, "Toolbx release verification")
    release_lines = [line.strip().strip('"') for line in (release_result.stdout or "").splitlines()]
    if release_lines[:2] != ["fedora", release]:
        actual = release_lines[1] if len(release_lines) >= 2 else "unknown"
        raise ToolboxProvisionError(
            f"Toolbx '{TOOLBOX_NAME}' uses Fedora release {actual}; host release {release} is required"
        )

    package_probe = _inside(toolbox, "rpm", "-q", "--quiet", *REQUIRED_PACKAGES)
    packages = _run(command_runner, package_probe, timeout=60)
    if packages.returncode != 0:
        output(f"  → Installing required build packages in '{TOOLBOX_NAME}'")
        installed = _run(
            command_runner,
            _inside(toolbox, "sudo", "dnf", "install", "-y", *REQUIRED_PACKAGES),
            timeout=1800,
        )
        _require_success(installed, "Toolbx package installation")
        packages = _run(command_runner, package_probe, timeout=60)
    _require_success(packages, "Toolbx package verification")

    tools = _run(
        command_runner,
        _inside(toolbox, "sh", "-lc", _COMMAND_PROBE),
        timeout=60,
    )
    _require_success(tools, "Toolbx build command verification")

    root = project_root.resolve()
    checkout = _run(
        command_runner,
        _inside(toolbox, "test", "-d", str(root)),
        timeout=30,
        cwd=root,
    )
    if checkout.returncode != 0:
        raise ToolboxProvisionError(
            f"host checkout is not visible inside Toolbx '{TOOLBOX_NAME}': {root}"
        )

    output(f"  ✓ Toolbx '{TOOLBOX_NAME}' is release-matched and ready")
    return TOOLBOX_NAME


def toolbox_npm_command(toolbox_executable: str, container: str = TOOLBOX_NAME) -> list[str]:
    """Return the npm command prefix used for install and packaging in Toolbx."""
    return [toolbox_executable, "run", "--container", container, "npm"]
