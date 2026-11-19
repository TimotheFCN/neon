# Containers port study (R1)

Study of Brave's containers implementation and how to carry it into Neon's patch queue. Written
2026-11-19 against Helium 0.22.1 (Chromium 158.0.8127.36). Nothing here has been built or run;
every claim is from reading source, and runtime behaviour that the source does not settle is
marked unverified.

## Summary

**Source used.** brave-core tag `v1.100.58`, commit `bd6d26d1bced03c17a98baa86d2cd3425d543d1b`
(2026-11-18), the newest tag on the `1.100.x` release branch. Its `package.json` pins Chromium
`158.0.8127.42`; `1.99.x` pins 157, so `1.100.x` is the first release branch on 158. Note that
`1.101.x` and `master` also pin `158.0.8127.42` today, and `master` already has about 950 changed
lines in the containers code since this tag (see "Upstream churn" below).

**Feasible: yes.** The core of Brave's containers is small and uses Chromium's own machinery: a
container is a `StoragePartitionConfig` with domain `"containers"` and the container UUID as
partition name, attached to the tab's `SiteInstance` through
`SiteInstance::CreateForFixedStoragePartition`. Chromium already keeps a fixed partition across
same-tab and cross-site navigation, redirects and opener popups. Brave adds the rest: inheriting
the partition for new tabs and `noopener` windows, and persisting it through session restore by
encoding the container id into the saved URL. Nothing in that path depends on Shields, AI chat,
Leo, sync or Brave's WebUI framework. Those only appear in UI, settings and fingerprinting
side features that M1 does not need.

**Size of M1.** About 3,000 lines of new files, almost all copied verbatim from Brave with
include paths rewritten, plus about 350 to 400 lines of hunks in 20 to 25 Chromium files. The
copied code is cheap; the hunks are the real cost and the maintenance surface. For comparison,
dvjn's whole Helium branch (including UI and a settings page, but no session persistence) is
3,550 lines across 10 patches.

**Main risks.**

1. Session persistence touches `content/`, `components/sessions/` and Blink `PageState`, and
   Brave's own open issues show restore paths it misses (saved tab groups, #58219; a startup
   report, #57068). Spaces live or die on restore.
