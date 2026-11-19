#!/usr/bin/env python3
"""M6 check: clear browsing data, the site data pages and DevTools act per space. Every space is
a container (its own storage partition); the default partition is the global context of the
extensions and the browser's own pages. Drives the real Linux dev build with real X input
(shortcuts, clicks on the settings pages, menus, bubbles and the reload button) and, for
DevTools, through its own front end and over the DevTools protocol.

Run from the neon-linux checkout (one command, from the host):

    neon/dev-container.sh sh 'python3 helium-chromium/neon/tests/data_tools_m6.py'

or inside `neon/dev-container.sh sh`:

    python3 helium-chromium/neon/tests/data_tools_m6.py [--keep] [--helium PATH]
        [--shots DIR] [--only PHASE]

Exit status is 0 when every check passes and 1 otherwise. It starts its own Xvfb, a local HTTP
server on 127.0.0.1 and the browser with throwaway profiles, and stops all of them at the end.

Every phase uses two spaces, A (container Alpha) and B (container Beta), with the same site
(127.0.0.1, and localhost for the site data pages) open in both, and data in the global context
set by the test extension's service worker. A space's data is: a cookie who=<space> (plus the
v_<page> cookie the server sets), a localStorage key, a sessionStorage key, an IndexedDB
database, a Cache Storage cache, a service worker and HTTP cache entries (/cached.js?n=...).

Deletions are proved on disk once the browser has exited, in each partition's own files:
Cookies (SQLite), Local Storage/leveldb (its log replayed), IndexedDB/<origin> (directory),
Service Worker/CacheStorage and Service Worker/Database, and Cache/Cache_Data (HTTP cache entry
keys). Each deletion check in one partition has a matching "still there" check in another.

Phases:

1. dialog-space: Ctrl+Shift+Delete in space A opens the dialog on A's scope; it offers only
   cookies and cache, says the rest is shared, counts A's data; deleting (all time) empties
   A and leaves B and the global context.
2. dialog-all: the "All spaces" scope offers every type; deleting empties A, B and the global
   context.
3. time-range: cookies made older than an hour (edited on disk between two runs) survive a
   "Last hour" deletion in either scope, newer ones do not.
4. site-data: chrome://settings/content/all shows the space of the tab, switches spaces with
   its Space menu, lists each space's data, deletes a site in one space only; the page of a
   site shows its usage in the chosen space and deletes it there; permissions say they are
   shared; "Outside spaces" lists the global context.
5. page-info: the page info bubble's site data dialog lists and deletes the tab's own data.
6. devtools: with DevTools open on A's tab, its Application panel models (cookies, storage,
   IndexedDB, Cache Storage, service workers, "Clear site data" per type, quota) and Network
   panel actions (disable cache, clear cache, clear cookies, throttling, request blocking)
   act on A only; "Empty cache and hard reload" from the reload button's menu empties A's
   cache only.
7. protocol: the Storage and Network protocol methods a client calls on A's target act on A
   only; Target.createTarget lands in the visible space; chrome://serviceworker-internals and
   chrome://inspect list the workers of both spaces.
8. clear-space: "Clear this space's data…" from the switcher row's menu and "Clear data" on the
   Spaces settings page, each confirmed, empty that space only, keep its tabs and reload them.
9. entry-points: what clear-on-exit (the ClearDataOnExit flag) and a site set to "delete data
   when closed" do in containers. Recorded as checks of the behaviour M6 settled.

The browser log must hold no CHECK failure or crash.

Needs python3-websocket, python3-pil and xdotool (all in the dev image).
"""
import argparse
import http.server
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import containers_m1 as m1  # noqa: E402
import spaces_m2 as m2  # noqa: E402  (also gives Session.call a deadline)
import spaces_m4 as m4  # noqa: E402
import all_containers_m5 as m5  # noqa: E402
from containers_m1 import Browser, Page, check, free_display  # noqa: E402

ROOT = m1.ROOT
LOG_DIR = m1.LOG_DIR
failures = m1.failures
HERE = os.path.dirname(os.path.abspath(__file__))
SCREEN = (1600, 1000)
EXT_ARGS = m5.EXT_ARGS

ALPHA, BETA = m1.ALPHA, m1.BETA
SPACE_A = m1.space_for(ALPHA, 'A')
SPACE_B = m1.space_for(BETA, 'B')
CONTAINER = {'A': ALPHA['id'], 'B': BETA['id'], 'G': ''}
CONTAINER_NAME = {'A': 'Alpha', 'B': 'Beta'}
COOKIE_AGE = 30 * 24 * 3600

settle = m4.settle
key = m4.key
click = m4.click
center = m4.center
wait_for = m4.wait_for


# --- Local test server -------------------------------------------------------------------------

PAGE = '''<!doctype html><meta charset="utf-8"><title>{name}</title>
<link rel="icon" href="data:,">
<body style="font: 15px sans-serif"><p>page {name}</p>
<script src="/cached.js?n={name}"></script>
'''
SW_JS = 'self.addEventListener("fetch", () => {});'
hits = {}
hits_lock = threading.Lock()


class Handler(http.server.BaseHTTPRequestHandler):
    """/page?n=NAME sets the cookie v_NAME and loads /cached.js?n=NAME, which the browser may
    cache for a day. Every request is counted by path and query."""

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        with hits_lock:
            hits[self.path] = hits.get(self.path, 0) + 1
        headers = [('Cache-Control', 'no-store')]
        status = 200
        if url.path == '/page':
            name = q.get('n', ['x'])[0]
            body, kind = PAGE.format(name=name).encode(), 'text/html; charset=utf-8'
            headers.append(('Set-Cookie', f'v_{name}=1; Max-Age={COOKIE_AGE}; Path=/'))
        elif url.path == '/cached.js':
            body, kind = f'// {q.get("n", [""])[0]}'.encode(), 'text/javascript'
            headers = [('Cache-Control', 'max-age=86400')]
        elif url.path == '/sw.js':
            body, kind = SW_JS.encode(), 'text/javascript'
        elif url.path == '/plain':
            body, kind = b'plain', 'text/plain'
        elif url.path == '/setcookie':
            name = q.get('n', ['x'])[0]
            body, kind = b'ok', 'text/plain'
            headers.append(('Set-Cookie', f'{name}=1; Max-Age={COOKIE_AGE}; Path=/'))
        else:
            status, body, kind = 404, b'', 'text/plain'
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def hit_count(path):
    with hits_lock:
        return hits.get(path, 0)


# --- On-disk state of a partition --------------------------------------------------------------

def partition_path(profile, container_id):
    """The directory of a partition: the profile's Default for the global context."""
    if not container_id:
        return os.path.join(profile, 'Default')
    return m1.partition_dir(profile, container_id)


def _varint(buf, pos):
    shift = value = 0
    while True:
        b = buf[pos]
        pos += 1
        value |= (b & 0x7f) << shift
        if not b & 0x80:
            return value, pos
        shift += 7


def leveldb_state(path):
    """(live keys and values from the write-ahead logs, raw bytes of the tables) of a leveldb
    directory. The logs are replayed in order, so a deleted key is gone even though its bytes
    are still in the file. A fresh profile that ran once only has a log; tables appear when a
    database is reopened, and are read as raw bytes."""
    live, tables = {}, b''
    if not os.path.isdir(path):
        return live, tables
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if name.endswith('.ldb') or name.endswith('.sst'):
            tables += open(full, 'rb').read()
        if not name.endswith('.log'):
            continue
        data = open(full, 'rb').read()
        pos, pending, batches = 0, b'', []
        while pos + 7 <= len(data):
            left = 32768 - pos % 32768
            if left < 7:
                pos += left
                continue
            length = int.from_bytes(data[pos + 4:pos + 6], 'little')
            kind = data[pos + 6]
            if kind == 0 or pos + 7 + length > len(data):
                pos += left   # zero padding up to the end of the block
                continue
            payload = data[pos + 7:pos + 7 + length]
            pos += 7 + length
            if kind == 1:
                batches.append(payload)
            elif kind == 2:
                pending = payload
            elif kind == 3:
                pending += payload
            elif kind == 4:
                batches.append(pending + payload)
                pending = b''
        for batch in batches:
            count = int.from_bytes(batch[8:12], 'little')
            p = 12
            try:
                for _ in range(count):
                    tag = batch[p]
                    p += 1
                    klen, p = _varint(batch, p)
                    k = batch[p:p + klen]
                    p += klen
                    if tag == 1:
                        vlen, p = _varint(batch, p)
                        live[k] = batch[p:p + vlen]
                        p += vlen
                    else:
                        live.pop(k, None)
            except IndexError:
                pass
    return live, tables


