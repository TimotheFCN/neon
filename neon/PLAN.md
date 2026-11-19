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
| M0 | Linux dev build of stock Helium 0.22.1 from `neon-linux`, running under Xvfb | done | |
| R1 | Containers port study: map Brave's implementation onto Helium's patch layout | done | |
| R2 | Spaces design: data model, sidebar pager, persistence | done | |
| R3 | Study: all spaces as containers, a global context for extensions, per-space extension access | doing | |
| M1 | Containers core: a tab can live in a named persistent container | done | M0, R1 |
| M1b | Containers hardening: fix the review findings on the port | done | M2 |
| M2 | Spaces model: tabs grouped by space, one visible space, switch by command | done | M1, R2 |
| M3 | Sidebar pager: space switcher row, slide animation, scroll gesture | done | M2 |
| M4 | Space management: create, rename, icon, colour, delete, reorder, settings page | doing | M2 |
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

M1 landed as seven patches: 3,490 lines of new files and 382 hook lines in 32 Chromium files.
`neon/tests/containers_m1.py` proves the criteria above. Left for later milestones: a new tab
page or blank tab opened "in a container" still lands in the default partition (M2 must solve
this for new tabs in a space), and deleting a container's storage, temporary containers and
reopening a closed tab compile but have never been run.

### M1b: Containers hardening

An independent review of the M1 patches (read from code, nothing run) found these. The first
three were handed to the M2 agent because they sit in the new-tab code it is changing.

| # | Finding | Owner |
|---|---|---|
| 1 | A container tab left on a WebUI page or `about:blank` restores into the default partition | M2 |
| 2 | `OpenTabInContainer` accepts unknown and uppercase ids, and fails for `about:blank` | M2 |
| 3 | Incognito profiles get a containers service although spaces must not exist there | M2 |
| 4 | Saved tab groups reopen their tabs in the default partition (also Brave issue #58219) | M1b |
| 5 | Brave skips platform SSO in container tabs and the port does not. On a managed Mac every container would be signed into the device's work account | M1b |
| 6 | New split tabs do not inherit the container | M1b |
| 7 | Helium's deferred pinned startup tabs ignore the container | M5, with per-space pins |
| 8 | The M1 check never tests lazy restore, reopen closed tab or window, Back and Forward after restart, discard and reload | M2 for the first three, M1b for the rest |

Findings 1 to 3 are fixed (commit `09af908b`). The suggested fix for blank tabs was unsafe: a
blank page committed in a SiteInstance with a fixed partition and no site crashes on its next
navigation. A restored blank tab now anchors its container on a site from its own history, and a
brand-new `about:blank` tab opens in the default space. Any restore fix for findings 4 to 6 must
respect the same limit.

Findings 4 to 6 and the rest of 8 landed in M1b, with `neon/tests/hardening_m1b.py` (205
checks). It added `containers/platform-auth.patch` and `spaces/groups.patch`. What it changed:

- The spaces command ids collided with Chromium's focus commands (39000 and 39001), so Ctrl+L,
  Alt+D and F6 ran "previous space" and a typed URL loaded in another space. The ids now start at
  39500. Earlier checks missed it because they navigated through DevTools, not the omnibox. New
  checks must type into the real omnibox.
- Saved tab groups reopen in their container. The group's container is kept in the pref
  `neon.spaces.saved_group_containers`. Restoring a closed group still loses each tab's history.
- New split tabs inherit the container, and the split picker lists only tabs of the split's
  space.
- View source keeps the tab's container. Extension pages stay in the default partition.
- Removing a split with spaces on crashed (SIGSEGV). Fixed.
- The platform SSO skip is ported from Brave. It only compiles on macOS and Windows and has
  never run. Check it on a managed Mac in M6.

Behaviour M1b chose, and the owner's answer (2026-11-19):

1. Extension pages (options, popups opened as tabs) always open in the default space. Under
   review, see "Open design question" below.
2. A tab group belongs to the space of the first tab that joined. A tab from another space that
   is dragged in is taken out of the group. Confirmed.
3. A split that ends up spanning two spaces is unsplit. Confirmed.
4. Extension popup windows open on the space of the last active window. Under review with 1.
5. In a space with a single tab, the split picker shows a plain new tab page. Confirmed.

#### Open design question: extensions and spaces

Extensions are shared by all spaces, and an extension signed in to one account acts on pages of
every space. The owner wants a model that makes this obvious and controllable. Their proposal:
no visible space uses the default partition. Every space the user sees is a container, and the
default partition becomes a "global" context that holds the browser's own pages and the
extensions. Pages that belong to no space (extension pages, extension sign-in) would open in a
floating window over the browser, as Arc does, and extensions could be turned off per space.
This would replace decision 10. Study R3 (`neon/docs/global-space-design.md`) checks what it
costs before anything is decided.

### M2: Spaces model (done)

Five patches under `patches/neon/spaces/` (service, window-controller, model-filter,
strip-filter, commands), about 1,600 new lines and about 100 hook lines.
`neon/tests/spaces_m2.py` proves the ten behaviours with real key events. New tabs get their
space through a provider the spaces layer registers with the containers seam, which needed no
new Chromium hook.

Left open by M2, to be picked up where noted:

- Pinned tabs are hidden per space by the same layout hook but no test pins a tab (M5).
- Shift-click range selection and Ctrl+Shift+PgDn are not filtered by space (M3).
- A change to the space list at runtime does not relay out the sidebar until the active tab
  changes (M4).
- Keeping a space's tabs together was skipped in a section that contains a group or split, and
  groups that mixed containers were not ungrouped. Fixed in M1b (`spaces/groups.patch`).
- macOS shortcuts are not wired, and Ctrl+Alt+Up/Down may clash with GNOME (M6).
- Tabs in a container that no space uses count as the default space.
- Not tested: dragging tabs between windows, tab search, tabs opened by extensions, bookmarks.

### M3: Sidebar pager (done)

Several live tab strips on one tab model work on this tree, so the pager uses real pages and no
snapshots. Four patches (pager, switcher-row, swipe, range-filter): about 2,900 new lines, with
Chromium hooks in `base_tab_strip_region_view.cc`, `root_tab_collection_node`, `tab_view.cc` and
`tab_strip_model.cc`. `neon/tests/spaces_m3.py` drives clicks and a synthetic touchpad through a
real X server and checks frames in the middle of each transition.

Left open by M3:

- Typing a URL into the new tab page of a non-default space loaded it outside that space. Found
  by the M3 agent and fixed in M1b.
- Each window builds three full copies of every tab view, and a switch costs about 40 ms of
  layout with 90 tabs. Measure on real hardware before optimising.
- The swipe was only run on X11 with a synthetic touchpad. Wayland and macOS paths compile but
  have never run. The macOS patch feeds `SpacesSwipeTracker` with native phases (M6).
- Screen readers and the accessibility tree were not tested with peek pages.
- Labels in the switcher row and its menu are English only.

### M4 and M5

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
- Chromium's large test binaries (`unit_tests`, `browser_tests`, `interactive_ui_tests`) do not
  compile on a Helium tree. Neon is tested with scripts that drive the real browser over the
  DevTools protocol, kept under `neon/tests/`. See `neon/docs/dev-linux.md`.
- Hosting is undecided. Until then the three repos exist only on the dev VM.
- Containers inherit Brave's limits: extensions cannot read or set a container's cookies, tabs
  opened by an extension land in the default partition, site permissions are shared across
  containers, and web push inside a container is unverified. Password manager and sign-in flows
  that rely on an extension opening tabs may misbehave outside the default space.
