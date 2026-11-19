# Global context and per-space extensions (R3)

Status: design only, no code. Written 2026-11-19 against Helium 0.22.1 on Chromium 158.0.8127.36,
with the Neon containers and spaces patches applied (M1 to M3, M1b, M4 in progress).

Every claim carries a tag:

- **[code]** read in the patched tree at `~/projects/neon-linux/build/src`, in the Neon patches
  under `~/projects/neon/patches/neon/`, or in brave-core `v1.100.58` (commit `bd6d26d1`).
- **[doc]** read in public documentation, with the source named.
- **[assumed]** reasoned from code or general Chromium knowledge but not confirmed by reading the
  exact path, or behaviour that only running the browser can settle. Each one maps to an
  experiment in section 8.

Line numbers refer to the patched tree. Another agent was building in that tree while this was
written, so `chrome/browser/neon/` and `chrome/browser/ui/` line numbers may have moved.

## Summary

**The question.** The owner's proposal has three parts: (1) no visible space uses the default
storage partition, which becomes a "global" context for the browser's own pages and for
extensions; (2) extensions can be turned off per space; (3) pages that belong to no space open in
a floating window over the browser, as Arc's Little Arc does. The worry behind it: an extension
signed in to one account (a ChatGPT extension) acts on pages of every space.

**Finding 1: part 1 does not fix the owner's example. Part 2 does.** An extension's background
and pages always live in the default partition [code], and its content scripts run in every
renderer of the profile, container renderers included [code]. A ChatGPT extension that reads the
page in the work space and sends it to its background, which calls ChatGPT with the account it
is signed in to, behaves the same whether that account's cookies sit in "space 1" or in a
"global" context. Moving all spaces into containers closes a different leak: the extension's
ambient access to the first space's logins (its background `fetch` with credentials, the
`cookies` API, omnibox suggestions), which today ride on whatever the user signed in to in the
default space. Only a per-space switch stops the extension from seeing or acting on a space's
pages.

**Finding 2: a per-space switch is feasible with small hooks, and most of it reuses Chromium's
incognito seams.** A container has its own renderer processes (a `RenderProcessHost` is never
shared between storage partitions [code]), so "is this extension allowed in this process's
partition" can sit right next to every existing "is this extension allowed in incognito" check:
renderer activation, webRequest listener matching, declarativeNetRequest ruleset selection, and
messaging. The browser-side tab APIs need a few more one-line checks (debugger, script
injection, tab capture, tab URL scrubbing, webNavigation). About 60 hook lines in 13 Chromium
files, only 4 of which a Helium patch already touches. What cannot be gated: the `history`,
`downloads`, `bookmarks`, `sessions` and `topSites` APIs (profile-wide stores with no partition),
the existence and position of tabs (ids, window, index), the extension's own popup, and scripts
already injected until the tab reloads.

**Finding 3: "all spaces are containers" is expensive and mostly unrelated to security.** It
moves every Brave container limit onto all browsing: web push and notification clicks reach the
default partition only [code], speculation-rules prefetch is refused [code], installed web apps
open in the default partition, "Clear browsing data" and the "All sites" settings page only see
the default partition today [code], DevTools targets carry no container, WebHID is unavailable
(Brave test), and a downgrade to plain Helium restores every tab blank (R1). It needs a migration
for existing profiles, where only cookies can be copied cleanly. Several of those must be fixed
for non-default spaces anyway, but today the first space is a working escape hatch.

**Recommendation.**

1. Build the per-space extension switch now (patches X1 and X2 below). It secures the owner's
   example, it is independent of the partition model, and its hooks are small.
2. Keep decision 10 (the default partition is the first space) for now, and say plainly in the
   UI that extensions are global and "use the logins of" the first space. Rename nothing in code.
3. Route tabs that extensions open with a narrow, explicit rule (patch X3): extension pages and
   extension popup windows go to the global surface; everything else opens in the current space,
   with a per-extension override "Open this extension's sign-in pages in: current space / the
   extension's own context".
4. Build the floating window (patch X4) as an in-window overlay hosting a `views::WebView`, the
   pattern Chromium already uses for `ai_overlay_dialog_view_` in `ContentsContainerView`. It
   hosts extension pages, extension popup windows and `identity.launchWebAuthFlow`, and later a
   Glance-style link preview.
5. Decide "all spaces are containers" later, after X1 to X4 ship and after push, clear browsing
   data and the site data pages work in containers (they must for non-default spaces anyway).
   If the owner then still wants an empty global context, the switch is a small spaces-layer
   change plus a migration (patch X5), and X1 to X4 are reused unchanged.

**Main risks.**

1. Renderer-side gating relies on the renderer not having the extension loaded. Content scripts
   of a disabled extension stop only after the tab's process is replaced (reload or navigation to
   another site); unloading from a live process is possible through the existing
   `UnloadExtension` mojo call but is untested for this use [assumed].
2. Service worker and shared worker requests, and requests from the browser process, carry a
   process id that may not map to a tab. The gate keys on the process's partition, which covers
   workers, but browser-initiated requests (favicons, omnibox) are attributed to no partition and
   stay visible to webRequest [assumed].
3. The `history` API leaks every space's URLs to any extension with that permission. No hook can
   fix that without partitioning history, which PLAN keeps shared on purpose.
4. The floating window on macOS: overlapping two web contents in one widget relies on Chromium's
   layer-based Mac compositing. Chromium ships two such overlays today, but neither has been run on
   a Neon build [assumed].
