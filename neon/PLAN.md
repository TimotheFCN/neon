# Neon plan

Status legend: `todo`, `doing`, `done`, `blocked`. The orchestrator updates this file as
milestones land. Subagents report results back instead of editing the status table.

## Goal

One browser window whose sidebar holds several spaces (perso, work, dossardclub, ...). Each space
has its own tabs, pinned tabs, icon and colour, and its own logins. A two-finger swipe on the
sidebar slides between spaces, as in Arc. Settings, extensions, history and bookmarks are shared
across spaces on purpose.

## Decisions

1. **Spaces sit on containers, not profiles.** A container is a persistent storage partition
   inside the one profile: its own cookies, cache and site storage. A space is a container plus a
   page of the sidebar. Chromium binds a window to one profile, so real profiles in one window
   would mean patching that assumption everywhere. Containers avoid it.
2. **Containers come from Brave's implementation.** Brave ships containers (MPL-2.0, on by
   default since June 2026). Helium's maintainers said on issue #199 that they would rather import
   Brave's code than accept another implementation. We port the same code so that, if upstream
   lands it, we can drop our copy and keep only the spaces layer.
3. **Containers and spaces are separate patch groups.** `patches/neon/containers/` is the port.
   `patches/neon/spaces/` is ours and talks to containers through one small interface, so the
   port can be replaced by upstream's without touching spaces.
4. **Tabs carry their space.** Every tab belongs to exactly one space for its lifetime. New tabs
   and links opened from a tab inherit that tab's space. The window shows one space at a time, and
   tabs of hidden spaces stay loaded.
5. **Shared code is cross-platform Views code.** macOS-only pieces (trackpad gesture tracking,
   glass tint, branding, updater) live in `neon-macos` and wait for Mac access.
6. **We build on Helium's vertical tabs and do not restyle them.** Upstream is actively working
   on native materials and pinned tabs (PRs #2898, #2931, #2939, #2941). Our visual changes stay
   limited to what spaces need.
7. **Track upstream release tags.** See "Syncing with upstream" in `AGENTS.md`.
8. **The containers port keeps Brave's file layout and pref names** (`components/containers/`,
   `chrome/browser/containers/`, `brave.containers.*`), with Brave's license headers. If Helium
   imports the same code, our patches then collide on the same files, which is the signal to
   drop ours. Brave source: brave-core `v1.100.58`. See `neon/docs/containers-port.md`.
9. **One flat tab model per window, filtered by space.** A tab's space is derived from its
   container and stored nowhere else. The visible space of a window is the space of its active
   tab, so switching space is activating a tab. This copies how Chromium's tab group focus mode
   hides tabs. See `neon/docs/spaces-design.md`.
10. **The default storage partition is the first space.** Existing logins stay where they are
    and show up in that space.

## Milestones

| ID | Milestone | Status | Needs |
|---|---|---|---|
| M0 | Linux dev build of stock Helium 0.22.1 from `neon-linux`, running under Xvfb | doing | |
| R1 | Containers port study: map Brave's implementation onto Helium's patch layout | done | |
| R2 | Spaces design: data model, sidebar pager, persistence | done | |
| M1 | Containers core: a tab can live in a named persistent container | todo | M0, R1 |
| M2 | Spaces model: tabs grouped by space, one visible space, switch by command | todo | M1, R2 |
| M3 | Sidebar pager: space switcher row, slide animation, scroll gesture | todo | M2 |
| M4 | Space management: create, rename, icon, colour, delete, reorder, settings page | todo | M2 |
| M5 | Per-space pinned tabs, and site rules that route a URL to a space | todo | M3 |
| M6 | macOS: build from `neon-macos`, finger-tracked swipe, per-space glass tint | blocked | Mac access |
| M7 | Distribution: own bundle id and data dir, updater off, hosting, sync script | todo | M6 for the Mac parts |

### M0: Linux dev build