2. Extensions cannot see or target containers. `chrome.cookies` always uses the default
   partition, and tabs opened by extensions land outside the container. This breaks some
   password manager and OAuth flows (Brave #57209, #60927, #54688).
3. Web push to a service worker inside a container is routed to the default partition by
   Chromium and Brave does nothing about it (unverified at runtime, but the routing code is
   unambiguous).
4. Permissions and site settings are shared across containers (one content settings map per
   profile), WebHID is unavailable in containers, and Chrome's site-data settings UI only shows
   the default partition.
5. Content-layer patches (`NavigationEntry`, `ContentBrowserClient`, `WebContentsImpl`) are
   the most fragile kind of patch on each Chromium bump.

**Recommended patch breakdown** (detail in "Proposed Neon patches"). In apply order, under
`patches/neon/containers/`:

| # | Patch | Milestone | Rough size |
|---|---|---|---|
| 1 | `core.patch` (service, prefs, mojom, feature) | M1 | ~1,500 new |
| 2 | `content-partition.patch` (partition utils, NavigationEntry key, noopener inherit) | M1 | ~620 new, ~70 hook |
| 3 | `session-persistence.patch` (URL prefix in session file and PageState) | M1 | ~130 hook |
| 4 | `browser-service.patch` (keyed service, delegate, tab tracker) | M1 | ~620 new, ~30 hook |
| 5 | `tab-partition.patch` (new tabs, restore, permissions) | M1 | ~110 hook |
| 6 | `command-line.patch` (`--container`, pinned tabs, startup tabs) | M1 (test harness) | ~300 new, ~40 hook |
| 7 | `spaces-interface.patch` (the seam spaces depends on) | M1 | ~150 new |
| 8 | `clear-browsing-data.patch` | M2 or M4 | ~70 new, ~20 hook |
| 9 | `zoom-and-flag.patch`, `sync.patch`, UI patches, Traffic Control | later or never | see below |

## Conventions in this document

- Brave paths are relative to the brave-core root at the commit above, for example
  `components/containers/core/browser/containers_service.cc:28`.
- `chromium/` paths are Chromium `158.0.8127.36`, read from the GitHub mirror tag of that name.
  The local tree at `~/projects/neon-linux/build/src` did not exist during this study.
- Helium patch paths are relative to `patches/` in this repo.
- Brave keeps Chromium changes in three forms: `chromium_src/` overrides (a file that is
  compiled instead of the Chromium file of the same path and usually `#include`s the original
  with macros around it), `patches/*.patch` (plain diffs), and since 2026 `rewrite/*.yaml`
  ("Plaster", regex substitutions documented in `docs/plaster.md`). The `patches/*.patch` files
  are generated from the yaml, so the patch files are the authoritative diff. Helium has none
  of the first and third mechanisms; everything becomes an ordinary quilt patch.

## 1. The mechanism

### Where the container lives

A tab's container is not stored as a separate field. It is the `StoragePartitionConfig` of the
tab's `SiteInstance`:

- Domain `"containers"` (`components/containers/content/browser/storage_partition_utils.h:25`).
- Name: the container id, a lowercase UUID v4 (`containers_settings_handler.cc:75`), or
  `t-<uuid>` for temporary containers (`temporary_container.cc:100-107`). Valid ids are ASCII
  alphanumerics and `-` (`storage_partition_utils.cc:34-39`).
- `in_memory` follows the profile's off-the-record state, but the service only exists for
  regular profiles (`browser/containers/containers_service_factory.cc:38-43`).
- On disk: `<profile>/Storage/ext/containers/<hex of first 6 bytes of SHA-256(id)>`
  (`browser/containers/containers_service_delegate.cc:46-56`), Chromium's standard layout for
  non-default partitions.

Reading a tab's container: `containers::GetContainerIdForWebContents()`
(`storage_partition_utils.cc:62-84`) reads the partition name from
`web_contents->GetSiteInstance()`, and falls back to `ContainersWebContentsUserData` (a
`WebContentsUserData` holding the id, `containers_web_contents_user_data.h:22-44`). The fallback
exists for discarded tabs, whose stub `WebContents` does not carry the right `SiteInstance` yet;
`ContainerTabTracker::OnDiscardContents` copies the id across
(`browser/containers/container_tab_tracker.cc:37-46`).

Two runtime copies of the id also exist for serialization:
`NavigationEntryImpl::storage_partition_key_to_restore_` (added by
`chromium_src/content/browser/renderer_host/navigation_entry_impl.h:14-27`) and
`SerializedNavigationEntry::storage_partition_key_` plus `virtual_url_prefix_`
(`chromium_src/components/sessions/core/serialized_navigation_entry.h:32-57`).

### Path by path

| Path | What keeps the partition | Brave change? |
|---|---|---|
| Opening a tab in a container | Caller sets `NavigateParams::storage_partition_config` (new field, `patches/chrome-browser-ui-navigator-browser_navigator_params.h.patch`). `CreateTargetContents` passes it to `tab_util::GetSiteInstanceForNewTab`, which calls `SiteInstance::CreateForFixedStoragePartition` (`chromium_src/chrome/browser/tab_contents/tab_util.cc:15-25`, `chromium_src/content/browser/site_instance_impl.cc:15-26`). Example caller: `brave::OpenUrlInContainer`, `browser/ui/browser_commands.cc:1282-1305`. | Yes |
| Same-tab navigation, same site | Same `SiteInstance`. | No, Chromium |
| Same-tab cross-site navigation (BrowsingInstance swap) | Chromium copies the fixed partition into the new unrelated `SiteInstance`: `chromium/content/browser/renderer_host/render_frame_host_manager.cc:4027-4034` ("If the current SiteInstance has fixed storage partition ... the new unrelated SiteInstance must also stay in the same StoragePartition"). Brave test `CrossSiteNavigationPersistence`, `browser/containers/containers_browsertest.cc:2116`. | No, Chromium |
| Redirects | Same code path: the SiteInstance is recomputed through `GetSiteInstanceForNavigation`, which applies the rule above. | No, Chromium |
| Popups and `window.open` with opener | New window gets the opener's `SiteInstance` (`chromium/content/browser/web_contents/web_contents_impl.cc:5813-5815`). | No, Chromium |
| `noopener` windows, `target=_blank` links (implicit noopener) | Chromium creates `SiteInstance::Create()` in the default partition (`web_contents_impl.cc:5804-5812`). Brave inserts a branch: if `ContentBrowserClient::ShouldInheritStoragePartition(partition_config)` returns true, use `CreateForFixedStoragePartition` (`patches/content-browser-web_contents-web_contents_impl.cc.patch`, from `rewrite/content/browser/web_contents/web_contents_impl.cc.yaml:22-36`). The new virtual is declared in `chromium_src/content/public/browser/content_browser_client.h:49-50` and implemented in `browser/brave_content_browser_client.cc:1793-1801`. Test `LinkNavigationInheritsContainerStoragePartition`, `containers_browsertest.cc:793-887`. | Yes |
| Links opened in a new tab (context menu, middle or ctrl click), new tabs from a page | `NavigateParams::source_site_instance` is set (context menu: `chromium/components/renderer_context_menu/render_view_context_menu_base.cc:549`; copied from `OpenURLParams` in `chromium/chrome/browser/ui/navigator/browser_navigator_params.cc:62`). Brave's `GetStoragePartitionConfigToInherit` (`chromium_src/chrome/browser/ui/navigator/browser_navigator.cc:44-62`) returns that partition when it is a container one, and the `CreateTargetContents` hunk passes it to `GetSiteInstanceForNewTab` (`patches/chrome-browser-ui-navigator-browser_navigator.cc.patch`, hunk at line 578). | Yes |
| Ctrl+T, new tab button | Not inherited in v1.100.58: `chrome::AddTabAt` builds params with no source instance. Only `NewSplitTab` inherits, through `brave::MaybeAddTabAtInheritingActiveContainer` (`browser/ui/browser_commands.cc:1229-1265`, hooked by `patches/chrome-browser-ui-browser_commands.cc.patch`). `master` adds a "new tab target" setting for this (see "Upstream churn"). Spaces will need its own rule here. | Partly |
| Downloads | No Brave code. Chromium downloads through the initiating frame's partition and stores the partition in the download's embedder data for resumption (`chromium/content/browser/download/download_manager_impl.cc:570-630`). Not tested by Brave; resumption after restart is unverified. | No |
| Service workers | Live in the partition, so they are isolated per container. Brave tests `IsolateServiceWorkers` (`containers_browsertest.cc:889`) and `ServiceWorkerPersistenceAcrossSessions` (`:1797`). Push delivery is a gap, see section 5. | No |
| Session restore, reopen closed tab, restore window | See next section. Tests `StoragePersistenceAcrossSessions` (`:684`), `HotRestoreClosedContainerTab` (`:1947`), `NavigationHistoryPersistence` (`:2194`), `MixedTabsPersistence` (`:2299`). | Yes |
| Pinned tabs (`PinnedTabCodec`, used when the session is not restored) | The tab's container id is written as `"container_id"` in each pinned tab's pref dict (`browser/containers/container_specifier_utils.cc:21-53`, called from `chromium_src/chrome/browser/ui/tabs/pinned_tab_codec.cc:39-50`), read back into `StartupTab::container`, then turned into `NavigateParams::storage_partition_config` (`chromium_src/chrome/browser/sessions/session_restore.cc:18-28`, `chromium_src/chrome/browser/ui/startup/startup_browser_creator_impl.cc:22-32`). Test `PinnedTabRestorePreservesContainer` (`:2817`). | Yes |
| Command line | `--container=<name>` and `--temporary-container[=<name>]` (`components/containers/core/common/switches.h:14-17`, `command_line_container.cc:19-43`) set `StartupTab::container` in `BraveStartupTabProviderImpl::GetCommandLineTabs` (`browser/ui/startup/brave_startup_tab_provider_impl.cc:37-51`). | Yes |
| Tab discard | `ContainersWebContentsUserData` keeps the id for UI and pinned-tab encoding (above). Reload of the discarded tab relies on Chromium copying navigation entries with their `SiteInstance`; not verified line by line. | Partly |

### How session restore keeps the container

Design notes are in brave-core `docs/containers/session_persistence.md`. In short, Chromium's
session format has no field for a storage partition, so Brave encodes it into the URL:

```
https://example.com  ->  containers+<uuid>:https://example.com
```

Save path:

1. `NavigationEntryImpl::GetStoragePartitionKeyToRestore()` returns `(domain, name)` from the
   entry's frame `SiteInstance`, or the key it was restored with if the entry never loaded
   (`chromium_src/content/browser/renderer_host/navigation_entry_impl.cc:82-110`). This is what
   keeps a lazily restored, never-activated tab in its container when the session is saved
   again, which matters with Helium's `helium/core/lazy-session-restore.patch`.
2. `ContentSerializedNavigationBuilder::FromNavigationEntry` calls
   `MaybeSaveStoragePartitionKey`, which stores the key and the prefix
   `containers+<uuid>:` on the `SerializedNavigationEntry`
   (`chromium_src/components/sessions/content/content_serialized_navigation_builder.cc:43-69`,
   call site in `patches/components-sessions-content-content_serialized_navigation_builder.cc.patch`).
3. `GetSanitizedPageStateForPickle` prefixes the top URL inside the encoded Blink `PageState`
   (`chromium_src/components/sessions/content/content_serialized_navigation_driver.cc:48-62`,
   using `PageState::PrefixTopURL`, `chromium_src/third_party/blink/common/page_state/page_state.cc:31-49`).
4. `SerializedNavigationEntry::WriteToPickle` writes `maybe_prefixed_virtual_url_spec()`
   instead of `virtual_url_.spec()` (`patches/components-sessions-core-serialized_navigation_entry.cc.patch`).
   Session sync does the same (`patches/components-sync_sessions-synced_session.cc.patch`).

Restore path:

1. `ContentSerializedNavigationDriver::Sanitize` parses the prefix with
   `RestoreStoragePartitionKeyFromUrl` (`components/containers/content/browser/session_utils.cc:61-89`),
   strips it from `virtual_url` and from the `PageState`, and stores the key
   (`content_serialized_navigation_driver.cc:77-119`).
2. `ToNavigationEntry` copies the key onto the new `NavigationEntry` with
   `SetStoragePartitionKeyToRestore` (`content_serialized_navigation_builder.cc:25-35`).
3. `CreateRestoredTab` in `browser_tabrestore.cc` calls `MaybeSetContainerSiteInstance`, which
   builds the config with `containers::GetStoragePartitionConfigToRestore` (`tab_restore_utils.cc:16-45`)
   and sets `create_params.site_instance` through `GetSiteInstanceForNewTab`
   (`chromium_src/chrome/browser/ui/browser_tabrestore.cc:23-45`, call site in
   `patches/chrome-browser-ui-browser_tabrestore.cc.patch`). WebUI URLs are skipped (`:36-42`).

If containers are disabled, the prefixed URL is an unknown scheme, so the tab restores blank
instead of silently loading in the default partition (`session_utils.h:42-50`; test
`ContainersDisabledAfterRestoreBrowserTest`, `containers_browsertest.cc:2483-2569`). Downgrading
Neon to plain Helium would therefore blank every container tab in the session.

Paths that do not go through `CreateRestoredTab` lose the container. Brave's own open issue
#58219 reports this for saved tab groups (which reopen from sync data, by URL).

### Container list and storage lifetime

- `ContainersService` (`components/containers/core/browser/containers_service.h:29-137`) is a
  per-profile `KeyedService`. The list itself lives in prefs: `brave.containers.list` (syncable
  list of `{id, name, icon, background_color}`), `brave.containers.used` (local dict of every
  container a tab has used, so removed-but-referenced containers still render), and the UI
  switch `brave.containers.enabled` (`pref_names.h:13-20`).
- Create, update, remove and reorder are only in `ContainersSettingsHandler`, the settings
  page's mojo handler (`containers_settings_handler.cc:62-159`). The service itself has no
  create call. `AddContainer` assigns the UUID but does not return it.
- `ContainerTabTracker` marks a container used on the tab's first main-frame navigation
  (`container_tab_tracker.cc:48-71`).
- Removing a container from the list does not delete its data. At the next profile start,
  `ScheduleOrphanedContainersCleanup` (`containers_service.cc:194-229`) asks the delegate for
  every container id referenced by open tabs, the last session and tab restore
  (`containers_service_delegate.cc:181-341`), then clears and deletes the partition directory of
  every used container that is neither listed nor referenced
  (`containers_service_delegate.cc:194-237`, `containers_service.cc:231-274`).

## 2. Complete inventory

### Chromium files Brave changes for containers

Size is lines of the override file (whole file, including non-container code where noted) or
`+added/-removed` for patches. "Flag" means the code is inside `BUILDFLAG(ENABLE_CONTAINERS)`;
the content and Blink pieces are not flagged even though containers are their only user.

**Content layer (partition plumbing)**

| Chromium file | Form | Size | Change |
|---|---|---|---|
| `content/public/browser/navigation_entry.h` | override | 26 | Adds pure virtual `Set/GetStoragePartitionKeyToRestore` |
| `content/browser/renderer_host/navigation_entry_impl.h` | override | 33 | Member `storage_partition_key_to_restore_` and the two methods |
| `content/browser/renderer_host/navigation_entry_impl.cc` | override | 112 (about 45 for containers) | Method bodies, `:69-110` |
| `content/public/browser/site_instance.h` | override | 26 | Declares `CreateForURLWithOptionalFixedStoragePartition` |
| `content/browser/site_instance_impl.cc` | override | 28 | Defines it, `:15-26` |
| `content/public/browser/content_browser_client.h` | override | 60 (2 of 9 virtuals) | `ShouldInheritStoragePartition`, `ShouldUseDefaultHostZoomMapForStoragePartition` |
| `content/public/browser/content_browser_client.cc` | override | 78 (8 lines) | Default `false` implementations, `:63-71` |
| `content/browser/web_contents/web_contents_impl.cc` | patch | +11/-1 (6 for containers) | Inherit partition for `noopener` windows |
| `content/browser/host_zoom_map_impl.cc` | override 18 + patch +1 | 19 | Container partitions use the default zoom map |

**Blink**

| Chromium file | Form | Size | Change |
|---|---|---|---|
| `third_party/blink/public/common/page_state/page_state.h` | override | 24 | Declares `PrefixTopURL`, `RemoveTopURLPrefix` |
| `third_party/blink/common/page_state/page_state.cc` | override | 90 | Defines them, `:31-87` |

**Sessions and sync**

| Chromium file | Form | Size | Change |
|---|---|---|---|
| `components/sessions/core/serialized_navigation_entry.h` | override | 84 | Runtime fields and `maybe_prefixed_virtual_url_spec()` |
| `components/sessions/core/serialized_navigation_entry.cc` | patch | +1/-1 | Write prefixed URL to the session file |
| `components/sessions/content/content_serialized_navigation_builder.cc` | override 73 + patch +2 | 75 | Save and restore the key |
| `components/sessions/content/content_serialized_navigation_driver.cc` | override | 123 (about 50 for containers; the rest is Brave's `brave://` scheme handling) | Prefix and unprefix `PageState` and `virtual_url` |
| `components/sync_sessions/synced_session.cc` | patch | +1/-1 | Prefixed URL in session sync |
| `components/sync_preferences/common_syncable_prefs_database.cc` | override | 143 (one entry) | Registers `brave.containers.list` as syncable |
| `chrome/browser/ui/sync/tab_contents_synced_tab_delegate.cc` | override | 47 | Temporary-container tabs are not synced |

**Tab creation and navigation**

| Chromium file | Form | Size | Change |
|---|---|---|---|
| `chrome/browser/tab_contents/tab_util.h` | override | 25 | Adds an optional `StoragePartitionConfig` parameter to `GetSiteInstanceForNewTab` |
| `chrome/browser/tab_contents/tab_util.cc` | override | 34 | Routes it to `CreateForURLWithOptionalFixedStoragePartition` |
| `chrome/browser/ui/navigator/browser_navigator_params.h` | override 14 + patch +3 | 17 | New field `storage_partition_config` |
| `chrome/browser/ui/navigator/browser_navigator.cc` | override 67 + patch +12/-1 | about 25 for containers | `GetStoragePartitionConfigToInherit` and the call in `CreateTargetContents` |
| `chrome/browser/ui/browser_commands.cc` | patch | +23/-3 (6 for containers) | `NewSplitTab` inherits the active tab's container |
| `chrome/browser/ui/web_applications/web_app_launch_process.cc` | override 59 + patch +1/-2 | 61 | `--container` for PWA launches |

**Restore and startup**

| Chromium file | Form | Size | Change |
|---|---|---|---|
| `chrome/browser/ui/browser_tabrestore.cc` | override 63 + patch +7/-1 | about 30 for containers | `MaybeSetContainerSiteInstance` in `CreateRestoredTab` |
| `chrome/browser/sessions/session_restore.cc` | override 32 + patch +1 | 33 | Startup tabs carry their container |
| `chrome/browser/ui/startup/startup_tab.h` | override 17 + patch +7 | 24 | `StartupTab::container` field and copy/move members |
| `chrome/browser/ui/startup/startup_tab.cc` | override | 11 | Defaulted copy/move members |
| `chrome/browser/ui/startup/startup_browser_creator_impl.cc` | override 40 + patch +2 | 42 | Apply the startup tab's container to `NavigateParams` |
| `chrome/browser/ui/tabs/pinned_tab_codec.cc` | override 54 + patch +5 | about 15 for containers | Encode and decode `container_id` for pinned tabs |

**Permissions and privacy**

| Chromium file | Form | Size | Change |
|---|---|---|---|
| `components/permissions/permission_util.cc` | override | 240 (16 for containers) | `IsPermissionBlockedInPartition` returns false in containers, `:223-238`. Without it Chromium denies undelegated permissions such as notifications outside the home partition (`chromium/components/permissions/permission_util.cc:678-698`) |
| `chrome/browser/content_settings/content_settings_manager_delegate.cc` | override | 97 (9 for containers) | Container id as extra Shields farbling entropy |

**UI**

| Chromium file | Form | Size | Change |
|---|---|---|---|
| `chrome/browser/renderer_context_menu/render_view_context_menu.cc` | override | 966 (about 70 for containers) | "Open link in container" submenu, `:642-697`, `:755-798` |
| `chrome/browser/renderer_context_menu/render_view_context_menu.h` | override | 113 (about 20) | Delegate methods |
| `chrome/browser/ui/views/bookmarks/bookmark_context_menu.cc` | override 32 + patch +2/-1 | 34 | Container submenu on bookmarks |
| `chrome/browser/ui/views/tabs/hovercard/hover_card_anchor_target.h` | override 22 + patch +3 | 25 | `ContainerCardData`, `GetContainerCardData()` |
| `chrome/browser/ui/views/tabs/hovercard/hover_card_anchor_target.cc` | override | 17 | Default implementation |
| `chrome/browser/ui/views/tabs/hovercard/tab_hover_card_bubble_view.cc` | override | 150 (about 60) | Container label in the hover card |

Not confirmed: the brief mentions `permissions/request_type.cc` and `accelerator_table.cc`. At
this commit neither Brave's override nor any patch of those files has container code. There is
no `browser/containers/containers_navigator_utils.cc` at `v1.100.58`; it exists only on `master`.

### Brave-side files that host container hooks

Brave subclasses many Chromium classes, so some hooks sit in Brave's own files. In Helium each
of these becomes a hunk in the Chromium base class instead.

| Brave file | Lines | Role for containers | Helium equivalent |
|---|---|---|---|
| `browser/brave_content_browser_client.cc` | `:689-694`, `:954-960`, `:1793-1811` | Implements the two content hooks, binds the settings mojo interface, Shields entropy | `chrome/browser/chrome_content_browser_client.{h,cc}` |
| `browser/browser_context_keyed_service_factories.cc` | `:210-212` | Registers `ContainersServiceFactory` | `chrome/browser/profiles/chrome_browser_main_extra_parts_profiles.cc` |
| `browser/ui/tabs/brave_tab_features.cc` | `:172-185` | Creates `ContainerTabTracker` and the page action controller | `chrome/browser/ui/tabs/tab_features.{h,cc}` |
| `browser/browsing_data/brave_browsing_data_remover_delegate.cc` | `:170-192` | Clear browsing data also clears every used container partition | `chrome/browser/browsing_data/chrome_browsing_data_remover_delegate.cc` |
| `browser/ui/startup/brave_startup_tab_provider_impl.cc` | `:37-51` | Command-line container | `chrome/browser/ui/startup/startup_tab_provider.cc` |
| `browser/ui/browser_commands.cc` | `:1229-1388` | Open URL or tabs in a container, temporary container, new split tab | New Neon file |
| `browser/enterprise/platform_auth_helper.cc` | `:18-30` | Skip enterprise platform SSO in container tabs | Only relevant on macOS and Windows; M6 |
| `browser/brave_shields/brave_shields_web_contents_observer.cc`, `browser/net/brave_reduce_language_network_delegate_helper.cc` | `:336-341`, `:115-127` | Farbling entropy per container | Drop (see section 3) |
| `browser/about_flags.cc`, `browser/extensions/api/settings_private/brave_prefs_util.cc`, `browser/ui/webui/brave_settings_ui.cc`, `browser/ui/webui/settings/brave_settings_localized_strings_provider.cc`, `browser/resources/settings/...` | | Flag, settings page and its pref allowlist | Later, our own settings |
| `browser/ui/views/tabs/brave_tab.cc`, `brave_tab_strip.cc`, `brave_new_tab_button.cc`, `browser/ui/tabs/brave_tab_menu_model.cc`, `browser/ui/brave_browser_command_controller.cc`, `browser/ui/brave_browser_actions.cc`, `browser/ui/toolbar/brave_bookmark_context_menu_controller.cc` | `brave_tab.cc` has 21 `ENABLE_CONTAINERS` lines, `brave_tab_strip.cc` 12, `brave_tab_menu_model.cc` 6 | Tab accent and icon, new tab button menu, tab context menu, commands | Later, replaced by spaces UI |

### New files

All carry the MPL-2.0 header. Test files are listed separately.

**`components/containers/`**

| File | Lines | Role |
|---|---|---|
| `buildflags/buildflags.gni`, `buildflags/BUILD.gn` | 8, 13 | `enable_containers = is_linux \|\| is_mac \|\| is_win` |
| `core/common/features.{h,cc}` | 18, 14 | `kContainers`, enabled by default (`features.cc:12`) |
| `core/common/switches.h` | 21 | `--container`, `--temporary-container` |
| `core/common/BUILD.gn` | 30 | |
| `core/mojom/containers.mojom`, `BUILD.gn` | 99, 19 | `Container` struct, `Icon` enum, settings handler and UI interfaces |
| `core/browser/containers_service.{h,cc}` | 141, 276 | Keyed service: lookup, used-container tracking, orphan cleanup |
| `core/browser/containers_service_observer.h` | 23 | `OnContainersListChanged()` |
| `core/browser/container_specifier.h` | 27 | `ContainerId` or `ContainerName` variant |
| `core/browser/containers_settings_handler.{h,cc}` | 75, 187 | CRUD and reorder, validation, change notification |
| `core/browser/prefs.{h,cc}`, `pref_names.h` | 59, 147, 24 | Pref encoding of containers |
| `core/browser/prefs_registration.{h,cc}` | 24, 39 | Registers the three prefs; UI pref defaults off on stable channel unless a field trial says otherwise (`:20-24`) |
| `core/browser/default_containers_list.{h,cc}` | 34, 50 | Four default containers with fixed ids, Nala colors, localized names |
| `core/browser/temporary_container.{h,cc}` | 30, 109 | `t-` ids, BIP-39 two-word names, random icon and color |
| `core/browser/unknown_container.{h,cc}` | 28, 22 | Placeholder for an id not in any list |
| `core/browser/command_line_container.{h,cc}` | 37, 45 | Resolve `--container` switches |
| `core/browser/BUILD.gn`, `DEPS` | 108, 14 | |
| `content/browser/storage_partition_utils.{h,cc}` | 69, 86 | Domain constant, id validation, id from `WebContents` |
| `content/browser/containers_web_contents_user_data.{h,cc}` | 48, 54 | Id on discarded tabs |
| `content/browser/session_utils.{h,cc}` | 107, 91 | URL prefix encode and decode |
| `content/browser/tab_restore_utils.{h,cc}` | 40, 47 | Partition config from serialized navigations |
| `content/browser/BUILD.gn`, `DEPS` | 79, 16 | Two components, split to avoid a sessions dependency cycle |
| `components/resources/containers_strings.grdp` | 15 | Default container names |

**`browser/containers/`** (would become `chrome/browser/containers/`)

| File | Lines | Role |
|---|---|---|
| `containers_service_factory.{h,cc}` | 39, 77 | Regular profiles only; depends on session and tab restore services |
| `containers_service_delegate.{h,cc}` | 124, 341 | Referenced ids from tabs, last session, tab restore; deletes partition storage |
| `container_tab_tracker.{h,cc}` | 56, 73 | Tab feature: mark used, copy id on discard |
| `container_specifier_utils.{h,cc}` | 39, 85 | Pinned-tab serialization, specifier to partition config |
| `used_container_storage_partitions.{h,cc}` | 24, 42 | Partition configs of used containers (for clear browsing data) |
| `BUILD.gn` | 106 | |

**UI and settings (later milestones)**

| File | Lines | Role |
|---|---|---|
| `browser/ui/containers/container_model.{h,cc}` | 68, 87 | Container plus icon for UI |
| `browser/ui/containers/containers_menu_model.{h,cc}` | 74, 152 | Shared "open in container" submenu |
| `browser/ui/containers/containers_icon_generator.{h,cc}` | 52, 202 | Icons from Leo vector icons and Nala colors |
| `browser/ui/containers/containers_bookmark_menu_model_delegate.{h,cc}` | 45, 78 | Bookmark menu delegate |
| `browser/ui/tabs/containers_tab_menu_model_delegate.{h,cc}` | 44, 85 | Tab menu delegate |
| `browser/ui/views/page_action/partitioned_storage_page_action_controller.{h,cc}` | 88, 246 | Omnibox page action showing the container |
| `browser/resources/settings/brave_content_page/containers*.{ts,html.ts,css}` | 915 total | Settings page in Brave's WebUI settings |

**Traffic Control** (URL rules that route to a container): `components/traffic_control/`
(service, rule matcher, prefs, settings handler, mojom) and `browser/traffic_control/`
(navigation throttle that cancels a matching navigation and reopens it in the target
container, `traffic_control_apply.h:20-40`). 1,606 lines without tests.

**Tests**: unit tests 2,749 lines (`components/containers/**/_unittest.cc`,
`browser/containers/*_unittest.cc`, test utils), browser test 3,272 lines
(`browser/containers/containers_browsertest.cc`), plus `test/data/containers/`.

## 3. Brave-only dependencies

| Dependency | Where | Replace or drop |
|---|---|---|
| `chromium_src` overrides | All Chromium changes above | Rewrite as quilt hunks. Most overrides are macro tricks (renaming a function to `_ChromiumImpl`, injecting members via a macro on a neighbouring declaration). As patches they become a few direct lines each, usually smaller than the override. `CreateForURLWithOptionalFixedStoragePartition` in `content/` can be dropped: do the `if` inside `tab_util.cc`, which already includes `site_instance.h`. |
| `ENABLE_CONTAINERS` buildflag | Every hook | Keep the `.gni` and generated header (21 lines) so ported files stay verbatim; set true on desktop. Hooks in Chromium files can test only the `base::Feature`. |
| `brave/` include prefix and GN labels | Every file | Mechanical rewrite `brave/components/containers` to `components/containers`, `brave/browser/containers` to `chrome/browser/containers`. |
| Brave pref plumbing (`browser_context_keyed_service_factories.cc`, `brave_prefs_util.cc`) | Factory registration, settings allowlist | The factory registers its own prefs through `RegisterProfilePrefs` (`containers_service_factory.cc:52-55`), so only factory registration in `chrome_browser_main_extra_parts_profiles.cc` is needed. Allowlist only matters for a settings page. |
| Syncable pref and syncable prefs database | `prefs_registration.cc:32-35`, `common_syncable_prefs_database.cc` | Helium has its own sync backend that does carry `PREFERENCES` and `SESSIONS` (`patches/helium/core/sync/datatype-policy.patch`, `CustomSyncSupportedTypes`). For M1 register the list as a local pref (one-line change) and decide sync later. Whether a `SYNCABLE_PREF` missing from the database trips a check is unverified. |
| Nala colors (`brave/ui/color/nala`) | `default_containers_list.cc`, `temporary_container.cc` | Replace with fixed `SkColor`s or tab group colors. Spaces will set colors itself. |
| BIP-39 wordlist (`brave/third_party/bip39wally-core-native`) | `temporary_container.cc:29-49` | Replace the name generator with something trivial, or drop temporary containers. |
| Brave strings (`brave_components_strings`, `brave_generated_resources`) | Default names, menus | Default names: drop the default list (spaces creates its own containers). Menus: Helium's `generated_resources.grd` later. |
| Leo vector icons, `mojom::Icon` set | `containers_icon_generator.cc` | Later; spaces has its own icons. Keep the enum so prefs stay compatible. |
| `BraveContentBrowserClient` | Content hooks | `ChromeContentBrowserClient`. Note `helium/core/noise/core.patch` already adds virtuals to `content_browser_client.h`. |
| Brave tab features, tab strip and vertical tabs (`brave_tab.cc`, `BraveTabFeatures`, hover card, new tab button) | Indicators, tracker | Tracker goes into Chromium `TabFeatures`. Indicators are replaced by the spaces sidebar; Helium builds on its own vertical tabs (PLAN decision 6). |
| Brave WebUI settings framework | Settings page | Write our own spaces settings (M4) against `ContainersSettingsHandler`'s mojom, or adapt dvjn's people-page section. |
| brave_shields farbling | `content_settings_manager_delegate.cc`, workers, Accept-Language | Drop. Helium's noise tokens are per browser context (`helium/core/noise/core.patch`, `HeliumNoiseTokenData`), so all containers share one fingerprint noise seed. Adding the container id there is a possible later privacy change, not a port item. |
| AI chat, Leo assistant | None in containers code | Nothing to do. The `chromium_src/content/browser/browser_context.cc` override mentions AI chat but is unrelated. |
| Brave sync (session sync, temporary container exclusion) | `synced_session.cc`, `tab_contents_synced_tab_delegate.cc` | Skip for M1. Without the `synced_session.cc` hunk, container tabs sync as plain URLs and open in the default partition on the other device. |
| Tree tabs, split view | Mixed into `browser_tabrestore.cc` and `browser_commands.cc` overrides | Strip; not container code. |

## 4. Scope for M1

M1: "a tab can live in a named persistent container; two tabs on the same site in different
containers have separate logins; this survives restart."

**Needed for M1**

- Core: `ContainersService`, prefs, mojom, `ContainersSettingsHandler` (used as the CRUD API),
  `unknown_container`, a reduced `temporary_container` (the service calls it), feature flag.
- Content: `storage_partition_utils`, `containers_web_contents_user_data`, `session_utils`,
  `tab_restore_utils`.
- Content hooks: `NavigationEntry` key, `ShouldInheritStoragePartition` plus the
  `web_contents_impl.cc` branch.
- Session hooks: `serialized_navigation_entry.{h,cc}`, `content_serialized_navigation_builder.cc`,
  `content_serialized_navigation_driver.cc`, Blink `page_state.{h,cc}`.
- Chrome hooks: `tab_util`, `NavigateParams` field, `browser_navigator.cc`,
  `browser_tabrestore.cc`, `chrome_content_browser_client`, `permission_util.cc`.
- Browser service: factory, delegate (needed for orphan cleanup and safe deletion), tab tracker.
- A way to put a tab in a container without UI, for the M1 test: the `--container` command-line
  path (`command_line_container`, `container_specifier_utils`, `startup_tab` hooks) plus the
  spaces interface's `OpenTabInContainer`. The scripted check can launch with
  `--container=<name> https://...`, log in, restart, and compare cookies over DevTools.
- Unit tests worth porting: `session_utils_unittest.cc`, `storage_partition_utils_unittest.cc`,
  `tab_restore_utils_unittest.cc`, the two serialized-navigation unit tests,
  `containers_service_unittest.cc` (about 1,250 lines). Whether any Chromium test target is
  affordable on the dev VM is M0's question.

**Needed later**

| Item | Milestone | Notes |
|---|---|---|
| Pinned tab codec and startup-tab container | M2 or with upstream #2931 | Only used when the session is not restored; interacts with Helium's pinned tab work |
| Clear browsing data across containers | M2 to M4 | Without it, "clear browsing data" leaves container data untouched |
| New tab inherits space | M2 | Spaces rule, not Brave's (Brave v1.100 does not inherit on Ctrl+T) |
| Settings page, container CRUD UI | M4 | Ours, through the spaces interface |
| Tab indicators, hover card, page action | Probably never | The sidebar shows the space |
| "Open link in container" context menu | M4 or M5 | Becomes "open in space" |
| Traffic Control | M5 | Site rules that route a URL to a space; port Brave's or write a smaller throttle |
| Shared zoom (`host_zoom_map_impl.cc`) | Optional | Without it each container has its own zoom levels |
| Session sync and syncable list | Decide with sync | Helium sync carries sessions and prefs |

**Skip**

Shields farbling entropy, Brave's tab accent and vertical-tab code, the new tab button menu,
PWA `--container` (`web_app_launch_process.cc`), split view hook, platform auth (until M6),
Brave settings framework pieces, AI chat, Leo.

## 5. Known gaps and risks

| Area | Finding | Evidence |
|---|---|---|
| Extension cookies API | `chrome.cookies` reads and writes only the default partition; `getAllCookieStores` reports one store | `chromium/extensions/browser/api/cookies/cookies_api.cc:123`, `:267`; Brave #57209 (open); issue #199 comment from 2026-08-01 (live test on Brave 1.93) |
| Extension tabs API | `chrome.tabs` has no container field; tabs created by extensions open in the default partition | Brave #60927 (open). Unverified for `chrome.windows.create` popups used by OAuth flows, but nothing in Brave sets a partition for them |
| Password managers, OAuth | 1Password Google sign-in breaks across partitions; Bitwarden inline menu report | Brave #54688, #56377 (open). Root cause unverified |
| Clear browsing data | Brave clears every used container partition when the filter names no partition | `browser/browsing_data/brave_browsing_data_remover_delegate.cc:170-192`. Needs a Chromium-side hunk in Neon |
| Site data and cookie settings UI | Chrome's site-data pages enumerate the default partition only; Brave has no container code there | Absence verified by grep; UI behaviour unverified |
| Content settings and permissions | One `HostContentSettingsMap` per profile, so a grant in one container applies to the same origin in all containers. Brave overrides `IsPermissionBlockedInPartition` so prompts work at all | `chromium_src/components/permissions/permission_util.cc:223-238`; test `NotificationPermissionNotSilentlyDeniedInContainerTab`, `containers_browsertest.cc:2861`; Brave #56925 (notifications denied on Linux, open) |
| WebHID | Unavailable in container tabs (Brave asserts this as expected) | Test `HidUnavailableInContainer`, `containers_browsertest.cc:3085`. WebUSB and Web Serial unverified |
| Push messaging | Push delivery looks up the service worker in `GetStoragePartitionForUrl(origin)`, the default partition, so pushes to a container's worker will not arrive | `chromium/content/browser/push_messaging/push_messaging_router.cc:114-115`; no Brave code or test. Runtime unverified |
| Service workers | Isolated and persisted per container | Tests at `containers_browsertest.cc:889`, `:1797` |
| HTTP auth | Auth cache lives in each partition's network context, so containers are separate | By design of Chromium's per-partition `NetworkContext`; not tested by Brave, unverified |
| DevTools and CDP | Container tabs share the profile's `browserContextId`; targets carry no container info; `Target.createTarget` cannot pick a container | Brave #61986 (open). Matters for Neon's scripted tests: use the command line or our own hook to open container tabs |
| Prefetch and prerender | Speculation-rules prefetch is refused for non-default partitions (`kNonDefaultStoragePartition`) | `chromium/content/browser/preloading/prefetch/prefetch_service.cc:953-967`. Prerender eligibility for container tabs unverified. Performance only |
| Preconnect | Not examined; preconnects carry no cookies | Unverified |
| Downloads | Partition stored in download embedder data | `chromium/content/browser/download/download_manager_impl.cc:570-630`; untested |
| Restore paths outside `CreateRestoredTab` | Saved tab groups reopen without the container | Brave #58219 (open). #57068 reports container tabs loading the default profile at startup on 1.92; whether it still reproduces at 1.100 is unverified |
| Container deletion | Data is deleted only at the next start, and only when nothing references the id; temporary container data accumulates | `containers_service.cc:194-274`; Brave #59853, #59802 (open) |
| Tab cannot change container | "Open in container" duplicates the tab | Brave #53532 (open). Fits PLAN decision 4 (a tab keeps its space for life) |
| Session format | URL-prefix encoding is invisible to Chromium; a Neon build without containers, or plain Helium, restores those tabs blank | `session_utils.h:42-50` |

## 6. dvjn's Helium branch

`github.com/dvjn/helium`, branch `feat/containers`, head `32f50b3707ed4eb4ee236051ea6c4ef283a010ac`
(2026-10-14), 11 commits on top of Helium `aec023c7`, which is on Chromium `155.0.8059.39`. It
would need a rebase to 158.

| Patch | Added lines | Content |
|---|---|---|
| `helium/containers/add-containers-flag.patch` | 121 | `--helium-containers` flag, `kHeliumContainers` feature |
| `add-containers-service.patch` | 802 | Own `ContainersService`, mojom, prefs, factory, colors, starter containers |
| `add-containers-web-contents-utils.patch` | 153 | Partition helpers |
| `open-tabs-in-containers.patch` | 202 | `NavigateParams` field, `tab_util`, command line |
| `keep-links-in-container.patch` | 79 | Link inheritance and the `noopener` content hook |
| `share-permissions-with-profile.patch` | 13 | Same idea as Brave's `permission_util.cc` override |
| `add-container-tab-indicator.patch` | 183 | Tab strip indicator |
| `open-links-in-container.patch` | 313 | Link context menu |
| `open-tabs-in-container-menu.patch` | 52 | Tab context menu |
| `add-containers-settings-page.patch` | 713 | Settings section in the people page |

Differences from Brave:

- **No session persistence.** No patch touches sessions, `NavigationEntry` or `PageState`
  (grep for session, restore and serialize finds nothing), so container tabs would reopen in the
  default partition after restart. That is the hardest part of M1 and the main reason to take
  Brave's code.
- Same partition domain `"containers"` with a UUID name, so on-disk data would be compatible.
- A cleaner service API: `AddContainer` returns the new id, `UpdateContainer`, `RemoveContainer`,
  `MoveContainer` live on the service, observer `OnContainersChanged()`. This is closer to what
  spaces needs than Brave's split between service and settings handler.
- No orphan cleanup, no temporary containers, no Traffic Control, no sync handling.
- Layout follows Helium conventions: one topic per patch under `patches/helium/containers/`,
  new files and their hooks in the same patch, a Helium flag entry in `helium_flag_entries.h`.
  That is the same layout Neon's `AGENTS.md` asks for, and a good model for our patch files.

Worth reusing: the patch layout and flag wiring as a template, possibly the settings section for
M4, and the service API shape as the model for our spaces interface. Not worth reusing as the
base: it lacks restore and would diverge from whatever upstream imports.

**License.** The repo is a GitHub fork of `imputnet/helium` reporting GPL-3.0, and every new file
carries the Helium GPL-3.0 header ("Copyright 2026 The Helium Authors"). The only author is
Divya Jain. Uncertain: the design mirrors Brave's closely (same file names, a
`ContainerOperationError` enum with the same `kIdShouldBeEmpty`, `kIdShouldBeSet`,
`kInvalidName`, `kNotFound` values, the "used containers" record) but the code is not a copy of
Brave's text as far as I compared. Whether any of it is derived from MPL-2.0 files, which would
call for MPL notices, cannot be settled from the source. GPL-3.0 itself is compatible with Neon.

Other attempts: issue #199 mentions `leopoldj4/helium` branch `containers`; that repository no
longer exists. On 2026-11-06 dvjn told another commenter "go ahead" on #199, apparently about a
Brave port; the comment it answered is not visible, so the state of any upstream port effort is
unverified. No container pull request exists on `imputnet/helium`.

## 7. Licensing

Brave's files are MPL-2.0 and none of the container files carries the "Incompatible With
Secondary Licenses" notice (checked by grep; the phrase only appears in Brave's `LICENSE` text).
MPL-2.0 section 3.3 then lets a larger work combine them with GPL code, and GPL-3.0 counts as a
Secondary License ("GNU General Public License, Version 2.0 ... or any later versions").

Practical rules for Neon:

1. Keep Brave's MPL header in every copied file. Do not remove or change existing copyright or
   license notices (MPL 3.4). Adding a line for our changes is fine.
2. Our modifications to a copied file stay under MPL-2.0 for that file (MPL is file-level).
   Neon's own new files can be GPL-3.0.
3. Patch files that carry Brave code get the same header Helium already uses for its Brave
   imports: the MPL notice, "Copyright (c) <year>, The Brave Authors", the Helium or Neon
   copyright, and the "Alternatively ... GNU General Public License Version 3" paragraph (see
   the top of `patches/brave/tab-cycling-mru-impl.patch` and `patches/brave/custom-importer.patch`).
4. Source availability (MPL 3.2) is met by the patch queue itself, which is the source.
5. Add a line to `neon/THIRD_PARTY.md`: `brave/brave-core`, commit
   `bd6d26d1bced03c17a98baa86d2cd3425d543d1b` (tag `v1.100.58`), MPL-2.0, and the list of ported
   paths. `AGENTS.md` requires this and the file does not exist yet.

Uncertain:

- Whether Brave's hunks inside BSD-licensed Chromium files (for example the six-line
  `web_contents_impl.cc` branch) are "Covered Software". Helium treats a patch that modifies a
  Chromium file with Brave code as MPL (`custom-importer.patch` has the MPL header), and following
  that convention is the low-risk choice.