def leveldb_has(path, marker):
    """Whether |marker| (str) is in a live key or value of the leveldb at |path|, in UTF-8 or
    UTF-16 (IndexedDB stores names as UTF-16)."""
    live, tables = leveldb_state(path)
    needles = (marker.encode(), marker.encode('utf-16-be'), marker.encode('utf-16-le'))
    for k, v in live.items():
        if any(n in k or n in v for n in needles):
            return True
    return any(n in tables for n in needles)


def dir_contains(path, needle):
    return os.path.isdir(path) and m4.dir_contains(path, needle.encode())


def cookie_names(profile, container_id, host='127.0.0.1'):
    path = os.path.join(partition_path(profile, container_id), 'Cookies')
    return {n for h, n in m5.disk_cookies(path) if h == host}


def disk(profile, container_id, tag, port, host='127.0.0.1'):
    """What the partition of |container_id| holds on disk of the data seed() made with |tag|
    on |host|."""
    base = partition_path(profile, container_id)
    sw = os.path.join(base, 'Service Worker')
    idb = os.path.join(base, 'IndexedDB', f'http_{host}_{port}.indexeddb.leveldb')
    return {
        'cookie': f'who_{tag}' in cookie_names(profile, container_id, host),
        'local': leveldb_has(os.path.join(base, 'Local Storage', 'leveldb'), f'ls_{tag}'),
        'idb': os.path.isdir(idb) and leveldb_has(idb, f'db_{tag}'),
        'caches': dir_contains(os.path.join(sw, 'CacheStorage'), f'cache_{tag}'),
        'sw': leveldb_has(os.path.join(sw, 'Database'), f'sw.js?{tag}'),
        'http': dir_contains(os.path.join(base, 'Cache', 'Cache_Data'), f'cached.js?n={tag}'),
    }


def describe(state):
    return ' '.join(k for k, v in state.items() if v) or 'nothing'


def expect_disk(ctx, profile, container_id, tag, present, what, keys=None, host='127.0.0.1'):
    """Every kind in |keys| (all by default) is on disk if |present|, gone otherwise."""
    state = disk(profile, container_id, tag, ctx.port, host)
    keys = keys or list(state)
    ok = all(state[k] == present for k in keys)
    check(ok, f'{what}: on disk {"still holds" if present else "no longer holds"} '
              f'{", ".join(keys)} (has: {describe(state)})')
    return ok


# --- Browser, pages and seeding ----------------------------------------------------------------

class Ctx:
    pass


def page_url(ctx, name, host='127.0.0.1'):
    return f'http://{host}:{ctx.port}/page?n={name}'


def prefs_for(**extra):
    prefs = m2.prefs_for([SPACE_A, SPACE_B])
    prefs['brave']['containers']['list'] = [ALPHA, BETA]
    prefs.update(extra)
    return prefs


def start(ctx, name, *args, prefs=None, profile=None):
    b = m4.start(ctx, name, prefs or prefs_for(), *EXT_ARGS, *args, profile=profile)
    settle(2)
    return b


def stop(ctx, b):
    m4.stop(ctx, b)


def open_in(ctx, b, space, name, host='127.0.0.1'):
    """Opens page |name| in a new tab of |space| (the command line's --container)."""
    url = page_url(ctx, name, host)
    before = m5.target_ids(b)
    b.remote(f'--container={CONTAINER_NAME[space]}', url)
    p = m5.loaded(b, f'n={name}', exclude=before)
    if not p:
        raise RuntimeError(f'page {name} did not open in {space}')
    return p


def seed(p, tag):
    """Gives page |p|'s partition one of each kind of data, named after |tag|."""
    p.activate()
    settle(0.3)
    return p.eval(f'''(async () => {{
      document.cookie = "who_{tag}=1; max-age={COOKIE_AGE}; path=/";
      localStorage.setItem("ls_{tag}", "v_{tag}");
      sessionStorage.setItem("ss_{tag}", "v_{tag}");
      await new Promise((ok, err) => {{
        const r = indexedDB.open("db_{tag}", 1);
        r.onupgradeneeded = () => r.result.createObjectStore("s");
        r.onsuccess = () => {{
          const t = r.result.transaction("s", "readwrite");
          t.objectStore("s").put("{tag}", "k");
          t.oncomplete = () => {{ r.result.close(); ok(); }};
        }};
        r.onerror = err;
      }});
      const c = await caches.open("cache_{tag}");
      await c.put("/cs_{tag}", new Response("{tag}"));
      await navigator.serviceWorker.register("/sw.js?{tag}");
      await navigator.serviceWorker.ready;
      await fetch("/cached.js?n={tag}");
      return true;
    }})()''')


def live(p):
    """What page |p| sees of its partition's data."""
    p.activate()
    settle(0.3)
    try:
        return p.eval('''(async () => ({
          cookies: document.cookie.split("; ").filter(c => c.startsWith("who_"))
              .map(c => c.split("=")[0].slice(4)).sort().join(","),
          local: Object.keys(localStorage).filter(k => k.startsWith("ls_"))
              .map(k => k.slice(3)).sort().join(","),
          idb: (await indexedDB.databases()).map(d => d.name.slice(3)).sort().join(","),
          caches: (await caches.keys()).map(k => k.slice(6)).sort().join(","),
          sw: (await navigator.serviceWorker.getRegistrations()).map(r =>
              (r.active || r.waiting || r.installing).scriptURL.split("?")[1]).sort().join(","),
        }))()''')
    except RuntimeError as e:
        return {'error': str(e)[:200]}


def seed_global(ctx, b, tag='G'):
    """Data in the global context: a cookie and an HTTP cache entry, through the extension."""
    sw = m5.ext_worker(b)
    return sw.eval(f'''(async () => {{
      await fetch("http://127.0.0.1:{ctx.port}/setcookie?n=who_{tag}", {{credentials: "include"}});
      await fetch("http://127.0.0.1:{ctx.port}/cached.js?n={tag}");
      return true;
    }})()''')


def global_cookies(b):
    """Cookie names of the default partition, through the browser target."""
    return sorted(c['name'] for c in b.session.call('Storage.getCookies').get('cookies', []))


def two_spaces(ctx, b, seed_data=True):
    """Opens the test page in A and B (and seeds both and the global context)."""
    pa = open_in(ctx, b, 'A', 'a')
    pb = open_in(ctx, b, 'B', 'b')
    if seed_data:
        seed(pa, 'A')
        seed(pb, 'B')
        seed_global(ctx, b)
    return pa, pb


# --- Settings pages ----------------------------------------------------------------------------

DEEP_JS = r'''
window.neonFind = (root, sel) => {
  const hit = root.querySelector(sel);
  if (hit) return hit;
  for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot) {
      const f = neonFind(el.shadowRoot, sel);
      if (f) return f;
    }
  }
  return null;
};
// Each selector is looked up anywhere below the previous match (its shadow root included).
window.neonQ = (...sels) => {
  let root = document, el = null;
  for (const s of sels) {
    el = neonFind(root, s);
    if (!el) return null;
    root = el.shadowRoot || el;
  }
  return el;
};
window.neonRect = (...sels) => {
  const el = neonQ(...sels);
  if (!el) return null;
  el.scrollIntoViewIfNeeded && el.scrollIntoViewIfNeeded();
  const r = el.getBoundingClientRect();
  if (!r.width || !r.height) return null;
  return [r.x + r.width / 2, r.y + r.height / 2];
};
'''


def q(p, *sels, expr='el'):
    """Evaluates |expr| on the element at |sels| (null if there is none)."""
    p.eval(DEEP_JS)
    return p.eval(f'(() => {{ const el = neonQ(...{json.dumps(list(sels))}); '
                  f'return el ? ({expr}) : null; }})()')


def click_in(ctx, b, p, *sels, button=1, settle_time=0.8):
    """A real click on the element at |sels| of page |p|, which is brought to the front."""
    if p.eval('document.visibilityState') != 'visible':
        p.activate()
        settle(0.6)
    p.eval(DEEP_JS)
    rect = None
    for _ in range(20):
        rect = p.eval(f'neonRect(...{json.dumps(list(sels))})')
        if rect:
            break
        time.sleep(0.25)
    if not rect:
        raise RuntimeError(f'nothing to click at {sels}')
    origin = m5.content_origin(b)
    click(ctx, (int(origin[0] + rect[0]), int(origin[1] + rect[1])), button=button,
          settle_time=settle_time)