Bring up a development build of unmodified Helium 0.22.1 from `~/projects/neon-linux` on the dev
VM, and write `neon/docs/dev-linux.md`.

Done when:

- The browser binary builds and starts under Xvfb.
- The doc gives exact commands for: first setup, incremental build, running the browser with a
  throwaway profile, taking a screenshot of the browser window, driving pages over the DevTools
  protocol, editing a patch and refreshing it, and adding a new Neon patch to the series.
- The doc records measured numbers: full build time, incremental build time after touching one
  UI file, disk used, peak memory, and the parallelism that proved safe.
- The doc says how Neon code should be tested (which Chromium test targets are affordable to
  build here, if any, and what to use otherwise).

### R1: Containers port study

Write `neon/docs/containers-port.md`. Read Brave's containers code at the brave-core release that
matches Chromium 158. Cover: how a tab gets its storage partition and keeps it across navigation,
popups and session restore, every Chromium file Brave patches or overrides for it, what depends
on Brave-only infrastructure and how to replace it, which parts we need for M1 and which we skip
(sync, AI chat, Brave Shields hooks), and a proposed breakdown into Neon patches with a size
estimate. Compare with the community Helium branch `dvjn/helium` `feat/containers`, and note
licensing.

### R2: Spaces design

Write `neon/docs/spaces-design.md` against the Chromium 158 source with Helium's patches applied.
Cover: how spaces map onto the tab strip model and its tab collections, how the vertical tab
strip is built and where a per-space pager fits, the animation and gesture path on Views (and
what differs on macOS), where space state is stored and how session restore brings it back, how
upstream's draft persistent pinned tabs (#2931) interacts with per-space pins, and a proposed
breakdown into Neon patches with the hooks each needs.

### M1: Containers core

Port the patches R1 lists for M1, in its order, under `patches/neon/containers/`: `core`,
`content-partition`, `session-persistence`, `browser-service`, `tab-partition`, `command-line`,
`spaces-interface`. About 3,200 lines copied from Brave plus about 400 lines of hooks in about
25 Chromium files.

Done when, in the Linux build:

- A tab opened in a named container keeps that container across same-tab and cross-site
  navigation, links opened in a new tab, and popups.
- Two tabs on the same site in different containers hold different cookies, and a third in the
  default partition holds its own.
- Closing and restarting the browser restores each tab into its container with its cookies.
- A scripted check proves all three and is committed under `neon/tests/`.
- `neon/THIRD_PARTY.md` records the Brave import.

### M2 to M5

The patch list for each is in `neon/docs/spaces-design.md`. Each milestone ends with the feature
working in the Linux build, its patches in `patches/neon/`, and a test or scripted check that
proves it. Each is scoped in detail when the one before it lands.

## Product defaults taken from the design study

These follow the study's recommendations until the owner says otherwise:

- Closing the last tab of a space leaves a new tab page in that space. The window closes only
  when every space is empty.
- A new window shows the same space as the window it came from.
- The space list does not sync between devices.
- Moving a tab to another space reloads it there, so the menu entry reads "Reopen in space".
- The swipe works anywhere on the sidebar.
- Outside the vertical tabs layout, all tabs show and the space switcher is hidden.
- Extensions see the tabs of every space in a window.

## Known constraints

- One Chromium tree and one build at a time on the dev VM. Work that needs compiling is
  serialized. Research and design can run in parallel.
- A Neon build must not share Helium's data directory, bundle id or updater on the owner's Mac,
  or the two installs will overwrite each other. Handled in M7, before the first Mac install.
- Hosting is undecided. Until then the three repos exist only on the dev VM.
- Containers inherit Brave's limits: extensions cannot read or set a container's cookies, tabs
  opened by an extension land in the default partition, site permissions are shared across
  containers, and web push inside a container is unverified. Password manager and sign-in flows
  that rely on an extension opening tabs may misbehave outside the default space.