- Whether the "Alternatively ... GPL-3" wording in Helium's headers is a correct way to express
  section 3.3 for files that are still MPL. It matches the intent of 3.3, but this is not legal
  advice and I have not checked it against any authoritative source.
- Whether a credits entry in `chrome://credits` is expected. MPL does not require it.

## 8. Proposed Neon patches

Apply order under `patches/neon/containers/`, appended to the `neon/` block of `patches/series`.
Paths: Brave `brave/components/containers/...` becomes `components/containers/...` (as
`AGENTS.md` allows for ported components) and `brave/browser/containers/...` becomes
`chrome/browser/containers/...`. The second is a choice to flag: `AGENTS.md` asks for
`chrome/browser/neon/`, but mirroring Brave's tree means that if Helium imports Brave the same
way, our patch fails to apply on the very files upstream now ships, which is the signal to drop
ours. If the owner prefers the rule as written, use `chrome/browser/neon/containers/`.

Line counts are rough: "new" is files added, "hook" is hunk lines in existing files.

### 1. `core.patch`

- Purpose: container list, prefs, mojom, feature, CRUD handler.
- Adds: `components/containers/buildflags/*`, `core/common/{features.*,BUILD.gn}`,
  `core/mojom/*`, `core/browser/{containers_service.*,containers_service_observer.h,container_specifier.h,containers_settings_handler.*,prefs.*,pref_names.h,prefs_registration.*,unknown_container.*,temporary_container.*,BUILD.gn,DEPS}`.
  Default containers list: drop or reduce to an empty list.
