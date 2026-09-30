# Fedora Silverblue downstream updates

`hermes-downstream-update` is the supported source-update entry point for a
Fedora Silverblue installation that runs the long-lived `ken/downstream`
branch. It is installed with the normal editable project environment because it
is declared in `pyproject.toml`:

```text
uv sync --locked --extra all --group dev
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

The updater performs these 16 gates in this exact order:

1. Acquire a deterministic owner-only, per-checkout transaction lock. A live
   concurrent invocation fails before checkout validation or mutation; an
   unlocked stale metadata file is safely reused. The runtime root must be an
   absolute, real, owner-only directory owned by the current user; the
   dedicated lock directory and lock file reject symlinks.
2. Validate the exact checkout, `ken/downstream` branch, cleanliness, and
   canonical fetch and push remote transports. These read-only Git checks run
   under the lock before any host or Toolbx preflight.
3. Require the conservative 6 GiB initial free-space policy bound on the
   canonical checkout filesystem. This is the maximum stage bound, not the sum
   of later bounds, and runs before host provisioning or source mutation.
4. Require native `aarch64`, Fedora Silverblue metadata,
   `/run/ostree-booted`, and a release-matched persistent
   `hermes-arm-build` Toolbx. Compiler and Node build packages are installed
   inside Toolbx, never layered onto the immutable host.
5. Fetch `upstream/main`, `fork/main`, and `fork/ken/downstream`.
6. Reject local or fork `main` when it is ahead or diverged, fast-forward local
   `main`, prove exact equality with `upstream/main`, push it to `fork/main`
   without force, and read back the exact SHA.
7. Restore `ken/downstream` in a `BaseException`-safe path and merge `main`
   without rebasing, force-pushing, or automatically resolving conflicts. A
   failed or interrupted downstream merge triggers one bounded `git merge
   --abort` recovery attempt; the merge failure remains primary if recovery
   also fails.
8. Run `uv sync --locked --extra all --group dev` from `ken/downstream`, so
   runtime and test extras are retained before the focused suite.
9. Refresh the checkout-filesystem probe and require the conservative 4 GiB
   policy bound immediately before destructive `npm ci`.
10. Run the focused updater tests, Web and Desktop typechecks, and Web UI build.
11. Refresh the checkout-filesystem probe and require the conservative 6 GiB
   policy bound immediately before the shared Toolbx-aware ARM64 Desktop package
   path.
12. Recheck the expected live branch, require a clean checkout, and prove `HEAD`
   still equals the intended post-merge SHA. Only then require a current Desktop
   content stamp plus bounded, structurally plausible ARM64 ELF64 binaries for
   both the packaged application and packaged `node-pty`.
13. Push `ken/downstream` to `fork/ken/downstream` without force and read back
   the exact remote SHA.
14. Run `hermes config migrate`, then the non-interactive strict
   `hermes config validate` gate. Validation fails nonzero for an unsupported
    migration floor, a post-migration schema-version mismatch, parse or schema
    failures, missing required config settings, or missing required environment
    variables. Then require a fresh CLI runtime identity report whose checkout
    and revision exactly match the accepted downstream commit.
15. Prove the OneCLI loopback service accepts a TCP connection and
   `onecli auth status` reports authenticated. No `/health` or `/healthz` route
   is assumed.
16. Independently discover and fingerprint the canonical live gateway process,
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

## Disk-headroom policy

The 4 GiB `npm ci` and 6 GiB Desktop packaging thresholds are conservative
policy bounds, not fabricated measurements of historical peaks. The Node bound
allows simultaneous replacement of the dependency tree, npm cache activity,
and native add-on builds. The Desktop bound additionally allows renderer/native
build intermediates plus electron-builder staging and packaged output to coexist
with the live Desktop output. The early check uses the maximum 6 GiB bound, not
4 GiB + 6 GiB.

Every probe reads `shutil.disk_usage(canonical_checkout).free`, which is the byte
count available to the unprivileged updater. Exact equality passes. The updater
probes once after canonical checkout validation, again immediately before
`npm ci`, and again immediately before Desktop packaging because Python sync,
tests, typechecks, and the Web build can consume capacity in between. Probe
errors fail closed.

On a low-space error, inspect filesystem usage with `df` and directory sizes
with `du`. Reclaim only outputs or caches that an operator has verified are
regenerable. Preserve user data, backups, container volumes, credentials, and
durable state. The updater never performs automatic cleanup and provides no
force or skip escape hatch.

The probe covers the filesystem containing the canonical checkout. That includes
the checkout dependency tree, Web output, and Desktop staging/output (Desktop
staging is deliberately a sibling of `release` on the same filesystem). An npm
cache or Toolbx/container storage configured on another filesystem is not
covered by this probe and can still independently report `ENOSPC`.

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

- A failed or interrupted downstream merge is aborted automatically when Git can
  restore a clean `ken/downstream`; if abort/restoration also fails, inspect the
  checkout and preserve the primary error before retrying.
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
