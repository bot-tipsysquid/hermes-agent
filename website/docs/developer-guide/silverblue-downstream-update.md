# Fedora Silverblue downstream updates

`hermes-downstream-update` is the supported source-update entry point for a
Fedora Silverblue installation that runs the long-lived `ken/downstream`
branch. It is installed with the normal editable project environment because it
is declared in `pyproject.toml`:

```text
uv sync --locked
.venv/bin/hermes-downstream-update --dry-run
```

The dry run prints the ordered transaction without reading Git state or making
changes. Run the real command only from the editable checkout that supplied the
entry point:

```text
.venv/bin/hermes-downstream-update
```

The command refuses a different checkout, detached HEAD, a branch other than
`ken/downstream`, tracked or untracked changes, and Git remotes that do not map
to `NousResearch/hermes-agent` (`upstream`) and
`bot-tipsysquid/hermes-agent` (`fork`).

## Transaction

The updater performs these gates in order:

1. Fetch `upstream/main` and `fork/ken/downstream`.
2. Prove local `main` can fast-forward, fast-forward it, and require exact
   equality with `upstream/main`.
3. Restore `ken/downstream` and merge `main` without rebasing, force-pushing,
   aborting, or automatically resolving conflicts.
4. Run `uv sync --locked` from `ken/downstream`.
5. Provision and verify the persistent, Fedora-release-matched
   `hermes-arm-build` Toolbx. Compiler and Node build packages are installed
   inside Toolbx, never layered onto the immutable host.
6. Run locked Node installation, focused updater tests, Web/Desktop typechecks,
   the Web UI build, and the shared Toolbx-aware Desktop package path.
7. Require a current Desktop content stamp plus ARM64 aarch64 ELF binaries for
   both the packaged application and packaged `node-pty`.
8. Push `ken/downstream` to `fork/ken/downstream` without force and read back the
   exact remote SHA.
9. Require authenticated OneCLI health, restart the gateway, then require the
   runtime receipt to report the pushed SHA and at least one connected platform.

Any failed gate stops the sequence. In particular, no push occurs after a test,
build, stamp, or architecture failure, and no gateway restart occurs before the
remote SHA is verified.

## Toolbx lifecycle

The only supported native build container name is `hermes-arm-build`. Its Fedora
`VERSION_ID` must match the host. Provisioning is idempotent and verifies these
packages after any install:

```text
make gcc gcc-c++ python3 nodejs npm pkgconf-pkg-config
```

It also verifies `make`, `gcc`, `g++`, `python3`, `node`, `npm`, and
`pkg-config`, plus visibility of the host checkout through Toolbx's shared home.
An existing container from the wrong Fedora release is never deleted or
silently recreated; inspect and replace it manually before retrying.

## Failure recovery and limitations

- Merge conflicts are intentionally left in place on `ken/downstream`; resolve
  or abort them manually after inspecting the conflict.
- A failure while `main` is checked out attempts to restore `ken/downstream`
  before returning. Builds and restarts never run from `main`.
- This immediate implementation publishes only `fork/ken/downstream`; mirroring
  `main` to `fork/main` remains a separate follow-up policy decision.
- Gateway readiness requires a current `gateway_state.json` receipt with at
  least one platform in `connected`, `running`, or `ok` state. Installations
  intentionally configured with no platforms need a separately reviewed
  readiness policy before using the real command.
- The command is for Fedora Silverblue source installations on native ARM64. It
  is not a general cross-platform updater or a Flatpak/prebuilt distribution
  path.
