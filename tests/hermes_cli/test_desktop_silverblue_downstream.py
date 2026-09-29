"""Downstream regression coverage for immutable-Fedora Desktop builds."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import desktop_toolbox, main_desktop, source_build


def test_silverblue_without_host_make_is_detected(monkeypatch):
    monkeypatch.setattr(main_desktop.sys, "platform", "linux")
    monkeypatch.setattr(
        main_desktop.shutil, "which", lambda name: None if name == "make" else f"/usr/bin/{name}")
    monkeypatch.setattr(
        main_desktop.Path,
        "read_text",
        lambda self, **kwargs: "ID=fedora\nVARIANT_ID=silverblue\nOSTREE_VERSION=44.1\n",
    )

    assert main_desktop._fedora_silverblue_without_host_make() is True


def test_silverblue_with_partial_host_toolchain_uses_toolbox(monkeypatch):
    monkeypatch.setattr(main_desktop.sys, "platform", "linux")
    monkeypatch.setattr(
        main_desktop.Path,
        "read_text",
        lambda self, **kwargs: "ID=fedora\nVARIANT_ID=silverblue\n",
    )
    monkeypatch.setattr(
        main_desktop.shutil,
        "which",
        lambda name: None if name == "g++" else f"/usr/bin/{name}",
    )

    assert main_desktop._fedora_silverblue_without_host_make() is True


def test_auto_ozone_x11_is_scoped_to_silverblue_gnome_wayland_arm(monkeypatch):
    monkeypatch.setattr(main_desktop.sys, "platform", "linux")
    monkeypatch.setattr(
        main_desktop.Path,
        "read_text",
        lambda self, **kwargs: "ID=fedora\nVARIANT_ID=silverblue\n",
    )
    monkeypatch.setattr(
        main_desktop.os,
        "uname",
        lambda: SimpleNamespace(machine="aarch64"),
    )
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    monkeypatch.setenv("DISPLAY", ":0")

    assert main_desktop._desktop_auto_ozone_platform_hint() == "x11"

    monkeypatch.setattr(
        main_desktop.os,
        "uname",
        lambda: SimpleNamespace(machine="x86_64"),
    )
    assert main_desktop._desktop_auto_ozone_platform_hint() is None


def test_toolbox_probe_provisions_named_hermes_container(monkeypatch, tmp_path):
    monkeypatch.setattr(main_desktop, "_fedora_silverblue_without_host_make", lambda: True)
    monkeypatch.setattr(main_desktop.shutil, "which", lambda name: "/usr/bin/toolbox")
    captured = {}

    def fake_ensure(**kwargs):
        captured.update(kwargs)
        return "hermes-arm-build"

    monkeypatch.setattr(desktop_toolbox, "ensure_desktop_toolbox", fake_ensure)

    assert main_desktop._desktop_toolbox_build_container(
        project_root=tmp_path
    ) == "hermes-arm-build"
    assert captured["toolbox_executable"] == "/usr/bin/toolbox"
    assert captured["project_root"] == tmp_path


def test_source_dependency_preparation_uses_toolbox_node_command(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("pm.progress.run_contained", lambda command, *args, **kwargs: calls.append(command))

    source_build.prepare_source_dependencies(
        tmp_path,
        ("apps/desktop",),
        env={"PATH": "/usr/bin"},
        explicit=True,
        node_command=[
            "/usr/bin/toolbox", "run", "--container", "hermes-arm-build", "node"
        ],
    )

    assert calls[0][:5] == [
        "/usr/bin/toolbox", "run", "--container", "hermes-arm-build", "node"
    ]
    assert calls[0][5] == str(tmp_path / "scripts/build/node-deps.mjs")


def test_forced_preflighted_toolbox_overrides_complete_host_toolchain(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(main_desktop, "_fedora_silverblue_without_host_make", lambda: False)
    monkeypatch.setattr(
        main_desktop.shutil,
        "which",
        lambda name: f"/usr/bin/{name}",
    )

    def verify_exact_toolbox(**kwargs):
        captured.update(kwargs)
        return desktop_toolbox.TOOLBOX_NAME

    monkeypatch.setattr(desktop_toolbox, "ensure_desktop_toolbox", verify_exact_toolbox)

    command = main_desktop._desktop_npm_command(
        "/usr/bin/npm",
        tmp_path,
        toolbox_container=desktop_toolbox.TOOLBOX_NAME,
    )

    assert command == [
        "/usr/bin/toolbox",
        "run",
        "--container",
        desktop_toolbox.TOOLBOX_NAME,
        "npm",
    ]
    assert captured["project_root"] == tmp_path
    assert captured["allow_provision"] is False


def test_forced_toolbox_name_must_match_supported_preflight_contract(monkeypatch, tmp_path):
    monkeypatch.setattr(main_desktop.shutil, "which", lambda name: f"/usr/bin/{name}")

    with pytest.raises(RuntimeError, match="does not match.*hermes-arm-build"):
        main_desktop._desktop_npm_command(
            "/usr/bin/npm",
            tmp_path,
            toolbox_container="different-container",
        )


def test_forced_toolbox_fails_closed_when_container_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(main_desktop.shutil, "which", lambda name: f"/usr/bin/{name}")

    def unavailable(**_kwargs):
        raise desktop_toolbox.ToolboxProvisionError("container unavailable")

    monkeypatch.setattr(desktop_toolbox, "ensure_desktop_toolbox", unavailable)

    with pytest.raises(RuntimeError, match="container unavailable"):
        main_desktop._desktop_npm_command(
            "/usr/bin/npm",
            tmp_path,
            toolbox_container=desktop_toolbox.TOOLBOX_NAME,
        )


def test_packaging_reuses_the_toolbox_npm_command_used_for_dependency_install(
    monkeypatch, tmp_path
):
    desktop_dir = tmp_path / "apps" / "desktop"
    desktop_dir.mkdir(parents=True)
    staging_dir = tmp_path / "staging"
    toolbox_npm = ["toolbox", "run", "--container", "hermes-arm-build", "npm"]
    calls = []
    monkeypatch.setattr(main_desktop, "_desktop_staging_dir", lambda _desktop: staging_dir)
    monkeypatch.setattr(main_desktop, "_stop_desktop_processes_locking_build", lambda _desktop: [])
    monkeypatch.setattr(
        main_desktop,
        "_promote_staged_desktop_app",
        lambda _desktop, _staging: staging_dir / "linux-arm64-unpacked" / "hermes",
    )
    monkeypatch.setattr(main_desktop, "_discard_desktop_staging", lambda _staging: None)
    monkeypatch.setattr("pm.progress.run_contained", lambda command, *args, **kwargs: calls.append(command))

    main_desktop.build_prepared_desktop(
        desktop_dir,
        source_mode=False,
        npm="/usr/bin/npm",
        env={"CI": "1"},
        npm_command=toolbox_npm,
    )

    assert len(calls) == 2
    assert all(command[: len(toolbox_npm)] == toolbox_npm for command in calls)
    assert calls[0][len(toolbox_npm) :3 + len(toolbox_npm)] == ["run", "build", "--"]
    assert calls[1][len(toolbox_npm) :] == [
        "run", "builder", "--", "--dir", "--publish", "never",
        f"-c.directories.output={staging_dir}",
    ]