def settings_page(b, part, timeout=15):
    def find():
        for t in b.pages():
            if t['url'].startswith('chrome://settings') and part in t['url']:
                return Page(b, t['targetId'])
        return None
    p = wait_for(find, timeout)
    if p:
        p.wait_loaded('settings')
        settle(1)
    return p


def focus_main(ctx):
    m5.focus_main(ctx)


def open_dialog(ctx, b, p):
    """Ctrl+Shift+Delete on the window showing tab |p|. Returns the settings page."""
    p.activate()
    settle(0.5)
    focus_main(ctx)
    key(ctx, 'ctrl+shift+Delete')
    s = settings_page(b, 'clearBrowserData')
    if s:
        wait_for(lambda: q(s, 'settings-clear-browsing-data-dialog', '#deleteButton',
                           expr='true'), 10)
        settle(1.5)
    return s


DIALOG = 'settings-clear-browsing-data-dialog'


def dialog_state(s):
    """Scope chips, offered types, note, and counter texts of the open dialog."""
    s.eval(DEEP_JS)
    return s.eval(f'''(() => {{
      const dlg = neonQ("{DIALOG}");
      if (!dlg) return null;
      const scope = neonQ("{DIALOG}", "settings-neon-space-scope");
      const chips = scope ? [...scope.shadowRoot.querySelectorAll("cr-chip")].map(c =>
          [c.textContent.trim(), c.hasAttribute("selected")]) : [];
      const boxes = [...dlg.shadowRoot.querySelectorAll("settings-checkbox")].map(c =>
          [c.prefKey, c.checked, (c.subLabelHtml || c.subLabel || "").replace(/<[^>]+>/g, "")]);
      const note = dlg.shadowRoot.querySelector("#neonSharedNote");
      return {{chips, boxes, note: note && !note.hidden ? note.textContent.trim() : "",
               open: dlg.shadowRoot.querySelector("#deleteBrowsingDataDialog").open}};
    }})()''')


def choose_all_time(ctx, b, s):
    click_in(ctx, b, s, DIALOG, 'settings-clear-browsing-data-time-picker', '#moreButton')
    click_in(ctx, b, s, DIALOG, 'settings-clear-browsing-data-time-picker',
             'button.dropdown-item[data-time-period="4"]')


def choose_last_hour(ctx, b, s):
    click_in(ctx, b, s, DIALOG, 'settings-clear-browsing-data-time-picker',
             'cr-chip[data-time-period="0"]')


def selected_period(s):
    return q(s, DIALOG, 'settings-clear-browsing-data-time-picker',
             expr='el.getSelectedTimePeriod()')


def ensure_checked(ctx, b, s, prefs_wanted):
    """Ticks exactly the boxes of |prefs_wanted| among those shown, with real clicks."""
    for pref, checked, _ in dialog_state(s)['boxes']:
        if checked != (pref in prefs_wanted):
            click_in(ctx, b, s, DIALOG, f'settings-checkbox[pref-key="{pref}"]', '#checkbox',
                     settle_time=0.4)


def delete_and_wait(ctx, b, s, timeout=30):
    click_in(ctx, b, s, DIALOG, '#deleteButton', settle_time=0.5)
    closed = wait_for(lambda: not (dialog_state(s) or {}).get('open'), timeout)
    settle(1)
    return bool(closed)


COOKIES_PREF = 'browser.clear_data.cookies'
CACHE_PREF = 'browser.clear_data.cache'
HISTORY_PREF = 'browser.clear_data.browsing_history'


def shot(ctx, name):
    path = os.path.join(ctx.shots, name)
    m4.shot(ctx, path)
    return path


# --- Phases ------------------------------------------------------------------------------------

def phase_dialog_space(ctx):
    print('== dialog-space: Ctrl+Shift+Delete in a space deletes that space only', flush=True)
    b = start(ctx, 'dialog-space')
    profile = b.profile_dir
    pa, pb = two_spaces(ctx, b)
    check(live(pa) == {'cookies': 'A', 'local': 'A', 'idb': 'A', 'caches': 'A', 'sw': 'A'},
          f'A holds its own data ({live(pa)})')
    check(live(pb) == {'cookies': 'B', 'local': 'B', 'idb': 'B', 'caches': 'B', 'sw': 'B'},
          f'B holds its own data ({live(pb)})')
    check('who_G' in global_cookies(b), 'the global context holds its own cookie')

    s = open_dialog(ctx, b, pa)
    check(s is not None, 'Ctrl+Shift+Delete opens the dialog')
    st = wait_for(lambda: (lambda d: d if d and d['chips'] and all(
        x[2] for x in d['boxes']) else None)(dialog_state(s)), 15) or dialog_state(s)
    check(st['chips'] == [['A', True], ['All spaces', False]],
          f'the dialog offers A (chosen) and All spaces ({st["chips"]})')
    check(sorted(x[0] for x in st['boxes']) == sorted([COOKIES_PREF, CACHE_PREF]),
          f'with A chosen it offers only cookies and cache ({[x[0] for x in st["boxes"]]})')
    check('shared by all spaces' in st['note'] and 'All spaces' in st['note'],
          f'it says what is shared ({st["note"]!r})')
    counters = {x[0]: x[2] for x in st['boxes']}
    check(counters.get(COOKIES_PREF, '').startswith('From 1 site'),
          f'the cookie counter counts A ({counters.get(COOKIES_PREF)!r})')
    check(bool(counters.get(CACHE_PREF)), f'the cache counter has a text ({counters.get(CACHE_PREF)!r})')
    shot(ctx, 'dialog-space-scope.png')

    choose_all_time(ctx, b, s)
    check(selected_period(s) == 4, 'All time is chosen with the More menu')
    ensure_checked(ctx, b, s, {COOKIES_PREF, CACHE_PREF})
    check(delete_and_wait(ctx, b, s), 'Delete closes the dialog')
    la, lb = live(pa), live(pb)
    check(la == {'cookies': '', 'local': '', 'idb': '', 'caches': '', 'sw': ''},
          f'A has no data left ({la})')
    check(lb == {'cookies': 'B', 'local': 'B', 'idb': 'B', 'caches': 'B', 'sw': 'B'},
          f'B kept its data ({lb})')
    check('who_G' in global_cookies(b), 'the global context kept its cookie')
    stop(ctx, b)
    expect_disk(ctx, profile, CONTAINER['A'], 'A', False, 'A after deleting A')
    expect_disk(ctx, profile, CONTAINER['B'], 'B', True, 'B after deleting A')
    expect_disk(ctx, profile, '', 'G', True, 'global context after deleting A',
                keys=['cookie', 'http'])


def phase_dialog_all(ctx):
    print('== dialog-all: All spaces deletes in every space and the global context', flush=True)
    b = start(ctx, 'dialog-all')
    profile = b.profile_dir
    pa, pb = two_spaces(ctx, b)
    s = open_dialog(ctx, b, pa)
    click_in(ctx, b, s, DIALOG, 'settings-neon-space-scope', '#allChip')
    st = wait_for(lambda: (lambda d: d if d and HISTORY_PREF in [x[0] for x in d['boxes']]
                           else None)(dialog_state(s)), 10) or dialog_state(s)
    check(st['chips'] == [['A', False], ['All spaces', True]],
          f'clicking All spaces chooses it ({st["chips"]})')
    prefs = [x[0] for x in st['boxes']]
    check(HISTORY_PREF in prefs and 'browser.clear_data.download_history' in prefs and
          COOKIES_PREF in prefs and CACHE_PREF in prefs,
          f'All spaces offers the profile-wide types too ({prefs})')
    check(st['note'] == '', 'the shared-data note is gone')
    shot(ctx, 'dialog-all-spaces.png')
    choose_all_time(ctx, b, s)
    ensure_checked(ctx, b, s, {COOKIES_PREF, CACHE_PREF})
    check(delete_and_wait(ctx, b, s), 'Delete closes the dialog')
    for name, p in (('A', pa), ('B', pb)):
        state = live(p)
        check(state == {'cookies': '', 'local': '', 'idb': '', 'caches': '', 'sw': ''},
              f'{name} has no data left ({state})')
    check('who_G' not in global_cookies(b), 'the global context lost its cookie')
    stop(ctx, b)
    expect_disk(ctx, profile, CONTAINER['A'], 'A', False, 'A after deleting all spaces')
    expect_disk(ctx, profile, CONTAINER['B'], 'B', False, 'B after deleting all spaces')
    expect_disk(ctx, profile, '', 'G', False, 'global context after deleting all spaces',
                keys=['cookie', 'http'])