- Edits inside ported files: Nala colors, BIP-39 names, Brave strings, syncable flag on the list
  pref.
- Hooks: none (nothing references the targets until patch 4).
- Size: ~1,450 new, ~40 changed inside ported files.
- Helium overlap: none.

### 2. `content-partition.patch`

- Purpose: partition helpers and the content hooks every later patch relies on.
- Adds: `components/containers/content/browser/{storage_partition_utils.*,containers_web_contents_user_data.*,session_utils.*,tab_restore_utils.*,BUILD.gn,DEPS}`.
- Hooks: `content/public/browser/navigation_entry.h` (+4),
  `content/browser/renderer_host/navigation_entry_impl.{h,cc}` (+45),
  `content/public/browser/content_browser_client.{h,cc}` (+8),
  `content/browser/web_contents/web_contents_impl.cc` (+6).
- Size: ~620 new, ~65 hook.
- Helium overlap: `content_browser_client.{h,cc}` by `helium/core/noise/core.patch` (hunk at
  `content_browser_client.h:3353`); `web_contents_impl.cc` by `helium/core/force-paste-action.patch`
  (hunk at `:7268`, far from `CreateNewWindow` at `:5804`). Low conflict risk.

### 3. `session-persistence.patch`

- Purpose: write and read the `containers+<uuid>:` prefix.
- Adds: none.
- Hooks: `third_party/blink/public/common/page_state/page_state.h` (+2),
  `third_party/blink/common/page_state/page_state.cc` (+50),
  `components/sessions/core/serialized_navigation_entry.h` (+20),
  `components/sessions/core/serialized_navigation_entry.cc` (+1/-1),
  `components/sessions/content/content_serialized_navigation_builder.cc` (+35),
  `components/sessions/content/content_serialized_navigation_driver.cc` (+40),
  `components/sessions/BUILD.gn` (+2 deps).
