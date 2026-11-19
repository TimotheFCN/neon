# Linux dev build

How to build, run and change Neon on the Linux dev VM. Everything here was measured
on that VM in November 2026 with Helium 0.22.1 (Chromium 158.0.8127.36), building from
`~/projects/neon-linux`.

## The short version

The toolchain runs in a Docker container built from upstream's `docker/build.Dockerfile` plus a
thin Neon layer. The Chromium tree, the build output and all logs live on the host under
`~/projects/neon-linux/build/`, bind-mounted at the same path inside the container, so paths are
identical inside and outside.

```sh
cd ~/projects/neon-linux
neon/dev-container.sh start         # once per boot; idempotent
neon/dev-container.sh sh            # shell with upstream's `he` commands and the Neon helpers
```

Inside that shell:

```sh
neon_build                          # incremental build of chrome and chromedriver, 8 jobs, nice
neon/browser.sh start               # Xvfb plus the browser with a throwaway profile
neon/browser.sh shot build/shot.png # screenshot of the whole X screen
neon_cdp open https://example.com   # drive pages over the DevTools protocol
neon/browser.sh stop
```

From the host you can run any of these without an interactive shell, for example
`neon/dev-container.sh sh 'neon_build'`.

The tree at `~/projects/neon-linux/build/src` is shared by every agent on the VM. Only the agent
that was told it owns the tree may change it or build in it, and only one build runs at a time.

## Why a container

The source tarball ships its own clang, lld, rustc, gn, siso and Debian sysroot, so the build
needs no compiler on the host. What it does need are the `-dev` packages that GN probes with
`pkg-config` (GTK, NSS, PipeWire and so on), quilt for the patch queue, and Xvfb plus the X
libraries to run the browser. Upstream already lists those in `docker/build.Dockerfile`, and its
release build runs in that image, so the dev flow uses it too. The host stays clean and nothing
had to be installed on it.

The Neon layer (`neon/docker/dev.Dockerfile`) adds quilt, xdotool, `python3-websocket` for the
DevTools client, and Noto fonts.

The container is long-lived (`sleep infinity` as PID 1 behind `--init`) and every command runs
through `docker exec`, so a build started detached keeps running after your shell exits. It uses:

- `--memory 10g --memory-swap 14g`. Docker containers run outside the T3 agent cgroup, so this is
  the only thing that stops a runaway build from starving the other agents. Override with
  `NEON_DEV_MEMORY` and `NEON_DEV_MEMORY_SWAP`.
- `--network host`, so the browser's DevTools port binds to the host's 127.0.0.1 without
  publishing anything. Nothing listens on a public interface.
