# Neon agent guide

Neon is a personal fork of the [Helium](https://github.com/imputnet/helium) browser. It adds
Arc-style spaces: one window, a sidebar you swipe between spaces, and each space signed in with
its own accounts. Spaces are built on containers (separate cookies and site storage per space
inside one profile), not on Chromium profiles.

The owner uses macOS as their main machine. Development and testing currently happen on a
headless Linux VM, so shared code must build on both, and macOS-only work waits for Mac access.

Read [neon/PLAN.md](neon/PLAN.md) before starting work. It holds the architecture decisions, the
milestones and their status.

## Rules

- Agents may write code here. The upstream Helium `AGENTS.md` forbids agent contributions to
  the Helium project. That policy still applies to upstream: never open issues, pull requests or
  comments on any `imputnet` repository, and never push to one. The `upstream` remotes have their
  push URL disabled on purpose. Nothing from this fork gets sent upstream.
- Keep the diff against upstream small, because every upstream line we touch can conflict on the
  next sync. Do not edit upstream patch files, scripts or docs unless there is no other way, and
  say so in the commit message when you do.
- Keep the Helium name in code, strings and paths. Renaming to Neon is deferred.
- Neon is GPL-3.0 like Helium. Code ported from another project keeps its original license
  header, and each port gets a line in `neon/THIRD_PARTY.md` (source repo, commit, license).
- Commit only when the task you were given says to. Never push without being asked.

## Repositories

Neon mirrors upstream's three-repo layout. All three live in `~/projects` on the dev VM.

| Repo | Upstream | Holds |
|---|---|---|
| `neon` (this one) | `imputnet/helium` | Shared Chromium patches, resources, tooling |
| `neon-linux` | `imputnet/helium-linux` | Linux build scripts and Linux-only patches |
| `neon-macos` | `imputnet/helium-macos` | macOS build scripts and macOS-only patches |

Each platform repo includes this repo as the submodule `helium-chromium`. Day-to-day work on
shared patches happens inside `neon-linux/helium-chromium`, which pushes to `~/projects/neon`.

Each repo's `main` is an upstream release tag plus Neon commits. We track release tags, not
upstream `main`, and the three repos always sit on the same release (for example `0.22.1` with
`0.22.1.1` on the platform repos).

## Where Neon code goes

- Shared patches: `patches/neon/<area>/<name>.patch`, listed at the end of `patches/series`.
  Every series line that starts with `neon/` belongs to us, and that block stays last. Do not
  add comment lines to `series`, the upstream lint treats every line as a patch path.
- Platform-only patches: `patches/neon/...` in the platform repo, listed at the end of its own
  `patches/series`.
- Inside the Chromium tree, put our own code in new files under `chrome/browser/neon/`,
  `chrome/browser/ui/neon/` and `chrome/browser/ui/views/neon/`. Code ported from another project
  keeps that project's paths and names (the containers port stays in `components/containers/` and
  `chrome/browser/containers/`). Hooks into existing Chromium or Helium files should be a few
  lines that call into our files.
- One patch per topic, with new files and their hooks in the same patch. Follow the existing
  patch format (quilt, `a/` and `b/` prefixes, no timestamps).
- Everything Neon-specific goes under `neon/` in this repo: plan, docs, scripts.

## Building and testing

Read `neon/docs/dev-linux.md` before building. The toolchain runs in a Docker container started by
`neon-linux/neon/dev-container.sh`, and builds use `neon_build` (8 jobs, niced), not `he build`.
Tests are scripts under `neon/tests/` that drive the built browser over the DevTools protocol.

The dev VM is shared with other agents and has about 16 GB of RAM:

- There is one Chromium source tree, at `~/projects/neon-linux/build/src`. Only one agent changes
  it or builds in it at a time. If you were not told you own the tree, treat it as read-only.
- Never run two builds at once. Cap build parallelism as the dev doc says and run builds with
  `nice`.
- Stop any browser, Xvfb or container you started when you finish.

## Syncing with upstream

Done per upstream release, core repo first, then the platform repos:

1. `git fetch upstream --tags`, then merge the new release tag into `main`.
2. If `patches/series` conflicts, take upstream's version and re-append our `neon/` lines.
3. Apply all patches to the new Chromium source and refresh any Neon patch that no longer
   applies cleanly. Build and run the Neon tests.
4. In each platform repo, merge its matching tag and point `helium-chromium` at the new Neon
   commit.