- Size: ~150 hook.
- Helium overlap: none. Helium's lazy restore lives in `chrome/` and `tab_restore_service_helper.cc`.

### 4. `browser-service.patch`

- Purpose: profile keyed service, orphan cleanup, used-container tracking.
- Adds: `chrome/browser/containers/{containers_service_factory.*,containers_service_delegate.*,container_tab_tracker.*,BUILD.gn}`.
- Hooks: `chrome/browser/profiles/chrome_browser_main_extra_parts_profiles.cc` (+3),
  `chrome/browser/ui/tabs/tab_features.{h,cc}` (+10), `chrome/browser/BUILD.gn` and
  `chrome/browser/ui/tabs/BUILD.gn` deps (+4), optional Helium flag entry in
  `chrome/browser/helium_flag_entries.h` (+10).
- Size: ~620 new, ~30 hook.
- Helium overlap: `chrome_browser_main_extra_parts_profiles.cc` (`helium/core/custom-keyboard-shortcuts.patch`,
  `ungoogled-chromium/disable-ai.patch`, `ungoogled-chromium/fix-building-without-safebrowsing.patch`);
  `tab_features.cc` (`ungoogled-chromium/disable-ai.patch`,
  `ungoogled-chromium/add-flag-for-search-engine-collection.patch`); `chrome/browser/BUILD.gn` (8
  patches); `chrome/browser/ui/tabs/BUILD.gn` (`helium/ui/layout/core.patch`,
  `helium/ui/layout/vertical.patch`); `helium_flag_entries.h` (16 patches, append-only list).

