"""Supported downstream-aware update transaction for Fedora Silverblue.

The command intentionally owns Git synchronization, verification, publication,
and gateway restart as one fail-closed sequence. External actions cross an
injectable runner boundary so unit tests never fetch, merge, push, or restart.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time
from typing import Any, Callable, ContextManager, Sequence
from urllib.parse import urlsplit

from hermes_cli.desktop_toolbox import (
    CommandRunner,
    SubprocessRunner,
    TOOLBOX_NAME,
    ensure_desktop_toolbox,
    fedora_release,
    toolbox_npm_command,
)


_LIVE_BRANCH = "ken/downstream"
_UPSTREAM_SLUG = "NousResearch/hermes-agent"
_FORK_SLUG = "bot-tipsysquid/hermes-agent"
_ONECLI_HOST = "127.0.0.1"
_ONECLI_PORT = 10254


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
GatewayVerifier = Callable[[CommandRunner, str, object, float], None]
GatewayReceiptCapturer = Callable[[CommandRunner], object]
RestartClock = Callable[[], float]
HostPreflight = Callable[
    [CommandRunner, UpdateConfig, ProvisionToolbox, Callable[[str], None]], str
]
TransactionLock = Callable[[UpdateConfig], ContextManager[object]]
ConfigVerifier = Callable[[CommandRunner, UpdateConfig], None]
RuntimeIdentityVerifier = Callable[[CommandRunner, UpdateConfig, str], None]
OneCLIServiceReady = Callable[[UpdateConfig], bool]


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
    """Return a slug only for the three reviewed canonical GitHub transports."""
    if not isinstance(url, str) or url != url.strip():
        return None
    if url.startswith("git@github.com:"):
        path = url.removeprefix("git@github.com:")
    else:
        try:
            parsed = urlsplit(url)
            _ = parsed.port  # malformed ports raise ValueError
        except ValueError:
            return None
        if parsed.scheme == "https":
            if (
                parsed.netloc != "github.com"
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port is not None
            ):
                return None
        elif parsed.scheme == "ssh":
            if (
                parsed.netloc != "git@github.com"
                or parsed.username != "git"
                or parsed.password is not None
                or parsed.hostname != "github.com"
                or parsed.port is not None
            ):
                return None
        else:
            return None
        if parsed.query or parsed.fragment:
            return None
        path = parsed.path.removeprefix("/")
    if not path.endswith(".git"):
        return None
    repository = path.removesuffix(".git")
    parts = repository.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    if any(part in {".", ".."} or any(character.isspace() for character in part) for part in parts):
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
    if len(urls) != 1 or _remote_slug(urls[0]) != expected_slug:
        kind = "push URL" if push else "URL"
        raise DownstreamUpdateError(
            f"remote {remote} has wrong {kind}; expected GitHub {expected_slug}"
        )


def _os_release_values(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DownstreamUpdateError(f"cannot read host OS metadata: {exc}") from exc
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value.strip().strip('"')
    return values


def preflight_silverblue_host(
    runner: CommandRunner,
    config: UpdateConfig,
    provision_toolbox: ProvisionToolbox,
    output: Callable[[str], None],
    *,
    machine: Callable[[], str] = platform.machine,
    os_release: Path = Path("/etc/os-release"),
    ostree_booted: Path = Path("/run/ostree-booted"),
) -> str:
    """Prove native Silverblue/OSTree and the persistent Toolbx policy."""
    architecture = machine().strip().lower()
    if architecture != "aarch64":
        raise DownstreamUpdateError(
            f"downstream update requires native aarch64; found {architecture or 'unknown'}"
        )
    values = _os_release_values(os_release)
    if values.get("ID") != "fedora" or values.get("VARIANT_ID") != "silverblue":
        raise DownstreamUpdateError("downstream update requires Fedora Silverblue")
    if not ostree_booted.exists():
        raise DownstreamUpdateError("downstream update requires an OSTree-booted host")
    try:
        release = fedora_release(os_release)
        container = provision_toolbox(
            runner=runner,
            project_root=config.repo.resolve(),
            host_release=release,
            toolbox_executable=config.toolbox_executable,
            output=output,
        )
    except Exception as exc:
        raise DownstreamUpdateError(f"persistent Toolbx preflight failed: {exc}") from exc
    if container != TOOLBOX_NAME:
        raise DownstreamUpdateError(
            f"persistent Toolbx preflight returned {container!r}; expected {TOOLBOX_NAME!r}"
        )
    return container


@contextmanager
def _default_transaction_lock(config: UpdateConfig):
    from hermes_cli.downstream_update_lock import (
        DownstreamUpdateLockError,
        UpdaterTransactionLock,
        transaction_lock_path,
    )

    try:
        with UpdaterTransactionLock(path=transaction_lock_path(config.expected_repo)):
            yield
    except DownstreamUpdateLockError as exc:
        raise DownstreamUpdateError(f"updater transaction lock failed: {exc}") from exc


def _restore_live_branch(runner: CommandRunner, config: UpdateConfig) -> None:
    """Restore the live branch with one bounded retry while preserving interruptions."""

    def checkout_live(description: str) -> None:
        _git(
            runner,
            config.repo,
            "checkout",
            config.live_branch,
            description=description,
        )

    def reraise_with_restore_failure(
        original: BaseException, restoration: BaseException
    ) -> None:
        original.add_note(
            "live-branch restoration also failed: "
            f"{type(restoration).__name__}: {restoration}"
        )
        raise original from restoration

    try:
        current_branch = _branch(runner, config.repo)
    except BaseException as original:
        # The failed probe cannot prove whether checkout-main already took effect.
        # Make exactly one unconditional restoration attempt before preserving it.
        try:
            checkout_live(f"restore {config.live_branch} after branch-probe failure")
        except BaseException as restoration:
            reraise_with_restore_failure(original, restoration)
        raise

    # A downstream merge conflict is explicit recovery state. Avoid even a
    # same-branch checkout so Git's merge state remains untouched.
    if current_branch == config.live_branch:
        return

    try:
        checkout_live(f"restore {config.live_branch}")
    except BaseException as original:
        # An interrupt can land before or after Git switches branches. Retrying
        # checkout unconditionally is bounded and safe in both cases.
        try:
            checkout_live(f"restore {config.live_branch} after interruption")
        except BaseException as restoration:
            reraise_with_restore_failure(original, restoration)
        raise


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
        "+refs/heads/main:refs/remotes/fork/main",
        f"+refs/heads/{branch}:refs/remotes/fork/{branch}",
        description=f"fetch fork/main and fork/{branch}",
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
        "fork/main",
        "upstream/main",
        description="prove fork main can fast-forward to upstream/main",
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
        _git(
            runner,
            repo,
            "push",
            config.fork_remote,
            f"{upstream_main}:refs/heads/main",
            description="publish pristine fork/main",
        )
        fork_main = _remote_branch_sha(runner, config, "main")
        if fork_main != upstream_main:
            raise DownstreamUpdateError(
                f"fork/main {fork_main} does not match upstream/main {upstream_main}"
            )
    finally:
        # BaseException-safe: interrupts after checkout must not strand the live checkout on main.
        _restore_live_branch(runner, config)

    _require_branch(runner, repo, branch)
    _git(
        runner,
        repo,
        "merge",
        "--no-edit",
        "main",
        description=f"merge main into {branch}",
    )
    _require_branch(runner, repo, branch)
    _require_clean(runner, repo)


def _default_artifact_verifier(_runner: CommandRunner, repo: Path) -> None:
    from hermes_cli.downstream_update_verify import verify_arm64_update_artifacts

    verify_arm64_update_artifacts(repo)


def _default_gateway_receipt_capturer(_runner: CommandRunner) -> object:
    from hermes_cli.downstream_update_verify import capture_gateway_receipt_identity

    return capture_gateway_receipt_identity()


def _default_gateway_verifier(
    _runner: CommandRunner,
    expected_sha: str,
    pre_restart: object,
    restart_started_at: float,
) -> None:
    from hermes_cli.downstream_update_verify import GatewayReceiptIdentity
    from hermes_cli.downstream_update_verify import verify_gateway_ready

    if not isinstance(pre_restart, GatewayReceiptIdentity):
        raise DownstreamUpdateError("pre-restart gateway receipt identity is invalid")
    verify_gateway_ready(
        expected_sha,
        pre_restart=pre_restart,
        restart_started_at=restart_started_at,
    )


def _onecli_local_service_ready(_config: UpdateConfig) -> bool:
    """Use a real loopback TCP connection; OneCLI exposes no health HTTP route."""
    try:
        with socket.create_connection((_ONECLI_HOST, _ONECLI_PORT), timeout=2.0):
            return True
    except OSError:
        return False


def _verify_onecli(
    runner: CommandRunner,
    config: UpdateConfig,
    service_ready: OneCLIServiceReady,
) -> None:
    if not service_ready(config):
        raise DownstreamUpdateError("OneCLI local service is not ready")
    status = _run(
        runner,
        [config.onecli_executable, "auth", "status"],
        repo=config.repo,
        description="OneCLI authentication check",
    )
    try:
        payload = json.loads(_stdout(status))
    except (TypeError, ValueError) as exc:
        raise DownstreamUpdateError("OneCLI authentication check returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise DownstreamUpdateError(
            "OneCLI authentication check must return a JSON object"
        )
    if payload.get("authenticated") is not True:
        raise DownstreamUpdateError("OneCLI authentication check is not authenticated")


def _verify_config(runner: CommandRunner, config: UpdateConfig) -> None:
    for command, description in (
        ("migrate", "configuration migration"),
        ("validate", "strict configuration validation"),
    ):
        _run(
            runner,
            [config.python_executable, "-m", "hermes_cli.main", "config", command],
            repo=config.repo,
            description=description,
            capture_output=False,
        )


def _verify_runtime_identity(
    runner: CommandRunner,
    config: UpdateConfig,
    expected_sha: str,
) -> None:
    result = _run(
        runner,
        [
            config.python_executable,
            "-m",
            "hermes_cli.downstream_update",
            "--runtime-identity-json",
        ],
        repo=config.repo,
        description="runtime revision identity",
    )
    try:
        payload = json.loads(_stdout(result))
    except (TypeError, ValueError) as exc:
        raise DownstreamUpdateError("runtime revision identity returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise DownstreamUpdateError("runtime revision identity must be a JSON object")

    checkout_value = payload.get("checkout_root")
    if not isinstance(checkout_value, str) or not checkout_value.strip():
        raise DownstreamUpdateError(
            "runtime checkout_root must be a non-empty absolute string"
        )
    checkout_path = Path(checkout_value)
    if not checkout_path.is_absolute():
        raise DownstreamUpdateError(
            "runtime checkout_root must be a non-empty absolute string"
        )
    try:
        actual_root = checkout_path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise DownstreamUpdateError("runtime checkout_root cannot be resolved") from exc
    if actual_root != config.expected_repo.resolve():
        raise DownstreamUpdateError(
            f"runtime checkout {actual_root} does not match {config.expected_repo.resolve()}"
        )
    if payload.get("source") != "git":
        raise DownstreamUpdateError("runtime identity source must be git")
    if payload.get("sha") != expected_sha:
        raise DownstreamUpdateError("runtime revision identity does not match expected revision")
    version = payload.get("version")
    if not isinstance(version, str) or not version.strip():
        raise DownstreamUpdateError("runtime version identity is missing")


def _gate(description: str, operation: Callable[[], Any]) -> Any:
    try:
        return operation()
    except DownstreamUpdateError:
        raise
    except Exception as exc:
        raise DownstreamUpdateError(f"{description} failed: {exc}") from exc


def _remote_branch_sha(
    runner: CommandRunner,
    config: UpdateConfig,
    branch: str,
) -> str:
    ref = f"refs/heads/{branch}"
    result = _git(
        runner,
        config.repo,
        "ls-remote",
        "--exit-code",
        "--heads",
        config.fork_remote,
        ref,
        description=f"read fork/{branch}",
    )
    for line in _stdout(result).splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == ref:
            return fields[0]
    raise DownstreamUpdateError(f"fork/{branch} did not return an exact branch ref")


def _validate_checkout(command_runner: CommandRunner, config: UpdateConfig) -> Path:
    """Read-only checkout and transport validation performed under the transaction lock."""
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
    return repo


def _run_update_transaction(
    config: UpdateConfig,
    *,
    command_runner: CommandRunner,
    container: str,
    verify_artifacts: ArtifactVerifier,
    verify_gateway: GatewayVerifier,
    capture_gateway_receipt: GatewayReceiptCapturer,
    restart_clock: RestartClock,
    verify_config: ConfigVerifier,
    verify_runtime_identity: RuntimeIdentityVerifier,
    onecli_service_ready: OneCLIServiceReady,
    output: Callable[[str], None],
) -> str:
    """Run one validated, locked, and host-preflighted downstream transaction."""
    repo = config.repo.resolve()

    output("→ Synchronizing pristine main and downstream source")
    _sync_source(command_runner, config)
    intended_sha = _stdout(
        _git(command_runner, repo, "rev-parse", "HEAD", description="pin downstream revision")
    )

    output("→ Synchronizing locked Python dependencies with runtime and dev extras")
    _run(
        command_runner,
        [config.uv_executable, "sync", "--locked", "--extra", "all", "--extra", "dev"],
        repo=repo,
        description="locked Python dependency synchronization",
        capture_output=False,
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
            "tests/hermes_cli/test_downstream_update_lock.py",
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

    _require_branch(command_runner, repo, config.live_branch)
    _require_clean(command_runner, repo)
    local_sha = _stdout(
        _git(command_runner, repo, "rev-parse", "HEAD", description="read downstream revision")
    )
    if local_sha != intended_sha:
        raise DownstreamUpdateError(
            f"downstream HEAD moved during verification: {intended_sha} -> {local_sha}"
        )
    _gate("ARM64 artifact verification", lambda: verify_artifacts(command_runner, repo))

    output(f"→ Pushing {config.live_branch} without force")
    _git(
        command_runner,
        repo,
        "push",
        config.fork_remote,
        f"{intended_sha}:refs/heads/{config.live_branch}",
        description=f"push {config.live_branch}",
    )
    remote_sha = _remote_branch_sha(command_runner, config, config.live_branch)
    if remote_sha != intended_sha:
        raise DownstreamUpdateError(
            f"fork/{config.live_branch} {remote_sha} does not match local {intended_sha}"
        )

    output("→ Migrating and validating configuration")
    _gate("configuration gate", lambda: verify_config(command_runner, config))
    output("→ Verifying final CLI runtime revision identity")
    _gate(
        "runtime-identity gate",
        lambda: verify_runtime_identity(command_runner, config, intended_sha),
    )
    _verify_onecli(command_runner, config, onecli_service_ready)
    pre_restart = _gate(
        "pre-restart gateway receipt capture",
        lambda: capture_gateway_receipt(command_runner),
    )
    restart_started_at = restart_clock()
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
    _gate(
        "gateway readiness verification",
        lambda: verify_gateway(
            command_runner,
            intended_sha,
            pre_restart,
            restart_started_at,
        ),
    )
    output(f"✓ Downstream update complete at {intended_sha}")
    return intended_sha


def run_update(
    config: UpdateConfig,
    *,
    runner: CommandRunner | None = None,
    provision_toolbox: ProvisionToolbox = ensure_desktop_toolbox,
    verify_artifacts: ArtifactVerifier = _default_artifact_verifier,
    verify_gateway: GatewayVerifier = _default_gateway_verifier,
    capture_gateway_receipt: GatewayReceiptCapturer = _default_gateway_receipt_capturer,
    restart_clock: RestartClock = time.time,
    host_preflight: HostPreflight = preflight_silverblue_host,
    transaction_lock: TransactionLock = _default_transaction_lock,
    verify_config: ConfigVerifier = _verify_config,
    verify_runtime_identity: RuntimeIdentityVerifier = _verify_runtime_identity,
    onecli_service_ready: OneCLIServiceReady = _onecli_local_service_ready,
    output: Callable[[str], None] = print,
) -> str:
    """Run the complete single-writer downstream update transaction."""
    command_runner: CommandRunner = runner if runner is not None else SubprocessRunner()
    with transaction_lock(config):
        _validate_checkout(command_runner, config)
        container = _gate(
            "Silverblue host and persistent Toolbx preflight",
            lambda: host_preflight(
                command_runner,
                config,
                provision_toolbox,
                output,
            ),
        )
        return _run_update_transaction(
            config,
            command_runner=command_runner,
            container=container,
            verify_artifacts=verify_artifacts,
            verify_gateway=verify_gateway,
            capture_gateway_receipt=capture_gateway_receipt,
            restart_clock=restart_clock,
            verify_config=verify_config,
            verify_runtime_identity=verify_runtime_identity,
            onecli_service_ready=onecli_service_ready,
            output=output,
        )


def _runtime_identity_payload() -> dict[str, object]:
    from hermes_cli.build_info import get_code_identity

    payload: dict[str, object] = dict(get_code_identity(refresh=True))
    payload["checkout_root"] = str(checkout_root().resolve())
    return payload


def _dry_run_plan() -> tuple[str, ...]:
    return (
        "acquire owner-only single-writer transaction lock",
        "validate clean expected ken/downstream checkout and canonical GitHub remotes",
        "prove native Fedora Silverblue/OSTree aarch64 and persistent Toolbx readiness",
        "fetch upstream/main plus fork/main and fork/ken/downstream",
        "fast-forward main, prove equality, publish and read back pristine fork/main",
        "restore ken/downstream in a finally-safe path and merge main",
        "uv sync --locked --extra all --extra dev",
        "npm ci, focused tests, Web/Desktop typechecks, Web UI build",
        "ARM64 Desktop build through shared Toolbx-aware path",
        "recheck clean branch and intended SHA, then verify Desktop build stamp, bounded ARM64 ELF app, and node-pty",
        "push fork ken/downstream without force and verify SHA",
        "migrate then strictly validate config and verify final CLI runtime revision identity",
        "verify OneCLI loopback readiness plus authentication",
        "capture pre-restart gateway identity, record restart threshold, gateway restart, then verify a fresh new live gateway incarnation and matching platform writer",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for ``hermes-downstream-update``."""
    parser = argparse.ArgumentParser(
        prog="hermes-downstream-update",
        description="Fail-closed Fedora Silverblue downstream update transaction",
    )
    parser.add_argument("--repo", type=Path, default=checkout_root())
    parser.add_argument(
        "--runtime-identity-json",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the transaction plan without reading or changing the checkout",
    )
    args = parser.parse_args(argv)
    if args.runtime_identity_json:
        print(json.dumps(_runtime_identity_payload(), sort_keys=True))
        return 0
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