def cookie_list(p):
    p.activate()
    settle(0.2)
    return sorted(c.split('=')[0] for c in (p.eval('document.cookie') or '').split('; ') if c)


def age_cookies(profile, container_id, names, hours=3):
    """Makes cookies |names| of a partition look |hours| old (the browser is not running)."""
    path = os.path.join(partition_path(profile, container_id), 'Cookies')
    then = int((time.time() - hours * 3600 + 11644473600) * 1e6)
    db = sqlite3.connect(path)
    n = 0
    for name in names:
        n += db.execute('UPDATE cookies SET creation_utc=?, last_access_utc=?, '
                        'last_update_utc=? WHERE name=?', (then, then, then, name)).rowcount
    db.commit()
    db.close()
    return n


def set_cookie_via(p, name):
    return p.eval(f'fetch("/setcookie?n={name}").then(r => r.status)')


def phase_time_range(ctx):
    print('== time-range: "Last hour" keeps older data in either scope', flush=True)
    b = start(ctx, 'time-range')
    profile = b.profile_dir
    pa, pb = two_spaces(ctx, b, seed_data=False)
    set_cookie_via(pa, 'old_A')
    set_cookie_via(pb, 'old_B')
    m5.ext_worker(b).eval(f'fetch("{ctx.site}/setcookie?n=old_G", {{credentials: "include"}})'
                          '.then(r => r.status)')
    stop(ctx, b)
    aged = [age_cookies(profile, CONTAINER[t], [f'old_{t}']) for t in 'ABG']
    check(aged == [1, 1, 1], f'three cookies made three hours old on disk ({aged})')

    b = start(ctx, 'time-range', profile=profile)
    pa = open_in(ctx, b, 'A', 'a2')
    pb = open_in(ctx, b, 'B', 'b2')
    set_cookie_via(pa, 'new_A')
    set_cookie_via(pb, 'new_B')
    m5.ext_worker(b).eval(f'fetch("{ctx.site}/setcookie?n=new_G", {{credentials: "include"}})'
                          '.then(r => r.status)')
    check({'old_A', 'new_A'} <= set(cookie_list(pa)), f'A has an old and a new cookie ({cookie_list(pa)})')

    s = open_dialog(ctx, b, pa)
    choose_last_hour(ctx, b, s)
    check(selected_period(s) == 0, 'Last hour is chosen')
    ensure_checked(ctx, b, s, {COOKIES_PREF})
    check(delete_and_wait(ctx, b, s), 'Delete closes the dialog')
    ca, cb, cg = cookie_list(pa), cookie_list(pb), global_cookies(b)
    check('new_A' not in ca and 'old_A' in ca, f'A lost its new cookie and kept the old one ({ca})')
    check({'new_B', 'old_B'} <= set(cb), f'B kept both ({cb})')
    check({'new_G', 'old_G'} <= set(cg), f'the global context kept both ({cg})')

    s = open_dialog(ctx, b, pb)
    click_in(ctx, b, s, DIALOG, 'settings-neon-space-scope', '#allChip')
    choose_last_hour(ctx, b, s)
    ensure_checked(ctx, b, s, {COOKIES_PREF})
    check(delete_and_wait(ctx, b, s), 'Delete with All spaces closes the dialog')
    ca, cb, cg = cookie_list(pa), cookie_list(pb), global_cookies(b)
    check('old_A' in ca, f'A kept its old cookie ({ca})')
    check('new_B' not in cb and 'old_B' in cb, f'B lost its new cookie and kept the old one ({cb})')
    check('new_G' not in cg and 'old_G' in cg,
          f'the global context lost its new cookie and kept the old one ({cg})')
    stop(ctx, b)
    for t in 'ABG':
        names = cookie_names(profile, CONTAINER[t])
        check(f'old_{t}' in names and f'new_{t}' not in names,
              f'{t} on disk: the old cookie is kept, the new one gone ({sorted(names)})')


def phase_protocol(ctx):
    print('== protocol: Storage and Network methods act on the target\'s space', flush=True)
    b = start(ctx, 'protocol')
    profile = b.profile_dir
    pa, pb = two_spaces(ctx, b)

    sp = Page(b, b.session.call('Target.createTarget',
                                {'url': 'chrome://serviceworker-internals'})['targetId'])
    settle(3)
    text = sp.eval('document.body.innerText')
    check('sw.js?A' in text and 'sw.js?B' in text,
          'chrome://serviceworker-internals lists the workers of both spaces')
    ip = Page(b, b.session.call('Target.createTarget',
                                {'url': 'chrome://inspect/#service-workers'})['targetId'])
    settle(3)
    text = ip.eval('document.body.innerText')
    check('sw.js?A' in text and 'sw.js?B' in text,
          'chrome://inspect lists the workers of both spaces')
    for p in (sp, ip):
        b.session.call('Target.closeTarget', {'targetId': p.target_id})

    origin = ctx.site
    names = lambda r: sorted(c['name'] for c in r.get('cookies', []))  # noqa: E731
    got = names(pa.call('Storage.getCookies'))
    check('who_A' in got and 'who_B' not in got and 'who_G' not in got,
          f'Storage.getCookies on A\'s target reads A ({got})')
    got = names(pa.call('Network.getCookies'))
    check('who_A' in got and 'who_B' not in got, f'Network.getCookies on A reads A ({got})')
    got = names(b.session.call('Storage.getCookies'))
    check('who_G' in got and 'who_A' not in got,
          f'Storage.getCookies on the browser target reads the global context ({got})')

    qa = pa.call('Storage.getUsageAndQuota', {'origin': origin})
    usage = lambda r, t: next((u['usage'] for u in r['usageBreakdown'] if u['storageType'] == t), 0)  # noqa: E731
    pa.call('Storage.clearDataForOrigin', {
        'origin': origin, 'storageTypes': 'local_storage,indexeddb,cache_storage,service_workers'})
    settle(1)
    la, lb = live(pa), live(pb)
    check(la == {'cookies': 'A', 'local': '', 'idb': '', 'caches': '', 'sw': ''},
          f'Storage.clearDataForOrigin on A empties A\'s storage, keeps its cookies ({la})')
    check(lb == {'cookies': 'B', 'local': 'B', 'idb': 'B', 'caches': 'B', 'sw': 'B'},
          f'... and leaves B ({lb})')
    qa2 = pa.call('Storage.getUsageAndQuota', {'origin': origin})
    qb = pb.call('Storage.getUsageAndQuota', {'origin': origin})
    check(usage(qa, 'indexeddb') > 0 and usage(qa2, 'indexeddb') == 0 and usage(qb, 'indexeddb') > 0,
          f'Storage.getUsageAndQuota reads the target\'s space (A {usage(qa, "indexeddb")} then '
          f'{usage(qa2, "indexeddb")}, B {usage(qb, "indexeddb")})')

    pa.call('Storage.setCookies', {'cookies': [{'name': 'who_S', 'value': '1', 'url': origin}]})
    check('who_S' in cookie_list(pa) and 'who_S' not in cookie_list(pb) and
          'who_S' not in global_cookies(b), 'Storage.setCookies on A sets it in A only')
    pa.call('Storage.clearCookies')
    settle(0.5)
    check(cookie_list(pa) == [], f'Storage.clearCookies on A empties A ({cookie_list(pa)})')
    check('who_B' in cookie_list(pb) and 'who_G' in global_cookies(b),
          'Storage.clearCookies on A leaves B and the global context')
    pa.eval(f'document.cookie = "who_R=1; max-age={COOKIE_AGE}; path=/"')
    pa.call('Network.enable')
    pa.call('Network.clearBrowserCookies')
    settle(0.5)
    check(cookie_list(pa) == [], f'Network.clearBrowserCookies on A empties A ({cookie_list(pa)})')
    check('who_B' in cookie_list(pb) and 'who_G' in global_cookies(b),
          'Network.clearBrowserCookies on A leaves B and the global context')
    pa.call('Network.clearBrowserCache')
    settle(1)

    # Target.createTarget: the visible space.
    pb.activate()
    settle(0.8)
    t = b.session.call('Target.createTarget', {'url': page_url(ctx, 'created')})
    pc = Page(b, t['targetId'])
    pc.wait_loaded('n=created')
    check('who_B' in cookie_list(pc), f'Target.createTarget opens in the visible space B ({cookie_list(pc)})')
    t = b.session.call('Target.createTarget', {'url': 'about:blank'})
    pd = Page(b, t['targetId'])
    settle(1)
    pd.call('Page.navigate', {'url': page_url(ctx, 'created2')})
    pd.wait_loaded('n=created2')
    check('who_B' in cookie_list(pd), 'a blank target navigated later is in B too')
    pa.activate()
    settle(0.8)
    t = b.session.call('Target.createTarget', {'url': page_url(ctx, 'created3'), 'newWindow': True})
    pw = Page(b, t['targetId'])
    pw.wait_loaded('n=created3')
    stop(ctx, b)
    expect_disk(ctx, profile, CONTAINER['A'], 'A', False,
                'A after the protocol clears', keys=['cookie', 'local', 'idb', 'caches', 'http'])
    expect_disk(ctx, profile, CONTAINER['B'], 'B', True, 'B after A\'s protocol clears')
    expect_disk(ctx, profile, '', 'G', True, 'global context after A\'s protocol clears',
                keys=['cookie', 'http'])
    created = cookie_names(profile, CONTAINER['B'])
    check({'v_created', 'v_created2'} <= created, 'the created targets\' cookies are in B on disk')
    check('v_created3' in cookie_names(profile, CONTAINER['A']),
          'Target.createTarget with newWindow, A visible, opened its window in A')
    check(not {'v_created', 'v_created2', 'v_created3'} & cookie_names(profile, ''),
          'no created target used the default partition')