### 5. `tab-partition.patch`

- Purpose: new tabs, links and restored tabs get the right partition; permissions work.
- Adds: none, or a small `chrome/browser/containers/containers_navigator_utils.{h,cc}` holding
  `GetStoragePartitionConfigToInherit` (the shape Brave `master` moved to).
- Hooks: `chrome/browser/tab_contents/tab_util.{h,cc}` (+12),
  `chrome/browser/ui/navigator/browser_navigator_params.h` (+2),
  `chrome/browser/ui/navigator/browser_navigator.cc` (+8),
  `chrome/browser/ui/browser_tabrestore.cc` (+30),
  `chrome/browser/chrome_content_browser_client.{h,cc}` (+12),
  `components/permissions/permission_util.cc` (+12), `components/permissions/BUILD.gn` (+1).
- Size: ~80 new, ~80 hook.
- Helium overlap: `browser_navigator.cc` by `helium/core/override-chrome-protocol.patch` and
  `helium/ui/layout/zen-mode.patch` (hunks around lines 202 to 268 and 1101 to 1167; ours is in
  `CreateTargetContents` near 578); `browser_tabrestore.cc` by `helium/core/lazy-session-restore.patch`
  (hunks in `AddRestoredTabImpl` and `AddRestoredTab`; ours is in `CreateRestoredTab` near line
  72); `chrome_content_browser_client.{h,cc}` by 8 patches including `helium/core/noise/core.patch`.
  Rebase risk is moderate on `browser_tabrestore.cc` because Helium's lazy restore is active work.