5. Extension sign-in flows break whenever the sign-in tab lands in a partition other than the one
   the extension's background uses. This is true today for every non-default space and would
   become true for all spaces under part 1, unless X3 routes them.

**Proposed patches** (detail in section 7). New Neon files go under `chrome/browser/neon/extensions/`,
`chrome/browser/ui/neon/extensions/` and `chrome/browser/ui/views/neon/global/`, plus one small new
file in `extensions/browser/` because `//extensions` cannot depend on `//chrome`.

| # | Patch | Milestone | New | Hook lines | Chromium files hooked | Helium overlap |
|---|---|---|---|---|---|---|
| X1 | `extensions/space-gate.patch` | M4b | ~450 | ~60 | 13 | 4 files |
| X2 | `extensions/space-gate-ui.patch` | M4b | ~450 | ~20 | 3 | none |
| X3 | `extensions/tab-target.patch` | M4b | ~150 | ~10 | 3 | none |
| X4 | `ui/global-overlay.patch` | M5b | ~800 | ~10 | 1 to 2 | `browser_view.cc` (12 patches) if hooked there |
| X5 | `spaces/all-containers.patch` (only if chosen) | later | ~400 | ~40 | 6+ | see section 3 |

A first version (M4b) is X1 plus X2 plus X3. It needs no floating window: extension pages keep
opening as tabs, in the first space, as M1b already does.

## 1. What an extension reaches in a container tab today

Facts every channel rests on:

- An extension's own pages and its background service worker run in the default partition.
  `ChromeContentBrowserClient::GetStoragePartitionConfigForSite` returns
  `GetStoragePartitionConfigForExtensionId` for `chrome-extension://` sites
  (`chrome/browser/chrome_content_browser_client.cc:1825-1850`), which is the default partition
  for every ordinary extension [code; the isolated-storage case only applies to platform apps,
  assumed]. Neon keeps extension pages opened as tabs in the default partition on purpose
  (`patches/neon/containers/tab-partition.patch`, `GetStoragePartitionConfigToInherit`, the
  `kExtensionScheme` branch) [code].
- Container tabs share the profile's `BrowserContext`. Every per-context extension service
  (`ExtensionRegistry`, `EventRouter`, `RendererStartupHelper`, `RulesetManager`) is keyed on the
  `BrowserContext`, so containers are invisible to them [code].
- Extensions are loaded into every renderer of the context. `RendererStartupHelper::InitializeProcess`
  returns early only for another context (`extensions/browser/renderer_startup_helper.cc:329`) and
  filters each extension with `util::IsExtensionVisibleToContext(*ext, renderer_context)` (`:401`),
  which only looks at themes and `IsOffTheRecord()` (`extensions/browser/extension_util.cc:377-391`)
  [code].
- A renderer process belongs to one storage partition. `RenderProcessHostImpl::InSameStoragePartition`
  (`content/browser/renderer_host/render_process_host_impl.cc:4417-4420`) is part of
  `IsSuitableHost` (`:4974`), so container pages never share a process with default-partition
  pages [code].

