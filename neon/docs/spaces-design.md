# Spaces design (R2)

Status: design only, no code. Written 2026-11-19 against Helium 0.22.1 on Chromium 158.0.8127.36.

## Summary

**Source used.** The patched tree at `~/projects/neon-linux/build/src`, read-only. Its
`chrome/VERSION` is 158.0.8127.36 with `HELIUM_MAJOR=0 HELIUM_MINOR=22 HELIUM_PATCH=1`, and
`build/logs/setup.log` shows the full series (ungoogled-chromium, Helium, helium-linux) applied
with exit 0 and no `patches/neon/` entries. Line numbers below refer to that tree. Paths under
`patches/` refer to `~/projects/neon`. A partial clone of Chromium was started first and dropped
once the shared tree turned out to be ready. Upstream draft pull requests were read with
`gh pr diff` on 2026-11-19 (#2931, #2939, #2941 are drafts; #2898 is open and no longer a draft).
R1's `neon/docs/containers-port.md` appeared while this was written and is referenced where it
settles a question.

**Recommended data model: one flat `TabStripModel` per window, each tab tagged by its container,
and a filtered view.** A tab's space is not stored anywhere: it is derived from the tab's
container id (`GetTabContainerId`), with the default partition mapping to the default space.
This is the model Chromium already uses for tab group focus mode (`kTabGroupsFocusing`, off by
default), which hides tabs outside the focused group while the model stays flat. Neon follows
that precedent hook for hook: the same five or six places in `tab_strip_model.cc` and
`browser_commands.cc` that check `focused_group` get one extra predicate call. Within the pinned
and unpinned collections, tabs of one space are kept contiguous, so space-relative indices,
range selection and "close tabs to the right" stay simple. The visible space of a window is the
space of its active tab, so switching space is activating a tab and every existing tab-switch
path (focus, omnibox state, split views) keeps working.

**Sidebar structure.** Helium's vertical strip is `VerticalTabStripRegionView` with a content
column of [tab strip view, bottom container (new tab button), gemini placeholder]; Helium
removed the top container. The tab strip view is built by a `RootTabCollectionNode` that mirrors
the model's collection tree into views. Neon inserts a `SpacesPagerView` where the tab strip view
goes, by wrapping it in `BaseTabStripRegionView::SetTabStripView`, a Chromium file no Helium
patch touches. The pager holds the real tab strip view as the page for the visible space and up
to two extra "peek" pages (previous and next space), each a full mirror of the model whose tabs
from other spaces are hidden by the layout predicate. The space switcher row sits at the bottom
of the pager, above Helium's new tab button. A pre-target event handler on the region view
tracks the two-finger swipe, as Chromium's own `VerticalTabStripFocusSwipeController` already
does for focus mode.

**Main risks.**

1. Several live tab strip views on one model. `TabStripModel::SetTabStripUI` allows one UI
   observer (`tab_strip_model.cc:357-373`), and nothing in Chromium runs two
   `RootTabCollectionNode`s on one model today. Unverified until built.
2. Hidden children in the pinned container. Focus mode only ever hides unpinned tabs, so drag,
   link-drop and keyboard paths over a pinned container with hidden children have no precedent.
3. Hooks in `tab_strip_model.cc`. Few lines, but the file is large, changes every Chromium
   release, and carries 8 upstream patches already.
4. Upstream's pinned tabs work (#2931, #2939, #2941) rewrites the pinned container layout and
   pin persistence while we design around them. #2931 also rewrites the `kPinnedTabs` list
   without the `container_id` key R1 adds to it.
5. Linux gives weak gesture data. X11 touchpads send phase-less `kScroll` events with no lift
   signal; Wayland signals lift through `kScrollFlingStart`; macOS sets only the momentum phase.
   Finger tracking works everywhere, but the end of a gesture on X11 is a timeout.
6. Spaces depend on R1 restoring every tab into its container. Any restore path that loses the
   container silently moves the tab into the default space (R1 lists saved tab groups as one).

**Patch list** (`patches/neon/spaces/`, apply order; detail in "Patch breakdown"):

| # | Patch | Milestone | Rough size |
|---|---|---|---|
| 1 | `service.patch` space list, prefs, keyed service | M2 | ~600 new, ~5 hook |
| 2 | `window-controller.patch` visible space per window, switching | M2 | ~550 new |
| 3 | `model-filter.patch` model and command predicates | M2 | ~150 new, ~30 hook |
| 4 | `new-tab-space.patch` new tabs open in the visible space | M2 | ~80 new, ~5 hook |
| 5 | `strip-filter.patch` hide other spaces' tabs in the strip | M2 | ~120 new, ~10 hook |
| 6 | `commands.patch` commands and shortcuts | M2 | ~150 new, ~60 hook |
| 7 | `pager.patch` pager view with peek pages | M3 | ~900 new, ~15 hook |
| 8 | `switcher-row.patch` space icons row | M3 | ~350 new |
| 9 | `swipe.patch` finger-tracked gesture and animation | M3 | ~450 new |
| 10 | `management-ui.patch` create, rename, icon, colour, delete, reorder | M4 | ~800 new |
| 11 | `settings-page.patch` settings subpage | M4 | ~700 new, ~60 hook |
| 12 | `move-to-space.patch` tab context menu entry | M4 | ~200 new, ~10 hook |
| 13 | `pins.patch` per-space persistent pins (only if #2931 lands) | M5 | ~150 hook in Helium files |
| 14 | `url-rules.patch` route URLs to a space | M5 | ~500 new, ~5 hook |
| mac | `neon-macos: spaces/mac-swipe.patch` native swipe tracking, optional | M6 | ~200 new |

## Conventions

- `file:line` is a path in the patched tree unless it starts with `patches/` or `pr`, in which
  case it is a patch file in `~/projects/neon` or an upstream PR diff.
- "Hook" means an edit to an existing Chromium or Helium file. New Neon files go under
  `chrome/browser/neon/spaces/`, `chrome/browser/ui/neon/spaces/` and
  `chrome/browser/ui/views/neon/spaces/`, per `AGENTS.md`.
- "Churn" counts commits in `~/projects/neon` touching a patch file since 2026-05-19 (180 days),
  split into all commits and feature commits (all minus commits whose subject is a Chromium
  refresh, update or merge).

## 1. Data model

### What the model looks like in 158

`TabStripModel` stores tabs in a tree of `tabs::TabCollection`s. The collection types are a
closed enum: `TABSTRIP, PINNED, UNPINNED, GROUP, SPLIT`
(`components/tabs/public/tab_collection.h:153-161`), with a `LINT.ThenChange` to the views-side
copy of the enum. The root `TabStripCollection` has exactly one pinned and one unpinned child
(`components/tabs/public/tab_strip_collection.h:39-40`), and pinned tabs come first in model
order (`IndexOfFirstNonPinnedTab`, `tab_strip_model.cc:1580`). Each `Browser` owns one model
(`chrome/browser/ui/browser.cc:271`), exposed as `BrowserWindowInterface::GetTabStripModel()`
(`browser_window/public/browser_window_interface.h:244`).

The views side mirrors this tree. `RootTabCollectionNode` observes the root collection and the
model (`views/tabs/common/root_tab_collection_node.h`), and `TabCollectionNode::CreateViewForNode`
maps `TABSTRIP` to `TabStripView`, `PINNED` to `PinnedTabContainerView`, `UNPINNED` to
`UnpinnedTabContainerView`, and so on (`views/tabs/common/tab_collection_node.cc:98-115`). A
node's children are kept at the same indices as the collection's direct children
(`tab_collection_node.cc:242-266`, `391-396`).

### A precedent: tab group focus mode

Chromium 158 ships a filtered view of a flat model behind `kTabGroupsFocusing`
(`ui_features.cc:570`, disabled by default; Helium does not enable it). While a group is focused:

- the unpinned container hides children outside the group
  (`UnpinnedTabContainerView::LayoutManager::IsChildVisibleInContainer`,
  `views/tabs/common/unpinned_tab_container_view.cc:150-166`);
- next/previous tab skips tabs outside the group (`SelectRelativeTab`, `tab_strip_model.cc:4650-4668`);
- Ctrl+1..9 and Ctrl+9 count only selectable tabs (`IsTabSelectable`, `browser_commands.cc:599-616`,
  used by `SelectNumberedTab` and `SelectLastTab`, `browser_commands.cc:1603-1638`);
- closing the active tab picks a successor inside the group
  (`DetermineNewSelectedIndex`, `is_valid_for_focus`, `tab_strip_model.cc:6189-6195`, retried
  without the filter at `6282-6286`);
- "close other tabs" and "close tabs to the right" skip tabs outside the group
  (`tab_strip_model.cc:4088-4096`);
- moving a tab with the keyboard cannot cross the group boundary (`tab_strip_model.cc:4719-4724`);
- activating a tab outside the group exits focus mode (`SetSelection`, `tab_strip_model.cc:4478-4489`);
- a two-finger horizontal swipe on the vertical strip rotates the focused group
  (`views/tabs/vertical/vertical_tab_strip_focus_swipe_controller.cc`, installed at
  `views/frame/vertical_tab_strip_region_view.cc:213-216`).

Spaces need the same filter with a different predicate, plus one behavioural difference:
activating a tab in another space switches the visible space instead of leaving the filter.

### Options compared

| Concern | (a) Flat model, space tag, filtered view | (b) Space collection level in the model | (c) One `TabStripModel` per space |
|---|---|---|---|
| Model invariants | Unchanged. Pinned tabs of all spaces sit in the pinned collection, unpinned in the unpinned collection. | Breaks "pinned tabs first": each space has its own pins, so pinned tabs are no longer a prefix. Adds a type to a closed enum mirrored in views. `TabStripCollection` assumes one pinned and one unpinned child. | Each model is a normal model. |
| Browser and window code | Unchanged: one model per `Browser`. | Unchanged API, rewritten internals. | Breaks the one-model-per-`Browser` assumption used by `BrowserView`, commands, session service, extensions, `TabStripModelDelegate`. Swapping a window's model has no support anywhere. |
| Active tab and selection | One active tab per window, the visible space's. Selection stays inside one space because only its tabs are clickable. | Same as (a). | One active tab per model; the window must pick which model is live. |
| Extension tab indices | Indices span all spaces in one window. Extensions see every tab, which is fine for tab managers. Indices of a space's tabs shift when another space gains tabs. | Same as (a). | Extensions would see one model only, or need the window to expose several. |
| Ctrl+Tab and Helium's MRU cycling | One predicate in `SelectRelativeTab` and in the MRU list builder. | Same predicates. | Free. |
| Ctrl+1..9 | `IsTabSelectable` predicate, so numbers count within the space. | Same. | Free. |
| Tab search | Lists all tabs; picking one in another space switches space. | Same. | Needs to activate a tab in a hidden model. |
| Tab groups | Groups live inside one space (contiguity enforces it). | Groups nest under the space collection; needs new nesting rules. | Free. |
| Split view | Split collections live inside one space. | As groups. | Free. |
| Drag between windows | Tab keeps its container, so its space; the target window shows that space because the dropped tab becomes active. | Same. | Needs a target model choice. |
| Closing the last tab of a space | Model stays non-empty; needs a policy (open a new tab page or switch space). | Same. | Model becomes empty, and an empty model closes the window today. |
| New tab placement | `DetermineInsertionIndex` (`tab_strip_model.cc:6097-6141`) plus a clamp into the space's block. | Insert into the space collection. | Free. |
| Session restore | Free: tabs restore into containers (R1), active tab restores the visible space. | Needs a new session command for the space level. | Needs several models per session window: new session format. |
| Tab sync | One synced window with all tabs (unverified whether Helium syncs sessions at all). | Same. | Several windows or a format change. |
| Size of change | ~30 hook lines in model and commands, view predicates. | Thousands of lines in `components/tabs`, `TabStripModel` and views; deep conflict exposure on every Chromium release. | Very large; touches most of `chrome/browser/ui`. |

**Recommendation: (a).** It reuses a filter pattern Chromium already maintains, keeps every
model invariant, and costs the fewest hook lines. (b) is the "clean" model but rewrites code
Chromium is still actively migrating to collections. (c) fights the `Browser` design.

### Details of the flat model

**A tab's space.** `space_of(tab) = SpaceForContainer(GetTabContainerId(tab->GetContents()))`,
with `""` (default partition) mapping to the default space. No per-tab state, no session hook.
R1 makes container persistence the containers layer's job: it encodes the container into the
saved URL (`containers-port.md`, "How session restore keeps the container"), so it survives the
session file rebuild that drops tab extra data (see "Persistence").

**Contiguity.** Within each of the pinned and unpinned collections, tabs of one space form one
block. Block order is irrelevant (never shown side by side) and follows space creation order.
Benefits: a space-relative index is `block_start + i`, shift-click range selection never spans
hidden tabs, "close tabs to the right" stops at the block end, keyboard moves clamp at the block
edge. Enforcement, all in Neon code called from one-line hooks:

- `TabStripModel::ConstrainInsertionIndex` and `ConstrainMoveIndex` (`tab_strip_model.cc:3975-3984`)
  call `neon::spaces::ConstrainIndex(model, tab_or_space, index, pinned)` to clamp into the block.
  Both are private and take only `(index, pinned)`; the hook needs the space of the tab being
  inserted, which the caller (`InsertTabAtIndexImpl`, `tab_strip_model.cc:5215`) has. If that
  makes the hook too wide, an observer repairs order after the fact instead (one extra
  `onMoved` event per repair). Pin and unpin use their own index math (`SetTabPinnedImpl`,
  `tab_strip_model.cc:5805-5835`), so they are repaired by the observer either way.
- Groups and splits are single blocks already, so a group or split cannot straddle spaces as long
  as its tabs share a container. Creation from the UI only sees one space. Drags from another
  window and extension calls (`chrome.tabs.group`) can mix: the window controller ungroups the
  foreign tabs (unverified how often this happens in practice).

**Visible space.** Each window has a `SpacesWindowController` holding the visible space id. It
observes the model:

- When the active tab changes to a tab of another space, the visible space follows (tab search,
  extensions, links from other apps, cross-window drags, session restore).
- Switching space by command or gesture activates the space's most recently active tab, chosen
  by `WebContents::GetLastActiveTimeTicks()` (persisted, `session_service_base.cc:634-636`).
- Switching to an empty space opens a new tab page in its container.

**Closing the last tab of a space.** The model is still non-empty, and Chromium always needs an
active tab when the model has tabs. Recommended default (open question 1): open a new tab page in
the same space, as Arc keeps you in the space; if that tab is itself a fresh new tab page, close
it and switch to the previous space, and close the window only when every space is empty.

**New tab placement.** `DetermineInsertionIndex` (`tab_strip_model.cc:6097-6141`) puts link tabs
next to the opener and Ctrl+T tabs at `count()`. Both work after the clamp: opener tabs are
already in the space's block, and `count()` clamps to the block's end. Helium's
`open-new-tabs-next-to-active-tab-option.patch` inserts after the active tab, also inside the block.

## 2. The sidebar

### How the vertical strip is built (with Helium's patches)

- `BrowserView` creates `VerticalTabStripRegionView` once and calls `InitializeTabStrip()` on it
  when vertical mode is on (`views/frame/browser_view.cc:1118`, `1791-1830`; Helium's
  `vertical.patch` changes `OnVerticalTabStripModeChanged` to only relayout when right alignment
  changes).
- The region view (`views/frame/vertical_tab_strip_region_view.cc:125-221`) adds a vertical
  `FlexLayoutView` content area (`:153`) holding the bottom container (`:163`, new tab button,
  Helium's `VerticalNewTabButton`), a gemini placeholder (`:178`), and a `ResizeArea` (`:184`).
  Helium removed the top container and separator (`patches/helium/ui/layout/vertical.patch:590-614`).
- `BaseTabStripRegionView::InitializeTabStrip` (`views/frame/base_tab_strip_region_view.cc:78-120`)
  creates the hover card controller, `RootTabCollectionNode`, `TabDragHandler` and
  `TabStripCollectionController`, then `root_node_->Init()`, which calls
  `model->SetTabStripUI(this)` and hands the new `TabStripView` to `SetTabStripView`
  (`:425-439`) and then to the virtual `AddTabStripView`.
- `VerticalTabStripRegionView::AddTabStripView` (`vertical_tab_strip_region_view.cc:864-900`)
  inserts the tab strip view into the content area before the bottom container with a flex
  specification.
- `TabStripView` (`views/tabs/common/tab_strip_view.cc`) holds two `views::ScrollView`s, pinned
  and unpinned, both with `ScrollWithLayers::kEnabled` (`tab_strip_view.cc:177-190`), laid out by
  `TabStripViewLayout`. Helium removes the separator between them (`vertical.patch:1597-1680`).
- Collapsed and expand-on-hover: `VerticalTabStripStateController` holds the collapse state;
  the region view animates width through `BrowserAnimationController`
  (`TabStripAnimations::kVerticalTabStrip`), and expand-on-hover floats the strip over the page
  with a shadow (`vertical_tab_strip_region_view.cc:997-1240`). Collapsed width comes from
  Helium's `GetVerticalTabStripCollapsedWidth()` (`vertical.patch:714`).
- Right alignment: Helium pref `helium.browser.vertical_right_aligned`
  (`patches/helium/ui/layout/core.patch:233-238`); the region view mirrors the resize area and
  resize direction (`vertical.patch:766-818`).
- Zen mode: Helium's `zen-mode.patch` adds `SetFloatingStyleForLayout` (attached or detached
  floating, rounded corners, clipping) and a top margin that follows the top chrome reveal
  (`patches/helium/ui/layout/zen-mode.patch`, region view hunks).
- Native materials (`native-frame-materials.patch`, and #2898) paint the region background and
  blur the page under the floating strip, so anything Neon puts in the strip must not paint an
  opaque background.

### Where the pager goes

```
VerticalTabStripRegionView
  content_area_view_ (vertical FlexLayout)
    SpacesPagerView            <- replaces the bare TabStripView, same flex spec and margins
      PagesViewport (clips, paints to a layer)
        page[prev]  TabStripView of a peek root, other spaces hidden
        page[cur]   the real TabStripView from BaseTabStripRegionView
        page[next]  TabStripView of a peek root
      SpaceSwitcherRow         <- space icons, current highlighted
    VerticalTabStripBottomContainer (Helium's new tab button)
    gemini placeholder
  ResizeArea, ShadowFrameView (unchanged)
```

**Hook point.** `BaseTabStripRegionView::SetTabStripView` (`base_tab_strip_region_view.cc:425`)
gets one line for vertical orientation:
`view = neon::spaces::WrapTabStripView(this, std::move(view));`, which returns the pager owning
the tab strip view. `tab_strip_view_` still points at the real `TabStripView`, and the existing
`CHECK(Contains(tab_strip_view_))` holds. `ClearTabStripView` (`:441-452`) gets the matching
line to remove the pager. This keeps the Helium-patched region view free of Neon hooks: no Helium
patch touches `base_tab_strip_region_view.cc` (#2941 does, in a different function).

**Peek pages.** A page for a non-visible space is its own `RootTabCollectionNode`,
`TabStripCollectionController` (no hover card controller) and `TabDragHandler`, created by Neon
the same way `InitializeTabStrip` does (`base_tab_strip_region_view.cc:78-120`), with input
disabled (`SetCanProcessEventsWithinSubtree(false)`). Each page is a full mirror of the model;
tabs outside the page's space are hidden by the layout predicate (section "Hiding tabs"). The
cost is up to three mirrors of every tab view; pages are created for the previous and next
space only, and torn down when the space list changes.

`RootTabCollectionNode::Init` (`root_tab_collection_node.cc:52-56`) always calls
`SetTabStripUI`, which `CHECK`s that no UI was set (`tab_strip_model.cc:357-373`, not fatal
until M162). Peek roots need a second mode that uses `AddObserver` instead: a defaulted
parameter on `Init`, about four hook lines in a file Helium does not patch (#2941 does).

**Committing a page turn.** When a swipe or command finishes, nothing is rebuilt. The real page
and the peek pages swap the space they show: the predicate changes, layouts invalidate, the
pager's transform resets to zero in the same frame. The real `TabStripView` stays where
`BrowserView` and `BaseTabStripRegionView` expect it (hover cards, `GetTabAnchorView`, drag,
accessibility). Per-space unpinned scroll offsets are stored in the pager and applied on rebind.

**Fallback if peek roots prove fragile.** Render peek pages with a lightweight Neon list
(favicon and title rows bound to the model through `TabStripModelObserver`). Less risk, but it
re-implements Helium's tab styling, which `PLAN.md` decision 6 asks us not to do. Keep this as
plan B for M3.

### Hiding tabs

| Container | Hook | Precedent |
|---|---|---|
| Unpinned | `UnpinnedTabContainerView::LayoutManager::IsChildVisibleInContainer` (`unpinned_tab_container_view.cc:150-166`): one line `if (!neon::spaces::IsChildInPageSpace(collection_node_, child)) return false;` | Focus mode uses the same function. |
| Unpinned minimum size | `UnpinnedTabContainerViewVerticalLayout::GetMinimumSize` counts children (`unpinned_tab_container_view_vertical_layout.cc:41-53`): one line. | Same loop handles focus mode. |
| Pinned | `PinnedTabContainerViewVerticalLayout::CalculateProposedLayout` takes all children (`pinned_tab_container_view_vertical_layout.cc:52-55`) and computes rows from their count (`:64-104`). Hidden children must be removed from the count and laid out invisible: about five lines. | None. #2939 replaces this layout with `helium_pinned_tab_container_view_vertical_layout.cc`, so this hook moves when it lands. |

The predicate resolves a child view to its tab: a `TabView`, or the first tab of a
`TabGroupView` or `SplitTabView`. Each page's space is looked up from its
`TabStripCollectionController` in a Neon-side map, so no Chromium class gains a field.

### The switcher row

`SpaceSwitcherRow` (Neon view) shows one icon button per space, current space highlighted with
its colour, and a "+" button. It sits inside the pager view at the bottom, so it needs no hook.
Collapsed strip: the row shows only the current space icon, which opens a menu of spaces.
Expand-on-hover: the row expands with the strip. Right-aligned: unchanged. RTL: icon order
mirrors and so does the swipe direction. Zen mode: the row hides with the sidebar.

## 3. Animation and gesture

### Event paths

| Platform | What arrives | Begin | Update | Finger lift | Momentum |
|---|---|---|---|---|---|
| Linux, Wayland, touchpad | `ui::ScrollEvent` `kScroll` from `WL_POINTER_AXIS_SOURCE_FINGER` (`ui/ozone/platform/wayland/host/wayland_event_source.cc:1243-1262`) | First event after idle, or `kScrollFlingCancel` from a hold gesture (`:940-975`) | `kScroll` deltas, no phase set | `kScrollFlingStart` with velocity when `axis_stop` arrives with zero deltas (`:1195-1208`) | None from the compositor; Chromium synthesizes flings for web content only |
| Linux, Wayland, mouse wheel | `MouseWheelEvent` (`:1213-1233`) | none | discrete deltas | none | none |
| Linux, X11, touchpad | `kScroll` if the XInput device type is `TOUCHPAD` (`ui/events/x/events_x_utils.cc:446-458`, `ui/events/devices/x11/device_data_manager_x11.cc:196-204`), otherwise `kMousewheel` | none | deltas, no phase | none; fling data only from ChromeOS CMT drivers (`device_data_manager_x11.cc:574-594`) | none |
| macOS, trackpad | `ui::ScrollEvent` built from the `NSEvent` (`components/remote_cocoa/app_shim/bridged_content_view.mm:1043-1054`) | `momentum_phase() == MAY_BEGIN` (`ui/events/cocoa/events_mac.mm:240-254`) | `momentum_phase() == NONE` | `END` while not in momentum (`:256-258`) | `INERTIAL_UPDATE`, then `END` (`:259-262`) |

Two caveats from the source:

- `ScrollEvent::scroll_event_phase()` is never filled from native events on any platform
  (`ui/events/event.cc:1204-1206`, a TODO). Chromium's focus swipe controller tests it for
  `kBegan` and `kEnd` on macOS (`vertical_tab_strip_focus_swipe_controller.cc:73-84`); those
  branches look dead. Neon must use `momentum_phase()` on macOS and event types on Wayland.
- On macOS both "fingers lifted" and "momentum finished" map to `END` (`events_mac.mm:256-258`).
  They are told apart by order: an `END` before any `INERTIAL_UPDATE` is the lift.

**Delivery into Views.** The widget sends the `ScrollEvent` through the views event sink first,
and only converts an unhandled `kScroll` into a `MouseWheelEvent` afterwards
(`ui/views/widget/widget.cc:2445-2454`). A pre-target handler on the region view therefore sees
every `ScrollEvent` before the tab list's `ScrollView` (`ui/views/controls/scroll_view.cc:1063-1100`,
which forwards to the compositor scroller and marks the event handled). Upstream's controller uses
`OnMouseEvent` on Linux and Windows (`vertical_tab_strip_focus_swipe_controller.cc:28-39`), which
only works because the vertical list leaves horizontal deltas unhandled; Neon handles
`OnScrollEvent` on all platforms and `OnMouseEvent` for wheel-only devices.

### The pager's gesture handler

`SpacesSwipeHandler` (shared Views code) is a `ui::EventHandler` installed as a pre-target
handler on the region view by the pager when it is added to a widget (no hook). It reuses the
upstream state machine and constants (`kAxisLockThreshold = 15`,
`vertical_tab_strip_focus_swipe_controller.h:20-23`):

1. **Idle.** Accumulate `x`, `y`. When `|y| >= 15` and `|y| > |x|`, lock to vertical passthrough:
   never touch events again in this gesture, so the tab list scrolls normally. When `|x| >= 15`
   and `|x| > |y|`, lock horizontal.
2. **Tracking.** Mark each event handled and stop propagation, so the `ScrollView` and the
   wheel conversion never see it. Set the pager offset to the accumulated `x` (layer transform
   only, no relayout) and record `(time, x)` samples for velocity. Pages past the first and last
   space rubber-band at a third of the finger distance.
3. **Release.** On a lift signal (macOS `END` before momentum, Wayland `kScrollFlingStart`) or,
   without one, after 120 ms with no events, decide the target: next page if `|offset| > 1/3`
   of the page width or the release velocity exceeds a threshold in the same direction, else
   snap back. Animate with `gfx::LinearAnimation` and an ease-out tween from the current offset,
   duration scaled by remaining distance. Consume trailing momentum events (`INERTIAL_UPDATE`,
   `END`) until they stop, as upstream does (`:59-71`).
4. **Commit.** At the end of the animation, activate the target space's last active tab and
   rebind pages (section 2).

**History swipe.** On macOS, back and forward swipes are handled inside the web contents view,
not by Views, so a gesture that starts over the sidebar never reaches it, and one that starts
over the page never reaches the pager. Over the floating expand-on-hover or zen strip, the
strip is a Views child above the contents; whether AppKit routes the scroll to the browser
widget or the web contents `NSView` there is unverified and must be tested on a Mac. On Linux
there is no touchpad history swipe in Chromium (overscroll history navigation is ChromeOS and
Windows touch only, unverified for Helium's Linux builds).

**Fallback without phase data** (X11, mouse wheels, Windows precision touchpads delivering
wheel events). Finger tracking still works from deltas; the end of the gesture is the 120 ms
quiet timeout. A discrete mouse wheel (no fractional deltas, `MouseWheelEvent` with 120-unit
steps) or Shift+wheel turns pages one at a time with the snap animation, no tracking.

**Conflict with focus mode.** If `kTabGroupsFocusing` is ever enabled, both handlers would
compete. Neon's handler returns early when the model has a focused group, and focus mode stays
usable from menus.

### Shared code and macOS-only code

| Piece | Where |
|---|---|
| Pager view, pages, animation, switcher row | Shared Views code in `patches/neon/spaces/` |
| `SpacesSwipeHandler` state machine, momentum-phase handling, timeout fallback | Shared; macOS phases arrive through cross-platform `ui::ScrollEvent` fields |
| Reading the raw `NSEvent` phase, `-[NSEvent trackSwipeEventWithOptions:]` for native rubber-band feel, honouring the "swipe between pages" system setting | `neon-macos`, optional M6 refinement. The `ui::ScrollEvent` keeps the native event (`event.cc:1195-1196`), so a small `.mm` helper can read it without touching shared code. |
| Per-space glass tint | `neon-macos`, M6 |

## 4. Switching semantics

Switching space is `model->ActivateTab(last_active_tab_of_space)`, so it inherits tab-switch
behaviour:

- **Active tab.** Each space remembers its last active tab implicitly through last active time.
  The window has one active tab, the visible space's.
- **Focus.** `BrowserView::OnActiveTabChanged` stores focus on the old contents and restores it
  on the new (`views/frame/browser_view.cc:2650-2790`, `StoreFocus` at `:2664`, `RestoreFocus`
  at `:2790`). If the page had focus, the page in the new space gets it back; if the omnibox had
  focus with a half-typed query, the omnibox state is per tab and comes back when returning.
- **Omnibox.** Shows the new active tab's URL; the per-tab edit state is saved and restored by
  the location bar on tab switch (standard Chromium, not re-read here).
- **Split views.** A split is a collection inside one space. Switching away hides it and
  `MultiContentsView` shows the new active tab, which may itself be a split. Nothing new.
- **Picture-in-picture.** A video or document PiP window belongs to its tab and keeps playing
  when the tab is backgrounded, as with any tab switch. Whether Helium enables automatic PiP on
  tab switch is unverified; if it does, switching space triggers it too.
- **Links from other apps.** They reach `StartupBrowserCreatorImpl::OpenURLsInBrowser`
  (`ui/startup/startup_browser_creator_impl.cc:311`) and `Navigate`. The new tab opens in the
  visible space of the last active window by default. With M5 URL rules, a matching rule picks
  the space first; the window then switches to it because the new tab is active.
- **New window (Ctrl+N).** Opens a window on the same space list, showing the space that was
  visible in the source window, with a new tab page in it. Spaces are per profile; tabs are per
  window.
- **Move tab to another space.** A tab's container cannot change. "Move" therefore opens a new
  tab in the target container with the same navigation history (built like a restored tab from
  `SerializedNavigationEntry`s, `browser_tabrestore.cc:55-91`, with the target container key set
  as R1's restore path does), then closes the original. The page reloads in the other container
  and is signed in as that space; form state and scroll position are lost. The menu label should
  say "Reopen in space ...".
- **Incognito and guest.** No spaces: R1's containers service exists for regular profiles only
  (`containers-port.md`, "Where the container lives"). Incognito windows show the plain strip.
- **Drag a tab to another window.** It keeps its space; the target window switches to that space
  because the dropped tab becomes active. Dragging a tab out into a new window opens that window
  on the tab's space.
- **Extensions.** `chrome.tabs.create` without an opener goes through `Navigate` and lands in
  the visible space. Activating a tab in another space switches space. R1 notes that extension
  tabs land outside containers in Brave; Neon's new-tab rule (patch 4) puts them in the visible
  space's container instead, which is better for password managers but should be tested.

## 5. Persistence

| State | Where | How it comes back |
|---|---|---|
| Space list: id, name, icon, colour, order, container id | Profile pref `neon.spaces.list` (list of dicts, list order is display order), registered by the keyed service factory. The default space has `container_id = ""`. | Read at service creation. Reconciled against `ListContainers` and its observer: a missing container marks the space broken, a new container (created outside Neon) is not auto-added. |
| A tab's space | Not stored. Derived from the tab's container. | R1 restores the container through the URL-prefixed navigation entries (`containers-port.md`, "How session restore keeps the container"). |
| Visible space per window | Not stored. Derived from the restored active tab (`CreateSetSelectedTabInWindowCommand`, `session_service_base.cc:726-730`). | Free. |
| Last active tab per space | Not stored. Derived from last active time (`CreateLastActiveTimeCommand`, `session_service_base.cc:634-636`). | Free. |
| Per-space sidebar scroll offset | Pager memory only. | Not restored (acceptable). |

**Why not session extra data.** `SessionService::AddTabExtraData` and `AddWindowExtraData`
exist (`chrome/browser/sessions/session_service.cc:365-385`), and restored tabs receive the map
(`browser_tabrestore.cc:64-91`). But when the session file is rebuilt,
`SessionServiceBase::BuildCommandsForTab` (`session_service_base.cc:605-683`) re-emits
navigations, index, pinned state and last active time, and no tab extra data. Window extra data
is re-emitted only for the keys Chromium knows (focused group and vertical strip state,
`session_service_base.cc:766-790`). Upstream's #2931 hits this and adds a hook in
`BuildCommandsForTab` to re-emit its pin keys (`pr2931.diff:1013-1035`). Deriving everything
from containers and active tab avoids that hook entirely. If a future need arises (for example a
"space of a tab in the default partition that is not the default space"), the hook to copy is
#2931's.

**Restore order.** Restored tabs are created by Helium's lazy session restore
(`patches/helium/core/lazy-session-restore.patch`) unloaded; R1 keeps the container on a
never-loaded entry (`NavigationEntryImpl::GetStoragePartitionKeyToRestore`), so a lazily restored
tab stays in its space across a second save. The window controller starts observing before the
first tab is inserted (it is created by the service on `OnBrowserCreated`).

## 6. Pinned tabs

### How #2931 stores persistent pins

- Per-tab state: `HeliumPinsTabController` (tab feature, `pr2931.diff:577-831`) holds `pin_id_`
  (UUID) and `pinned_url_`, mirrored into session tab extra data under `helium.pinned_tab_id`
  and `helium.pinned_tab_url`, and stashed across restore through `RestoredPinState`
  (`:690-760`). It is created for every tab in `TabFeatures::Init` (`:963-971`).
- Per-profile list: `HeliumPinsService` (keyed service, `pr2931.diff:1063-1700`) keeps
  `std::vector<Pin{id, url}>` (later with split fields) in `prefs::kPinnedTabs`, the same list
  `PinnedTabCodec` uses, as dicts `{url, id}` (`LoadPins`/`SavePins`, `:1404-1432`). It writes
  immediately with `CommitPendingWrite`.
- Sync across windows: every normal window of the profile has one tab per pin.
  `Reconcile(browser)` (`:1587-1660`) matches pinned tabs to pins by id (or by URL for pins
  without id), closes duplicates, unpins tabs whose pin was removed, and inserts missing pins as
  hibernated tabs with `chrome::AddRestoredTab` (`InsertHibernatedPin`, `:1676-1705`).
- Closing a pin hibernates it through a new `TabStripModelDelegate::HandleUserClosingTabs`
  (`pr2931.diff:3050-3112`). Links to other origins open in a new tab
  (`HeliumPinsNavigationThrottle`, `:464-573`, `2114-2245`).

### Smallest change to make pins per space

1. `Pin` gains `space_id` (equivalently the container id), saved as one more key in each
   `kPinnedTabs` dict. R1 already writes `"container_id"` into the same dicts through
   `PinnedTabCodec` (`containers-port.md`, pinned tabs row); #2931's `SavePins` rebuilds the
   list with only `url` and `id`, which would drop it. The Neon delta must write the container
   id under R1's key so both readers agree.
2. `GetPinnedTabControllers(browser)` (`pr2931.diff:1247-1259`) and `Reconcile` iterate per
   `(browser, space)`: pins of a space are matched against pinned tabs whose container is that
   space's, and `next_index` runs inside the space's pinned block.
3. `InsertHibernatedPin` builds a `SerializedNavigationEntry` by hand (`pr2931.diff:1679-1700`);
   it must also set R1's storage partition key on it so `CreateRestoredTab` puts the tab in the
   space's container.
4. URL matching for id-less pins (startup via `PinnedTabCodec`, extensions) also compares the
   container.

That is about 150 lines, all inside Helium's new `helium_pins_*` files, so it is a patch on top
of Helium's patch: exposed to every #2931 revision, but contained. It should be written only
after #2931 lands.

### If #2931 never lands

Per-space pins already work from M2: pinned tabs are ordinary pinned tabs in the model, the
filter shows each space its own, and session restore keeps them (pinned state and container).
What is missing is Arc's behaviour (pins survive closing every window, closing a pin hibernates
it, "return to pinned URL"). Options, in order of preference: keep waiting and live without it;
carry a copy of #2931's patches under `patches/neon/` with the per-space delta folded in (it is
GPL-3.0, same as Neon); or write a smaller Neon version storing pins per space in our own pref.
Recommended default: wait until M5 starts, then decide (open question 5).

### Interaction with #2939 and #2941

#2939 replaces the vertical pinned layout with `helium_pinned_tab_container_view_vertical_layout.cc`
(balanced rows, outlined container). The strip-filter hook (patch 5) moves into that file when
it lands. #2941 (pin by dragging) changes `TabDragHandler`, `RootTabCollectionNode` and
`BaseTabStripRegionView::...` drag paths; our pager hook in `SetTabStripView` and the `Init`
parameter sit in the same files, different functions.

## 7. Commands, shortcuts and settings

### How Helium does it

- **Command ids**: `chrome/app/chrome_command_ids.h`, Helium uses the 34080-34091 block
  (`chrome_command_ids.h:106-115`).
- **Dispatch**: a `case` in `BrowserCommandController::ExecuteCommandWithDisposition`
  (`browser_command_controller.cc`, for example `IDC_CTRL_S_SHORTCUT` in
  `patches/helium/ui/layout/shortcuts.patch:74-88`) and enablement in `InitCommandState`
  (`browser_command_controller.cc:1887`) or a pref-driven updater.
- **Default accelerators**: `accelerator_table.cc` (Linux, Windows), `global_keyboard_shortcuts_mac.mm`
  and `ui/cocoa/accelerators_cocoa.mm` (macOS menus), `chrome_dll.rc` (Windows),
  `BrowserNativeWidgetMac::ValidateUserInterfaceItem` (`browser_native_widget_mac.mm`) for mac
  menu validation (`shortcuts.patch:105-176`).
- **Custom shortcuts**: Helium's `custom-keyboard-shortcuts.patch` builds the editable list from
  `GetAcceleratorList()` (`browser_shortcuts/browser_shortcut_service.cc:481`) and requires a label
  for every command with a default accelerator; a missing label hits
  `DCHECK(false) << "Missing browser shortcut label"` (`browser_shortcuts/browser_shortcut_metadata.cc:300`).
  Commands without a default accelerator are listed in `GetBrowserShortcutCommandsWithoutDefault`
  (`:305`).
- **Settings toggles**: pref registered in `browser_ui_prefs.cc`, name in `pref_names.h`,
  allowlisted in `extensions/api/settings_private/prefs_util.cc`, string in
  `settings_strings.grdp` and `settings_localized_strings_provider.cc`, a `settings-toggle-button`
  in a page (`tab-cycling-mru.patch:1-120`).
- **Settings subpage with a backend**: `helium/settings/custom-keyboard-shortcuts-page.patch`
  (18 files: page, mojom, handler, `settings_ui.cc`, `route.ts`, `router.ts`, BUILD files,
  interface binders). Helium's layout subpage under Appearance (`helium/ui/layout/settings.patch`)
  is the pattern for a prefs-only subpage.

### What Neon adds

| Command | Id (new block, 39000+) | Default (Linux, Windows) | Default (macOS) |
|---|---|---|---|
| Next space | `IDC_NEON_NEXT_SPACE` | Ctrl+Alt+Down | Ctrl+Cmd+Right |
| Previous space | `IDC_NEON_PREV_SPACE` | Ctrl+Alt+Up | Ctrl+Cmd+Left |
| Space 1..9 | `IDC_NEON_SPACE_0..8` | Alt+Shift+1..9 | Ctrl+1..9 (Arc) |
| New space | `IDC_NEON_NEW_SPACE` | none | none |
| Reopen tab in space... | `IDC_NEON_MOVE_TAB_TO_SPACE` | none | none |

The defaults are proposals: collisions with desktop environments (GNOME, KDE workspace keys) and
with macOS Mission Control "switch to desktop N" were not checked (open question 6). Using a new
id block keeps our lines out of Helium's hunks in `chrome_command_ids.h`.

Dispatch goes through one hook at the top of `BrowserCommandController::ExecuteCommandWithDisposition`
calling `neon::spaces::ExecuteCommand(browser_, id)`, plus one in `InitCommandState`, instead of
cases in Helium's busy switch. Labels go into `browser_shortcut_metadata.cc` (one `case` block
returning a Neon string id).

Settings (M4): a "Spaces" subpage under Appearance, next to Helium's layout page, listing
spaces with rename, icon, colour, reorder and delete, plus toggles ("show switcher row",
"swipe to switch spaces", "empty-space behaviour"). It needs a backend to create and delete
containers, so it follows the shortcuts-page pattern with a `WebUIMessageHandler` registered in
`settings_ui.cc`; a mojo interface is not required.

## 8. Conflict exposure

Helium patches touching each file we would hook, from the series index, with churn of the most
active of them.

| File we hook | Patch | Helium and other patches already modifying it | Churn of busiest (180 d, all / feature) |
|---|---|---|---|
| `chrome/browser/ui/tabs/tab_strip_model.cc` | 3 | ungoogled `add-flag-to-close-window-with-last-tab`, brave `tab-cycling-mru-impl`, helium `open-new-tabs-next-to-active-tab-option`, `tab-cycling-mru`, `hibernate-tab-context-menu`, `close-tabs-to-left`, `copy-url-tab-context-menu`, `mute-tab-context-menu`; #2931 adds two more | `copy-url-tab-context-menu` 16 / 4 |
| `chrome/browser/ui/browser_commands.cc` | 3 | ungoogled `enable-paste-and-go-new-tab-button`, helium `override-chrome-protocol`, `copy-url-tab-context-menu`, `force-paste-action`, `layout/context-menu` | `layout/context-menu` 18 / 6 |
| `chrome/browser/ui/navigator/browser_navigator.cc` | 4 | helium `override-chrome-protocol`, `layout/zen-mode`; R1 hooks `CreateTargetContents` too | `zen-mode` 53 / 40 (other hunks) |
| `views/tabs/common/unpinned_tab_container_view.cc`, `unpinned_tab_container_view_vertical_layout.cc` | 5 | none | n/a |
| `views/tabs/common/pinned_tab_container_view_vertical_layout.cc` | 5 | none today; #2939 replaces it | n/a |
| `chrome/app/chrome_command_ids.h` | 6 | `services-schema-nag`, `keyboard-shortcuts`, `force-paste-action`, `layout/context-menu`, `layout/shortcuts`, `layout/zen-mode-wiring` | `zen-mode-wiring` 35 / 21 |
| `chrome/browser/ui/browser_command_controller.cc` | 6 | ungoogled `remove-unused-preferences-fields`, helium `services-schema-nag`, `keyboard-shortcuts`, `force-paste-action`, `layout/context-menu`, `layout/shortcuts`, `layout/zen-mode-wiring`, `layout/zen-mode` | `zen-mode` 53 / 40 |
| `chrome/browser/ui/accelerator_table.cc` | 6 | ungoogled `remove-f1-shortcut`, helium `keyboard-shortcuts`, `layout/shortcuts`, `layout/zen-mode-wiring` | `zen-mode-wiring` 35 / 21 |
| `chrome/browser/global_keyboard_shortcuts_mac.mm`, `ui/cocoa/accelerators_cocoa.mm` | 6 | `keyboard-shortcuts`, `custom-keyboard-shortcuts`, `layout/shortcuts`, `layout/zen-mode-wiring` | `zen-mode-wiring` 35 / 21 |
| `chrome/browser/ui/browser_shortcuts/browser_shortcut_metadata.cc` (Helium's own file) | 6 | `custom-keyboard-shortcuts`, `force-paste-action` | `custom-keyboard-shortcuts` 17 / 6 |
| `chrome/browser/profiles/chrome_browser_main_extra_parts_profiles.cc` | 1 | helium `custom-keyboard-shortcuts` and others (factory list) | 17 / 6 |
| `views/frame/base_tab_strip_region_view.cc` | 7 | none; #2941 touches drag code | n/a |
| `views/tabs/common/root_tab_collection_node.{h,cc}` | 7 | none; #2941 touches it | n/a |
| `chrome/browser/ui/tabs/tab_menu_model.cc` | 12 | `hibernate-tab-context-menu`, `close-tabs-to-left`, `copy-url-tab-context-menu`, `mute-tab-context-menu`, `layout/vertical` | `layout/vertical` 43 / 27 |
| `settings_ui.cc`, `route.ts`, `router.ts`, settings `BUILD.gn`, `settings_strings.grdp`, `settings_localized_strings_provider.cc` | 11 | up to 40 patches on the strings files; `route.ts` 7, `router.ts` 3 | `layout/settings` 23 / 11 |

Files deliberately not hooked, and why:

| Avoided file | Helium patches | Churn (180 d, all / feature) | Instead |
|---|---|---|---|
| `views/frame/vertical_tab_strip_region_view.{h,cc}` | `layout/vertical`, `layout/zen-mode`, `native-frame-materials`; #2898 too | 43 / 27, 53 / 40, 38 / 26 | Wrap in `BaseTabStripRegionView::SetTabStripView`; switcher row lives inside the pager; gesture handler installed from the pager |
| `views/frame/browser_view.{h,cc}` | 12 patches | `zen-mode` 53 / 40 | Window controller created by the keyed service from `BrowserCollectionObserver`, as #2931's service does |
| `browser_window/internal/browser_window_features.cc` | `tab-search-in-toolbar`, `layout/core` | 23 / 8 | Same as above |
| `chrome/browser/sessions/session_service_base.cc` | none, but #2931 hooks `BuildCommandsForTab` | n/a | Derive space from container |
| `views/tabs/vertical/vertical_tab_strip_bottom_container.cc` | `layout/vertical` | 43 / 27 | Row placed in the pager, above it |

Churn for Chromium-owned files themselves could not be measured: the tree is an unpacked tarball
without history.

## 9. Patch breakdown

All patches live in `patches/neon/spaces/`, after `patches/neon/containers/` in `series`. Each
milestone ends with a working browser.

### M2: spaces model, switching by command

1. **`service.patch`**. Space list and its pref.
   New: `chrome/browser/neon/spaces/{space.h, spaces_service.{h,cc}, spaces_service_factory.{h,cc}, spaces_prefs.{h,cc}, BUILD.gn}`.
   The service owns the list, maps container id to space, observes containers through R1's
   interface, observes `GlobalBrowserCollection` and creates one window controller per normal
   window. Seeds a default space (`container_id = ""`) on first run.
   Hooks: `chrome_browser_main_extra_parts_profiles.cc` (factory, 2 lines), `chrome/browser/BUILD.gn` (dep).
   Size: ~600 new, ~5 hook.
2. **`window-controller.patch`**. Visible space and switching.
   New: `chrome/browser/ui/neon/spaces/{spaces_window_controller.{h,cc}, space_tab_utils.{h,cc}}`.
   `SwitchToSpace`, follow-active-tab, last active tab per space, empty-space and last-tab
   policies, contiguity repair after pin and unpin.
   Hooks: none. Size: ~550 new.
3. **`model-filter.patch`**. Model and command predicates.
   New: `chrome/browser/ui/neon/spaces/space_tab_filter.{h,cc}`.
   Hooks (one or two lines each): `tab_strip_model.cc` at `SelectRelativeTab` (`:4654`),
   `SelectMRUTab` list builder (`:4601-4608`), `DetermineNewSelectedIndex` (`:6189`),
   close-tabs command (`:4088`), `MoveTabRelative` (`:4719`), `ConstrainInsertionIndex` and
   `ConstrainMoveIndex` (`:3975-3984`); `browser_commands.cc` `IsTabSelectable` (`:599`).
   Size: ~150 new, ~30 hook.
4. **`new-tab-space.patch`**. Ctrl+T, new tab button, omnibox new tab, bookmarks, extensions and
   external links open in the visible space's container.
   Hooks: `CreateTargetContents` (`navigator/browser_navigator.cc:631-670`), one line asking
   `neon::spaces::GetContainerForNewTab(params)` when no opener or source site instance is set.
   R1 hooks the same function; coordinate so there is one hunk (R1's
   `GetStoragePartitionConfigToInherit` can consult the spaces rule).
   Size: ~80 new, ~5 hook.
5. **`strip-filter.patch`**. Hide other spaces' tabs.
   New: `chrome/browser/ui/views/neon/spaces/space_view_filter.{h,cc}`.
   Hooks: `unpinned_tab_container_view.cc:150`, `unpinned_tab_container_view_vertical_layout.cc:41`,
   `pinned_tab_container_view_vertical_layout.cc:52-104`; horizontal layouts are left alone
   (spaces require vertical layout; in other layouts all tabs show, open question 7).
   Size: ~120 new, ~10 hook.
6. **`commands.patch`**. Commands and default shortcuts.
   New: `chrome/browser/ui/neon/spaces/space_commands.{h,cc}`, strings in a Neon `.grdp`.
   Hooks: `chrome_command_ids.h`, `browser_command_controller.cc` (2 lines),
   `accelerator_table.cc`, `browser_shortcut_metadata.cc`, macOS: `global_keyboard_shortcuts_mac.mm`,
   `accelerators_cocoa.mm`, `browser_native_widget_mac.mm`; `generated_resources.grd` (include
   the `.grdp`).
   Size: ~150 new, ~60 hook.

Done when: with two spaces, the sidebar shows only the visible space's tabs, the shortcuts
switch spaces, new tabs open in the visible space's container, and a restart restores both.

### M3: pager, switcher row, animation, gesture

7. **`pager.patch`**. `SpacesPagerView` and peek pages.
   New: `chrome/browser/ui/views/neon/spaces/{spaces_pager_view.{h,cc}, space_page.{h,cc}}`.
   Hooks: `base_tab_strip_region_view.cc` `SetTabStripView`/`ClearTabStripView` (2 lines);
   `root_tab_collection_node.{h,cc}` `Init(bool register_as_tab_strip_ui = true)` (~4 lines);
   views `BUILD.gn` dep. Command-driven switches animate.
   Size: ~900 new, ~15 hook.
8. **`switcher-row.patch`**. Space icons row, click to switch, current highlighted, collapsed
   variant. New: `space_switcher_row.{h,cc}`. Hooks: none. Size: ~350 new.
9. **`swipe.patch`**. `SpacesSwipeHandler`, finger tracking, release decision, momentum
   swallowing, timeout fallback, discrete wheel paging, and a pref to turn swiping off.
   New: `spaces_swipe_handler.{h,cc}` and unit tests driving synthetic `ScrollEvent`s.
   Hooks: none. Size: ~450 new.

### M4: managing spaces

10. **`management-ui.patch`**. Context menu on the switcher row (rename, icon, colour, delete,
    new space), an edit bubble (name, emoji or vector icon, colour from a fixed palette), delete
    confirmation that closes the space's tabs and calls `DeleteContainer`, drag to reorder in the
    row. New files only. Size: ~800 new.
11. **`settings-page.patch`**. Spaces subpage under Appearance. New: page TS/HTML, a
    `SpacesHandler`. Hooks: `settings_ui.cc`, `route.ts`, `router.ts`, settings `BUILD.gn`,
    appearance page index, `settings_localized_strings_provider.cc` (or a Neon strings function
    called from it), `prefs_util.cc` for toggles. Size: ~700 new, ~60 hook.
12. **`move-to-space.patch`**. "Reopen in space" submenu in the tab context menu.
    New: `space_menu_model.{h,cc}`. Hooks: `tab_menu_model.cc` `Build` (2 lines),
    `components/tabs/public/tab_context_menu_command.h` (1 id; #2931 adds ids there too).
    Size: ~200 new, ~10 hook.

### M5: per-space pins and URL routing

13. **`pins.patch`**. Only if #2931 has landed: the per-space delta of section 6. Hooks into
    Helium's `helium_pins_service.cc` and `helium_pins_util.cc`. Size: ~150 hook.
14. **`url-rules.patch`**. Rules `{pattern, space}` in a pref; evaluated for new top-level tabs
    without an opener (external links, omnibox, bookmarks) in the same `CreateTargetContents`
    hook as patch 4, and optionally by a navigation throttle that reopens same-tab navigations
    in the rule's space (like #2931's open-links-outside throttle, `pr2931.diff:464-573`).
    Hooks: the existing patch 4 hunk, `chrome_content_browser_client_navigation_throttles.cc`
    (1 line, if the throttle is wanted). Size: ~500 new, ~5 hook.

### macOS (neon-macos, M6)

- **`spaces/mac-swipe.patch`**. Optional native swipe tracking from the raw `NSEvent`. New `.mm`
  helper; hook: none or one line in the shared handler behind `BUILDFLAG(IS_MAC)` through a
  Neon header. Size: ~200 new.

## 10. Open questions for the owner

| # | Question | Recommended default |
|---|---|---|
| 1 | Closing the last tab of a space: stay in the space with a new tab page, or switch to another space? And when does the window close? | Stay with a new tab page. Closing that untouched new tab page switches to the previous space. The window closes only when every space is empty. |
| 2 | Is the default space (default partition, today's logins) one of the spaces, or must every space have a container? | Yes: "perso" is the default partition, so existing logins and extensions' cookie access keep working there. |
| 3 | Should the space list sync between devices? (Brave's container list is a syncable pref.) | No. Local only until sync is looked at. |
| 4 | Do new windows show the same space as the source window, or a chosen "home" space? | Same space as the source window. |
| 5 | If #2931 has not landed when M5 starts: wait, fork it, or write our own? | Wait; per-space pins already work without persistence across closed windows. |
| 6 | Default shortcuts (table in section 7). | Arc's Ctrl+1..9 on macOS; Alt+Shift+1..9 and Ctrl+Alt+Up/Down elsewhere, after checking GNOME and KDE defaults. |
| 7 | Spaces outside the vertical layout (classic, compact, dynamic). | All tabs show, switcher hidden, commands still switch the active tab's space. Spaces are a vertical-layout feature. |
| 8 | "Move tab to space" loses page state and sign-in. Offer it, or only "open link in space"? | Offer it, labelled "Reopen in space", plus "Open link in space" in the link context menu later. |
| 9 | Swipe on the whole sidebar or only on the tab list? | Whole sidebar except while dragging tabs or resizing. |
| 10 | Space icon: emoji, Chromium vector icons, or both? | Emoji plus a small fixed set of vector icons; colour from a fixed palette that reads in light and dark themes. |
| 11 | Extensions see all spaces' tabs in one window. Acceptable? | Yes. |
| 12 | Tab groups across spaces when a group is dragged in from another window with mixed containers? | Ungroup tabs that do not belong to the receiving space's container. |

## 11. Not verified

- Nothing was built or run; all behaviour is read from source.
- Two or more `RootTabCollectionNode`s and `TabStripCollectionController`s on one model in one
  window: rendering, accessibility tree, hover state and the "UI observer first" ordering for
  peek pages.
- Drag and drop, link drop and keyboard focus traversal in a pinned container with hidden
  children.
- Whether hidden tab views are excluded from the accessibility tree in all paths (focus mode
  relies on layout visibility, not checked line by line).
- Whether AppKit delivers sidebar scrolls to the browser widget when the strip floats over web
  contents (expand on hover, zen mode).
- That libinput touchpads report the XInput device type `TOUCHPAD` on X11 (the classification
  code was read; real devices were not checked).
- Shortcut collisions with GNOME, KDE and macOS system shortcuts.
- Whether Helium's sync engine syncs open tabs (sessions); tab sync behaviour above assumes
  Chromium's model.
- Automatic picture-in-picture on tab switch in Helium.
- Omnibox per-tab state restore on tab switch (standard Chromium behaviour, not re-read in 158).
- R1's container persistence for every restore path (R1's own "Not verified" list applies, in
  particular saved tab groups).
- Churn of Chromium-owned files (no history in the unpacked tree).