### 6. `command-line.patch`

- Purpose: open startup tabs in a container (`--container`), keep pinned tabs in their
  container. Gives M1 a test entry point that needs no UI.
- Adds: `components/containers/core/common/switches.h`,
  `components/containers/core/browser/command_line_container.*`,
  `chrome/browser/containers/container_specifier_utils.*`.
- Hooks: `chrome/browser/ui/startup/startup_tab.{h,cc}` (+12),
  `chrome/browser/ui/startup/startup_tab_provider.cc` (+12),
  `chrome/browser/ui/startup/startup_browser_creator_impl.cc` (+10),
  `chrome/browser/sessions/session_restore.cc` (+6),
  `chrome/browser/ui/tabs/pinned_tab_codec.cc` (+8).
- Size: ~300 new, ~50 hook.
- Helium overlap: `startup_browser_creator_impl.cc` (`helium/core/lazy-session-restore.patch`,
  `helium/core/onboarding-page.patch`); `session_restore.cc` (`helium/core/lazy-session-restore.patch`).
  The pinned tab part could wait for upstream's pinned tab work (#2931).

### 7. `spaces-interface.patch`

- Purpose: the single seam that `patches/neon/spaces/` includes (section 9).
- Adds: `chrome/browser/neon/containers_api.{h,cc}`, `BUILD.gn`.
- Hooks: none.
- Size: ~150 new.