- `-u $(id -u):$(id -g)`, so every file in `build/` and `patches/` belongs to `agent`.
- The core repo (`~/projects/neon`, the submodule's origin) is also mounted, so `he pull` and git
  inside `helium-chromium` work in the container.

## Files

Neon's Linux helpers live in `~/projects/neon-linux/neon/`. Upstream scripts are not modified.

| File | Runs on | Does |
| --- | --- | --- |
| `neon/dev-container.sh` | host | `image`, `start`, `sh [cmd]`, `stop`, `status` |
| `neon/docker/dev.Dockerfile` | host | Neon layer on top of upstream's build image |
| `neon/env.sh` | container | sources `scripts/dev.sh`, sets sccache and job count, defines `neon_build`, `neon_cdp` |
| `neon/browser.sh` | container | Xvfb, throwaway profile, screenshot, stop |
| `neon/cdp.py` | container | small DevTools protocol client |
| `neon/quilt_place.py` | container | move a patch into the core or platform block without popping |
| `neon/memlog.sh` | host | sample host and container memory into a log while a build runs |

Under `build/` (ignored by git):

| Path | Holds |
| --- | --- |
| `build/src` | Chromium tree with all patches applied (quilt state in `build/src/.pc`) |
| `build/src/out/Default` | build output, `helium` binary |
| `build/download_cache` | source tarballs from tarballs.imput.net |
| `build/testdata_cache` | Google's full and testdata tarballs, used to make test targets buildable |
| `build/logs` | build, setup, browser and memory logs |
| `build/run` | pid files of the running browser and Xvfb |

## First setup

Only needed on a fresh VM or after `he reset`. Today the tree already exists, so skip to
[incremental build](#incremental-build).

```sh
cd ~/projects/neon-linux
git submodule update --init
neon/dev-container.sh image         # upstream image, then the Neon layer (about 1 minute cached)
neon/dev-container.sh start
mkdir -p build/logs
docker exec -d -w "$PWD" neon-dev bash -c \
  '. neon/env.sh && { time he setup; } > build/logs/setup.log 2>&1'
```

`he setup` downloads the prepared source tarballs (3.3 GB), unpacks them, prunes binaries, copies
Helium's resources, writes `out/Default/args.gn` (upstream's flags with `is_official_build`
replaced by `is_component_build`), merges the patch series, applies all 368 patches with
`quilt push -a --refresh` and runs `gn gen`. It took 111 seconds here.

Then the first full build, detached so it survives your shell, with a memory log next to it:

```sh
docker exec -d -w "$PWD" neon-dev bash -c \
  '. neon/env.sh && { time neon_build; echo EXIT=$?; } > build/logs/build-full.log 2>&1'
setsid neon/memlog.sh build/logs/mem.log &
```

Watch it with `tail -f build/logs/build-full.log` and `tail build/logs/mem.log`. If the build is
interrupted, run the same command again: siso resumes where it stopped. Stop the memory logger
with `pkill -f 'memlog[.]sh'` when the build is done.

### Making the test targets buildable

The prepared tarball leaves out test data, and Helium's pruning removes some test fixtures. To
build `unit_tests`, `browser_tests` or `interactive_ui_tests` you must add them back once per
source tree. This only adds files that do not exist yet. It never overwrites a patched file.

```sh
cd ~/projects/neon-linux/build
mkdir -p testdata_cache && cd testdata_cache
base=https://commondatastorage.googleapis.com/chromium-browser-official
curl -fLO $base/chromium-158.0.8127.36-testdata.tar.xz   # 1.5 GB
curl -fLO $base/chromium-158.0.8127.36.tar.xz            # 4.2 GB, full source
curl -fsSL $base/chromium-158.0.8127.36-testdata.tar.xz.hashes | grep sha256
curl -fsSL $base/chromium-158.0.8127.36.tar.xz.hashes | grep sha256
sha256sum chromium-158.0.8127.36*.tar.xz
```

Then, inside the container (`neon/dev-container.sh sh`):

```sh
cd build/src
tar -xJf ../testdata_cache/chromium-158.0.8127.36-testdata.tar.xz \
  --strip-components=1 --skip-old-files
```

That still leaves 363 files that the build graph references but neither tarball provides
(chromecast's cast_core gRPC sources, chromium-bidi, closure library, and fixtures such as
`bigbuck.webm` that `pruning.list` deletes). This lists them and restores only those from the full
tarball:

```sh
cd build/src
tar -tJf ../testdata_cache/chromium-158.0.8127.36.tar.xz \
  | sed 's|^chromium-158.0.8127.36/||' | grep -v '/$' > ../testdata_cache/full-list.txt
python3 - <<'EOF'
import os, re
full = [l.rstrip('\n') for l in open('../testdata_cache/full-list.txt')]
missing = {p for p in full if not os.path.lexists(p)}
refs, tok = set(), re.compile(r'\.\./\.\./([^\s:|$]+)')
for root, _, files in os.walk('out/Default'):
    for f in files:
        if f.endswith('.ninja'):
            for line in open(os.path.join(root, f), errors='replace'):
                refs.update(tok.findall(line))
with open('../testdata_cache/restore.txt', 'w') as out:
    for p in sorted(refs & missing):
        out.write('chromium-158.0.8127.36/' + p + '\n')
EOF
tar -xJf ../testdata_cache/chromium-158.0.8127.36.tar.xz \
  --strip-components=1 --skip-old-files -T ../testdata_cache/restore.txt
```

Run it after `gn gen`, because it reads the generated ninja files. The extra files are for local
tests only. A release build always starts from a fresh tree via `scripts/docker-build.sh`, so none
of this reaches a shipped binary.

## Incremental build

```sh
neon/dev-container.sh sh 'neon_build'
```

`neon_build` is upstream's `build` function (autoninja with siso, targets `chrome chromedriver`,
`-k 0`) plus `nice -n 10` and `-j $NEON_JOBS` (default 8). Extra arguments are added as targets:
`neon_build unit_tests`. Upstream's `he build` also works, but it lets autoninja pick the job count
(20 on this VM), which is more than the memory allows.

