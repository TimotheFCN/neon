# Neon plan

Status legend: `todo`, `doing`, `done`, `blocked`. The orchestrator updates this file as
milestones land. Subagents report results back instead of editing the status table.

## Goal

One browser window whose sidebar holds several spaces (personal, work, a client, ...). Each space
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
10. **Every visible space is a container. The default storage partition is the global context.**
    It holds the browser's own pages and the extensions, and no space the user sees. All spaces
    then behave the same, any of them can be deleted except the last, and it is plain that
    extensions belong to the browser and not to a space. Decided by the owner on 2026-11-19,
    replacing "the default partition is the first space". Cost accepted: every container limit
    applies to all browsing until fixed (see `neon/docs/global-space-design.md`, section 2).
11. **Extensions are global, with a per-space off switch.** An extension runs in every space
    unless turned off for a space. Its pages and sign-in flows belong to the global context and
    open outside any space: first in a small separate window, later in a floating overlay.
    Tabs an extension opens for the user land in the current space.

## Milestones

| ID | Milestone | Status | Needs |
|---|---|---|---|
| M0 | Linux dev build of stock Helium 0.22.1 from `neon-linux`, running under Xvfb | done | |
| R1 | Containers port study: map Brave's implementation onto Helium's patch layout | done | |
| R2 | Spaces design: data model, sidebar pager, persistence | done | |
| R3 | Study: all spaces as containers, a global context for extensions, per-space extension access | done | |
| R4 | Study: upstream's pinned tabs pull requests and per-space pins | done | |
| M1 | Containers core: a tab can live in a named persistent container | done | M0, R1 |
| M1b | Containers hardening: fix the review findings on the port | done | M2 |
| M2 | Spaces model: tabs grouped by space, one visible space, switch by command | done | M1, R2 |
| M3 | Sidebar pager: space switcher row, slide animation, scroll gesture | done | M2 |
| M4 | Space management: create, rename, icon, colour, delete, reorder, settings page | done | M2 |
| M5 | All spaces as containers: global context, no default space, extension pages in a separate window, tabs opened by extensions land in the current space | doing | M4, R3 |
| M6 | Clear browsing data, site data settings and DevTools per space | todo | M5 |
| M7 | Per-space pinned tabs | blocked | Upstream merging its persistent pinned tabs (#2931) |
| M8 | Extension switch per space | todo | M5 |
| M9 | Floating overlay for global pages (extension pages, extension sign-in) | todo | M5 |
| M10 | Web push and notification clicks in containers | todo | M5 |
| M11 | Site rules that route a URL to a space | todo | M5 |
| Mac | macOS: build from `neon-macos`, finger-tracked swipe, per-space glass tint | blocked | Mac access |
| Dist | Distribution: own bundle id and data dir, updater off, sync script | todo | Mac for the Mac parts |

The order of M5 to M11 is the owner's (2026-11-19). M7 waits for upstream, so M8 follows M6. The macOS and distribution work was called
M6 and M7 in older notes and commit messages.

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
| 7 | Helium's deferred pinned startup tabs ignore the container | M7, with per-space pins |
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
  never run. Check it on a managed Mac in the macOS milestone.

Behaviour M1b chose, and the owner's answer (2026-11-19):

1. Extension pages (options, popups opened as tabs) always open in the default space. Replaced
   by decision 11: they open outside any space.
2. A tab group belongs to the space of the first tab that joined. A tab from another space that
   is dragged in is taken out of the group. Confirmed.
3. A split that ends up spanning two spaces is unsplit. Confirmed.
4. Extension popup windows open on the space of the last active window. Replaced by decision
   11: they are global.
5. In a space with a single tab, the split picker shows a plain new tab page. Confirmed.

### M2: Spaces model (done)

Five patches under `patches/neon/spaces/` (service, window-controller, model-filter,
strip-filter, commands), about 1,600 new lines and about 100 hook lines.
`neon/tests/spaces_m2.py` proves the ten behaviours with real key events. New tabs get their
space through a provider the spaces layer registers with the containers seam, which needed no
new Chromium hook.

Left open by M2, to be picked up where noted:

- Pinned tabs are hidden per space by the same layout hook but no test pins a tab (M7).
- Shift-click range selection and Ctrl+Shift+PgDn are not filtered by space (M3).
- A change to the space list at runtime does not relay out the sidebar until the active tab
  changes (M4).
- Keeping a space's tabs together was skipped in a section that contains a group or split, and
  groups that mixed containers were not ungrouped. Fixed in M1b (`spaces/groups.patch`).
- macOS shortcuts are not wired, and Ctrl+Alt+Up/Down may clash with GNOME (macOS milestone).
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
  have never run. The macOS patch feeds `SpacesSwipeTracker` with native phases (macOS milestone).
- Screen readers and the accessibility tree were not tested with peek pages.
- Labels in the switcher row and its menu are English only.

### M4: Space management (done)

Three patches (management-ui, settings-page, move-to-space): about 2,100 new lines, 24 hook
lines in 7 settings files and 15 in 5 tab menu files. `neon/tests/spaces_m4.py` (76 checks)
drives the row, the bubbles, the settings page and the tab menu with real input, and proves
that a deleted space's cookies and site storage are gone at once and its directory after a
restart. It also fixed a use-after-free in `SpacesService::UpdateSpace` from M2.

Behaviour M4 chose, to be confirmed by the owner:

1. "+" creates the space at once ("Space N", first unused colour), shows it with a new tab
   page and opens the edit bubble. Edits apply as they are made.
2. Tabs of a deleted space close without unload prompts and leave no "recently closed" entries.
3. "Reopen in space" makes the first reopened tab active, so the window follows the tabs. The
   tabs go to the end of the target space and leave their group or split.
4. With six or more spaces the row falls back to a single icon with a menu.
5. With one space the row is hidden, so the first extra space is created from Settings or from
   "Reopen in space". M5 changes what "one space" means and should revisit this.
6. Nine colours plus none. Icons are the first letter, an emoji or one of ten vector icons.

Left open by M4:

- Strings are English literals, not in a `.grdp`.
- "Reopen in space" on a split, and on a tab that was restored but never loaded, are untested.
- No test swipes while a delete is in progress.
- No shortcut for "New space", and tabs cannot be dragged onto a space icon.
- Keyboard and screen reader use of the edit bubble and the row menu are untested.

### M5 to M11

M5 follows `neon/docs/global-space-design.md` (patch X5 with its spaces-layer changes, and
patch X3 for tabs opened by extensions). M6 covers that document's "Clear browsing data", "Site
data settings" and "DevTools" rows. M8 is its patches X1 and X2, M9 is X4. M7 waits for study
R4 (`neon/docs/pinned-tabs-design.md`).

The owner decided on 2026-11-19 not to import upstream's pinned tabs pull request before it
merges, and not to build our own pins in the meantime: #2931 was force-pushed seven times in two
days and its first review asked for restructuring. M7 starts at the sync that brings the merged
code (the release whose `series` lists `helium/core/pins/pin-sync.patch`). The per-space patch
the study describes (about 250 new lines, section D) is then part of that sync, because the
upstream feature breaks spaces without it. Until then pinned tabs are stock Chromium ones:
hidden per space by the layout filter, restored into their container by session restore, and
untested. Finding 7 of M1b stays open (with session restore off, Helium's deferred pinned
startup tabs lose their container).

Each milestone ends with the feature working in the
Linux build, its patches in `patches/neon/`, and a scripted check that proves it.

## Product defaults taken from the design study

These follow the study's recommendations until the owner says otherwise:

- Closing the last tab of a space leaves a new tab page in that space. The window closes only
  when every space is empty.
- A new window shows the same space as the window it came from.
- The space list does not sync between devices.
- Moving a tab to another space reloads it there, so the menu entry reads "Reopen in space".
- The swipe works anywhere on the sidebar.
- Outside the vertical tabs layout, all tabs show and the space switcher is hidden.
- Extensions see the tabs of every space in a window, until M8 adds the per-space switch.

## Known constraints

- One Chromium tree and one build at a time on the dev VM. Work that needs compiling is
  serialized. Research and design can run in parallel.
- A Neon build must not share Helium's data directory, bundle id or updater on the owner's Mac,
  or the two installs will overwrite each other. Handled in the distribution work, before the
  first Mac install.
- Chromium's large test binaries (`unit_tests`, `browser_tests`, `interactive_ui_tests`) do not
  compile on a Helium tree. Neon is tested with scripts that drive the real browser over the
  DevTools protocol, kept under `neon/tests/`. See `neon/docs/dev-linux.md`.
- The three repos are public on GitHub as standalone repos, not GitHub forks, so that no pull
  request can target Helium by accident. GitHub Actions is turned off on them because Helium's
  release workflows come along with the code.
- Containers inherit Brave's limits: extensions cannot read or set a container's cookies, tabs
  opened by an extension land in the default partition, site permissions are shared across
  containers, and web push never reaches a container. Under decision 10 these apply to all
  browsing. The full list is in `neon/docs/global-space-design.md`, section 2.
- No migration from an existing Helium profile. A profile copied into Neon keeps its settings
  and extensions, and its logins stay in the global context where no space sees them.