def space_row_button(b, space_id):
    bounds, _ = m4.button(b, space_id)
    return bounds


def reload_marker(p):
    try:
        return p.eval('performance.timeOrigin')
    except RuntimeError:
        return None


def phase_clear_space(ctx):
    print('== clear-space: a space\'s own "clear data", from the row and from settings', flush=True)
    b = start(ctx, 'clear-space')
    profile = b.profile_dir
    pa, pb = two_spaces(ctx, b)
    pa.activate()
    settle(1)
    focus_main(ctx)
    before = reload_marker(pa)
    bounds = space_row_button(b, SPACE_A['id'])
    check(bounds is not None, 'the switcher row shows space A')
    click(ctx, center(bounds), button=3)
    items = m4.menu_items(b)
    check(len(items) == 1 and len(items[0]) == 4,
          f'the space menu has Edit, Clear data, Delete and New space ({[len(m) for m in items]})')
    labels = [b.ui.props(n).get('Text') or '' for n, _ in
              b.ui.find(lambda n: n['cls'] == 'MenuItemView', 2)]
    shot(ctx, 'space-menu-clear.png')
    click(ctx, center(items[0][1][0]))
    ok = wait_for(lambda: m4.dialog_button(b, 'Clear data', 1), 8)
    check(bool(ok), 'choosing it asks for a confirmation')
    shot(ctx, 'space-clear-confirm.png')
    texts = [b.ui.props(n).get('Text') or '' for n, _ in
             b.ui.find(lambda n: n['cls'] in ('Label', 'StyledLabel'), 2)]
    check(any('Clear the data of “A”?' in t for t in texts),
          f'the confirmation names the space ({[t for t in texts if "Clear" in t][:3]})')
    click(ctx, center(m4.dialog_button(b, 'Clear data')))
    reloaded = wait_for(lambda: (lambda m: m and m != before and m)(reload_marker(pa)), 15)
    check(bool(reloaded), 'A\'s tab reloaded')
    la, lb = live(pa), live(pb)
    check(la == {'cookies': '', 'local': '', 'idb': '', 'caches': '', 'sw': ''},
          f'A has no data left ({la})')
    check(lb == {'cookies': 'B', 'local': 'B', 'idb': 'B', 'caches': 'B', 'sw': 'B'},
          f'B kept its data ({lb})')
    check(any(t['url'].endswith('n=a') for t in b.pages()), 'A\'s tab is still open')
    seed(pa, 'R')

    # From the Spaces settings page.
    pa.activate()
    focus_main(ctx)
    m5.new_tab(ctx, b)
    m5.type_url(ctx, 'chrome://settings/spaces')
    s = settings_page(b, 'spaces')
    check(s is not None, 'the Spaces settings page opens')
    row = f'.space-row[data-id="{SPACE_B["id"]}"]'
    shot(ctx, 'spaces-settings-clear-data.png')
    click_in(ctx, b, s, 'settings-neon-spaces-page', f'{row} .clear-data')
    opened = wait_for(lambda: q(s, 'settings-neon-spaces-page', '#clearDataDialog',
                                expr='el.open'), 5)
    check(bool(opened), '"Clear data" asks for a confirmation')
    title = q(s, 'settings-neon-spaces-page', '#clearDataDialog [slot=title]',
              expr='el.textContent.trim()')
    check(title == 'Clear the data of “B”?', f'it names the space ({title!r})')
    shot(ctx, 'space-clear-confirm-settings.png')
    before = reload_marker(pb)
    click_in(ctx, b, s, 'settings-neon-spaces-page', '#confirmClearData')
    reloaded = wait_for(lambda: (lambda m: m and m != before and m)(reload_marker(pb)), 15)
    check(bool(reloaded), 'B\'s tab reloaded')
    la, lb = live(pa), live(pb)
    check(lb == {'cookies': '', 'local': '', 'idb': '', 'caches': '', 'sw': ''},
          f'B has no data left ({lb})')
    check(la == {'cookies': 'R', 'local': 'R', 'idb': 'R', 'caches': 'R', 'sw': 'R'},
          f'A kept its new data ({la})')
    check('who_G' in global_cookies(b), 'the global context kept its cookie')
    stop(ctx, b)
    expect_disk(ctx, profile, CONTAINER['A'], 'A', False, 'A after clearing A')
    expect_disk(ctx, profile, CONTAINER['A'], 'R', True, 'A\'s later data after clearing B')
    expect_disk(ctx, profile, CONTAINER['B'], 'B', False, 'B after clearing B')
    expect_disk(ctx, profile, '', 'G', True, 'global context after clearing A and B',
                keys=['cookie', 'http'])


# --- Site data pages ---------------------------------------------------------------------------

ALL_SITES = 'all-sites'


def site_entries(s):
    """[(display name, text)] of the entries All sites lists."""
    s.eval(DEEP_JS)
    return s.eval('''(() => {
      const page = neonQ("all-sites");
      if (!page) return null;
      return [...page.shadowRoot.querySelectorAll("site-entry")]
          .filter(e => e.offsetParent !== null && e.siteGroup)
          .map(e => [e.siteGroup.displayName,
                     e.shadowRoot.querySelector("#displayName").innerText.replace(/\\s+/g, " ").trim()]);
    })()''') or []


def space_select(s, page=ALL_SITES):
    return q(s, page, 'settings-neon-site-data-space', '#space',
             expr='[el.value, el.options[el.selectedIndex].textContent.trim(), '
                  '[...el.options].map(o => o.textContent.trim())]')


def pick_space(ctx, b, s, name, page=ALL_SITES):
    """Chooses |name| in the Space menu with real input: a click opens the menu, arrow keys
    move to the option, Return picks it."""
    value, current, names = space_select(s, page)
    click_in(ctx, b, s, page, 'settings-neon-site-data-space', '#space', settle_time=0.8)
    steps = names.index(name) - names.index(current)
    for _ in range(abs(steps)):
        key(ctx, 'Down' if steps > 0 else 'Up')
    key(ctx, 'Return')
    settle(2)
    return space_select(s, page)


def entry_names(s):
    return sorted(n for n, _ in site_entries(s))


def wait_entries(s, want, timeout=10):
    return wait_for(lambda: entry_names(s) == sorted(want), timeout)