### 8. `clear-browsing-data.patch` (M2 to M4)

- Adds: `chrome/browser/containers/used_container_storage_partitions.*`.
- Hooks: `chrome/browser/browsing_data/chrome_browsing_data_remover_delegate.cc` (+25).
- Helium overlap: none.

### 9. Later and optional

| Patch | Hooks | Helium overlap |
|---|---|---|
| `shared-zoom.patch` | `content/browser/host_zoom_map_impl.cc`, `content_browser_client.{h,cc}` | `helium/core/noise/core.patch` |
| `sync.patch` | `components/sync_sessions/synced_session.cc`, `chrome/browser/ui/sync/tab_contents_synced_tab_delegate.cc`, syncable prefs database | none found |
| `link-menu.patch` | `chrome/browser/renderer_context_menu/render_view_context_menu.{h,cc}` | `helium/core/degoogle-context-menu.patch`, `force-paste-action.patch`, `simplify-context-menu.patch`, `ungoogled-chromium/toggle-translation-via-switch.patch`, `helium/ui/translate-ui.patch` |
| `tab-menu.patch` | `chrome/browser/ui/tabs/tab_menu_model.cc` | `helium/core/close-tabs-to-left.patch`, `mute-tab-context-menu.patch`, `hibernate-tab-context-menu.patch`, `helium/ui/layout/vertical.patch`, `copy-url-tab-context-menu.patch` |
| Traffic Control | navigation throttle registration, settings | to be checked at M5 |

**M1 total**: patches 1 to 7, about 3,200 new lines (mostly verbatim Brave) and about 400 hook
lines in about 25 Chromium files, plus about 1,250 lines of ported unit tests if we build them.

## 9. Interface for spaces

Spaces should include only `chrome/browser/neon/containers_api.h`. Everything behind it maps to
Brave classes, so if Helium imports Brave's code, only `containers_api.cc` changes.

| Call | Purpose | Brave implementation |
|---|---|---|
| `std::vector<ContainerInfo> ListContainers(Profile*)` | Containers for the space list (`id`, `name`, `color`, `icon`) | `ContainersService::GetContainers()` (`containers_service.h:88`) |
| `std::optional<ContainerInfo> GetContainer(Profile*, std::string_view id)` | Includes removed-but-used containers | `ContainersService::GetRuntimeContainerById()` (`:82`) |
| `std::optional<std::string> CreateContainer(Profile*, name, color)` | New space | `ContainersSettingsHandler::AddContainer()` (`containers_settings_handler.cc:62-79`). It appends synchronously but does not return the id, so the adapter reads the last entry of `GetContainers()` afterwards. If that is too fragile, a 10-line `AddContainer` returning the id is the one deviation worth carrying in our copy (dvjn's service has exactly this). |
| `bool UpdateContainer(Profile*, id, name, color)` | Rename, recolor | `ContainersSettingsHandler::UpdateContainer()` (`:81-103`) |
| `bool DeleteContainer(Profile*, id)` | Delete space | `ContainersSettingsHandler::RemoveContainer()` (`:105-124`). Data is removed at the next start by orphan cleanup once no tab or session references it; spaces should close the space's tabs first. |
| `content::WebContents* OpenTabInContainer(BrowserWindowInterface*, const GURL&, id, index, foreground, pinned)` | Open a tab in a space | `NavigateParams` with `storage_partition_config = StoragePartitionConfig::Create(profile, "containers", id, profile->IsOffTheRecord())`, then `Navigate()`; same as `brave::OpenUrlInContainer` (`browser/ui/browser_commands.cc:1282-1305`) and `MaybeAddTabAtInheritingActiveContainer` (`:1229-1265`) |
| `std::string GetTabContainerId(content::WebContents*)` | Which space a tab is in | `containers::GetContainerIdForWebContents()` (`storage_partition_utils.h:64-65`) |
| `void AddObserver(Profile*, Observer*)`, `RemoveObserver` | React to list changes, including changes from sync | `ContainersService::AddObserver()` with `ContainersServiceObserver::OnContainersListChanged()` (`containers_service_observer.h:15-19`) |

Notes for R2:

- There is no "tab moved to another container" event because it cannot happen; a tab's
  partition is fixed when its `WebContents` is created. Spaces can observe the tab strip and read
  each new tab's container.
- "New tabs inherit the space" (PLAN decision 4) needs a spaces-owned rule for Ctrl+T and the
  new tab button: Brave `v1.100.58` only inherits for links, popups and split view. Brave
  `master`'s new `containers_navigator_utils.cc` shows where such a rule plugs in
  (`GetStoragePartitionConfigToInherit`, consulted from `CreateTargetContents`).
- The default space (no container) is the profile's default partition. It cannot be deleted and
  needs no container entry.

## Upstream churn

Between `v1.100.58` and `master` (`1d59f2fde0cac8fcb12edbfed1748bd8401ac569`, 2026-11-19), the
containers, Traffic Control and related override paths changed by about 956 added and 49 removed
lines in 24 files. Notable: `browser/containers/containers_navigator_utils.{h,cc}` (new; moves
`GetStoragePartitionConfigToInherit` out of the override and adds a "new tab target" setting),
`NewTabTarget` in `containers.mojom` and `brave.containers.new_tab_target` in `pref_names.h`, and
`browser_navigator_params.h`'s override removed in favour of a patch. All three branches
(`1.100.x`, `1.101.x`, `master`) pin the same Chromium, so taking a later commit at port time
costs nothing in Chromium terms. Re-check this before M1 starts and record the chosen commit in
`neon/THIRD_PARTY.md`.

## Not verified

- Nothing was built or run. All behaviour is read from source.
- Download resumption in a container after restart; DevTools cookie and storage domains on
  container targets; HTTP auth isolation; prerender and preconnect behaviour; push delivery
  failing at runtime (the routing code is clear, the end-to-end result is not tested).
- Whether Brave issues #57068 (container tabs restored in the default partition) and #58219
  (tab groups) still reproduce at `v1.100.58`.
- The reload path of a discarded container tab.
- Whether registering a `SYNCABLE_PREF` that is not in the syncable prefs database fails a check.
- The "on by default since June 2026" claim: at this commit `kContainers` is enabled by default
  (`features.cc:12`) and the build flag is on for desktop (`buildflags.gni:7`), but the menus and
  management UI pref defaults to off on the stable channel unless a field trial turns it on
  (`prefs_registration.cc:20-24`). The June date comes only from an issue #199 comment.
- Whether dvjn's code derives from Brave's MPL files.
- The legal reading of MPL-2.0 section 3.3 above.
