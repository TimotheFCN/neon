# Pinned tabs per space (R4)

Status: design only, no code. Written 2026-11-19 against Neon `main` (Helium 0.22.1 on Chromium
158.0.8127.36, Neon patches through M4) and the upstream branches as fetched that day.

Every claim carries a tag:

- **[code]** read in the upstream branches in `~/projects/neon`, in the Neon patches, or in the
  patched tree at `~/projects/neon-linux/build/src` (read only, nothing run there).
- **[thread]** read in the pull request pages and review comments (`gh pr view`, `gh api`).
- **[test]** checked by applying patches with GNU `patch` to scratch copies of the affected
  files, taken from the patched tree. Nothing was built.
- **[assumed]** reasoned from code, not confirmed. Only running the browser settles these.

Paths like `pin-sync.patch:618` are line numbers in the upstream patch file at the branch head.
Paths under `chrome/` are lines in the patched tree. The upstream heads read were
`persistent-pinned-tabs` `89ccb52f` (#2931), `pins-ui` `50c48d90` (#2939) and `pins-drag`
`9cac737c` (#2941). GitHub reported the same heads for the three PRs at the time of writing.

## Summary

**Recommendation: option 1, scoped.** Import upstream's persistent pinned tabs (#2931) now as a
Neon-carried patch block and write the per-space behaviour as a separate Neon patch on top. Do not
import the UI pull requests (#2939, #2941) yet. Take them when they leave draft and are rebased on
the final #2931, or with the upstream release that merges them.

Why:

1. **Upstream's model already fits Neon.** #2931 keeps one pin list per profile and gives every
   normal window its own copy of every pin. A Neon window holds every space, so "every window has
   every pin, each in its own container, filtered by the visible space" is exactly per-space pins.
   What Neon has to add is small: the container of each pin, space-aware choices when a pin
   closes, and the delete and reopen paths. About 250 new lines and about 25 hook lines, most of
   them one-line call sites [code, assumed for sizes].
2. **Neon cannot take the upstream release without that delta anyway.** Unchanged, #2931 inside
   Neon puts hibernated copies of every pin in the default partition, which is now the global
   context and no space. Deleting a space brings its pins back. Closing a tab can jump to another
   space. "Reopen in space" on a pin leaves the original behind. So waiting (option 3) does not
   save the delta. It only moves the work onto the critical path of the sync that brings the
   merge [code, details in section C].
3. **The import is cheap to carry and to drop.** The whole stack applies with fuzz 0 at the end of
   the series on top of 0.22.1 plus Neon, needs nothing from upstream `main` that Neon lacks, and
   collides with Neon in one line of one enum [test]. It lives in its own directory and series
   block. At the sync that contains the merge, those lines and files go [assumed procedure,
   section F].
4. **Building it ourselves (option 2) is the worst long-term choice.** To match the owner's ask
   (a pin stays when closed or navigated away), Neon would rewrite the same close hook, session
   hook and tab menu entries upstream adds, in the same functions. All of it conflicts with
   upstream at merge and then gets thrown away [code, assumed sizes].

**What upstream's work gives the owner** [code]: pins kept across restarts independent of session
restore, closing a pin hibernates it instead of closing it, a hibernated pin returns to the URL it
was pinned at when activated, "Return to pinned URL" and "Use current URL as pinned URL" in the
tab menu, links and typed URLs to other sites open in a new tab instead of taking the pin away,
pinned splits, pins shared by every window, history titles for hibernated pins, and a migration
of old per-window pins. #2939 adds an outlined pin box with balanced rows. #2941 adds pinning by
dragging a tab into the pins.

**Main risks.**

1. **#2931 is still moving fast.** Seven force pushes between 2026-11-17 and 2026-11-19. Between
   two pushes on 2026-11-19 alone, its patch files changed by +601 and -161 lines. A maintainer
   (`dumbmoron`, member) reviewed it for the first time at 17:10 that day and asked for
   restructuring: the pin list as a value type with `FromPref`/`ToPref`, drag bookkeeping as a
   helper, incognito handling moved into the factory [thread]. The data model was not
   questioned. The Neon delta must avoid the code those requests will move.
2. **Profile data written by the import may not match the merged version.** If upstream renames
   the pref keys (`id`, `split`, ...) or the session keys (`helium.pinned_tab_id`,
   `helium.pinned_tab_url`) before merging, the owner's pins fall back to upstream's legacy
   migration once, at the drop sync, and may lose their space [assumed].
3. **Neon's ordering repair and upstream's pin order both move pinned tabs.** They should settle
   after one pass, because `ReorderPins` adopts whatever order the window ends up with, but this
   has not been run, least of all with two windows [assumed].
4. **#2939 makes Neon's pinned filter dead code without any build error.** It removes the stock
   `pinned_tab_container_view_vertical_layout.cc` from `BUILD.gn` and compiles its own file. Neon's
   hook in the stock file still applies but no longer runs, so every space's pins show [code,
   test].

**Patch list** for milestone M5a, in apply order (detail in section F):

| # | Patch | What | Size |
|---|---|---|---|
| U0 | `neon/upstream/pins/lazy-session-restore-deferred-tab.patch` | Upstream's change to Helium's own `lazy-session-restore.patch`, as a delta | ~65 changed lines |
| U1-U9 | `neon/upstream/pins/{tab-strip-close-hook, pin-state, pin-sync, sync-splits, sync-drags, hibernate-on-close, pinned-url, open-links-outside, migrate-legacy-pins}.patch` | Byte-identical copies of #2931's patch files | 3,761 patch lines |
| P1 | `neon/spaces/pins.patch` | Per-space pins on top of the import | ~250 new, ~25 hook |
| P2 | edits to `neon/spaces/move-to-space.patch` | One enum line moves below upstream's two | 1 line |
| later | `neon/upstream/pins/{pins-ui, drag-to-pin}.patch` and a hook move in `neon/spaces/strip-filter.patch` | #2939 and #2941, when they leave draft | 957 patch lines, ~15 hook lines move |

**Decisions the owner must take** (section G): which tab shows when the last unpinned tab of a
space with pins closes; whether pins of extension and other global-context pages are allowed;
whether to take #2939 and #2941 before they merge; and whether to accept a one-time loss of pin
order or space at the drop sync if upstream changes its stored keys.

## A. What the three branches do

### How they stack

| PR | Branch | Base | State on 2026-11-19 | Commits | Patch lines added |
|---|---|---|---|---|---|
| #2931 | `persistent-pinned-tabs` | `main` | Open, ready for review since 15:01 | 7 | +3,867 -63 over 17 files |
| #2939 | `pins-ui` | `persistent-pinned-tabs` | Draft | 8 (7 copied from #2931 plus 1) | +3,581 -10 |
| #2941 | `pins-drag` | `pins-ui` | Draft | 1 on top of #2939 | +444 |
| #2942 | `tab-drag-preview` | `pins-drag` | Draft, macOS only | | +744 |

[thread] The four started as one pull request. The first bot review on 2026-11-17 lists
`pin-on-drop.patch` and `tab-drag-image.patch` inside #2931. On 2026-11-18 the author split them
into a GitHub stack and renamed #2931 from "helium/pins: ..." to "helium/core: ...".

[code] All three branches sit on `1a551cd0`, which is upstream tag `0.22.1` plus one commit
(#2936, a component updater change in `component-updates.patch`, unrelated). `pins-ui` was not
rebased after #2931's later pushes. Its seven copied commits carry an older #2931:
`git diff upstream/persistent-pinned-tabs 2fbf853c` differs by +400 -1,148 lines over 12 patch
files. The two UI patch files themselves, `pins-ui.patch` and `drag-to-pin.patch`, apply with
fuzz 0 on top of the current #2931 [test].

### #2931, persistent pinned tabs

Nine new patches in `patches/helium/core/pins/`, listed in `patches/series` after
`helium/core/ui-density-prefs.patch`, plus a real change to Helium's own
`helium/core/lazy-session-restore.patch` and line-offset refreshes in five other Helium patches
(`layout/context-menu`, `layout/core`, `layout/vertical`, `layout/zen-mode-wiring`,
`ublock-show-in-settings`) and 24 lines in `i18n/source.gen.json` [code].

| Patch | Lines | Chromium files (lines added / removed) |
|---|---|---|
| `tab-strip-close-hook` | 79 | `tab_strip_model_delegate.{h,cc}` (+8), `tab_strip_model.cc` (+24 -1) |
| `pin-state` | 497 | new `helium_pins_tab_controller.{h,cc}` (+210), `helium_pins_util.{h,cc}` (+72); `tab_features.{h,cc}` (+10), `browser_tabrestore.cc` (+3), `browser_live_tab_context.cc` (+3), `session_service_base.cc` (+7), `tabs/BUILD.gn` (+4) |
| `pin-sync` | 851 | new `helium_pins_service.{h,cc}` (+650); `pinned_tab_service_factory.{h,cc}` (+23 -12), `tabs/BUILD.gn` |
| `sync-splits` | 434 | `helium_pins_service.{h,cc}` (+256 -6) |
| `sync-drags` | 362 | `helium_pins_service.{h,cc}` (+217 -7) |
| `hibernate-on-close` | 585 | `helium_pins_util.{h,cc}` (+302 -3), `tab_utils.{h,cc}` (+8), `tab_strip_model.cc` (+1 -3), `browser_tab_strip_model_delegate.{h,cc}` (+14 -5), `browser_commands.cc` (+7), `unload_controller.cc` (+7), `helium_pins_service.cc` (+4), `tabs/BUILD.gn` |
| `pinned-url` | 707 | `helium_pins_tab_controller.*` (+85 -2), new `helium_pins_navigation_throttle.{h,cc}` (+105), `helium_pins_service.*`, `helium_pins_util.*`, `tab_context_menu_command.h` (+2), `generated_resources.grd` (+12), `chrome_content_browser_client_navigation_throttles.cc` (+2), `tab_strip_model.cc` (+12), `tab_menu_model.cc` (+20 -4) |
| `open-links-outside` | 128 | `helium_pins_navigation_throttle.*` (+50 -4) |
| `migrate-legacy-pins` | 118 | `helium_pins_service.{h,cc}` (+72) |
| `lazy-session-restore` change | +120 -63 in the patch file | `browser_tabrestore.{h,cc}`, `startup_browser_creator_impl.cc`, `tab_utils.h`, `background_tab_loading_policy.h` |

**Data model** [code]:

- Each tab gets a `HeliumPinsTabController` (`pin-state.patch:92-271`), created for every tab in
  `TabFeatures::Init`. It holds `pin_id_` (a random UUID) and `pinned_url_`, and writes both to
  the session as tab extra data under `helium.pinned_tab_id` and `helium.pinned_tab_url`.
  Restored tabs hand their extra data to the controller through `RestoredPinState`
  (`:127-188`). Because Chromium drops tab extra data when it rebuilds the session file, the
  patch re-emits the two keys in `SessionServiceBase::BuildCommandsForTab` (`:464-475`).
- `HeliumPinsService` is a keyed service per profile (`pin-sync.patch:38-131`). It keeps
  `std::vector<Pin>`, where `Pin` is `{id, url}` plus split fields from `sync-splits`. It is
  stored in the existing pref `prefs::kPinnedTabs` as a list of dicts with keys `url`, `id`,
  `split`, `split_layout` and `split_ratio` (`pin-sync.patch:432-466`,
  `sync-splits.patch:91-93`). `SavePins` rebuilds every dict from the struct, so any other key
  in the list is dropped. It writes with `CommitPendingWrite` on every change.
- One set per profile, not per window. Incognito gets its own instance with in-memory pins only.
  `PinnedTabServiceFactory` now builds `HeliumPinsService` instead of Chromium's
  `PinnedTabService` (`pin-sync.patch:744-824`).

**Sync across windows** [code]: every normal window holds one tab, a "copy", per pin.
`Reconcile(browser)` (`pin-sync.patch:618-700`) sorts the window's pinned tabs by pin id, closes
duplicates, unpins tabs whose pin was removed, matches pinned tabs without an id to a pin by URL
(`:647-663`), inserts missing pins as hibernated tabs and restores the shared order.
`InsertHibernatedPin` (`:702-724`) calls `chrome::AddDeferredTab(browser, pin.url, title, index,
/*pin=*/true)`, a function the change to `lazy-session-restore.patch` adds. It builds one
`SerializedNavigationEntry` with just the URL and title and calls `chrome::AddRestoredTab` with
`kDeferUntilActivation`. Reconciles are posted, and retried after 250 ms while a window cannot be
edited, for example during a drag.

**Startup** [code]: windows restored from the session bring their pinned tabs with ids from the
extra data, and the first reconcile adds whatever is missing. Without session restore, Chromium's
`PinnedTabCodec` still reads URLs from `kPinnedTabs`. The startup code now opens those through
`chrome::AddDeferredTab` instead of Helium's old `AddDeferredPinnedStartupTab`, and `Reconcile`
links them to pins by URL and asks history for their titles.

**Closing a pin** [code]: `tab-strip-close-hook` adds `TabStripModelDelegate::HandleUserClosingTabs`
and calls it from `CloseWebContents`, `ExecuteCloseTabs` and `CloseAllTabsInGroup` whenever the
close carries `CLOSE_USER_GESTURE`. `HandleUserClosingTabs` (`hibernate-on-close.patch:357-`)
takes persistent pins and their split partners out of the close list and hibernates them after
any `beforeunload` dialog. If the active tab goes away it activates a replacement chosen by
`FindReplacementTab` (`:187-209`): the most recently used unpinned tab of the window, else a
loaded pin, never a hibernated pin. With no loaded tab left, hibernating opens a new tab page,
and closing the last unpinned tab closes the window unless `close-window-with-last-tab=never`.

**A pin that navigates away** [code]: the pin keeps its `pinned_url`. When a hibernated pin is
activated, `HeliumPinsNavigationThrottle` replaces the reload of its last page with the pinned URL
(`pinned-url.patch:455-560`). The tab menu gains "Return to pinned URL" and "Use current URL as
pinned URL", listed right after "Copy URL" (`pinned-url.patch:666-`). With `open-links-outside`,
links, typed URLs and searches with a user gesture that leave the pinned origin open in a new
foreground tab through `WebContents::OpenURL` (`open-links-outside.patch:60-115`).

**Drags** [code]: `sync-drags` applies pin changes from a drag only once the drag ends. A pin
dragged out of its window becomes a regular tab.

**Settings and commands** [code, thread]: no new setting, no feature flag. "Persistent pins are
always on." Two new tab context menu commands. No new keyboard shortcuts.

### #2939, pins UI

One patch, `patches/helium/ui/pins-ui.patch` (514 lines), listed after
`helium/ui/layout/min-window-width.patch`, plus offset refreshes in `native-frame-materials.patch`
[code].

- New `helium_pinned_tab_container_view_vertical_layout.cc` (+180) replaces Chromium's
  `pinned_tab_container_view_vertical_layout.cc` in `views/tabs/common/BUILD.gn`. Pins share a
  slot width (42 px minimum while expanded, 4 per row at the default width), a pinned split takes
  two slots, and pins spread evenly over rows (5 pins become 3 + 2) (`pins-ui.patch:143-349`).
- The pinned container draws one rounded outline with 2 px padding
  (`pinned_tab_container_view.cc`, `:69-137`). Individual pin borders go
  (`tab_view.cc`, `split_tab_view.cc`, `layout_constants.cc`).
- `tab_strip_view_layout.cc` (+25 -16) spaces pins 6 px from the tabs below and lets the outline
  reach into the side padding while collapsed.
- `dragged_tabs_container.{h,cc}` makes the drag clamping bounds virtual so the pinned container
  can clamp inside its outline.

### #2941, pin by dragging

One patch, `patches/helium/ui/drag-to-pin.patch` (443 lines), right after `pins-ui.patch` [code].

- `BaseTabStripRegionView::GetTabDragTarget` picks the pinned container whenever the pointer is
  over the pinned scroll view, not only when pinned tabs are dragged (`drag-to-pin.patch:401-425`).
- `TabDragHandler::HandleDraggedTabsIntoPosition` pins or unpins the dragged tabs as they enter a
  container (`:349-364`), through a new `helium_pins_drag_util.{h,cc}` and `SetTabsPinned`, which
  moves from private to public in `tab_strip_model.h`.
- `RootTabCollectionNode::OnChildMoved` keeps the view of a dragged tab whose pin state changes
  (`:39-57`). `TabDragData` gains `pinned_at_start` so a cancelled drag restores the state.
- The horizontal strip shows an empty drop zone while dragging if there are no pins. The vertical
  strip gets no drop zone, so a window, and in Neon a space, without pins cannot gain one by
  dragging [code].

## B. How stable the work is

[thread, code]

| | #2931 | #2939 | #2941 |
|---|---|---|---|
| Force pushes | 7 (17th 17:31; 18th 14:25; 19th 10:49, 11:54, 13:58, 14:47, 15:12) | Base moved 5 times on the 19th, own head unchanged since the 18th | None since the 18th |
| Churn | Patch files changed +1,146 -398 from `2fbf853c` (head on the 18th) to now; +601 -161 from `08e05287` (a push on the 19th) to now | Carries the stale #2931 copy | |
| Human review | One maintainer review (member `dumbmoron`, 2026-11-19 17:10), 13 inline comments | None | None |
| Bot reviews | 7 by `helium-gardener`, 2 by Greptile, many findings fixed between pushes | 1 gardener review, 2 findings | 1 gardener, 1 Greptile |

**Open on #2931** [thread]:

- Maintainer: replace `if (controller)` with expectations that it exists (three places); split
  `pin-sync` into a pin list value type with `FromPref`/`ToPref`, which would hold the migration
  and be unit-testable, and a drag bookkeeping helper; pass `PrefService*` and `HistoryService*`
  from the factory, null for incognito, instead of checking `IsOffTheRecord()` in three places;
  `UnpinCopies` should `CHECK` that all copies share a window; `open-links-outside` uses
  `ProfileIOData::IsHandledURL`, which also lets `data:`, `blob:`, `file:` and `chrome:` links
  through, so the fix is to reroute only http and https; a pinned split is lost when a page or
  extension closes one of its pins; "?" on two lines of the `lazy-session-restore` change;
  `MaybeCloseWindow(...)` on the last-tab logic; "i think this is a terrible idea but whatever"
  on making `PinnedTabServiceFactory` build the new service.
- Bot, latest review: with default settings, Ctrl+W on the only tab of a window without pins no
  longer closes the window (a regression); restored splits can overwrite newer shared split
  state; an unrelated zoom change should leave the PR.

**Open on #2939** [thread]: row capacity depends on child order, so dragging a pin past a split
can reflow the grid mid-drag; the drag stride still adds 4 px while the gap is now 2 px.

**Open on #2941** [thread]: a tab dragged into the pins and back over an expanded group stays
pinned; Greptile asks for cancellation tests.

**Maintainer intent** [thread]: the author is a maintainer (`wukko`) and the PR closes nine
issues, including #1233 and #1240. The review asks for structure changes and bug fixes, not a
different model. Nobody objected to "one pin list per profile, shared by all windows".

**Estimate of change before merge** [assumed]: in #2931, `helium_pins_service.cc` gets
restructured (pin list type, drag helper, factory arguments), and `open-links-outside`,
`hibernate-on-close` and `sync-splits` get fixes. Expect several hundred more lines of churn in
`pin-sync`, `sync-splits` and `hibernate-on-close`. The tab controller, the session keys, the
close hook in `tab_strip_model.cc` and `AddDeferredTab` were not criticised and look stable.
#2939 and #2941 will be rebased on the final #2931 and get their drag fixes.

**Which release** [thread, assumed]: no thread names a release. Upstream tagged nine releases
since 2026-10-08, every 2 to 7 days. The author's large pull requests merged within 1 to 10 days
(#2561, #2654, #2906, #2916), while #2898 (native materials, +1,585) has been open a week. My
guess: #2931 lands in 0.22.x or 0.23.0 within one to three weeks; the UI pull requests the same
time or a little later.

## C. Conflicts with Neon

### Applying at all

[test] On scratch copies of the 63 Chromium files the stack touches, taken from the patched tree,
with Neon's patches reversed and Helium 0.22.1's `lazy-session-restore.patch` hunks swapped for
#2931's version, every #2931 patch, `pins-ui.patch` and `drag-to-pin.patch` applied with fuzz 0.
Then every Neon patch applied on top. Only one hunk failed:

- `components/tabs/public/tab_context_menu_command.h`: Neon's `move-to-space.patch` adds
  `CommandNeonReopenInSpace` after `CommandToggleTabMuted`, where `pinned-url` adds
  `CommandReturnToPinnedUrl` and `CommandUseCurrentUrlAsPinnedUrl`. A one-line refresh.

So the branches apply on `0.22.1`. They need nothing from upstream `main`. #2936 under them and
#2947, #2949 above `0.22.1` touch unrelated files [code]. The pins stack also applies when placed
after all of Helium's patches instead of in the middle of `helium/core`, which is what an import
inside the `neon/` block needs [test]. Nothing was compiled, so semantic breakage is not ruled
out by this.

### Shared files, patch by patch

| Upstream patch | Chromium file | Neon patches on the same file | Overlap |
|---|---|---|---|
| `tab-strip-close-hook`, `hibernate-on-close`, `pinned-url` | `tabs/tab_strip_model.cc` | `model-filter`, `range-filter`, `move-to-space` | Different functions, applied with offsets [test]. Behaviour overlap below |
| `hibernate-on-close` | `browser_commands.cc` | `tab-partition`, `model-filter`, `groups` | Different functions [test] |
| `pin-state` | `tab_features.{h,cc}` | `containers/browser-service` | Neighbouring member lists, applied [test] |
| `pin-state`, lazy change | `browser_tabrestore.cc`, `browser_live_tab_context.cc` | `containers/tab-partition` | Different hunks [test] |
| lazy change | `startup_browser_creator_impl.cc`, `session_restore.cc` | `containers/command-line` | Different hunks [test]. Finding 7 moves into upstream's new call |
| `pinned-url` | `tab_context_menu_command.h` | `move-to-space` | **Conflict**, one enum line |
| `pinned-url` | `tab_menu_model.cc` | `move-to-space` | Applied; menu order to check |
| all core patches | `tabs/BUILD.gn` | `model-filter`, `move-to-space` | Applied [test] |
| `pins-ui` | `views/tabs/common/BUILD.gn` | `strip-filter` | Applied, but see the layout row |
| `pins-ui` | `pinned_tab_container_view_vertical_layout.cc` | `strip-filter` | **Silent**: file no longer compiled |
| `pins-ui` | `tab_view.cc` | `pager` (`IsTabActiveInPage`) | Different functions [test] |
| `drag-to-pin` | `root_tab_collection_node.cc` | `strip-filter`, `pager` (`Init`) | Different functions [test] |
| `drag-to-pin` | `base_tab_strip_region_view.cc` | `pager` (`SetTabStripView`) | Different functions [test] |

No Neon patch edits a Helium patch file today (`git diff 0.22.1 main -- patches ':!patches/neon'`
shows only `series`) [code].

### Pinned layout against the space filter and the pager

[code] Neon hides other spaces' pins in three places of the stock vertical layout:
`GetMinimumSize`, the row count and `should_show_child`
(`pinned_tab_container_view_vertical_layout.cc:38`, `:68`, `:128`). With only #2931 imported
nothing changes here: pins are ordinary pinned tabs in the model and the filter keeps working,
in the real page and in both peek pages, which run the same layout on their own
`RootTabCollectionNode`.

With #2939, the hook has to move into `helium_pinned_tab_container_view_vertical_layout.cc`:
`num_children` in `GetMinimumSize` (`pins-ui.patch:180-182`), `total_slots` and the row planning
in `CalculateProposedLayout` (`:220-226`, `:259-271`), and `should_show_child` (`:296`). One more
case: the new layout returns an empty size only when the container has no children at all
(`:214-217`). In a space without pins, while another space has some, it returns the outline's
insets as height, so an empty 3 px box would show. The Neon hook must treat "all children hidden"
as empty. About 15 hook lines, the same pattern as today.

`tab_strip_view_layout.cc` in #2939 adds the bottom gap only when the pinned container has a
preferred height, which the filter sets to zero for a space without pins. That works [assumed].

### Pin persistence against the container encoding and finding 7

[code] R1 saves a tab's container as the `containers+<id>:` prefix of each serialized navigation
and restores it into `SerializedNavigationEntry::storage_partition_key`
(`components/sessions/core/serialized_navigation_entry.h:112-118`), which
`containers::MaybeSetContainerSiteInstance` reads when `CreateRestoredTab` builds the tab
(`tab-partition.patch`, `browser_tabrestore.cc`). Restored pins therefore come back in their
container and keep their pin id. Upstream needs nothing more for that path.

Three paths lose the container:

1. `InsertHibernatedPin` builds its navigation entry by hand through `chrome::AddDeferredTab`,
   with URL and title only. Every copy added to a new window, after an unpin elsewhere or after
   a reconcile lands in the default partition, which is the global context under today's
   decision.
2. Startup without session restore: `PinnedTabCodec` reads only URLs, and
   `startup_browser_creator_impl.cc:459-464` opens them with no container. This is PLAN finding 7.
   #2931 replaces the call with `chrome::AddDeferredTab`, so the fix moves with it.
3. Matching by URL in `Reconcile` (`pin-sync.patch:647-663`) ignores the container. A startup
   copy in the wrong partition would be accepted as the pin, or the same URL pinned in two spaces
   would match the wrong one.

R1 planned to write `"container_id"` into the `kPinnedTabs` dicts through `PinnedTabCodec`
(`containers-port.md`, pinned tabs row). It was never ported (`command-line.patch` header), and
#2931's `SavePins` would drop the key anyway.

### Closing, deleting and reopening

[code] Behaviour clashes in Neon's own files and in `helium_pins_util.cc`:

- `FindReplacementTab` and `CouldActivatePin` (`hibernate-on-close.patch:187-245`) choose among
  all tabs of the window and activate with `ActivateTabAt`. That bypasses Neon's
  `DetermineNewSelectedIndex` hook, so closing a tab can switch to another space.
  `closes_last_unpinned` (`:379-385`) counts unpinned tabs of every space, and then closes the
  window. Neon's rule is a new tab page in the space, and the window closes only when every
  space is empty (`spaces_window_controller.cc:524-535`).
- `DeleteSpace` (`space_management.cc:76-136`) removes the space's tabs with
  `DetachAndDeleteWebContentsAt` in `EvacuateSpace`. Their pins stay in the profile list, so the
  next `Reconcile` adds them back hibernated, in a container that no longer exists, or without a
  container.
- `ReopenTabsInSpace` (`space_management.cc:138-188`) opens a pinned copy in the target space
  and closes the original with `CloseWebContentsAt(..., CLOSE_USER_GESTURE)`. That reaches
  `CloseWebContents`, which #2931 hooks (`tab_strip_model.cc:1408-1410`), so the original pin
  hibernates instead of closing. The user ends with the pin in both spaces.
- Neon's `RepairContiguity` (`spaces_window_controller.cc:636-650`) moves pinned tabs to keep
  each space's tabs together. Each move of a persistent pin calls `ReorderPins`, which adopts the
  window's order into the profile list and reconciles the other windows. `Reconcile` itself
  moves tabs under `applying_changes_`, so it does not trigger `ReorderPins`. Expected to settle
  after one pass [assumed].

Hibernating the last loaded pin opens a new tab page with `chrome::AddTabAt`. The visible space
is still the pin's, so Neon's new tab provider puts the page in that space's container [assumed,
from `GetContainerForNewTab`]. Links from a pin that open a new tab go through `OpenURL` with the
pin as source, and M1 already proved such tabs inherit the container [assumed].

### Drag code against the switcher row and the group rules

[code] The switcher row's drag only reorders space icons (`management-ui.patch:1508-1556`,
`:2030-2060`). It uses no `TabDragController`, so it does not meet #2941. The pager's swipe stops
while a tab drag runs (`swipe.patch:198`). Peek pages own a `TabDragHandler`
(`pager.patch:828`); #2941's `UpdatePinDropZone` reads the region view's real tab strip, which is
fine because the drop zone exists only in the horizontal strip.

The M1b group rule takes a tab out of a group when it comes from another space
(`RepairGroupsAndSplits`, `spaces_window_controller.cc:652-677`). #2941 pins tabs during a drag
and unpins them on the way out. Upstream's own open finding (a tab dragged through the pins into
an expanded group stays pinned) will need a fix first. A drag never changes a tab's container, so
a pin dragged into the pins of the visible space stays in its space [assumed].

## D. Per-space pins on top of upstream's model

The shape: every window keeps a copy of every pin, as upstream wants, and each copy lives in its
pin's container. The space filter does the rest. The design keeps hunks out of the code the
maintainer asked to restructure (`Pin`, `LoadPins`, `SavePins`, the drag bookkeeping).

**Where the container of a pin lives.** A Neon pref `neon.spaces.pin_containers`, a dict from pin
id to container id, owned by new Neon code. It is written whenever a pinned tab with a pin id
appears or gains an id (a `TabStripModelObserver` on every normal window, posted after upstream's
observer has assigned the id), and pruned against the ids in `kPinnedTabs` when that pref
changes. Copies that exist in some window are the source of truth; the map only matters when no
copy exists, which is startup without session restore. It would also be possible to add a
`container_id` key to the `kPinnedTabs` dicts, as R1 planned, but that means hunks in `Pin`,
`LoadPins`, `SavePins` and `InsertNewPin`, the code most likely to move before merge. Revisit
once #2931 has merged and the pin list has its `FromPref`/`ToPref` [assumed trade-off].

**Changes.**

| Need | How | Where | Size |
|---|---|---|---|
| A hibernated copy opens in its pin's container | Neon helper `AddDeferredPinInContainer(browser, url, title, index, container_id)` builds the `SerializedNavigationEntry`, sets `storage_partition_key` through the containers interface and calls `chrome::AddRestoredTab` | New code in `chrome/browser/ui/neon/spaces/space_pins.cc` and `chrome/browser/neon/containers_api.cc`; one call-site hunk in `InsertHibernatedPin` (`pin-sync.patch:715-716`) | ~60 new, 2 hook |
| Startup without session restore (finding 7) | The same helper at upstream's `chrome::AddDeferredTab` call in the startup path. Pins opened there are matched by id through the map, or skipped and left to `Reconcile`, which then adds every pin hibernated in its container. Pick after a test | `startup_browser_creator_impl.cc`, upstream's lazy hunk | ~30 new, 2 hook |
| Match by URL only within a container | One condition in the URL match lambda | `Reconcile`, `pin-sync.patch:651-653` | 1 hook |
| Show only the visible space's pins | Already done by `strip-filter`; moves with #2939 | Stock layout today, Helium layout later | 0 now, ~15 later |
| Closing stays in the space | Skip tabs hidden by the space filter in `FindReplacementTab` and `CouldActivatePin`; count only the closing space's unpinned tabs in `closes_last_unpinned`, and keep the window, leaving a new tab page or a loaded pin in the space per the owner's choice | `helium_pins_util.cc`, three spots | ~10 hook, ~40 new |
| "Reopen in space" on a pin | Unpin the original first (`SetTabPinned(false)`), which runs upstream's `RemovePin` and unpins and closes copies in other windows, then open the pinned copy in the target space. The new tab becomes a new pin with a new id at the end of that space's pins | `space_management.cc`, Neon file | ~6 |
| Deleting a space removes its pins | Before `EvacuateSpace`, unpin the space's pinned tabs in one window: upstream removes them from the list and from every window. Then drop the map entries | `space_management.cc`, Neon file | ~10 |
| Pins in the peek pages | Nothing beyond the filter: peek pages are full mirrors of the model | | 0 |
| Order | Rely on `ReorderPins` adopting Neon's contiguous order. If the test shows churn, sort `pins_` by space order in Neon's repair instead | | 0 or ~20 |
| Enum clash | Move `CommandNeonReopenInSpace` after upstream's two commands | `move-to-space.patch` | 1 |

Totals: about 250 new lines in Neon files and about 25 lines in imported upstream files, in five
hunks (`InsertHibernatedPin`, the URL match, three spots in `helium_pins_util.cc`) plus one in the
startup hunk [assumed sizes].

**Pins in the global context.** Under today's decision the default partition belongs to no space.
A pinned extension page or other default-partition tab has no space to show in, because Neon
derives a tab's space from its container (decision 9). Simplest rule: such tabs cannot become
persistent pins, or they stay pinned only in the global surface once R3's overlay exists. Owner
decision G2.

## E. The alternative: Neon's own per-space pins without the upstream branches

**What exists today** [code]: stock pinned tabs, Neon's filter hides other spaces' pins in the
sidebar, and session restore brings them back in their containers. No test pins a tab (PLAN M2).

**The minimal version** (about 2 days) [assumed]: a test that pins tabs in two spaces and checks
the filter, the peek pages, restart and "Reopen in space"; plus finding 7 (port Brave's
`container_id` in `PinnedTabCodec` and pass `StartupTab::container` at
`startup_browser_creator_impl.cc:459-464`, about 40 lines). This gives per-space pins that last
as long as the session does. It lacks everything the owner asked for beyond that: closing a pin
removes it, a pin that navigates away is just a tab on another page, there is no pinned URL to
return to, links do not leave the pin, a new window has no pins, and with session restore off the
pins come back only through `PinnedTabCodec`.

**The full version** [assumed]: the missing behaviour needs a close hook in `tab_strip_model.cc`
and the delegate, a pin list pref, per-tab pin state with the `BuildCommandsForTab` session
hook, a pinned URL with a navigation throttle and two tab menu entries, and hibernation. That is
the same set of hooks #2931 adds, in the same functions, about 1,000 to 1,500 lines with about
60 hook lines.

**When upstream merges** [assumed]: the minimal version's finding 7 hunk sits on Helium's
`AddDeferredPinnedStartupTab`, which #2931 deletes, so it conflicts and has to move to
`AddDeferredTab`; the test survives and becomes the M5 test. The full version conflicts in
`tab_strip_model.cc`, `tab_strip_model_delegate.h`, `session_service_base.cc`,
`tab_context_menu_command.h` and `tab_menu_model.cc`, owns the same `kPinnedTabs` pref as
upstream, and has to be thrown away and replaced by section D's delta, plus a migration from its
own pref format. The full version is the most expensive path in every respect.

## F. Recommendation and how to carry the import

### Options compared

| | Sync cost while carried | Drop or merge cost | Risk if upstream reworks | Owner gets Arc-style pins |
|---|---|---|---|---|
| 1. Import #2931 now, delta on top | Refresh the copied files and rebase ~6 delta hunks when we choose to; the block applies with fuzz 0 today | Delete the block and its series lines, refresh P1 against the merged code | Delta hunks sit on stable call sites; worst case P1 is rewritten once | Now (M5a) |
| 2. Own minimal pins, rebase later | Low | Finding 7 hunk conflicts; delta still to write at the merge sync | None | Not really (session-length pins only) |
| 2b. Own full pins | High | Throw away 1,000+ lines, migrate pref data | High | Now, then again |
| 3. Wait | None | Delta written during the merge sync, which cannot ship without it | None | 1 to 3 weeks from now [assumed], plus the delta |

Options 1 and 3 end in the same place: upstream's pins plus the same Neon delta. Option 1 costs a
few refreshes and one drop, and gives the owner the feature now and the delta tested before the
sync that needs it. That is why it wins. If the owner prefers zero carried upstream code, option
3 is the fallback; option 2 should not be built beyond its test.

### Where the import lives and how it is marked

- Files: `patches/neon/upstream/pins/`. Each `*.patch` is a byte-identical copy of
  `patches/helium/core/pins/<name>.patch` at the recorded upstream SHA, so `cmp` against
  `git show <sha>:patches/helium/core/pins/<name>.patch` proves it. The one exception is
  `lazy-session-restore-deferred-tab.patch`, generated by Neon as the difference between Helium
  0.22.1's and #2931's `lazy-session-restore.patch` effect on the tree (about 65 changed lines in
  5 files), so the Helium patch file itself stays untouched, per `AGENTS.md`.
- Series: at the start of the `neon/` block, before `neon/containers/core.patch`, in upstream's
  order: the lazy delta, then `tab-strip-close-hook`, `pin-state`, `pin-sync`, `sync-splits`,
  `sync-drags`, `hibernate-on-close`, `pinned-url`, `open-links-outside`, `migrate-legacy-pins`.
  The block stays last in `series`, as `AGENTS.md` requires. No comment lines in `series`.
- Marking: a manifest `neon/upstream-imports.md` with, per import, the PR number, branch, head
  SHA, date, the file list, the generated delta and how it was made, and the condition for
  dropping it. `THIRD_PARTY.md` does not apply (same project, same licence), but a line there
  does no harm. Commit subject: `neon: import imputnet/helium#2931 @<sha> (persistent pins)`.
- Strings: carry the two `generated_resources.grd` messages inside `pinned-url` as is. Skip
  `i18n/source.gen.json`; it only feeds Helium's translation tooling, so the new menu items stay
  English, like Neon's own strings [code: `devutils/i18n*.py`, `utils/i18n_apply.py`].

### Refreshing when the upstream branch moves

Do not chase every push; the branch moved five times on 2026-11-19 alone. Refresh at chosen
points: when the PR gets an approval, before a Neon upstream sync, or when a fix the owner needs
lands.

1. `git fetch upstream persistent-pinned-tabs`, note the new head.
2. Copy the nine files from the new head over `patches/neon/upstream/pins/`; add or remove files if
   upstream renamed any (compare the `series` diff of the branch against its base).
3. If `git diff <base> <head> -- patches/helium/core/lazy-session-restore.patch` changed, rebuild
   the lazy delta.
4. Apply the series on the tree, refresh `neon/spaces/pins.patch`, build, run the pins test.
5. Update the manifest. A script `neon/scripts/upstream-import.sh` with `check` (cmp against the
   recorded SHA), `refresh <sha>` (steps 1 to 3) and `status` (is the PR merged, which tag holds
   it) would make this mechanical; about 80 lines of shell [assumed].

### Dropping at the sync that contains the merge

Upstream squash-merges ("... (#2936)" commits), so the import's commits will never be ancestors
of a tag. Detect the merge by content: the new tag's `patches/series` lists
`helium/core/pins/pin-sync.patch`.

1. In the sync merge, take upstream's `series`, re-append Neon's lines, and leave out the import
   block.
2. `git rm -r patches/neon/upstream/pins/` and remove the manifest entry.
3. Refresh `neon/spaces/pins.patch` against the merged code. Expect real work here if upstream
   restructured `helium_pins_service.cc` (section B); the hunks are call sites, so the work is
   finding the new home of each.
4. Run the pins test with a profile created under the import, to see whether pins, their order
   and their spaces survive the data format of the merged version (risk 2 in the summary).

If #2939 and #2941 were imported later, they drop the same way, and `strip-filter.patch` keeps its
hook in the Helium layout file.

### Patch breakdown for M5a

| # | Patch | New | Hook | Hooks into |
|---|---|---|---|---|
| U0 | `neon/upstream/pins/lazy-session-restore-deferred-tab.patch` | | ~65 changed | `browser_tabrestore.{h,cc}`, `startup_browser_creator_impl.cc`, `tab_utils.h`, `background_tab_loading_policy.h` |
| U1-U9 | `neon/upstream/pins/*.patch` (copies) | ~1,950 | ~200 | as section A |
| P1 | `neon/spaces/pins.patch` | ~250 (`space_pins.{h,cc}`, `containers_api.cc` helper, pref registration, test hooks) | ~25 | `helium_pins_service.cc` (2 hunks), `helium_pins_util.cc` (3), `startup_browser_creator_impl.cc` (1); Neon files `space_management.cc`, `spaces_service.cc` |
| P2 | `neon/spaces/move-to-space.patch` refresh | | 1 | `tab_context_menu_command.h` |
| T | `neon/tests/pins_m5.py` | ~400 | | |

URL routing (`url-rules.patch`, the other half of M5 in PLAN) is independent and can follow as M5b.

### Done when

In the Linux build, with three container spaces and the default partition as global context:

- Pinning a tab in space A shows it at the top of A's page only, in the real page and in the
  peek pages during a swipe, and in a second window as a hibernated copy in A.
- Each pin's copies are in its space's container: two pins of the same site in A and B hold
  different cookies.
- After a restart with session restore on, and again with it off, every pin is back in its space,
  in order, hibernated, with a title, and none in the default partition.
- Closing a pin hibernates it and keeps it in its space; activating it loads its pinned URL;
  "Return to pinned URL" and "Use current URL as pinned URL" work; a link to another site from a
  pin opens a new tab in the same space.
- Closing the last unpinned tab of a space with pins behaves as the owner chose (G1), never
  switches space and never closes the window while another space has tabs.
- "Reopen in space" on a pin leaves exactly one pin, in the target space, in every window.
- Deleting a space removes its pins from every window and from `kPinnedTabs`, and they do not
  come back after a restart.
- Reordering pins in one space by drag or by command keeps the other spaces' pins in place, and
  two windows settle on the same order within one second.
- The test drives the omnibox and tab menu with real input, as M1b requires, and is committed as
  `neon/tests/pins_m5.py`; the M2, M3 and M1b checks still pass.
- `neon/upstream-imports.md` records the import, and `check` confirms the copies match the SHA.

## G. Decisions for the owner

| # | Question | Recommended default |
|---|---|---|
| G1 | Closing the last unpinned tab of a space that has pins: show a loaded pin of the space, or a new tab page in the space? | A loaded pin if one exists, else a new tab page. Never a hibernated pin, never another space |
| G2 | Pins of global-context pages (extension pages): allowed? | Not as persistent pins until R3's overlay exists |
| G3 | Take #2939 (outlined pins) and #2941 (drag to pin) before they merge? | No. Wait until they leave draft and are rebased on the final #2931 |
| G4 | Accept that pins made under the import may lose order or space once at the drop sync if upstream changes its stored keys? | Yes, with the Neon map also keeping URL to container as a fallback |
| G5 | Import now (option 1) or wait for the release (option 3)? | Import now |

## H. Not determined

- Nothing was built or run. Compile errors in the imported stack on top of Neon (for example GN
  deps between `//chrome/browser/ui/tabs` and Neon's `hooks` targets) are not ruled out.
- Whether `ReorderPins` and Neon's contiguity repair settle quickly with two windows, or ping-pong.
- Whether a deferred tab with a `storage_partition_key` on its single navigation entry gets its
  container through `MaybeSetContainerSiteInstance` exactly like a restored tab. It should, since
  that is the path R1 uses, but `AddDeferredTab` skips the session restore loader.
- Whether hibernation by discard keeps the container on every discard path (the containers
  interface says `GetTabContainerId` works for discarded tabs).
- The order of TabStripModel observers between `HeliumPinsService` and Neon's window controller,
  which matters for when the Neon map sees a new pin id.
- What the merged #2931 will look like, which release carries it, and whether upstream keeps the
  `kPinnedTabs` keys and session keys.
- Upstream's behaviour of pins in the horizontal and compact layouts inside Neon (spaces are off
  there, so all pins show, as before).
- Accessibility of the outlined pin container in peek pages.