For a long build, start it detached as in [first setup](#first-setup) and log to `build/logs/`.

## Measured numbers

| What | Result |
| --- | --- |
| `he setup` (download 3.3 GB, unpack, patch, gn gen) | 111 s |
| Full build of `chrome chromedriver`, 57,105 steps, `-j 8` | 3 h 08 min wall (1,484 min CPU) |
| Change one `.cc` under `chrome/browser/ui/views/` (`frame/browser_view.cc`) | 25 s (1 compile, relink `helium`) |
| `touch` the same `.cc` without changing it | 31 s, siso rebuilds on mtime |
| `touch` `chrome/browser/ui/views/frame/browser_view.h` | 4 min 46 s, 222 steps |
| Peak anonymous memory of the container during the full build | 8.0 GB |
| Lowest host `MemAvailable` during the full build (other agents running) | 2.4 GB |
| OOM kills inside the container | 0 |
| Source tree `build/src` without `out/` | 12 GB, 19 GB with test data added |
| Build output `out/Default` after the full build | 16 GB |
| Build output now, with the partial test builds | 20 GB |
| `build/download_cache` | 3.3 GB |
| `build/testdata_cache` (optional, can be deleted after extraction) | 5.5 GB |
| Everything under `build/` today | 48 GB |

Parallelism: `-j 8` proved safe while other agents used about 10 GB. Peak anonymous memory was
8.0 GB against the 10 GB container limit, so do not go above 8. If the hypervisor shrinks the VM
to 12 GB, or `neon/memlog.sh` shows host available memory under 2 GB, use `NEON_JOBS=6`. siso
cannot change its job count while running. Stop the build and start it again with the new value;
finished steps are not redone. (Interrupting a running build was not tested in this setup.)

The component build keeps relinks short: most code lives in about 550 shared libraries next to
the binary, and `chrome/browser/ui` links straight into `out/Default/helium`, which relinks in
about 3 to 6 seconds.

### sccache does nothing here

`he setup` sets `cc_wrapper = "sccache"` because `SCCACHE_*` variables are set. sccache 0.10
refuses every Chromium 158 compile (`-fmodules` for 42,423 calls, an unknown `-Xclang` flag for the
other 4,506), so it caches nothing and only adds a pass-through. Rebuilds after `quilt pop` and
`quilt push` therefore cost real compile time. It is left on to stay close to upstream. Check with
`neon/dev-container.sh sh 'sccache --show-stats'`.

## Running the browser

Upstream's `he run` uses a fixed profile under `$HOME` and the Chromium sandbox, which does not
start inside Docker (the default seccomp profile blocks the user namespaces it needs). Use
`neon/browser.sh` instead:

```sh
neon/dev-container.sh sh 'neon/browser.sh start'
neon/dev-container.sh sh 'NEON_VERTICAL_TABS=1 neon/browser.sh start https://example.com'
neon/dev-container.sh sh 'neon/browser.sh status'
neon/dev-container.sh sh 'neon/browser.sh stop'
```

`start` launches `Xvfb :99` (1600x1000, 24 bit) and `out/Default/helium` with:

- a fresh profile from `mktemp -d /tmp/neon-profile-XXXXXX` inside the container, deleted by
  `stop` and gone anyway when the container is removed
- `--no-first-run --no-default-browser-check --no-sandbox --password-store=basic --disable-gpu`
- `--remote-debugging-port=9333`, bound to 127.0.0.1
- `--window-position=0,0` and a window the size of the screen, so a screenshot of the X root is a
  screenshot of the browser window

Extra arguments after `start` go to the browser. Environment: `NEON_DISPLAY` (default 99),
`NEON_CDP_PORT` (default 9333), `NEON_SCREEN` (default 1600x1000). The browser log is
`build/logs/browser.log`. Check that nothing else listens on the port first: `ss -ltn | grep 9333`.

`NEON_VERTICAL_TABS=1` seeds the profile's `Preferences` with `{"helium":{"browser":{"layout":2}}}`.
That pref (`prefs::kHeliumLayout`) is Helium's layout switch: 0 classic, 1 compact, 2 vertical,
3 dynamic. The same mechanism works for any other profile pref you need at startup.

Always run `neon/browser.sh stop` when you are done. It stops the browser and Xvfb and deletes the
profile.

## Screenshot of the browser window

```sh
neon/dev-container.sh sh 'neon/browser.sh shot build/screenshot.png'
```

This is ImageMagick's `import -window root` on the Xvfb display, so it captures the real browser
UI (toolbar, tab strip, side panels), not only page content. Read the PNG with your image viewer
tool. Example from this setup with vertical tabs:
`~/projects/neon-linux/build/screenshot-vertical-tabs.png`.

For a single view or an element, `xdotool` is installed: `DISPLAY=:99 xdotool search --name
Helium` lists window ids, and `import -window <id>` captures one.

## Driving pages over the DevTools protocol

`neon_cdp` (`neon/cdp.py`) talks to the port from `browser.sh`:

```sh
neon/dev-container.sh sh '
id=$(neon_cdp open https://en.wikipedia.org/wiki/Helium)
neon_cdp eval "document.title" --target $id
neon_cdp eval "document.querySelector(\"#mw-content-text p:not(.mw-empty-elt)\").textContent"  --target $id
neon_cdp targets
neon_cdp call Browser.getVersion --browser
neon_cdp call Page.captureScreenshot "{\"format\":\"png\"}" --target $id > /dev/null
'
```

- `open URL` opens a tab, waits until the real document has loaded and prints its target id.
- `eval EXPR` evaluates in a page (first page if no `--target`) and prints the value. Promises
  are awaited.
- `call METHOD [JSON]` sends any protocol command to a page, or to the browser with `--browser`.
- `targets` and `version` list pages and the browser version.

Because the container uses host networking, you can also reach `http://127.0.0.1:9333` from the
host, for example with Playwright's `chromium.connectOverCDP('http://127.0.0.1:9333')` or Node's
built-in `WebSocket`. The DevTools protocol is also the way to inspect browser UI from a script:
`chrome://` pages are targets too, and `--enable-ui-devtools=PORT` (what `he run` passes) exposes
the views hierarchy.

## How the patch series is laid out

Two repos contribute patches:

- Core (`~/projects/neon`, checked out as `neon-linux/helium-chromium`): `patches/series`, 362
  patches shared by all platforms. Neon's shared patches go in `patches/neon/` and are listed at
  the end of this series.
- Platform (`~/projects/neon-linux`): `patches/series`, 7 Linux-only patches. Neon's Linux-only
  patches go in this repo's `patches/neon/` at the end of this series.

quilt needs one series, so the dev flow merges them. `he merge` (run by `he setup`):

1. copies every core patch file into `neon-linux/patches/`,
2. saves the core series as `patches/series.prepend` and the platform series as
   `patches/series.orig`,
3. writes `patches/series.merged` (core first, then platform) and removes `patches/series`.

`dev.sh` points quilt at `patches/series.merged`. While merged, `git status` in `neon-linux`
shows `patches/series` as deleted and all the core patch directories as untracked. That is
normal and is the state the tree is left in.

`he unmerge` reverses it, and position decides where each patch goes:

- every patch before the first platform patch is moved to `helium-chromium/patches/` and
  written to the core series, including new ones,
- every patch from the first platform patch on goes to the platform series.

So a new core patch must sit before
`ungoogled-chromium/portablelinux/use-oauth2-client-switches-as-default.patch` in
`series.merged`, and a new Linux-only patch goes at the very end. Edits to core patch files made
while merged are copied back to `helium-chromium/patches/` by the unmerge.

Run `he unmerge` before you commit, and `he merge` afterwards to keep working. Both leave the
source tree and quilt's applied state alone.

All quilt commands below run inside the container from `build/src`. Use an interactive
`neon/dev-container.sh sh` or a multi-line `sh` command: the `quilt` alias from `dev.sh` (which
adds `--quiltrc -` so patches keep the `a/` `b/` format without timestamps) only applies to
commands on a later line than the one that sourced it. `neon/dev-container.sh sh` handles that for
you.

## Editing an existing patch

quilt only refreshes a patch if no patch applied after it touches the same files. Most Helium
patches touch shared files such as `chrome/browser/helium_flag_entries.h`, which the Linux patches
also touch, so in practice you pop to the patch, edit, refresh and push again:

```sh
cd build/src
quilt pop helium/ui/hide-page-actions-flag.patch     # now the topmost patch
quilt edit chrome/browser/ui/views/location_bar/location_bar_view.cc
# or: quilt add <file>, then edit it with any tool
quilt refresh
quilt push -a
```

Popping undoes the later patches, so their files get new mtimes and are rebuilt. The 7 Linux
patches touch `chrome/BUILD.gn` and `ui/base/ui_base_features.h`, so popping any core patch means
a large rebuild. Plan edits to Helium's patches in batches.

If the patch's files are not touched by any later patch (true for Neon patches that only add files
under `chrome/browser/neon/`), skip the pop:

