# Fedora Silverblue downstream updates

`hermes-downstream-update` is the supported source-update entry point for a
Fedora Silverblue installation that runs the long-lived `ken/downstream`
branch. It is installed with the normal editable project environment because it
is declared in `pyproject.toml`:

```text
uv sync --locked --extra all --extra dev
.venv/bin/hermes-downstream-update --dry-run
```

The dry run prints the ordered transaction without reading Git state or making
changes. Run the real command only from the editable checkout that supplied the
entry point:

```text
.venv/bin/hermes-downstream-update
```

The command refuses a different checkout, detached HEAD, a branch other than
`ken/downstream`, tracked or untracked changes, and non-canonical remotes. The
only accepted transports are canonical `https://github.com/OWNER/REPO.git`,
`git@github.com:OWNER/REPO.git`, and
`ssh://git@github.com/OWNER/REPO.git` URLs for
`NousResearch/hermes-agent` (`upstream`) and
`bot-tipsysquid/hermes-agent` (`fork`). HTTP, userinfo, ports, aliases,
non-GitHub hosts, missing `.git` suffixes, and unexpected owner/repository
paths fail before fetch.

## Transaction

The updater performs these 14 gates in this exact order:

1. Acquire a deterministic owner-only, per-checkout transaction lock. A live
   concurrent invocation fails before checkout validation or mutation; an
   unlocked stale metadata file is safely reused. The runtime root must be an
   absolute, real, owner-only directory owned by the current user; the
   dedicated lock directory and lock file reject symlinks.
2. Validate the exact checkout, `ken/downstream` branch, cleanliness, and
   canonical fetch and push remote transports. These read-only Git checks run
   under the lock before any host or Toolbx preflight.
3. Require native `aarch64`, Fedora Silverblue metadata,
   `/run/ostree-booted`, and a release-matched persistent
   `hermes-arm-build` Toolbx. Compiler and Node build packages are installed
   inside Toolbx, never layered onto the immutable host.
4. Fetch `upstream/main`, `fork/main`, and `fork/ken/downstream`.
5. Reject local or fork `main` when it is ahead or diverged, fast-forward local
   `main`, prove exact equality with `upstream/main`, push it to `fork/main`
   without force, and read back the exact SHA.
6. Restore `ken/downstream` in a `BaseException`-safe path and merge `main`
   without rebasing, force-pushing, aborting, or automatically resolving
   downstream conflicts.
7. Run `uv sync --locked --extra all --extra dev` from `ken/downstream`, so
   runtime and test extras are retained before the focused suite.
8. Run locked Node installation, focused updater tests, Web and Desktop
   typechecks, and the Web UI build.
9. Run the shared Toolbx-aware ARM64 Desktop package path.
10. Recheck the expected live branch, require a clean checkout, and prove `HEAD`
    still equals the intended post-merge SHA. Only then require a current Desktop
    content stamp plus bounded, structurally plausible ARM64 ELF64 binaries for
    both the packaged application and packaged `node-pty`.
11. Push `ken/downstream` to `fork/ken/downstream` without force and read back
    the exact remote SHA.
12. Run `hermes config migrate`, then the non-interactive strict
    `hermes config validate` gate. Validation fails nonzero for an unsupported
    migration floor, a post-migration schema-version mismatch, parse or schema
    failures, missing required config settings, or missing required environment
    variables. Then require a fresh CLI runtime identity report whose checkout
    and revision exactly match the accepted downstream commit.
13. Prove the OneCLI loopback service accepts a TCP connection and
    `onecli auth status` reports authenticated. No `/health` or `/healthz` route
    is assumed.
14. Independently discover and fingerprint the canonical live gateway process,
    regardless of receipt quality, while separately capturing receipt PID/start
    and `updated_at` fields when present. Then record the restart threshold and
    restart the gateway. Require a newer parseable receipt created after that
    threshold, a different live Hermes gateway PID/start incarnation when one
    existed before restart, and a connected platform writer whose PID/start
    identity matches that new gateway incarnation.

Any failed gate stops the sequence. Publishing pristine `fork/main` necessarily
precedes the downstream merge and build gates, but `fork/ken/downstream` is not
pushed after a test, build, stamp, or architecture failure. No gateway restart
occurs before downstream remote SHA, configuration, runtime identity, and
OneCLI gates pass.

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
- `KeyboardInterrupt`, `SystemExit`, and other `BaseException` failures while
  `main` may be checked out trigger one bounded, unconditional restoration to
  `ken/downstream`, including failure of the initial branch probe. The original
  interruption is re-raised; if restoration also fails, that second failure is
  attached rather than hidden. An already-active downstream merge is left
  untouched. Builds and restarts never run from `main`.
- Pristine `fork/main` is an explicit transaction output. Local-ahead/diverged
  `main`, fork-ahead/diverged `main`, and post-push readback mismatches stop the
  update.
- Gateway readiness requires a `gateway_state.json` receipt with a parseable
  `updated_at` newer than the recorded restart threshold, canonical Hermes
  gateway kind/argv/home ownership, a matching live PID/start fingerprint, and
  at least one platform in `connected`, `running`, or `ok` state whose writer
  PID/start identity matches that new gateway incarnation. Installations
  intentionally configured with no platforms need a separately reviewed
  readiness policy before using the real command.
- The command is for Fedora Silverblue source installations on native ARM64. It
  is not a general cross-platform updater or a Flatpak/prebuilt distribution
  path.