| Channel | Works in a container tab? | Which partition's data it sees | Changes if nobody browses in the default partition? |
|---|---|---|---|
| Content scripts (manifest, `scripting.registerContentScripts`) | Yes: the extension is loaded in container renderers like any other [code, `renderer_startup_helper.cc:390-418`] | The page's own: DOM, `document.cookie` (non-HttpOnly), and `fetch` from the content script with the page's cookies, so the container's [assumed, standard MV3 behaviour] | No |
| `scripting.executeScript`, `tabs.executeScript` | Yes. Browser side checks host permission by tab (`extensions/browser/scripting_utils.cc:336`, `chrome/browser/extensions/api/tabs/tabs_api.cc:2811`), then the renderer runs it [code] | As content scripts | No |
| `tabs` API: URL, title, favicon | Yes. Container tabs are ordinary tabs of the window; URL and title are scrubbed only by permission (`ExtensionTabUtil::GetScrubTabBehavior`, `chrome/browser/extensions/extension_tab_util.cc:483-509`) [code] | n/a | No |
| `tabs.captureVisibleTab` | Yes, with `activeTab` or `<all_urls>` (`tabs_api.cc:2588-2630`) [code] | Pixels of the container page | No |
| `webRequest` | Yes. The proxying `URLLoaderFactory` is installed per frame with no partition check (`extensions/browser/api/web_request/web_request_api.cc`, `MaybeProxyURLLoaderFactory`), and listeners are matched with host permissions only (`extension_web_request_event_router.cc:3063-3130`) [code] | Container requests, including their `Cookie` and `Set-Cookie` headers with `extraHeaders` [assumed] | No |
| `declarativeNetRequest` | Yes. `RulesetManager::ShouldEvaluateRulesetForRequest` checks incognito, file access and host permissions, not the partition (`extensions/browser/api/declarative_net_request/ruleset_manager.cc:644-700`) [code] | Container requests | No |
| `cookies` API | Only the default partition. `GetCookieManager` returns `GetDefaultStoragePartition()->GetCookieManagerForBrowserProcess()` (`extensions/browser/api/cookies/cookies_api.cc:123`); change events too (`:267`) [code]. Brave does the same (R1, Brave #57209) | Default partition only | **Yes**: the extension no longer sees the user's site cookies, only what it or the global context set |
| Background or service worker `fetch` with credentials | n/a (runs in the default partition) | Default partition cookie jar [code, follows from the partition rule above] | **Yes**: no ambient sign-in. An extension that relies on the user's site session (clippers, translators, the ChatGPT extension) needs its own sign-in in the global context |
| `debugger` | Yes. `ExtensionMayAttachToAgentHost` checks profile, URL and guest partitions only (`chrome/browser/extensions/api/debugger/debugger_api.cc:184-196`, `:474-530`) [code] | Everything in the tab: CDP `Network.getCookies`, `Storage.*`, `Runtime.evaluate` in the container [assumed] | No |
| `webNavigation` | Yes. Events are broadcast to every listener of the context (`chrome/browser/extensions/api/web_navigation/web_navigation_api_helpers.cc:43-55`) [code] | URLs of container navigations | No |
| `externally_connectable`, `runtime.connect` from content scripts | Yes. `MessageService::OpenChannelToExtension` checks `externally_connectable` matches and incognito, not the partition (`extensions/browser/api/messaging/message_service.cc:448-680`) [code] | A container page can talk to the global background | No |
| Native messaging | Background only, partition irrelevant [code, by construction] | n/a | No |
| `identity.launchWebAuthFlow` | Not tied to a tab. `WebAuthFlow::Start` creates its own `WebContents` with `WebContents::CreateParams(profile_)` in the default partition and shows it in a `TYPE_POPUP` window (`chrome/browser/extensions/api/identity/web_auth_flow.cc:129-153`, `:287-310`) [code] | Default partition cookies of the identity provider | **Yes**: the provider asks the user to sign in again in the global context, which is the honest outcome |
| `chrome.tabs.create` | New tab gets no `source_site_instance` (`chrome/browser/extensions/open_tab_helper.cc:192-247`) [code], so Neon's provider puts it in the window's visible space; extension URLs stay in the default partition (`tab-partition.patch`) [code] | The current space | Only for sign-in tabs (section 5) |
| `chrome.windows.create` | Popup windows ask the provider, which answers with the last active window's space (`chrome/browser/ui/neon/spaces/spaces_window_controller.cc:143-172`) [code]; with `setSelfAsOpener` the extension frame's `SiteInstance` is the source (`windows_api.cc:766-779`) and the window lands in the default partition [code] | Current space, or default | As above |
| `history`, `downloads`, `bookmarks`, `sessions`, `topSites` | Profile-wide stores with no partition [code for history and bookmarks being profile keyed; assumed for the rest] | All spaces | No |

**What the all-containers model closes.** Ambient authority over the first space's logins: the
`cookies` API, background fetches with credentials, and browser-initiated requests that use the
default partition on behalf of every space (omnibox suggestions,
`chrome/browser/autocomplete/chrome_autocomplete_provider_client.cc:280`, and other
`GetDefaultStoragePartition()` users) [code]. Note that the omnibox one is a cross-space leak
today: suggestions typed in the work space go out with the first space's search engine cookies
[assumed from the code path].

**What it does not close.** Content scripts, injected scripts, webRequest, declarativeNetRequest,
debugger, tab capture, webNavigation and messaging all reach every space's pages and feed one
global background. The ChatGPT example lives here.

## 2. All spaces as containers: what assumes the default partition

Today non-default spaces already live with every item below, so most of this is not new cost;
what is new is losing the first space as the place where everything works.

| Area | Behaviour when browsing never uses the default partition | Brave | Evidence |
|---|---|---|---|
| Clear browsing data | Clears the default partition only. Neon has not ported Brave's delegate hook (R1 patch 8), so today it already misses non-default spaces | Clears every used container partition (`browser/browsing_data/brave_browsing_data_remover_delegate.cc:170-192`) | [code] `chrome/browser/browsing_data/chrome_browsing_data_remover_delegate.cc` uses `GetDefaultStoragePartition()` at `:295`, `:468`, `:521`, `:705`, `:986`, `:1395` |
| Site data settings ("All sites", "See all site data") | Empty: `SiteSettingsHandler` builds its model with `BrowsingDataModel::BuildFromDisk`, which reads the default partition | Nothing | [code] `site_settings_handler.cc:2308`, `components/browsing_data/content/browsing_data_model.cc:669-675` |
| Page info "cookies and site data" for a tab | Works: built from the tab's own partition | n/a | [code] `components/content_settings/browser/page_specific_content_settings.cc:645-650` |
| Downloads | Work, through the initiating frame's partition; resumption after restart unverified | Nothing | R1 [code], resumption [assumed] |
| Service workers | Isolated per container | Tested | R1 [code] |
| Web push | Delivered to the default partition's worker, so lost | Nothing | [code] `content/browser/push_messaging/push_messaging_router.cc:114-115` |
| Notification clicks and closes for persistent notifications | Dispatched to the default partition's worker | Nothing | [code] `content/browser/notifications/notification_event_dispatcher_impl.cc:366` |
| Payment handlers | Default partition only | Nothing | [code] `content/browser/payments/payment_app_provider_impl.cc:135` and others |
| Installed web apps | Launch in the default partition, so in the global context and signed out | `--container` for PWA launch (`chromium_src/chrome/browser/ui/web_applications/web_app_launch_process.cc`) | [code] Brave; Neon behaviour [assumed] |
| Password manager, autofill | Profile-keyed, so saved passwords fill in every space. Its own network calls use the default partition (`chrome_password_manager_client.cc:1359-1368`), which carry no user cookies that matter | Nothing | [code] for the network context, profile keying [assumed] |
| WebAuthn and passkeys | Platform authenticator is profile-level; works in containers | Nothing | [assumed] |
| Permissions | One content settings map per profile; Neon's `permission_util.cc` hunk stops Chromium from denying undelegated permissions in containers | Same hunk | [code] `components/permissions/permission_util.cc:681-692` |
| WebHID | Unavailable in container tabs | Asserted by test `HidUnavailableInContainer` | R1 [code]; WebUSB and Serial [assumed] |
| DevTools | Works; CDP targets carry no container and `Target.createTarget` cannot pick one | Brave #61986 open | R1 [code] |
| Prefetch and prerender | Speculation-rules prefetch refused (`kNonDefaultStoragePartition`) | Nothing | [code] `content/browser/preloading/prefetch/prefetch_service.cc:962-967`, prerender `prerender_host.cc:1919` |
| Favicons | Downloaded by the tab, stored in the profile's favicon database | n/a | [assumed] |
| Search suggestions | Fetched with the default partition's cookies, so logged-out suggestions | n/a | [code] `chrome_autocomplete_provider_client.cc:280`, `:625` |
| History | Profile-wide, shared by design | n/a | [code] |
| Session restore | Every tab carries the `containers+<uuid>:` prefix; plain Helium or containers off restores every tab blank instead of one space's worth | Same | R1 [code] |
| New tab page and WebUI in a container | Work: Ctrl+T opens the NTP in the visible space's container through the provider, and restored WebUI tabs anchor on the container (`tab-partition.patch`, `MaybeSetContainerSiteInstance`) | Restores WebUI in the default partition | [code] |
| Brand-new `about:blank` tab | Opens in the default partition: a blank page committed in a fixed-partition `SiteInstance` with no site trips a `CHECK` on its next navigation (PLAN M1b) | n/a | [code] |
| Platform SSO on a managed Mac | Skipped in every container tab (`patches/neon/containers/platform-auth.patch`), so enterprise SSO would never work | Same skip | [code] |
| Helium's bundled uBlock Origin | Runs everywhere, like every extension | n/a | [code] `patches/helium/core/ublock-reconfigure-defaults.patch` |

**Brave limits that would apply to all browsing:** `cookies` API blind to user cookies, tabs that
extensions open with an extension source landing outside the space, shared permissions, no web
push, broken notification clicks, no WebHID, no prefetch, no container in CDP, and blank tabs on
downgrade.

### Changes in the spaces layer

- **First run.** Seed one container-backed space ("Personal") instead of the default space.
  `SpacesService` today always keeps a `kDefaultSpaceId` space with an empty container
  (`chrome/browser/neon/spaces/spaces_service.cc:25`, `:188-200`) [code]; the reconciliation code
  would instead require at least one container space.
- **Existing profile.** Its tabs and logins sit in the default partition.
  - Cookies can be copied: read with `GetAllCookies` on the default partition's
    `CookieManager` and write with `SetCanonicalCookie` into the container partition's
    `CookieManager`, both browser-process calls that see HttpOnly cookies [assumed, standard
    `network::mojom::CookieManager` API]. Partitioned (CHIPS) cookies carry their key and should
    copy too [assumed].
  - Other site storage (localStorage, IndexedDB, Cache Storage, service worker registrations,
    OPFS, storage buckets) has no copy API between partitions. Copying directories on disk while
    the browser is closed is conceivable, since a container partition uses the same layout under
    `Storage/ext/containers/<hash>/` (R1), but the quota and buckets databases make it fragile
    [assumed]. In practice: copy cookies, accept that sites with storage-based sessions sign out.
  - Open tabs: reopen each default-partition tab in the new container with its history, as
    "Reopen in space" does, at first start after the upgrade [code: the mechanism exists in M4].
- **A default-partition tab in the window.** Some paths still create one: extension pages, a
  brand-new `about:blank`, `windows.create` with `setSelfAsOpener`, saved paths that lose the
  container, and today the "containers that no space uses count as the default space" rule
  (PLAN M2). Each needs a home: extension pages go to the global surface (X4), `about:blank`
  needs the anchoring fix extended to new tabs, and an orphaned tab would show in a hidden
  "Unsorted" page or be reopened into the visible space.
- **"The default space cannot be deleted"** becomes "the last space cannot be deleted"
  (`space_management.cc:83`, `space_edit_bubble.cc:444`, `space_switcher_row.cc:526`, `:569`
  check `is_default()` today [code]).

## 3. Turning an extension off for a space

### How Chromium keeps an extension out of incognito

Incognito is a separate `BrowserContext`, so the gate is mostly free: every service filters by
context and asks `util::IsIncognitoEnabled` at the crossing points [code]:

| Seam | Incognito check | File |
|---|---|---|
| Renderer activation | `IsExtensionVisibleToContext` | `extensions/browser/renderer_startup_helper.cc:401`, `:461`, `:508`, `:584`, `:605`; `extension_util.cc:377-391` |
| webRequest | `crosses_incognito` passed to `ListenerMatchesRequest` | `extension_web_request_event_router.cc:1694-1724`, `:3038-3056`, `:3063` |
| declarativeNetRequest | `is_incognito_context && !IsIncognitoEnabled` | `ruleset_manager.cc:667-672` |
| Messaging | `context->IsOffTheRecord() && !IsIncognitoEnabled` | `message_service.cc:665-680` |
| Tabs and windows | `GetTabById(..., include_incognito, ...)` searches the incognito profile only when allowed | `extension_tab_util.cc:618-673` |
| Events | `EventRouter` dispatch restricted by context and incognito | `extensions/browser/event_router.cc` |
| Debugger | `ExtensionMayAttachToTargetProfile(..., allow_incognito_access, ...)` | `debugger_api.cc:184-196` |

Per-extension site access and `runtime_blocked_hosts` are URL patterns
(`PermissionsData::GetPageAccess`, `extensions/common/permissions/permissions_data.cc:428`;
policy blocked hosts `:646`) [code]. The same URL appears in several spaces, and
`extensions/common` cannot see a partition, so they cannot express "off in this space".

### Which seams a partition gate can reuse

Containers share the `BrowserContext`, so the context and event seams do not apply. What does
apply is the process: each request, frame, worker and message source carries a
`ChildProcessId`, and each process has exactly one `StoragePartition`. So the gate is one
function, `IsExtensionBlockedInProcess(context, extension_id, process_id)`, called next to each
incognito check, plus a `WebContents` variant for the tab-based browser APIs.

Because `//extensions` cannot depend on `//chrome`, the gate is a new file
`extensions/browser/neon_partition_gate.{h,cc}` (about 60 lines) holding a callback that the
Neon code in `//chrome` sets at startup, the same shape as `SetNewTabContainerProvider`. No
`ExtensionsBrowserClient` virtual is added.

### Every hook

| # | Hook | File | Lines | Helium patches on the file |
|---|---|---|---|---|
| 1 | Skip the extension in `InitializeProcess`, `ActivateExtensionInProcess`, `OnExtensionLoaded` loops | `extensions/browser/renderer_startup_helper.cc` | ~10 | `helium/core/add-component-l10n-support.patch` |
| 2 | `ListenerMatchesRequest`: return false when the request's process is gated | `extensions/browser/api/web_request/extension_web_request_event_router.cc` | ~4 | `ungoogled-chromium/fix-building-without-safebrowsing.patch` |
| 3 | `ShouldEvaluateRulesetForRequest`: same, next to the incognito check | `extensions/browser/api/declarative_net_request/ruleset_manager.cc` | ~4 | none |
| 4 | `OpenChannelToExtension`: refuse when the source process is gated | `extensions/browser/api/messaging/message_service.cc` | ~6 | none |
| 5 | `ExtensionMayAttachToAgentHost`: refuse gated `WebContents` and processes | `chrome/browser/extensions/api/debugger/debugger_api.cc` | ~6 | `helium/ui/compact-debugger-warning.patch` |
| 6 | `GetScrubTabBehavior`: scrub URL, title and favicon of gated tabs (covers `tabs.query`, `get`, events, `windows` populate, context menus, commands) | `chrome/browser/extensions/extension_tab_util.cc` | ~6 | `helium/core/override-chrome-protocol.patch` |
| 7 | `CanAccessTarget`: clean error for `scripting.*` on a gated tab | `extensions/browser/scripting_utils.cc` | ~4 | none |
| 8 | `ExecuteCodeInTabFunction::CanExecuteScriptOnPage` and `TabsCaptureVisibleTabFunction::GetWebContentsForID` | `chrome/browser/extensions/api/tabs/tabs_api.cc` | ~8 | none |
| 9 | `DispatchEvent`: add a `will_dispatch_callback` that drops webNavigation events of gated tabs per extension | `chrome/browser/extensions/api/web_navigation/web_navigation_api_helpers.cc` | ~8 | none |
| 10 | Hide the extension's context menu items in gated tabs | `chrome/browser/extensions/context_menu_matcher.cc` | ~4 | none |
| 11 | `IsEnabled` and the grayscale decision: off in a gated tab | `chrome/browser/ui/extensions/extension_action_view_model.cc` | ~6 | none |
| 12 | BUILD deps for the gate | `extensions/browser/BUILD.gn` | ~2 | many, append-only |

Hook 1 is the real gate for page access (content scripts, injected scripts and page messaging
need the extension in the renderer: `ExtensionInjectionHost::Create` returns null for an
unknown extension, `extensions/renderer/extension_injection_host.cc:34-42`, and
`ScriptInjectionManager::HandleExecuteCode` then returns an empty result,
`script_injection_manager.cc:445-447`) [code]. Hooks 2 to 5 close the browser-side channels.
Hooks 6 to 10 stop the extension from seeing the space. Hook 11 is the visible state. Static
manifest content scripts reach the renderer through the user script loader's shared memory and
are matched against loaded extensions in the renderer [assumed, experiment E3].

### What cannot be gated

- `history`, `downloads`, `bookmarks`, `sessions`, `topSites`, `readingList`: profile-wide, no
  partition. An extension with `history` sees every space's URLs.
- The existence of gated tabs: ids, window, index, `active`, `audible` stay visible because
  `GetTabById` has no extension parameter (`extension_tab_util.cc:618`) and threading one through
  is a large change. `tabs.update({url})`, `tabs.remove`, `tabs.move` still act on a gated tab.
- The extension's own popup, options page and side panel: they are its pages, global by nature.
- Scripts already injected before the switch, until the tab reloads or changes process.
- Browser-initiated requests with no renderer (omnibox, favicons) seen by webRequest.
- Native messaging and the extension's own network traffic (by construction).

### Smallest honest design

- **Storage.** Pref `neon.spaces.extensions_off` on the profile: a dict from space id to a list
  of extension ids. Kept in the spaces service, not in `ExtensionPrefs`, so no extension-prefs
  schema change. Missing entry means on (Chromium's default for a regular profile).
- **Resolution.** partition config -> container id -> space (`SpacesService::SpaceForContainer`,
  already exists) -> list. The default partition maps to the first space while decision 10
  stands. Extension and WebUI processes are never gated.
- **Runtime toggling.** Turning an extension off sends `UnloadExtension` to that space's live
  processes and shows "Reload this space's tabs to finish"; turning it on sends `LoadExtensions`
  and the same hint [assumed, experiment E4]. Version one may simply require reload.
- **First version (M4b).** Hooks 1 to 6 and 11, the storage, and the UI in X2. Hooks 7 to 10 can
  follow; without them the gate still holds (scripts fail in the renderer, menu items appear but
  their `onClicked` events carry scrubbed tab data) but errors are less clear.

## 4. The floating global window

### How Arc and Zen do it

- **Arc, Little Arc** [doc, secondary sources: allthings.how "What's Little Arc"; Arc help centre
  article 19235387524503 was listed by search but refused automated fetches]: a small separate
  Arc window that opens for links clicked in other apps, with Cmd+Opt+click, or from the command
  bar. It has its own toolbar with an "Open in" button that moves the page into a space of the
  main window. Little Arc windows archive themselves after a while.
- **Arc, Peek**: an in-window preview for links opened from pinned tabs and favourites. Its exact
  look could not be read from Arc's documentation [doc unavailable].
- **Arc, extensions** [doc, search summary of Arc's help centre]: extensions belong to an Arc
  profile, and a profile scopes "logins, cookies, history, extensions" to one or more spaces. So
  in Arc a space's logins and its extensions travel together; spaces of one profile share logins.
- **Zen, Glance** [doc, `docs.zen-browser.app/user-manual/glance`]: Alt+click (configurable to
  Ctrl or Shift) previews a link "on top of your current tab"; links from Essentials and pinned
  tabs open in Glance automatically; clicking outside closes it; buttons expand it to a tab or
  split. Zen's docs say containers "don't separate ... extensions" and document no per-workspace
  extension switch [doc, `.../workspaces`, `.../extensions`].

### Options in Views

| Option | What it is | Effort | Hooks | macOS | Linux |
|---|---|---|---|---|---|
| A. In-window overlay | A Neon `views::View` painted to a layer, added on top of the contents area, holding a dimming scrim and a rounded `views::WebView`. Precedent: `ContentsContainerView` adds `ai_overlay_dialog_view_`, a `views::WebView` laid over the page (`chrome/browser/ui/views/frame/contents_container_view.cc:110-119`, `:612-633`) [code] | Medium (~800 lines with toolbar, sizing, focus, keyboard) | 0 to 5 lines: add the host as a child of `BrowserView` from Neon code and track the contents bounds with a `ViewObserver`; fall back to a one-line hook in `BrowserViewLayout` if foreign children get re-laid out [assumed, E6] | Same as the window: moves, resizes, full screen and Mission Control all follow, nothing for the window server to get wrong. Overlapping web contents in one widget is how Mac Views already composes these overlays [assumed, E7] | Same on X11 and Wayland |
| B. Borderless child widget | `views::Widget` `TYPE_WINDOW`, parent = browser window, centred | Medium | 0 | A child `NSWindow`: follows moves and full screen spaces [assumed]. Window-modal widgets would instead become sheets: Chromium uses `-beginSheet:` and skips its own scrim on Mac (`browser_view.cc:3675-3681`) [code], and sheets slide from the title bar rather than float centred | X11: positionable. Wayland: a child toplevel cannot place itself; the compositor decides [assumed]. Rounded corners and shadow differ per compositor |
| C. Popup `Browser` window | `TYPE_POPUP` browser, as `launchWebAuthFlow` already uses (`web_auth_flow.cc:295-310`) [code] | Low | 0 | A separate window: own Mission Control entry, its own full screen behaviour, can hide behind the main window | Same, placement up to the window manager |
| D. Tab-modal web dialog | `constrained_window::ShowWebModalDialogViews` with a `views::WebView`, as the payment handler does (`chrome/browser/ui/views/payments/payment_request_dialog_view.cc:182`, `payment_handler_web_flow_view_controller.cc:175-201`) [code] | Low | 0 | Tied to the active tab: switching tab or space hides it. Wrong lifetime for a global surface | Same |
| E. Existing Helium or Chromium surfaces | Side panel (already hosts extension side panels, which are global extension pages), document picture in picture (separate always-on-top window), Helium split view and zen mode (no floating surface) [code, patch list] | n/a | n/a | n/a | n/a |

**Recommendation: A.** It behaves identically on both platforms, it cannot be lost behind the
window, it follows full screen, and Chromium already lays a `WebView` over page contents in the
same way. Its `WebContents` is created by Neon in the default partition (or the extension's
partition) and owned by a per-window controller, so it survives space switches. It closes on
Escape or a click on the scrim, and has "Open in space" (reopen in the visible space, like
"Reopen in space") and "Open as tab" buttons. Keep C as the fallback for windows that must
outlive the browser window (an OAuth popup the extension positions itself): the window
controller can redirect `TYPE_POPUP` browsers with an extension opener into the overlay later.

What would use it: extension options and welcome pages (`chrome-extension://` tabs),
`windows.create({type: "popup"})` from an extension, `launchWebAuthFlow` (replace the popup in
`DisplayAuthPageInPopupWindow`, one hook in a file no Helium patch touches), and later a
Glance-style link preview.

## 5. Which partition an extension-opened tab gets

### What the API call tells us

`OpenTabHelper::OpenTab` receives the `ExtensionFunction` (`open_tab_helper.cc:155-160`)
[code], so the deciding code knows: the extension, the URL, the calling context (background
service worker, popup, options page, content script relay), `user_gesture()`, and whether an
`openerTabId` was passed. It does not know the intent. Signals, from strongest to weakest:

1. The URL is the extension's own page: global. Already implemented [code].
2. `launchWebAuthFlow`: global, already a separate default-partition `WebContents` [code].
3. A popup window from `windows.create`: almost always a sign-in or a small UI. Global.
4. An `openerTabId` naming a tab: the opener's space. Tab managers and keyboard navigation pass
   it or act on the current tab [assumed].
5. The URL's host matches one of the extension's explicit host permissions and the extension
   does not have broad host access (`<all_urls>` or `*://*/*`): likely its own service (the
   ChatGPT extension opening `chatgpt.com/auth/login`). Global [assumed heuristic].
6. Anything else: the current space. Password managers opening a vault entry's site have
   `<all_urls>` and fall here, which is right.

### Other browsers

- **Firefox** lets the extension choose: `tabs.create({cookieStoreId})`, "only available if the
  extension has the "cookies" permission" [doc, MDN tabs.create]. Content scripts can be
  registered for chosen `cookieStoreIds`, and webRequest reports each request's `cookieStoreId`
  [doc, MDN "Work with contextual identities"]. Without `cookieStoreId` a tab opens in the
  default container [assumed; MDN does not say]. No per-container extension switch exists
  [doc, Zen and MDN say nothing of one].
- **Brave**: tabs opened by extensions land in the default partition, and its open issue
  #60927 tracks it (R1) [code].
- **Neon today**: the visible space, except extension URLs (default) and `setSelfAsOpener`
  windows (default) [code].

### Recommended rule

Signals 1 to 3 go to the global surface, signal 4 to the opener's space, signal 6 to the current
space, and signal 5 to global only when the owner opts in to the heuristic. Plus a per-extension
override in the extension's context menu: "Open this extension's pages in: current space /
global window". Implementation: a `ScopedNewTabStoragePartition` (already in
`containers_navigator_utils.h`) set from three lines in `OpenTabHelper::OpenTab` and
`WindowsCreateFunction`, with the decision in a Neon file.

Under decision 10, "global" and the first space are the same partition, so signals 1 to 3 and 5
already make extension sign-in work and keep it out of work spaces.

## 6. User experience

### Wording

- Extensions menu header: "Extensions are shared by all spaces". Under each extension:
  "On in this space" switch.
- Space edit bubble: a section "Extensions in this space" with one switch per extension.
- Settings, extensions subpage: per extension, "Runs in: All spaces / Only in ..." with a list.
- Under decision 10, the first space shows a small note in its edit bubble: "Extensions sign in
  here". Under all-containers, the overlay title reads "Extensions" and the extension page's name.
- Toolbar button when off: grey icon (Chromium's grayscale state, hook 11), tooltip "Off in
  <space>", click opens a one-item menu "Turn on in <space>".
- A newly installed extension is on in every space, and the install bubble adds one line:
  "Runs in all spaces. Change". Helium's bundled uBlock Origin stays on everywhere.
- A new space starts with every extension on.

### Alternatives compared

| | Owner's proposal (all containers, global default, gate, overlay) | Recommended (default stays first space, gate, tab rule, overlay) | Gate only (no overlay, no tab rule) | Extension "home space" (each extension runs in all spaces or exactly one) |
|---|---|---|---|---|
| Stops the ChatGPT example | Yes, through the gate | Yes, through the gate | Yes | Yes |
| Ambient access to a space's logins | None: extensions see only the global context | Extensions see the first space's logins | As recommended | As recommended |
| Extension sign-in | Separate sign-in in the overlay; users sign in twice to services they also use in a space | Sign in once, in the first space or overlay | Breaks when the sign-in tab lands in another space (today's behaviour) | As recommended |
| Push, PWAs, prefetch, CBD, site data | Broken for all browsing until fixed | Work in the first space; other spaces as today | As today | As today |
| Migration | Needed; only cookies copy cleanly | None | None | None |
| Clarity | Highest: "global" is a real place | Clear if the wording above is used | Partial | Simple: one control per extension |
| Hook lines | ~60 (gate) + ~20 + ~10 + ~10 + ~40 plus push, PWA, CBD and site data fixes, several in `content/` | ~100 | ~80 | ~100 (same hooks, different UI) |
| Upstream drift | High: `content/` push and notification routing, CBD delegate, site settings, web app launch | Moderate: 13 files, 4 shared with Helium, mostly `extensions/` code that changes slowly | Lowest | Moderate |

The "home space" column is the same engineering as the recommendation with a narrower UI. It
fits the ChatGPT case well (put it in "Personal") and is easier to explain, but cannot express
"on everywhere except Work". It can be offered as the settings view of the same pref.

A fifth option was considered and rejected: moving extensions into a new non-default
"global" partition by changing `GetStoragePartitionConfigForSite` for `chrome-extension://`
sites. The `cookies` API, extension service worker registration and every extension's existing
IndexedDB and localStorage are tied to the default partition, so it would break more than
all-containers does [assumed from the code paths in section 1].

## 7. Patch breakdown

### X1 `neon/extensions/space-gate.patch` (M4b)

- Adds `extensions/browser/neon_partition_gate.{h,cc}` (callback holder, ~60),
  `chrome/browser/neon/extensions/space_extension_policy.{h,cc}` (pref, resolution, runtime
  toggle, ~300), `BUILD.gn` (~30).
- Hooks 1 to 6 now, 7 to 10 in the same patch if time allows, 12 (section 3). ~60 lines.
- Overlap: `renderer_startup_helper.cc`, `extension_web_request_event_router.cc`,
  `debugger_api.cc`, `extension_tab_util.cc` each carry one Helium or ungoogled patch; the hunks
  sit away from theirs [assumed until refreshed].

### X2 `neon/extensions/space-gate-ui.patch` (M4b)

- Adds the switch list to the space edit bubble and the spaces settings handler (Neon files),
  the toolbar tooltip and menu item, and the install bubble line.
- Hooks: `extension_action_view_model.cc` (hook 11), `extension_context_menu_model.cc` (one
  item), and the install bubble view (Helium's `clean-up-installed-extension-bubble.patch` owns
  that file, so this line is optional).

### X3 `neon/extensions/tab-target.patch` (M4b)

- Adds `chrome/browser/neon/extensions/extension_tab_target.{h,cc}` (~150).
- Hooks: `open_tab_helper.cc` (~3), `windows_api.cc` (~3), optionally `web_auth_flow.cc` (~3).

### X4 `neon/ui/global-overlay.patch` (M5b)

- Adds `chrome/browser/ui/views/neon/global/global_overlay_{view,controller}.{h,cc}`, toolbar
  row, scrim, sizing, focus trap, accessibility, ~800.
- Hooks: none if E6 passes, else ~5 in `browser_view_layout.cc` or `browser_view.cc`. Routing
  extension pages into it touches `tab-partition.patch` (our file).

### X5 `neon/spaces/all-containers.patch` (only if chosen, later)

- Spaces service: seed a container space, drop `kDefaultSpaceId` as a visible space, migrate
  cookies and tabs at first start (~400).
- Required companions: R1's `clear-browsing-data.patch` (needed anyway), a site data settings
  fix (`site_settings_handler.cc`, enumerate container partitions), push and notification
  routing (`push_messaging_router.cc`, `notification_event_dispatcher_impl.cc`: look up the
  partition the registration lives in; a `content/` change with real drift), web app launch in a
  container, and the `about:blank` new-tab anchoring.

### Milestones

| Milestone | Patches | Done when |
|---|---|---|
| M4b | X1, X2, X3 | A test extension with a content script, a webRequest listener, a DNR rule and `externally_connectable` is turned off in one space: none of them fire there and all fire in another space; its toolbar icon is grey there; its sign-in tab opens in the first space |
| M5b | X4 | Extension options pages, extension popup windows and `launchWebAuthFlow` open in the overlay; "Open in space" works |
| Later | X5 and companions | Owner decision after M4b and M5b |

## 8. Experiments to run first

| # | Experiment | Confirms |
|---|---|---|
| E1 | In a container tab, a test extension's content script reads `document.cookie` and fetches the page origin with credentials; its background fetches the same origin | Section 1 rows for content scripts and background fetch |
| E2 | `chrome.debugger.attach` to a container tab, then `Network.getCookies` | Debugger reaches container cookies |
| E3 | With hook 1 only, a manifest content script and `scripting.executeScript` in a gated container | The renderer gate stops both |
| E4 | Toggle an extension off for a space with live tabs, call `UnloadExtension` on those processes, check that new injections stop and what happens to existing ones | Runtime toggling |
| E5 | `webRequest` and DNR on requests from a container's service worker | Process-based gating covers workers |
| E6 | Add a child view to `BrowserView` from Neon code and resize, enter full screen, toggle vertical tabs | Overlay needs no layout hook |
| E7 | Overlay `WebView` above page contents on macOS: clicks, scrolling, IME, focus, full screen space | Option A on the owner's platform |
| E8 | Push subscription in a container (any web push demo), then close the tab and send a push | Push routing gap at runtime |
| E9 | Copy cookies from the default partition into a fresh container through `CookieManager`, then load a signed-in site | Cookie migration |
| E10 | ChatGPT-like flow: extension opens its login page with `tabs.create` while a work space is visible, with and without X3 | Sign-in routing |

## 9. Decisions for the owner

1. Keep the default partition as the first space (recommended for now), or make every visible
   space a container now and accept the migration and the push, web app and site data work?
2. Per-space control shape: a switch per extension per space (recommended), or "runs in all
   spaces or only in one"?
3. Is "extensions sign in as your first space" acceptable wording, or must extension sign-ins be
   separate from every space (which forces all-containers)?
4. Should signal 5 (narrow host permissions) send sign-in tabs to the global surface by default?
5. Is the floating overlay wanted for links too (Glance on modifier click), or only for
   extension and global pages?
6. Should a gated extension's popup still open in that space (global surface) or be disabled
   with the button? Recommended: disabled, with "Turn on in this space".

## 10. Not determined

- Nothing was built or run. Every [assumed] item above maps to an experiment.
- Arc's own help pages refused automated fetches; Little Arc and Peek behaviour comes from a
  third-party article and a search summary.
- Whether static manifest content scripts check the renderer's extension registry at match time
  (E3).
- Whether `UnloadExtension` on a live process is safe for a still-enabled extension (E4).
- Firefox's behaviour for `tabs.create` without `cookieStoreId` (MDN does not say).
- Behaviour of Wayland compositors with a centred child toplevel (only relevant to option B).
- Whether the omnibox suggestion cookie leak happens with Helium's default search engines and
  settings (Helium may disable suggestions by default).
