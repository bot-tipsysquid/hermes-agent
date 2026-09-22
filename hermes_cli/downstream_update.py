"""Supported downstream-aware update transaction for Fedora Silverblue.

The command intentionally owns Git synchronization, verification, publication,
and gateway restart as one fail-closed sequence. External actions cross an
injectable runner boundary so unit tests never fetch, merge, push, or restart.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

from hermes_cli.desktop_toolbox import (
    CommandRunner,
    SubprocessRunner,
    ensure_desktop_toolbox,
    toolbox_npm_command,
)


_LIVE_BRANCH = "ken/downstream"
_UPSTREAM_SLUG = "NousResearch/hermes-agent"
_FORK_SLUG = "bot-tipsysquid/hermes-agent"
class DownstreamUpdateError(RuntimeError):
    """Raised when any update gate cannot prove the transaction safe."""


@dataclass(frozen=True)
class UpdateConfig:
    """Explicit immutable inputs for one downstream update transaction."""

    repo: Path
    expected_repo: Path
    live_branch: str = _LIVE_BRANCH
    upstream_remote: str = "upstream"
    fork_remote: str = "fork"
    upstream_slug: str = _UPSTREAM_SLUG
    fork_slug: str = _FORK_SLUG
    python_executable: str = sys.executable
    uv_executable: str = "uv"
    toolbox_executable: str = "toolbox"
    onecli_executable: str = "onecli"


ProvisionToolbox = Callable[..., str]
ArtifactVerifier = Callable[[CommandRunner, Path], None]
GatewayVerifier = Callable[[CommandRunner, str], None]


def checkout_root() -> Path:
    """Return the checkout containing this installed updater."""
    return Path(__file__).resolve().parent.parent


def _run(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    repo: Path,
    description: str,
    capture_output: bool = True,
):
    try:
        result = runner.run(
            command,
            cwd=repo,
            capture_output=capture_output,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise DownstreamUpdateError(f"{description} could not start: {exc}") from exc
    if result.returncode == 0:
        return result
    detail = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
    suffix = f": {detail}" if detail else ""
    raise DownstreamUpdateError(f"{description} failed{suffix}")


def _git(
    runner: CommandRunner,
    repo: Path,
    *args: str,
    description: str,
):
    return _run(runner, ["git", *args], repo=repo, description=description)


def _stdout(result: Any) -> str:
    return str(getattr(result, "stdout", "") or "").strip()


def _branch(runner: CommandRunner, repo: Path) -> str:
    return _stdout(
        _git(
            runner,
            repo,
            "branch",
            "--show-current",
            description="read current branch",
        )
    )


def _require_branch(runner: CommandRunner, repo: Path, expected: str) -> None:
    actual = _branch(runner, repo)
    if actual != expected:
        shown = actual or "detached HEAD"
        raise DownstreamUpdateError(f"expected branch {expected}; found {shown}")


def _require_clean(runner: CommandRunner, repo: Path) -> None:
    status = _stdout(
        _git(
            runner,
            repo,
            "status",
            "--porcelain",
            "--untracked-files=all",
            description="inspect working tree",
        )
    )
    if status:
        raise DownstreamUpdateError("working tree is not clean")


def _remote_slug(url: str) -> str | None:
    value = url.strip()
    if value.startswith("git@github.com:"):
        path = value.removeprefix("git@github.com:")
    else:
        parsed = urlsplit(value)
        if (parsed.hostname or "").casefold() != "github.com":
            return None
        path = parsed.path.lstrip("/")
    path = path.removesuffix("/").removesuffix(".git")
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return f"{parts[0]}/{parts[1]}"


def _require_remote(
    runner: CommandRunner,
    repo: Path,
    remote: str,
    expected_slug: str,
    *,
    push: bool = False,
) -> None:
    args = ["remote", "get-url"]
    if push:
        args.extend(("--push", "--all"))
    args.append(remote)
    url = _stdout(
        _git(
            runner,
            repo,
            *args,
            description=f"read {remote} {'push ' if push else ''}remote",
        )
    )
    urls = [line for line in url.splitlines() if line.strip()]
    if len(urls) != 1 or (_remote_slug(urls[0]) or "").casefold() != expected_slug.casefold():
        kind = "push URL" if push else "URL"
        raise DownstreamUpdateError(
            f"remote {remote} has wrong {kind}; expected GitHub {expected_slug}"
        )


def _restore_live_branch(runner: CommandRunner, config: UpdateConfig) -> None:
    if _branch(runner, config.repo) == config.live_branch:
        return
    _git(
        runner,
        config.repo,
        "checkout",
        config.live_branch,
        description=f"restore {config.live_branch}",
    )


def _sync_source(runner: CommandRunner, config: UpdateConfig) -> None:
    repo = config.repo
    branch = config.live_branch
    _git(
        runner,
        repo,
        "fetch",
        config.upstream_remote,
        "+refs/heads/main:refs/remotes/upstream/main",
        description="fetch upstream/main",
    )
    _git(
        runner,
        repo,
        "fetch",
        config.fork_remote,
        f"+refs/heads/{branch}:refs/remotes/fork/{branch}",
        description=f"fetch fork/{branch}",
    )
    _git(
        runner,
        repo,
        "merge-base",
        "--is-ancestor",
        "main",
        "upstream/main",
        description="prove local main can fast-forward to upstream/main",
    )
    _git(
        runner,
        repo,
        "merge-base",
        "--is-ancestor",
        f"fork/{branch}",
        branch,
        description=f"prove local {branch} contains fork/{branch}",
    )

    try:
        _git(runner, repo, "checkout", "main", description="check out pristine main")
        _git(
            runner,
            repo,
            "merge",
            "--ff-only",
            "upstream/main",
            description="fast-forward local main",
        )
        local_main = _stdout(
            _git(runner, repo, "rev-parse", "main", description="read local main revision")
        )
        upstream_main = _stdout(
            _git(
                runner,
                repo,
                "rev-parse",
                "upstream/main",
                description="read upstream/main revision",
            )
        )
        if local_main != upstream_main:
            raise DownstreamUpdateError("local main does not exactly match upstream/main")
        _git(runner, repo, "checkout", branch, description=f"restore {branch}")
        _git(
            runner,
            repo,
            "merge",
            "--no-edit",
            "main",
            description=f"merge main into {branch}",
        )
    except Exception:
        _restore_live_branch(runner, config)
        raise

    _require_branch(runner, repo, branch)
    _require_clean(runner, repo)


def _default_artifact_verifier(_runner: CommandRunner, repo: Path) -> None:
    from hermes_cli.downstream_update_verify import verify_arm64_update_artifacts

    verify_arm64_update_artifacts(repo)


def _default_gateway_verifier(_runner: CommandRunner, expected_sha: str) -> None:
    from hermes_cli.downstream_update_verify import verify_gateway_ready

    verify_gateway_ready(expected_sha)


def _verify_onecli(runner: CommandRunner, config: UpdateConfig) -> None:
    status = _run(
        runner,
        [config.onecli_executable, "auth", "status"],
        repo=config.repo,
        description="OneCLI health check",
    )
    try:
        payload = json.loads(_stdout(status))
    except (TypeError, ValueError) as exc:
        raise DownstreamUpdateError("OneCLI health check returned invalid JSON") from exc
    if payload.get("authenticated") is not True:
        raise DownstreamUpdateError("OneCLI health check is not authenticated")


def _gate(description: str, operation: Callable[[], Any]) -> Any:
    try:
        return operation()
    except DownstreamUpdateError:
        raise
    except Exception as exc:
        raise DownstreamUpdateError(f"{description} failed: {exc}") from exc


def _remote_branch_sha(runner: CommandRunner, config: UpdateConfig) -> str:
    ref = f"refs/heads/{config.live_branch}"
    result = _git(
        runner,
        config.repo,
        "ls-remote",
        "--exit-code",
        "--heads",
        config.fork_remote,
        ref,
        description=f"read fork/{config.live_branch}",
    )
    for line in _stdout(result).splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == ref:
            return fields[0]
    raise DownstreamUpdateError(f"fork/{config.live_branch} did not return an exact branch ref")


def run_update(
    config: UpdateConfig,
    *,
    runner: CommandRunner | None = None,
    provision_toolbox: ProvisionToolbox = ensure_desktop_toolbox,
    verify_artifacts: ArtifactVerifier = _default_artifact_verifier,
    verify_gateway: GatewayVerifier = _default_gateway_verifier,
    output: Callable[[str], None] = print,
) -> str:
    """Run the complete downstream update, returning the published commit SHA."""
    command_runner: CommandRunner = runner if runner is not None else SubprocessRunner()
    repo = config.repo.resolve()
    expected_repo = config.expected_repo.resolve()
    if repo != expected_repo:
        raise DownstreamUpdateError(
            f"refusing unexpected checkout {repo}; expected {expected_repo}"
        )
    actual_root = Path(
        _stdout(
            _git(
                command_runner,
                repo,
                "rev-parse",
                "--show-toplevel",
                description="validate Git checkout",
            )
        )
    ).resolve()
    if actual_root != expected_repo:
        raise DownstreamUpdateError(
            f"Git top-level is {actual_root}; expected checkout {expected_repo}"
        )

    _require_branch(command_runner, repo, config.live_branch)
    _require_clean(command_runner, repo)
    _require_remote(
        command_runner,
        repo,
        config.upstream_remote,
        config.upstream_slug,
    )
    _require_remote(command_runner, repo, config.fork_remote, config.fork_slug)
    _require_remote(
        command_runner,
        repo,
        config.fork_remote,
        config.fork_slug,
        push=True,
    )

    output("→ Synchronizing pristine main and downstream source")
    _sync_source(command_runner, config)
    intended_sha = _stdout(
        _git(command_runner, repo, "rev-parse", "HEAD", description="pin downstream revision")
    )

    output("→ Synchronizing locked Python dependencies")
    _run(
        command_runner,
        [config.uv_executable, "sync", "--locked"],
        repo=repo,
        description="locked Python dependency synchronization",
        capture_output=False,
    )

    container = _gate(
        "Toolbx provisioning",
        lambda: provision_toolbox(
            runner=command_runner,
            project_root=repo,
            toolbox_executable=config.toolbox_executable,
            output=output,
        ),
    )
    npm = toolbox_npm_command(config.toolbox_executable, container)

    output("→ Synchronizing locked Node dependencies in Toolbx")
    _run(
        command_runner,
        [*npm, "ci", "--include=dev"],
        repo=repo,
        description="locked Node dependency synchronization",
        capture_output=False,
    )

    output("→ Running focused downstream updater tests")
    _run(
        command_runner,
        [
            str(repo / "scripts" / "run_tests.sh"),
            "tests/hermes_cli/test_desktop_toolbox.py",
            "tests/hermes_cli/test_desktop_silverblue_downstream.py",
            "tests/hermes_cli/test_downstream_update.py",
            "tests/hermes_cli/test_downstream_update_verify.py",
        ],
        repo=repo,
        description="focused downstream update tests",
        capture_output=False,
    )
    for workspace in ("web", "apps/desktop"):
        _run(
            command_runner,
            [*npm, "run", "typecheck", "--workspace", workspace],
            repo=repo,
            description=f"{workspace} typecheck",
            capture_output=False,
        )

    output("→ Building Web UI in Toolbx")
    _run(
        command_runner,
        [*npm, "run", "build", "--workspace", "web"],
        repo=repo,
        description="Web UI build",
        capture_output=False,
    )

    output("→ Building ARM64 Desktop through the shared Toolbx-aware path")
    _run(
        command_runner,
        [
            config.python_executable,
            "-m",
            "hermes_cli.main",
            "desktop",
            "--build-only",
            "--force-build",
        ],
        repo=repo,
        description="ARM64 Desktop build",
        capture_output=False,
    )
    _gate("ARM64 artifact verification", lambda: verify_artifacts(command_runner, repo))

    _require_branch(command_runner, repo, config.live_branch)
    _require_clean(command_runner, repo)
    local_sha = _stdout(
        _git(command_runner, repo, "rev-parse", "HEAD", description="read downstream revision")
    )
    if local_sha != intended_sha:
        raise DownstreamUpdateError(
            f"downstream HEAD moved during verification: {intended_sha} -> {local_sha}"
        )

    output(f"→ Pushing {config.live_branch} without force")
    _git(
        command_runner,
        repo,
        "push",
        config.fork_remote,
        f"{intended_sha}:refs/heads/{config.live_branch}",
        description=f"push {config.live_branch}",
    )
    remote_sha = _remote_branch_sha(command_runner, config)
    if remote_sha != intended_sha:
        raise DownstreamUpdateError(
            f"fork/{config.live_branch} {remote_sha} does not match local {intended_sha}"
        )

    _verify_onecli(command_runner, config)
    output("→ Restarting gateway after all source, test, build, artifact, and push gates")
    _run(
        command_runner,
        [
            config.python_executable,
            "-m",
            "hermes_cli.main",
            "gateway",
            "restart",
        ],
        repo=repo,
        description="gateway restart",
        capture_output=False,
    )
    _gate("gateway readiness verification", lambda: verify_gateway(command_runner, intended_sha))
    output(f"✓ Downstream update complete at {intended_sha}")
    return intended_sha


def _dry_run_plan() -> tuple[str, ...]:
    return (
        "validate clean expected ken/downstream checkout and remotes",
        "fetch upstream/main and fork/ken/downstream",
        "fast-forward pristine main, restore ken/downstream, merge main",
        "uv sync --locked",
        "provision and verify Fedora hermes-arm-build Toolbx",
        "npm ci, focused tests, Web/Desktop typechecks, Web UI build",
        "ARM64 Desktop build through shared Toolbx-aware path",
        "verify branch, Desktop build stamp, ARM64 app, and ARM64 node-pty",
        "push fork ken/downstream without force and verify SHA",
        "OneCLI health check, gateway restart, and platform-ready verification",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for ``hermes-downstream-update``."""
    parser = argparse.ArgumentParser(
        prog="hermes-downstream-update",
        description="Fail-closed Fedora Silverblue downstream update transaction",
    )
    parser.add_argument("--repo", type=Path, default=checkout_root())
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the transaction plan without reading or changing the checkout",
    )
    args = parser.parse_args(argv)
    if args.dry_run:
        print("DRY RUN — no commands will execute")
        for number, step in enumerate(_dry_run_plan(), start=1):
            print(f"{number:2}. {step}")
        return 0

    expected = checkout_root()
    try:
        run_update(UpdateConfig(repo=args.repo, expected_repo=expected))
    except DownstreamUpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
