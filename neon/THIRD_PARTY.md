# Third-party code in Neon

Code that Neon copies from other projects. Each entry gives the source, the exact revision, the
license and where the code lives. Patches that carry imported code start with the license
notice Helium already uses for its Brave imports (see `patches/brave/custom-importer.patch`).

## Brave containers

| | |
|---|---|
| Source | [brave/brave-core](https://github.com/brave/brave-core) |
| Tag | `v1.100.58` |
| Commit | `bd6d26d1bced03c17a98baa86d2cd3425d543d1b` (2026-11-18) |
| License | MPL-2.0, no "Incompatible With Secondary Licenses" notice |
| Imported in | M1, patches under `patches/neon/containers/` |

Ported files keep Brave's MPL-2.0 header unchanged. MPL-2.0 is file-level, so Neon's changes to
those files stay under MPL-2.0. Section 3.3 of the MPL lets them be combined with Neon's GPL-3.0
code. Each patch that carries Brave code starts with the MPL notice, the Brave and Neon
copyright lines and the GPL-3.0 alternative, as Helium's own Brave imports do.

### Paths

Brave's tree is mapped onto Chromium's: `brave/components/containers/` becomes
`components/containers/` and `brave/browser/containers/` becomes `chrome/browser/containers/`.
Include paths, GN labels and header guards are rewritten to match (`BRAVE_COMPONENTS_` becomes
`COMPONENTS_`, `BRAVE_BROWSER_` becomes `CHROME_BROWSER_`). Class names, file names and pref
names (`brave.containers.*`) are Brave's.

| Neon path | Brave path | Patch |
|---|---|---|
| `components/containers/buildflags/` | same under `brave/` | `core` |
| `components/containers/core/common/` (`features`, `switches.h`) | same | `core` |
| `components/containers/core/mojom/` | same | `core` |
| `components/containers/core/browser/` (`containers_service`, `containers_service_observer.h`, `container_specifier.h`, `containers_settings_handler`, `prefs`, `pref_names.h`, `prefs_registration`, `temporary_container`, `unknown_container`, `command_line_container`) | same | `core` |
| `components/containers/content/browser/` (`storage_partition_utils`, `containers_web_contents_user_data`, `session_utils`, `tab_restore_utils`) | same | `content-partition` |
| `chrome/browser/containers/` (`containers_service_factory`, `containers_service_delegate`, `container_tab_tracker`) | `browser/containers/` | `browser-service` |
| `chrome/browser/containers/containers_navigator_utils` | `chromium_src/chrome/browser/ui/navigator/browser_navigator.cc` and `chromium_src/chrome/browser/ui/browser_tabrestore.cc` (helpers moved into a file of their own, as Brave `master` does) | `tab-partition` |
| `chrome/browser/containers/container_specifier_utils` | `browser/containers/` | `command-line` |
| Hunks in Chromium files | Brave `chromium_src/` overrides and `patches/*.patch` for the same files, rewritten as plain hunks | `content-partition`, `session-persistence`, `browser-service`, `tab-partition`, `command-line` |
| `components/containers/content/browser/platform_auth_helper` | `browser/enterprise/platform_auth_helper.cc` (moved into the containers component, namespace `containers`), with the hunk from `rewrite/chrome/browser/enterprise/platform_auth/platform_auth_navigation_throttle.cc.yaml` | `platform-auth` (M1b) |
| `chrome/browser/containers/used_container_storage_partitions` | `browser/containers/` | `clear-browsing-data` (M6) |
| Hunk in `ChromeBrowsingDataRemoverDelegate::RemoveEmbedderData` | `BraveBrowsingDataRemoverDelegate::RemoveEmbedderData` in `browser/browsing_data/brave_browsing_data_remover_delegate.cc` (the containers block only) | `clear-browsing-data` (M6) |

### Changes from Brave

- `default_containers_list` is not imported. `brave.containers.list` starts empty and is
  registered as a local pref instead of a syncable one.
- `brave.containers.enabled` (the switch for menus and management UI) defaults to on for every
  channel. Brave turns it off on stable unless a field trial says otherwise.
- `temporary_container.cc` does not use the BIP-39 word list or Brave's Nala colours: generated
  names are `Temporary <4 hex digits>` and colours come from a fixed list of eight.
- Unit tests, browser tests and their GN targets are not imported.
- Clear browsing data (M6): the clear-on-exit observer in
  `chrome_browsing_data_lifetime_manager.cc` also waits for the per-container removals the
  delegate queues; in Brave the browser can exit before they run.

Not imported: settings UI, tab and hover card indicators, page action, context menus, Traffic
Control, sync, shared zoom, Shields and AI chat hooks, PWA `--container`,
Brave's split view helper (Neon's M1b inherits the container for new split tabs its own way).