```sh
quilt add -P neon/containers/storage.patch chrome/browser/neon/storage.cc
# edit the file
quilt refresh neon/containers/storage.patch
```

quilt refuses with "More recent patches modify files in patch" when that is not safe. Never use
`-f` to get past it, because it would fold the later patch's changes into this one.

`quilt refresh` and `quilt delete -r` leave `*.patch~` backups next to the patch. Delete them
before you commit.

## Adding a new Neon patch

quilt creates a new patch right after the topmost one, which in the merged tree is the last Linux
patch. For a core (shared) patch, create it there and then move it into the core block with
`neon/quilt_place.py`, which reorders `series.merged` and `build/src/.pc/applied-patches`
together without popping anything. It refuses if a patch it moves across touches the same files.

```sh
cd build/src
quilt new neon/containers/storage.patch
quilt add chrome/browser/neon/storage.cc chrome/browser/neon/storage.h
quilt add chrome/browser/BUILD.gn                  # files you hook into
# write the code
quilt refresh
python3 ../../neon/quilt_place.py neon/containers/storage.patch core
quilt applied | tail -9                            # it now sits before the Linux patches
```

For a Linux-only patch leave it at the end, or run
`python3 ../../neon/quilt_place.py PATCH platform` to move it back there. Then build, run and
commit:

