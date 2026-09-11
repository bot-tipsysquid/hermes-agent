"""Downstream regression coverage for immutable-Fedora Desktop builds."""

from pathlib import Path
from types import SimpleNamespace

from hermes_cli import main_desktop, main_web_build


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


def test_toolbox_probe_prefers_capable_hermes_container(monkeypatch):
    monkeypatch.setattr(main_desktop, "_fedora_silverblue_without_host_make", lambda: True)
    monkeypatch.setattr(main_desktop.shutil, "which", lambda name: "/usr/bin/toolbox")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["list", "--containers"]:
            return SimpleNamespace(
                returncode=0,
                stdout="CONTAINER ID  NAME                CREATED\nabc123        hermes-arm-build    today\n",
            )
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(main_desktop.subprocess, "run", fake_run)

    assert main_desktop._desktop_toolbox_build_container() == "hermes-arm-build"
    assert calls[1][:5] == [
        "/usr/bin/toolbox", "run", "--container", "hermes-arm-build", "sh"]


def test_desktop_dependency_install_uses_toolbox_prefix(monkeypatch, tmp_path):
    expected = SimpleNamespace(returncode=0)
    captured = {}
    monkeypatch.setattr(
        main_desktop, "_desktop_toolbox_build_container", lambda: "hermes-arm-build")
    monkeypatch.setattr(main_desktop.shutil, "which", lambda name: "/usr/bin/toolbox")
    monkeypatch.setattr(
        main_web_build,
        "_run_npm_install_deterministic",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("host npm used")),
    )

    def fake_prefixed(prefix, cwd, **kwargs):
        captured.update(prefix=prefix, cwd=cwd, kwargs=kwargs)
        return expected

    monkeypatch.setattr(
        main_web_build, "_run_npm_install_deterministic_with_prefix", fake_prefixed)

    result = main_desktop._run_desktop_dependency_install(
        "/usr/bin/npm", tmp_path, capture_output=False, env={"CI": "1"})

    assert result is expected
    assert captured["prefix"] == [
        "/usr/bin/toolbox", "run", "--container", "hermes-arm-build", "npm"]
    assert captured["cwd"] == tmp_path
    assert captured["kwargs"] == {"capture_output": False, "env": {"CI": "1"}}


def test_prefixed_npm_install_preserves_lockfile(monkeypatch, tmp_path):
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(main_web_build, "_run_npm_watching_for_engine_failure", fake_run)

    result = main_web_build._run_npm_install_deterministic_with_prefix(
        ["toolbox", "run", "--container", "hermes-arm-build", "npm"],
        Path(tmp_path),
    )

    assert result.returncode == 0
    assert calls == [[
        "toolbox", "run", "--container", "hermes-arm-build", "npm",
        "ci", "--include=dev",
    ]]