def phase_site_data(ctx):
    print('== site-data: the site data pages show and delete one space at a time', flush=True)
    notifications = {f'http://127.0.0.1:{ctx.port},*': {'setting': 1}}
    prefs = prefs_for(profile={'content_settings': {'exceptions': {
        'notifications': notifications}}})
    b = start(ctx, 'site-data', prefs=prefs)
    profile = b.profile_dir
    pa, pb = two_spaces(ctx, b)
    pbl = open_in(ctx, b, 'B', 'bl', host='localhost')
    seed(pbl, 'K')

    pa.activate()
    focus_main(ctx)
    m5.new_tab(ctx, b)
    m5.type_url(ctx, 'chrome://settings/content/all')
    s = settings_page(b, 'content/all')
    check(s is not None, 'All sites opens from the omnibox')
    sel = wait_for(lambda: space_select(s), 10)
    check(sel and sel[1] == 'A' and sel[2] == ['A', 'B', 'Outside spaces (extensions)'],
          f'it shows space A, the space of its tab, and offers every space and the global '
          f'context ({sel})')
    check(wait_entries(s, ['127.0.0.1']), f'A\'s data: one site ({site_entries(s)})')
    text_a = dict(site_entries(s)).get('127.0.0.1', '')
    check('cookie' in text_a, f'with its usage and cookies ({text_a!r})')

    sel = pick_space(ctx, b, s, 'B')
    check(sel and sel[1] == 'B' and sel[0] == CONTAINER['B'], f'the Space menu switches to B ({sel})')
    check(wait_entries(s, ['127.0.0.1', 'localhost']),
          f'B\'s data: both sites ({entry_names(s)})')
    shot(ctx, 'site-data-space-b.png')

    # Delete 127.0.0.1 in B from its entry.
    s.eval(DEEP_JS)
    s.eval('''(() => {
      const page = neonQ("all-sites");
      const e = [...page.shadowRoot.querySelectorAll("site-entry")].find(e =>
          e.offsetParent !== null && e.siteGroup && e.siteGroup.displayName === "127.0.0.1");
      e.setAttribute("neon-target", "1");
    })()''')
    click_in(ctx, b, s, ALL_SITES, 'site-entry[neon-target]', '#removeSiteButton')
    dialog_text = wait_for(lambda: q(s, ALL_SITES, 'cr-dialog[open]',
                                     expr='el.textContent.replace(/\\s+/g, " ").trim()'), 5)
    check(dialog_text and 'in every space, since all spaces share them' in dialog_text,
          f'the delete dialog says the site\'s permissions are shared ({dialog_text!r})')
    shot(ctx, 'site-data-delete-dialog.png')
    click_in(ctx, b, s, ALL_SITES, 'cr-dialog[open] .action-button')
    check(wait_entries(s, ['localhost']), f'B lists localhost only after the delete ({entry_names(s)})')
    lb, la = live(pb), live(pa)
    check(lb == {'cookies': '', 'local': '', 'idb': '', 'caches': '', 'sw': ''},
          f'B has no 127.0.0.1 data left ({lb})')
    check(la == {'cookies': 'A', 'local': 'A', 'idb': 'A', 'caches': 'A', 'sw': 'A'},
          f'A kept its 127.0.0.1 data ({la})')

    # The page of localhost, in B.
    s.eval('''(() => {
      const page = neonQ("all-sites");
      const e = [...page.shadowRoot.querySelectorAll("site-entry")].find(e =>
          e.offsetParent !== null && e.siteGroup && e.siteGroup.displayName === "localhost");
      e.setAttribute("neon-target", "1");
    })()''')
    click_in(ctx, b, s, ALL_SITES, 'site-entry[neon-target]', '#toggleButton')
    details = wait_for(lambda: q(s, 'site-details', '#storedData',
                                 expr='el.textContent.trim()'), 10)
    sel = space_select(s, 'site-details')
    check(sel and sel[1] == 'B', f'the site\'s page shows B in its usage section ({sel})')
    check(bool(details), f'with the site\'s usage in B ({details!r})')
    note = q(s, 'site-details', 'settings-neon-site-data-space[mode=note]',
             expr='el.shadowRoot.textContent.trim()')
    check(note == 'Shared by all spaces', f'permissions say they are shared ({note!r})')
    shot(ctx, 'site-details-space.png')
    sel = pick_space(ctx, b, s, 'A', 'site-details')
    empty = wait_for(lambda: q(s, 'site-details', '#noStorage', expr='!el.hidden'), 8)
    check(sel and sel[1] == 'A' and empty, 'in A the site has no usage')
    sel = pick_space(ctx, b, s, 'B', 'site-details')
    wait_for(lambda: q(s, 'site-details', '#storage', expr='!el.hidden'), 8)
    click_in(ctx, b, s, 'site-details', '#clearStorage')
    click_in(ctx, b, s, 'site-details', '#confirmClearStorage .action-button')
    gone = wait_for(lambda: q(s, 'site-details', '#noStorage', expr='!el.hidden'), 8)
    check(bool(gone), 'Delete data on the site\'s page empties its usage in B')
    lbl = live(pbl)
    check(lbl == {'cookies': '', 'local': '', 'idb': '', 'caches': '', 'sw': ''},
          f'B has no localhost data left ({lbl})')

    # The global context.
    s.navigate('chrome://settings/content/all')
    settle(2)
    sel = pick_space(ctx, b, s, 'Outside spaces (extensions)')
    check(sel and sel[0] == '', f'the global context can be chosen ({sel})')
    check(wait_entries(s, ['127.0.0.1']), f'it lists the extension\'s site data ({entry_names(s)})')
    stop(ctx, b)
    # The site data pages delete site data, not the HTTP cache (as in stock Chromium).
    expect_disk(ctx, profile, CONTAINER['B'], 'B', False, 'B\'s 127.0.0.1 data after its delete',
                keys=['cookie', 'local', 'idb', 'caches', 'sw'])
    expect_disk(ctx, profile, CONTAINER['A'], 'A', True, 'A\'s 127.0.0.1 data after B\'s delete')
    expect_disk(ctx, profile, CONTAINER['B'], 'K', False, 'B\'s localhost data after its delete',
                keys=['cookie', 'local', 'idb', 'caches', 'sw'], host='localhost')
    expect_disk(ctx, profile, '', 'G', True, 'global context after the site data deletes',
                keys=['cookie'])


# --- Page info ---------------------------------------------------------------------------------

def view_button(b, cls, tooltip, timeout=5):
    for n, bounds in b.ui.find(lambda n: n['cls'] == cls, timeout):
        props = b.ui.props(n)
        if props.get('IsDrawn') == 'true' and props.get('TooltipText') == tooltip:
            return bounds
    return None


def view_texts(b, timeout=2):
    out = []
    for n, _ in b.ui.find(lambda n: n['cls'] in ('Label', 'StyledLabel'), timeout):
        props = b.ui.props(n)
        if props.get('IsDrawn') == 'true' and props.get('Text'):
            out.append(props['Text'])
    return out


def phase_page_info(ctx):
    print('== page-info: the site data of page info is the tab\'s own', flush=True)
    b = start(ctx, 'page-info')
    profile = b.profile_dir
    pa, pb = two_spaces(ctx, b)
    pa.activate()
    settle(0.5)
    focus_main(ctx)
    icon = b.ui.find(lambda n: n['cls'] == 'LocationIconView', 3)
    click(ctx, center(icon[0][1]), settle_time=1.2)
    row = view_button(b, 'RichHoverButton', 'Options for cookies and site data')
    check(row is not None, 'page info has a "Cookies and site data" row')
    click(ctx, center(row), settle_time=1.2)
    more = view_button(b, 'RichHoverButton',
                       'Review a list of on-device site data in a new window')
    check(more is not None, 'its page offers the on-device site data list')
    click(ctx, center(more), settle_time=2)
    delete = view_button(b, 'ImageButton', 'Delete saved data for 127.0.0.1')
    check(delete is not None, f'the list shows 127.0.0.1 ({[t for t in view_texts(b) if "127" in t]})')
    shot(ctx, 'page-info-site-data.png')
    click(ctx, center(delete), settle_time=1.5)
    done = m4.dialog_button(b, 'Done')
    if done:
        click(ctx, center(done))
    settle(1)
    la, lb = live(pa), live(pb)
    check(la['cookies'] == '' and la['local'] == '' and la['idb'] == '' and la['caches'] == '',
          f'deleting it there empties A\'s cookies and storage ({la})')
    check(lb == {'cookies': 'B', 'local': 'B', 'idb': 'B', 'caches': 'B', 'sw': 'B'},
          f'B kept its data ({lb})')
    stop(ctx, b)
    expect_disk(ctx, profile, CONTAINER['A'], 'A', False, 'A after page info\'s delete',
                keys=['cookie', 'local', 'idb', 'caches'])
    expect_disk(ctx, profile, CONTAINER['B'], 'B', True, 'B after A\'s page info delete')