```sh
neon_build
he unmerge     # core patch moves to helium-chromium/patches/neon/, series lines follow
git -C helium-chromium status
# commit in helium-chromium, push to ~/projects/neon, then commit the submodule bump in neon-linux
he merge
```

Do not add comment lines to any series file. Upstream's lint treats every line as a patch path.

To drop a patch you created:

```sh
python3 ../../neon/quilt_place.py neon/containers/storage.patch platform   # to the top
quilt pop
quilt delete -r neon/containers/storage.patch
```

This whole flow was tested on this tree with a throwaway `neon/test/hello.patch`: created on top,
moved into the core block, refreshed in place, moved back, popped and deleted. Unmerging puts a
patch placed this way into the core series.

## Testing Neon code

Short answer: Chrome's big test binaries do not compile on a Helium tree, so Neon tests are
scripted checks against the real browser over the DevTools protocol, plus small gtest binaries
for code that only needs `//base` or `//ui/views`.

### What was tried

After adding the test data (see [first setup](#making-the-test-targets-buildable)), all three
large Chrome test targets get past `gn` and start compiling, then fail in shared test support code,
not in individual tests:

| Target | Steps needed | Result | Time until it stopped |
| --- | --- | --- | --- |
| `unit_tests` | 9,346 | 46 compile failures, stops at 5,685 | 17 min |
| `browser_tests` | 4,032 more | 38 failures | 2 min |
| `interactive_ui_tests` | 1,333 more | 30 failures | 1 min |
| `base_unittests` | 3,678 | builds, `StringUtilTest.*` passes | 3 min 48 s |
| `views_unittests` | 171 more | builds, `LabelTest.*` passes under Xvfb | 1 min 23 s |

The failures come from Helium's and ungoogled-chromium's patches, which remove or reshape what
Chrome's test harness relies on:

- `chrome/test/base/testing_browser_process.h` refers to `safe_browsing::SafeBrowsingService`,
  which the patches remove. Every Chrome test binary links `chrome/test/base`.
- `components/omnibox/browser/test_omnibox_client`, `components/sync/test/test_sync_service.h`,
  `content` and `blink` test helpers, and `TemplateURLService` test setup no longer match the
  patched interfaces.
- `chromecast/cast_core/grpc` headers are pruned from the source (pulled in through
  `components/policy/test_support`).

Fixing that would mean a Neon test-fix patch series across Chrome's test support code, which
breaks again on every Chromium update. If they compiled, the cost would be moderate: about 15,000
extra steps for all three (roughly an hour at `-j 8`) and 3.4 GB more output, measured from the
partial runs above. The output directory grew from 16.2 GB to 19.6 GB through all these runs.

Running Chrome tests in this tree is therefore not affordable as a routine practice. Revisit only
if a feature cannot be checked any other way.

### Recommended approach

1. Scripted checks against the real browser (the main tool). Each Neon milestone gets a script
   under `neon-linux/neon/tests/` (for example `containers_isolation.py`) that:
   - starts the browser with `neon/browser.sh start` and a seeded profile (`Preferences`
     written before launch, as `NEON_VERTICAL_TABS` does),
   - drives it with `neon/cdp.py` (import it, or copy its `Session` class): open tabs, set and
     read cookies and `localStorage`, use the `Storage`, `Network` and `Target` domains to check
     which storage partition a tab uses, read `chrome://` pages,
   - takes screenshots with `neon/browser.sh shot` for UI checks, which the agent reads,
   - exits non-zero on failure and always calls `neon/browser.sh stop`.

   Where state is not visible from the web platform (which container a tab belongs to, the
   sidebar's space list), add a small test hook in the Neon patch, such as a
   `chrome://neon-internals` page or a JSON dump behind a `--neon-test-state` switch, and read
   it over the DevTools protocol. Keep the hook in Neon's own files.

2. Small gtest binaries for logic that can be kept free of `chrome/browser`. Data models,
   persistence formats and pure helpers can live in a component that depends only on `//base`
   (and `//components/prefs` and similar), with a Neon test target such as `neon_unittests`
   built from `//base/test:run_all_unittests`. `base_unittests` shows that this layer compiles
   and runs here. `views_unittests` shows the same for view classes that only need `//ui/views`.
   Run them with `xvfb-run -a` when they need a display.

   For gestures, Xvfb is not enough: its only input devices are XTEST's, and `xdotool click 6`
   or `7` reaches Chromium as a notched mouse wheel. Chromium makes touchpad scroll events only
   from an XInput device of type TOUCHPAD. `neon/tests/x11_touchpad.py` runs Xorg with the dummy
   video driver and xserver's `inputtest` input driver, which adds such a device fed from a
   Unix socket, without root or kernel devices. Keys and clicks still go through XTEST. It also
   grabs parts of the screen in about half a millisecond, fast enough to catch frames in the
   middle of a 200 ms animation. `neon/tests/spaces_m3.py` shows how to use both. The dev image
   installs `xserver-xorg-core` and `xserver-xorg-video-dummy` for it.

3. `chrome` itself is the integration test. Build it, start it, and check `build/logs/browser.log`
   for new errors on every change.

Before each commit, run the scripted checks for the areas you touched. On an upstream sync, run
all of them.

## Cleaning up

```sh
neon/dev-container.sh sh 'neon/browser.sh stop'
neon/dev-container.sh stop          # removes the container; build/ is untouched
pkill -f 'memlog[.]sh'              # only if you started it; the brackets stop it matching its own shell
```

Leave the source tree with all patches applied (`quilt top` prints
`helium/linux/disable-tab-strokes.patch` or a later Neon patch) and the build output in place, so
the next agent can build incrementally.

## Gotchas

- Do not run upstream's `he reset`, `he pull` or `scripts/docker-build.sh` on the shared tree
  unless you mean to. `docker-build.sh` deletes `build/src/out` first. `he reset` deletes the tree.
- `he setup` does nothing if `build/src/out` exists.
- Timestamps in the browser log are UTC, the host is on Paris time.
- The browser logs a few harmless errors under Xvfb: no D-Bus login manager, and blocked requests
  to `helium-services-are-disabled.qjz9zk` (Helium's placeholder for its services).
- `patches/series` contains blank lines. Never build a series file by line count (`head -n`),
  it silently drops entries, and a later `he merge` then deletes the patch files that fell off.
  If that happens, the applied list in `build/src/.pc/applied-patches` is the source of truth.
- `neon/quilt_place.py PATCH core` puts a new patch at the end of the core block, after any
  earlier Neon patches, so Neon patches stay in the order they were created.
- To edit an earlier Neon patch that shares a file with a later one: move the later patches to
  the platform end with `quilt_place.py ... platform`, pop them, refresh the earlier patch, push,
  and move them back. This avoids popping the Linux patches, which forces a huge rebuild.
- The dev container uses host networking, so Xvfb display numbers are shared with the host and
  with other agents. Pick a free display instead of assuming `:99`.
- Running the browser binary a second time on a profile that is already open hands its command
  line to the running browser. Scripts use this to open a tab in a container:
  `--container=<name> URL`.
- Rebuild cost by what you touch: a `.cc` file takes 10 to 30 seconds, a `chrome/browser` header
  3 to 7 minutes, a `content/public` header about 15 minutes. Batch header edits.

## Notes from M4 (space management)

- **Finding views to click.** `--enable-ui-devtools=PORT` serves the views tree on
  `ws://127.0.0.1:PORT/0` (there is no `/json` list; asking for one logs a "Not implemented"
  error). `DOM.getDocument` gives every view with its class name and object name,
  `DOM.getNodeBoundsInScreen` its screen bounds, and `CSS.getMatchedStylesForNode` its views
  metadata properties (`TooltipText`, `Text`, `Enabled`, `IsDrawn`, `Visible`). Hidden views
  keep their bounds, so filter on `IsDrawn`. Neon's own views override `GetObjectName()` to add
  their space id. `neon/tests/spaces_m4.py` has the client (`UiDevTools`).
- **Menus.** Menu items show up as `MenuItemView` nodes with no title; tell them apart by order.
  A submenu that was opened stays in the tree, still drawn, after the pointer moves on.
- **Typing characters the X keymap lacks** (emoji): `xdotool type` remaps a spare key only for
  the instant of the press, and the browser has not reloaded the keymap by then, so nothing is
  typed. Map a spare keycode with `XChangeKeyboardMapping`, wait about a second, then press it
  through XTEST (`type_unicode` in `spaces_m4.py`). `xmodmap` is not in the dev image.
- **Clicking a `cr-input`** in a WebUI page must hit the inner `<input>` (`el.inputElement`);
  a click on the wrapper's padding does not focus it.
- **Restarts under Xvfb.** Without a window manager, session restore puts every window back at the
  origin. Move them again (`Browser.setWindowBounds`) if the test needs them apart. Background
  tabs after a restart may be frozen or never loaded and then never answer `Runtime.evaluate`.
- **The Preferences file** is written about ten seconds after a change; poll it, or read it after
  the browser has exited.
- **Refreshing a lower Neon patch without a rebuild.** Popping and pushing the patches above it
  gives their files new mtimes, and siso then rebuilds them (touching
  `components/tabs/public/tab_context_menu_command.h` costs about 6 minutes). Save the mtimes of
  the files of the patches above, pop, refresh, push, and restore the mtimes:

  ```sh
  cd build/src
  target=neon/spaces/management-ui.patch
  above=$(quilt applied | sed -n "\|^$target\$|,\$p" | tail -n +2)
  for p in $above; do quilt files $p; done | sort -u | while read -r f; do
    printf '%s\t%s\n' "$(date -r "$f" +%s.%N)" "$f"; done > /tmp/mtimes
  quilt pop -q $target && quilt refresh $target && quilt push -q $(echo "$above" | tail -1)
  while IFS=$'\t' read -r t f; do touch -d "@$t" "$f"; done < /tmp/mtimes
  ```

  Only do this when the patches above are already refreshed, so their content is unchanged.

## Notes from M5 (all spaces as containers)

- **`quilt refresh` drops files that have no change yet.** A file added to a patch with
  `quilt add` before you edit it, then refreshed while still unchanged, is no longer tracked:
  a later edit stays in the tree only, the build passes, and a clean apply of the series would
  not compile. Add a file right before editing it, and check `quilt files PATCH` before
  committing. M5 hit this once (`space_switcher_row.h`).
- **Always `quilt refresh` before any pop or move.** Edits to files a patch tracks live only in
  the tree until refreshed; `quilt pop` refuses an unrefreshed patch ("does not remove
  cleanly"), which is the safe outcome, but a script that goes on from there leaves the series
  half moved. Refresh the topmost Neon patch in place; a lower one that shares files with a
  later Neon patch cannot be refreshed in place (see below).
- **Refreshing a Neon patch that shares files with later Neon patches** without popping the
  Linux patches: refresh the later ones, move them to the platform end with
  `neon/quilt_place.py PATCH platform` (it only refuses when a patch it crosses shares a file),
  pop them, move the patch you edit to the end too, refresh it, push the others back and move
  everything back into the core block in order. Only the files of the moved patches get new
  mtimes. Splitting a patch works the same way: pop it, cut the file sections out of the
  `.patch` text, create the new patch with `quilt new`, `quilt add` its files, apply the cut
  sections with `patch -p1`, refresh, then push the trimmed patch.
- **Building an older tree for a fixture** costs two short builds when only Neon patches
  differ: refresh and move the newer patches to the platform end, pop them, `neon_build`
  (about a minute), make the fixture, push and move them back, build again.
  `neon/tests/make_m4_profile.py` was made this way from the M4 build.
- **Test windows are all the same size.** `--window-size` and `--window-position`, which the
  checks pass, apply to every window the browser opens, popups included. A check that measures
  where a popup opens must launch without them (`FreeSizeBrowser` in `all_containers_m5.py`).
- **The empty part of the vertical sidebar is window caption.** A right click there opens the
  window manager's menu (Minimize, Maximize...), not a view's context menu. The New Tab button
  of the sidebar has a context menu; M5 adds "New space" to it.
- **Blank pages and SiteInstances.** A SiteInstance without a site always reports the default
  partition, and a browser-initiated about:blank commits in one even in a fixed-partition
  BrowsingInstance, so the next navigation either hits the `CHECK` in
  `BrowsingInstance::ComputeSiteInfoForURL` or leaves the container. Anchoring on a site does
  not help (about:blank still gets its own SiteInstance). `containers/blank-tabs.patch` makes a
  BrowsingInstance fixed to a non-default partition assign a site to such pages, as content
  already does for `<webview>` guests.
- **Cookies on disk** are the reliable proof of a partition: `Default/Cookies` is the default
  partition, `Default/Storage/ext/containers/<hash>/Cookies` a container (hash: first 6 bytes
  of SHA-256 of the id, upper-case hex). Values are encrypted with `--password-store=basic`,
  names are not, so give each test cookie a distinct name. Read them after the browser exits.
- `--load-extension` needs `--disable-features=DisableLoadExtensionCommandLineSwitch` on this
  build. The test extension's service worker is a DevTools target of type `service_worker`;
  `Runtime.evaluate` with `awaitPromise` runs `chrome.*` calls there.

## Notes from M6 (data tools per space)

- **`quilt pop PATCH` pops the patches above PATCH**, not PATCH itself. To refresh a Neon patch
  that shares files with a later one, move the later one to the end with
  `neon/quilt_place.py PATCH platform`, `quilt pop` (no argument), refresh the earlier patch,
  `quilt push PATCH`, move it back with `quilt_place.py PATCH core`. Save and restore the
  mtimes of the moved patch's files around this (as in the M4 note) and nothing rebuilds.
- **Where each partition keeps what**, under `Default/` for the global context and under
  `Default/Storage/ext/containers/<hash>/` for a container: `Cookies` (SQLite),
  `Local Storage/leveldb`, `IndexedDB/http_<host>_<port>.indexeddb.leveldb`,
  `Service Worker/Database` (registrations) and `Service Worker/CacheStorage`, and the HTTP
  cache in `Cache/Cache_Data` (one file per entry, its key holds the URL).
- **leveldb on disk keeps deleted bytes** in its log until a compaction, so a grep for a
  deleted localStorage key still finds it. `neon/tests/data_tools_m6.py` replays the logs
  (`leveldb_state`) to get the live keys. A database reopened by a later run moves its log into
  `.ldb` tables, which the replay only reads as raw bytes, so on-disk storage checks are made
  on a profile that ran once.
- **Markers must not be prefixes of each other** (`ls_A` matches `ls_A2`). The M6 check uses
  single letters that never prefix one another.
- **Driving DevTools.** F12 docks DevTools; its front end is a `page` target at
  `devtools://devtools/bundled/devtools_app.html`. Attach to it and `Runtime.evaluate`
  `await import('./core/sdk/sdk.js')` (and `./panels/application/application.js`) to reach the
  inspected target (`SDK.TargetManager.TargetManager.instance().primaryPageTarget()`), its
  protocol agents and the panels' own code, such as `App.StorageView.StorageView.clear()`.
- **HTTP cache without the network**: `fetch(url, {cache: 'only-if-cached', mode:
  'same-origin'})` fails unless the entry is cached, which checks a cache without refilling it.
- **A `BrowsingDataFilterBuilder` in `kDelete` mode with no origins hits a CHECK** in the
  remover. An empty removal (mask 0, no filter) is fine and is how Neon waits for queued
  removals.
- **Content-setting patterns with a port** (`http://127.0.0.1:PORT,*`) do not match for cookie
  deletion on exit, which looks up the host only. Use `127.0.0.1,*` in seeded prefs.
- `Target.activateTarget` on a page tab of another space switches the window to that space;
  a check that clicks in a settings tab must bring it back to the front first
  (`click_in` in the M6 check does).