# --- DevTools ----------------------------------------------------------------------------------

DT_SETUP = '''(async () => {
  const SDK = await import('./core/sdk/sdk.js');
  const App = await import('./panels/application/application.js');
  const t = SDK.TargetManager.TargetManager.instance().primaryPageTarget();
  window.neonDT = {SDK, App, t};
  return t.inspectedURL();
})()'''


def devtools_frontend(b, timeout=15):
    def find():
        for t in b.session.call('Target.getTargets')['targetInfos']:
            if t['url'].startswith('devtools://devtools/'):
                return Page(b, t['targetId'])
        return None
    d = wait_for(find, timeout)
    if d:
        wait_for(lambda: d.eval('document.readyState') == 'complete', 10)
        url = wait_for(lambda: _try(lambda: d.eval(DT_SETUP)), 20)
        d.inspected = url
    return d


def _try(fn):
    try:
        return fn()
    except RuntimeError:
        return None


def dt(d, body):
    """Runs |body| (an async function body) in the DevTools front end with t, SDK, App, key
    (A's storage key) and origin in scope."""
    return d.eval('(async () => { const {SDK, App, t} = neonDT; '
                  'const origin = new URL(t.inspectedURL()).origin; '
                  'const key = origin + "/"; ' + body + ' })()')


def cached(p, tag):
    """Whether /cached.js?n=|tag| is in |p|'s HTTP cache (without touching the network)."""
    return p.eval(f'fetch("/cached.js?n={tag}", {{cache: "only-if-cached", mode: "same-origin"}})'
                  '.then(r => r.ok, () => false)')


def fetch_ok(p, path):
    return p.eval(f'fetch("{path}", {{cache: "no-store"}}).then(r => r.ok, () => false)')


def phase_devtools(ctx):
    print('== devtools: DevTools on a container tab acts on that tab\'s space', flush=True)
    b = start(ctx, 'devtools')
    profile = b.profile_dir
    pa, pb = two_spaces(ctx, b)
    pa.activate()
    settle(0.5)
    focus_main(ctx)
    key(ctx, 'F12')
    d = devtools_frontend(b)
    check(d is not None and 'n=a' in (d.inspected or ''), 'F12 opens DevTools on A\'s tab')
    shot(ctx, 'devtools-a.png')

    # Application panel: cookies.
    got = dt(d, 'return (await t.networkAgent().invoke_getCookies({urls: [key]})).cookies.map(c => c.name).sort();')
    check('who_A' in got and 'who_B' not in got and 'who_G' not in got, f'Cookies view reads A ({got})')
    dt(d, 'return (await t.networkAgent().invoke_setCookie({name: "who_E", value: "1", url: key})).success;')
    check('who_E' in cookie_list(pa) and 'who_E' not in cookie_list(pb),
          'Cookies view: an edited cookie is set in A only')
    dt(d, 'await t.networkAgent().invoke_deleteCookies({name: "who_E", url: key}); return 1;')
    check('who_E' not in cookie_list(pa), 'Cookies view: deleting it removes it from A')
    check('who_B' in cookie_list(pb), '... and B keeps its cookies')

    # Local and session storage.
    items = dt(d, '''const id = (l) => ({storageKey: key, isLocalStorage: l});
      const a = await t.domstorageAgent().invoke_getDOMStorageItems({storageId: id(true)});
      const b = await t.domstorageAgent().invoke_getDOMStorageItems({storageId: id(false)});
      return [a.entries, b.entries];''')
    check(items == [[['ls_A', 'v_A']], [['ss_A', 'v_A']]],
          f'Local and session storage views read A ({items})')
    dt(d, 'await t.domstorageAgent().invoke_removeDOMStorageItem({storageId: {storageKey: key, isLocalStorage: true}, key: "ls_A"}); return 1;')
    check(live(pa)['local'] == '' and live(pb)['local'] == 'B',
          'removing ls_A there removes it from A only')

    # IndexedDB.
    names = dt(d, 'return (await t.indexedDBAgent().invoke_requestDatabaseNames({storageKey: key})).databaseNames;')
    check(names == ['db_A'], f'IndexedDB view lists A\'s database ({names})')
    dt(d, 'await t.indexedDBAgent().invoke_deleteDatabase({storageKey: key, databaseName: "db_A"}); return 1;')
    check(live(pa)['idb'] == '' and live(pb)['idb'] == 'B', 'deleting it there deletes A\'s only')

    # Cache Storage.
    caches_a = dt(d, 'return (await t.cacheStorageAgent().invoke_requestCacheNames({storageKey: key})).caches.map(c => c.cacheName);')
    check(caches_a == ['cache_A'], f'Cache storage view lists A\'s cache ({caches_a})')
    dt(d, '''const r = await t.cacheStorageAgent().invoke_requestCacheNames({storageKey: key});
      await t.cacheStorageAgent().invoke_deleteCache({cacheId: r.caches[0].cacheId}); return 1;''')
    check(live(pa)['caches'] == '' and live(pb)['caches'] == 'B', 'deleting it there deletes A\'s only')

    # Service workers.
    regs = dt(d, '''const m = t.model(SDK.ServiceWorkerManager.ServiceWorkerManager);
      for (let i = 0; i < 50 && !m.registrations().size; i++) await new Promise(r => setTimeout(r, 100));
      return [...m.registrations().values()].map(r => [...r.versions.values()].map(v => v.scriptURL)).flat();''')
    check(len(regs) == 1 and regs[0].endswith('sw.js?A'), f'Service workers view lists A\'s worker ({regs})')
    upd = dt(d, '''const m = t.model(SDK.ServiceWorkerManager.ServiceWorkerManager);
      const r = [...m.registrations().values()][0];
      await m.updateRegistration(r.id); return true;''')
    check(upd is True, 'Update works on A\'s worker')
    dt(d, '''const m = t.model(SDK.ServiceWorkerManager.ServiceWorkerManager);
      m.deleteRegistration([...m.registrations().values()][0].id); return 1;''')
    gone = wait_for(lambda: live(pa)['sw'] == '', 8)
    check(bool(gone) and live(pb)['sw'] == 'B', 'Unregister removes A\'s worker only')

    # "Clear site data", one checkbox at a time, as the Storage view's button does.
    seed(pa, 'D')
    kinds = [('cookies', 'cookies'), ('local_storage', 'local'), ('indexeddb', 'idb'),
             ('cache_storage', 'caches'), ('service_workers', 'sw')]
    for i, (storage_type, field) in enumerate(kinds):
        dt(d, f'App.StorageView.StorageView.clear(t, key, origin, ["{storage_type}"], false); return 1;')
        ok = wait_for(lambda: live(pa)[field] == '', 8)
        rest = live(pa)
        left = {f2: rest[f2] for _, f2 in kinds[i + 1:]}
        check(bool(ok) and all(v == 'D' for v in left.values()),
              f'Clear site data with only "{storage_type}" clears that in A ({rest})')
    check(live(pb) == {'cookies': 'B', 'local': 'B', 'idb': 'B', 'caches': 'B', 'sw': 'B'},
          f'B kept everything ({live(pb)})')
    quota = dt(d, 'const r = await t.storageAgent().invoke_getUsageAndQuota({origin}); '
                  'return r.usageBreakdown.filter(u => u.usage).map(u => u.storageType);')
    quota_b = [u['storageType'] for u in
               pb.call('Storage.getUsageAndQuota', {'origin': ctx.site})['usageBreakdown'] if u['usage']]
    check('indexeddb' not in quota and 'indexeddb' in quota_b,
          f'the storage quota shown is A\'s (A uses {quota}, B {quota_b})')

    # Network panel.
    for p, tag in ((pa, 'P'), (pb, 'Q')):
        p.eval(f'fetch("/cached.js?n={tag}").then(r => r.status)')
    check(cached(pa, 'P') and cached(pb, 'Q'), 'both spaces cached an entry')
    dt(d, 'await t.networkAgent().invoke_setCacheDisabled({cacheDisabled: true}); return 1;')
    n = hit_count('/cached.js?n=P'), hit_count('/cached.js?n=Q')
    pa.eval('fetch("/cached.js?n=P").then(r => r.status)')
    pb.eval('fetch("/cached.js?n=Q").then(r => r.status)')
    check(hit_count('/cached.js?n=P') == n[0] + 1 and hit_count('/cached.js?n=Q') == n[1],
          'Disable cache bypasses the cache in A only')
    dt(d, 'await t.networkAgent().invoke_setCacheDisabled({cacheDisabled: false}); return 1;')
    dt(d, 'SDK.NetworkManager.MultitargetNetworkManager.instance().clearBrowserCache(); return 1;')
    gone = wait_for(lambda: not cached(pa, 'P'), 8)
    check(bool(gone) and cached(pb, 'Q'), 'Clear browser cache empties A\'s cache only')
    dt(d, 'SDK.NetworkManager.MultitargetNetworkManager.instance().clearBrowserCookies(); return 1;')
    gone = wait_for(lambda: cookie_list(pa) == [], 8)
    check(bool(gone) and 'who_B' in cookie_list(pb) and 'who_G' in global_cookies(b),
          'Clear browser cookies empties A only')
    dt(d, 'SDK.NetworkManager.MultitargetNetworkManager.instance().setNetworkConditions('
          'SDK.NetworkManager.OfflineConditions); return 1;')
    settle(0.5)
    check(not fetch_ok(pa, '/plain') and fetch_ok(pb, '/plain'), 'Offline throttling applies to A only')
    dt(d, 'SDK.NetworkManager.MultitargetNetworkManager.instance().setNetworkConditions('
          'SDK.NetworkManager.NoThrottlingConditions); return 1;')
    settle(0.5)
    check(fetch_ok(pa, '/plain'), 'A is back online')
    blocked = dt(d, '''try { await t.networkAgent().invoke_setBlockedURLs({urlPatterns: [{urlPattern: "*://*:*/plain", block: true}]}); }
      catch (e) {}
      const r = await t.networkAgent().invoke_setBlockedURLs({urls: ["*plain*"]});
      return r.getError ? r.getError() || "" : "";''')
    settle(0.5)
    check(not fetch_ok(pa, '/plain') and fetch_ok(pb, '/plain'),
          f'Request blocking applies to A only ({blocked!r})')
    dt(d, 'await t.networkAgent().invoke_setBlockedURLs({urls: []}); return 1;')

    # "Empty cache and hard reload", from the reload button's menu while DevTools is open.
    for p, tag in ((pa, 'X'), (pb, 'Y')):
        p.eval(f'fetch("/cached.js?n={tag}").then(r => r.status)')
    check(cached(pa, 'X') and cached(pb, 'Y'), 'both spaces cached another entry')
    pa.activate()
    focus_main(ctx)
    reload_button = b.ui.find(lambda n: n['cls'] == 'ReloadButton', 3)
    click(ctx, center(reload_button[0][1]), button=3)
    items = m4.menu_items(b)
    check(items and len(items[0]) == 3, 'the reload button\'s menu has three entries')
    shot(ctx, 'reload-menu.png')
    before = reload_marker(pa)
    click(ctx, center(items[0][2][0]), settle_time=2)
    wait_for(lambda: (lambda m: m and m != before)(reload_marker(pa)), 10)
    check(not cached(pa, 'X') and cached(pb, 'Y'),
          '"Empty cache and hard reload" empties A\'s cache only')
    check('who_G' in global_cookies(b), 'the global context kept its cookie')
    stop(ctx, b)
    expect_disk(ctx, profile, CONTAINER['B'], 'B', True, 'B after DevTools on A')
    check(not dir_contains(os.path.join(partition_path(profile, CONTAINER['A']), 'Cache',
                                        'Cache_Data'), 'cached.js?n=P'),
          'on disk, A\'s HTTP cache no longer holds the entry DevTools cleared')
    check(dir_contains(os.path.join(partition_path(profile, CONTAINER['B']), 'Cache',
                                    'Cache_Data'), 'cached.js?n=Q') and
          dir_contains(os.path.join(partition_path(profile, CONTAINER['B']), 'Cache',
                                    'Cache_Data'), 'cached.js?n=Y'),
          'on disk, B\'s HTTP cache still holds its entries')
    check(dir_contains(os.path.join(partition_path(profile, ''), 'Cache', 'Cache_Data'),
                       'cached.js?n=G'),
          'on disk, the global context\'s HTTP cache still holds its entry')


# --- Other entry points ------------------------------------------------------------------------

def phase_entry_points(ctx):
    print('== entry-points: clearing on exit and sites set to delete data on close', flush=True)
    b = start(ctx, 'exit', '--enable-features=ClearDataOnExit')
    profile = b.profile_dir
    two_spaces(ctx, b)
    stop(ctx, b)
    for t in 'AB':
        expect_disk(ctx, profile, CONTAINER[t], t, False,
                    f'ClearDataOnExit: {t} after exit', keys=['cookie', 'local', 'idb', 'caches', 'http'])
    expect_disk(ctx, profile, '', 'G', False, 'ClearDataOnExit: global context after exit',
                keys=['cookie', 'http'])

    session_only = {'127.0.0.1,*': {'setting': 4}}
    prefs = prefs_for(profile={'content_settings': {'exceptions': {'cookies': session_only}}})
    b = start(ctx, 'session-only', prefs=prefs)
    profile = b.profile_dir
    two_spaces(ctx, b)
    stop(ctx, b)
    for t in 'AB':
        expect_disk(ctx, profile, CONTAINER[t], t, False,
                    f'a site that deletes its data on close: {t} after exit',
                    keys=['cookie', 'local', 'idb', 'caches'])
    expect_disk(ctx, profile, '', 'G', False,
                'a site that deletes its data on close: global context after exit',
                keys=['cookie'])


PHASES = [
    ('dialog-space', phase_dialog_space),
    ('dialog-all', phase_dialog_all),
    ('time-range', phase_time_range),
    ('site-data', phase_site_data),
    ('page-info', phase_page_info),
    ('devtools', phase_devtools),
    ('protocol', phase_protocol),
    ('clear-space', phase_clear_space),
    ('entry-points', phase_entry_points),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--helium', default=os.environ.get('NEON_HELIUM', m1.DEFAULT_HELIUM))
    ap.add_argument('--keep', action='store_true', help='keep the profiles for inspection')
    ap.add_argument('--shots', default=os.path.join(ROOT, 'build', 'm6'),
                    help='where the screenshots go (default: build/m6/)')
    ap.add_argument('--only', action='append', default=[],
                    help='run only these phases: ' + ', '.join(n for n, _ in PHASES))
    args = ap.parse_args()
    if not os.access(args.helium, os.X_OK):
        raise SystemExit(f'browser binary not found: {args.helium}')

    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(args.shots, exist_ok=True)
    log_path = os.path.join(LOG_DIR, 'data_tools_m6.log')
    ctx = Ctx()
    ctx.helium, ctx.dirs, ctx.browsers = args.helium, [], []
    ctx.log = open(log_path, 'w')
    ctx.shots = args.shots
    ctx.tmp = tempfile.mkdtemp(prefix='neon-m6-shots-')
    ctx.dirs.append(ctx.tmp)

    server = serve()
    ctx.port = server.server_address[1]
    ctx.site = f'http://127.0.0.1:{ctx.port}'
    ctx.display = free_display()
    xvfb = subprocess.Popen(['Xvfb', f':{ctx.display}', '-screen', '0',
                             f'{SCREEN[0]}x{SCREEN[1]}x24', '-nolisten', 'tcp'],
                            stdout=ctx.log, stderr=subprocess.STDOUT)
    time.sleep(1)
    try:
        for name, phase in PHASES:
            if args.only and name not in args.only:
                continue
            try:
                phase(ctx)
            except Exception as e:  # report and go on
                import traceback
                traceback.print_exc()
                check(False, f'{name} raised {type(e).__name__}: {e}')
                for b in ctx.browsers:
                    b.kill()
    finally:
        for b in ctx.browsers:
            b.close() if b.proc and b.proc.poll() is None and b.session else b.kill()
        xvfb.send_signal(signal.SIGTERM)
        xvfb.wait()
        server.shutdown()
        if args.keep:
            print('profiles kept:', *ctx.dirs)
        else:
            for d in ctx.dirs:
                shutil.rmtree(d, ignore_errors=True)
        ctx.log.close()

    crashes = [line for line in open(log_path, errors='replace')
               if 'Check failed' in line or 'FATAL' in line or 'DUMP_WILL_BE_CHECK' in line
               or 'Received signal' in line]
    check(not crashes, f'no CHECK failure or crash in the browser log {crashes[:3]}')
    print(f'browser log: {log_path}')
    if failures:
        print(f'{len(failures)} check(s) failed:', *failures, sep='\n  ')
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
