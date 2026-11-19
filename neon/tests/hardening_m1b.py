#!/usr/bin/env python3
"""M1b hardening check: every way a tab is created, restored, moved or navigated keeps it in the
storage partition of its space, and nothing set in one space can be read from another. Drives the
real Linux dev build with real X key and mouse events and the DevTools protocol.

Run from the neon-linux checkout (one command, from the host):

    neon/dev-container.sh sh 'python3 helium-chromium/neon/tests/hardening_m1b.py'

or inside `neon/dev-container.sh sh`:

    python3 helium-chromium/neon/tests/hardening_m1b.py [--keep] [--helium PATH]
                                                       [--browser-arg ARG] [--only PHASE]

Exit status is 0 when every check passes and 1 otherwise. Like the other Neon checks it starts
its own Xvfb, a local HTTP server on 127.0.0.1 and the browser with throwaway profiles, picks a
free display and free ports, and stops all of them at the end.

How state is observed, without test hooks in the browser:

- A tab's partition is read from the cookie its container holds on 127.0.0.1 (who=D for the
  default space, who=A, who=B), from the page itself or, for requests that are not pages
  (downloads, the favicon of a restored tab), from the Cookie header the test server logs.
- The visible space is the space of the page whose document.visibilityState is "visible".
- Keys go to the browser window as real X key events (xdotool), so they take the same path as a
  user's: accelerators, the omnibox, menus.

Phases (see each function for the paths it covers):

1. navigate: a URL typed into the omnibox (after Ctrl+T, Ctrl+L, Alt+D), an omnibox search,
   Alt+Enter, the home button, paste and go, a bookmarks bar click, Back and Forward across a
   browser page, reload and hard reload, an error page then a retry, each from a page and from
   the new tab page of space A; a link on the history page.
2. groups: saved tab groups closed and reopened (tab restore, the bookmarks bar chip, a closed
   window, before and after a restart) come back in their space; a group never mixes spaces;
   a space's tabs stay together next to groups.
3. split: a new split tab is in the split tab's container, with spaces on and off; the split's
   tab picker only offers tabs of its space.
4. extensions: chrome.tabs.create, chrome.windows.create (normal and popup) follow the visible
   space; an extension's own pages keep sharing storage with its background page.
5. tabs: reopen closed tab and window (in memory and after a restart), duplicate, view source,
   discard and Helium's Hibernate, move tab to new window, drag a tab out and into another
   window, tab search into a hidden space, a blocked popup opened from its bubble, noopener
   window.open, Shift+click, middle click on Back, a crashed tab reloaded, the bookmarks
   manager, a download, document picture-in-picture, DevTools, the task manager, print
   preview, and a pinned tab across a space switch and a restart.
6. leaks: cookies, localStorage, IndexedDB, Cache Storage, service workers and HTTP auth set in
   space A are not readable from B or the default space.

Uses the uBlock Origin that Helium bundles as the extension (driven from its background page),
and Helium's custom shortcuts pref for commands without a default shortcut.
"""
import argparse
import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import containers_m1 as m1  # noqa: E402  (DevTools client and helpers)
import spaces_m2 as m2  # noqa: E402  (also gives Session.call a deadline)
from containers_m1 import Browser, Page, check  # noqa: E402

ROOT = m1.ROOT
LOG_DIR = m1.LOG_DIR
failures = m1.failures

ALPHA, BETA = m1.ALPHA, m1.BETA
DEFAULT, SPACE_A, SPACE_B = m2.DEFAULT, m2.SPACE_A, m2.SPACE_B
HEX = m2.HEX
WHO = {'who=D': 'D', 'who=A': 'A', 'who=B': 'B', '': 'none'}


# --- Local test server -------------------------------------------------------------------------

# Every request is logged with its Cookie header, so requests that are not pages (downloads,
# favicons) still tell which partition made them.
REQUESTS = []
REQUESTS_LOCK = threading.Lock()


AUTH = 'Basic ' + __import__('base64').b64encode(b'neon:secret').decode()

SW_JS = b'''self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
'''

POPPER = '''<!doctype html><meta charset="utf-8"><title>popper</title>
<link rel="icon" href="/fav?c={color}">
<p>opens a popup without a user gesture</p>
<script>setTimeout(() => window.open('/page?n={name}-popup&c={color}', 'pop',
  'width=400,height=300'), 300);</script>
'''


class Handler(m2.Handler):
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        with REQUESTS_LOCK:
            REQUESTS.append((self.path, self.headers.get('Cookie', ''),
                             self.headers.get('Authorization', '')))
        q = urllib.parse.parse_qs(url.query)
        if url.path == '/auth':
            if self.headers.get('Authorization') != AUTH:
                self.send_response(401)
                # quiet: no challenge, so no sign-in prompt; only credentials the browser
                # sends up front from its auth cache get through.
                if 'quiet' not in q:
                    self.send_header('WWW-Authenticate', 'Basic realm="neon"')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            self.path = self.path.replace('/auth', '/page', 1)
        if url.path in ('/sw.js', '/popper'):
            if url.path == '/sw.js':
                body, kind = SW_JS, 'text/javascript'
            else:
                body = POPPER.format(name=q.get('n', ['x'])[0],
                                     color=q.get('c', ['808080'])[0]).encode()
                kind = 'text/html; charset=utf-8'
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path == '/download':
            body = b'neon download\n'
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Disposition',
                             f'attachment; filename="{q.get("n", ["dl"])[0]}.bin"')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def requests_for(part):
    """[(path, cookie, authorization)] of the requests whose path has |part|."""
    with REQUESTS_LOCK:
        return [r for r in REQUESTS if part in r[0]]


# --- Helpers -----------------------------------------------------------------------------------

Ctx = m2.Ctx
url = m2.url
name_of = m2.name_of
key = m2.key
xdo = m2.xdo
settle = m2.settle


def space_of(p):
    """D, A, B or none, from the cookie the page sees."""
    try:
        c = p.cookie()
    except RuntimeError as e:
        return f'?{e}'
    return WHO.get(c, f'?{c}')


# Commands without a default shortcut, bound through Helium's custom shortcuts pref so the test
# runs them from a key press.
IDC = {'create_group': 34101, 'add_tab_to_group': 34100, 'close_group': 34104,
       'new_split': 34057, 'duplicate': 34027, 'pin': 35013, 'to_new_window': 34056}
SHORTCUTS = {'create_group': 'ctrl+alt+g', 'add_tab_to_group': 'ctrl+alt+h',
             'close_group': 'ctrl+alt+j', 'new_split': 'ctrl+alt+s',
             'duplicate': 'ctrl+alt+k', 'pin': 'ctrl+alt+p', 'to_new_window': 'ctrl+alt+m'}


def accelerators():
    out = dict(m2.CUSTOM_ACCELERATORS)
    for name, combo in SHORTCUTS.items():
        letter = combo.rsplit('+', 1)[1].upper()
        out[str(IDC[name])] = {'added': [f'Control+Alt+Key{letter}']}
    return out


def prefs_for(ctx, spaces, extra=None, layout=2):
    prefs = m2.prefs_for(spaces, layout=layout)
    prefs['helium']['browser']['custom_accelerators'] = accelerators()
    # A search engine on the local server, so omnibox searches are observable.
    prefs['default_search_provider_data'] = {'template_url_data': {
        'keyword': 'neon', 'short_name': 'Neon', 'prepopulate_id': 0,
        'url': f'{ctx.site}/page?n=search-{{searchTerms}}&c={HEX["A"]}',
        'safe_for_autoreplace': False, 'id': '1001'}}
    prefs['homepage'] = url(ctx, 'home', 'A')
    prefs['homepage_is_newtabpage'] = False
    prefs.setdefault('browser', {})['show_home_button'] = True
    prefs['bookmark_bar'] = {'show_on_all_tabs': True}
    for k, v in (extra or {}).items():
        prefs[k] = v
    return prefs


def start_spaces(ctx, name, extra_prefs=None, layout=2):
    """A browser with spaces D, A and B, one tab in each with its cookie set. Shows A."""
    profile = m2.new_profile(ctx, name, prefs_for(ctx, [DEFAULT, SPACE_A, SPACE_B], extra_prefs,
                                                  layout=layout))
    bookmarks_file(ctx, profile)
    b = m2.start(ctx, profile, url(ctx, 'd1', 'D'))
    b.ctx_site = ctx.site
    p = m1.find_page(b, 'n=d1')
    p.wait_loaded('n=d1')
    m1.set_cookie(p, 'D')
    for cont, space, n in (('Beta', 'B', 'b1'), ('Alpha', 'A', 'a1')):
        b.remote(f'--container={cont}', url(ctx, n, space))
        p = m1.find_page(b, f'n={n}')
        p.wait_loaded(f'n={n}')
        m1.set_cookie(p, space)
    ctx.window = m2.window_ids(ctx)[0]
    ctx.profile = profile
    href, p = m2.active(b)
    check(name_of(href) == 'a1' and space_of(p) == 'A', f'{name}: start in space A ({href})')
    return b


def cmd(ctx, name, window=None):
    key(ctx, SHORTCUTS[name], window)


# --- Extension helpers (the uBlock Origin that Helium bundles, driven from its background page) --

def ext_bg(b):
    for t in b.session.call('Target.getTargets')['targetInfos']:
        if t['type'] == 'background_page' and t['url'].startswith('chrome-extension://'):
            return Page(b, t['targetId'])
    raise RuntimeError('no extension background page')


def ext(b, js):
    """Evaluates |js| (an expression using chrome.* APIs, may be a promise) in the extension."""
    return ext_bg(b).eval(js)


def tab_list(b):
    """[(index, windowId, groupId, splitId or None, url, pinned, id)] for every tab, from
    chrome.tabs."""
    return ext(b, '''chrome.tabs.query({}).then(ts => ts.map(t =>
        [t.index, t.windowId, t.groupId, t.splitViewId ?? null, t.url || t.pendingUrl,
         t.pinned, t.id]))''')


def tab_id(b, part):
    for t in tab_list(b):
        if part in (t[4] or ''):
            return t[6]
    return None


def reopen_until(ctx, b, part, tries=3):
    """Ctrl+Shift+T until a tab with |part| is back. After a restart the first entry is the
    window the test closed over DevTools."""
    for _ in range(tries):
        key(ctx, 'ctrl+shift+t')
        settle(1.5)
        if any(part in t['url'] for t in b.pages()):
            return True
    return False


def space_from_url(u):
    """The space a test page was opened for, from its colour parameter."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
    c = q.get('c', [''])[0]
    return {v: k for k, v in HEX.items()}.get(c)


def check_contiguous(b, what):
    """Each space's test pages sit next to each other in each window (new tab pages and other
    pages without a space colour are ignored), and no group mixes spaces."""
    tabs = sorted(tab_list(b), key=lambda t: (t[1], t[0]))
    ok, groups = True, {}
    by_window = {}
    for index, win, group, split, u, *_ in tabs:
        sp = space_from_url(u or '')
        if sp is None:
            continue
        by_window.setdefault(win, []).append(sp)
        if group != -1:
            groups.setdefault(group, set()).add(sp)
    runs = {}
    for win, seq in by_window.items():
        seen, last = set(), None
        for sp in seq:
            if sp != last and sp in seen:
                ok = False
            seen.add(sp)
            last = sp
        runs[win] = ''.join(seq)
    mixed = {g: sorted(v) for g, v in groups.items() if len(v) > 1}
    check(ok and not mixed, f'{what}: spaces are contiguous {runs} and no group mixes spaces '
          f'{mixed}')


def find_new(b, part, before, timeout=20):
    return m1.find_page(b, part, timeout=timeout, exclude=before)


def ids(b):
    return {t['targetId'] for t in b.pages()}


def open_ntp(ctx, b):
    """Ctrl+T: a new tab page in the visible space. Returns its Page."""
    key(ctx, 'ctrl+t')
    href, p = m2.wait_active(b, lambda h: h and h.startswith('chrome://'))
    return p


def open_page_tab(ctx, b, n, space='A'):
    """A new tab in the visible space showing page n (Ctrl+T, then a browser navigation)."""
    p = open_ntp(ctx, b)
    p.call('Page.navigate', {'url': url(ctx, n, space)})
    p.wait_loaded(f'n={n}')
    return p


def type_in_omnibox(ctx, text, focus='ctrl+l', submit='Return', window=None):
    if focus:
        key(ctx, focus, window)
    xdo(ctx, 'type', '--delay', '20', text)
    settle(0.3)
    if submit:
        key(ctx, submit, window)


def expect_in(ctx, b, what, part, space, before=None, same_tab=None, timeout=20):
    """Waits for a page whose URL has |part| and checks its space (by cookie) and that the
    window still shows that space."""
    try:
        if same_tab is not None:
            p = same_tab
        else:
            p = m1.find_page(b, part, timeout=timeout, exclude=before or ())
        p.wait_loaded(part, timeout=timeout)
    except (TimeoutError, RuntimeError) as e:
        others = [t['url'] for t in b.pages() if part in t['url']]
        check(False, f'{what}: {e}' + (f' (it opened elsewhere: {others})' if others else ''))
        return None
    got = space_of(p)
    check(got == space, f'{what}: loads in {space} ({got})' +
          (', same tab' if same_tab is not None else ''))
    # Every visible web page (one per window, two for a split) is in that space.
    vis = set()
    for _ in range(25):
        visible = m2.visible_pages(b)
        if visible:
            break
        time.sleep(0.2)
    for href, vp in visible:
        if href and href.startswith('http'):
            vis.add(space_of(vp))
    check(vis <= {space}, f'{what}: the visible pages are in {space} ({sorted(vis)})')
    return p


# --- Phases ------------------------------------------------------------------------------------

def phase_navigate(ctx):
    """Defect 1 and its siblings. Each path starts from a page of space A and from a new tab
    page of space A, and must end in A."""
    print('== navigate', flush=True)
    b = start_spaces(ctx, 'navigate')

    for origin in ('page', 'ntp'):
        def start_tab(tag):
            key(ctx, 'alt+shift+2')  # a failed case may have left another space showing
            if origin == 'page':
                return open_page_tab(ctx, b, f'{origin}-{tag}-start')
            return open_ntp(ctx, b)

        # Typing a URL right after Ctrl+T (the omnibox has focus) and after Ctrl+L / Alt+D.
        for focus in (None, 'ctrl+l', 'alt+d'):
            if origin == 'page' and focus is None:
                continue
            tag = f'typed-{focus or "direct"}-{origin}'
            p = start_tab(tag)
            type_in_omnibox(ctx, url(ctx, tag, 'A'), focus=focus)
            expect_in(ctx, b, f'{origin}: URL typed after {focus or "Ctrl+T"}', f'n={tag}', 'A',
                      same_tab=p)

        # Omnibox search through the default search engine (the local server).
        tag = f'search-q{origin}'
        p = start_tab(tag)
        type_in_omnibox(ctx, f'q{origin} words')
        expect_in(ctx, b, f'{origin}: omnibox search', f'n=search-q{origin}', 'A', same_tab=p)

        # Alt+Enter opens the typed URL in a new tab.
        tag = f'altenter-{origin}'
        start_tab(tag)
        before = ids(b)
        type_in_omnibox(ctx, url(ctx, tag, 'A'), submit='alt+Return')
        expect_in(ctx, b, f'{origin}: Alt+Enter (new tab)', f'n={tag}', 'A', before=before)

        # Home button (Alt+Home): the home page is a web page.
        tag = f'home-{origin}'
        p = start_tab(tag)
        key(ctx, 'alt+Home')
        expect_in(ctx, b, f'{origin}: home button', 'n=home', 'A', same_tab=p)

        # Paste and go, from the omnibox context menu (a real right click).
        tag = f'paste-{origin}'
        p = start_tab(tag)
        type_in_omnibox(ctx, url(ctx, tag, 'A'), submit=None)
        key(ctx, 'ctrl+a')
        key(ctx, 'ctrl+x')
        paste_and_go(ctx)
        expect_in(ctx, b, f'{origin}: paste and go', f'n={tag}', 'A', same_tab=p)

        # A bookmark on the bookmarks bar, clicked.
        p = start_tab('bookmark')
        click_bookmark(ctx)
        expect_in(ctx, b, f'{origin}: bookmark bar click', 'n=bookmarked', 'A', same_tab=p)

        # Back and Forward across a browser page, then a typed URL.
        tag = f'bf-{origin}'
        p = start_tab(tag)
        type_in_omnibox(ctx, 'chrome://version')
        m2.wait_active(b, lambda h: h and h.startswith('chrome://version'))
        key(ctx, 'alt+Left')
        if origin == 'page':
            expect_in(ctx, b, f'{origin}: Back from a browser page', f'n={origin}-{tag}-start', 'A',
                      same_tab=p)
        else:
            href, _ = m2.wait_active(b, lambda h: h and h.startswith('chrome://new'))
            check(href is not None and href.startswith('chrome://new'),
                  f'{origin}: Back from chrome://version shows the new tab page ({href})')
        key(ctx, 'alt+Right')
        href, _ = m2.wait_active(b, lambda h: h and h.startswith('chrome://version'))
        check(href is not None, f'{origin}: Forward to chrome://version ({href})')
        type_in_omnibox(ctx, url(ctx, tag, 'A'))
        expect_in(ctx, b, f'{origin}: URL typed after Back and Forward', f'n={tag}&', 'A',
                  same_tab=p)
        key(ctx, 'alt+Left')
        key(ctx, 'alt+Left')
        if origin == 'page':
            expect_in(ctx, b, f'{origin}: Back twice', f'n={origin}-{tag}-start', 'A',
                      same_tab=p)

        # Reload, then a typed URL.
        tag = f'reload-{origin}'
        p = start_tab(tag)
        key(ctx, 'F5')
        settle(1)
        type_in_omnibox(ctx, url(ctx, tag, 'A'))
        expect_in(ctx, b, f'{origin}: reload, then a typed URL', f'n={tag}&', 'A', same_tab=p)
        key(ctx, 'ctrl+shift+r')
        expect_in(ctx, b, f'{origin}: hard reload', f'n={tag}&', 'A', same_tab=p)

        # An error page (nothing listens yet), then a retry once the server is up.
        tag = f'err-{origin}'
        p = start_tab(tag)
        port = m1.free_port()
        bad = f'http://127.0.0.1:{port}/page?n={tag}&c={HEX["A"]}'
        type_in_omnibox(ctx, bad)
        settle(2)
        srv = http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            key(ctx, 'F5')
            expect_in(ctx, b, f'{origin}: error page, then reload', f'n={tag}', 'A', same_tab=p)
        finally:
            srv.shutdown()
            srv.server_close()

    # A link on a browser page (the history page), from space A. The downloads page is
    # covered by the download phase.
    for page, part in (('chrome://history', 'n=hist'),):
        key(ctx, 'alt+shift+2')
        if part == 'n=hist':
            open_page_tab(ctx, b, 'hist')
        p = open_ntp(ctx, b)
        type_in_omnibox(ctx, page)
        href, p = m2.wait_active(b, lambda h: h and h.startswith(page))
        check(p is not None, f'{page} opened in A ({href})')
        if not p:
            continue
        before = ids(b)
        clicked = click_deep_link(p, part)
        check(clicked, f'{page}: found a link to {part}')
        if clicked:
            expect_in(ctx, b, f'{page}: link click', part, 'A', same_tab=p)
    b.close()


def paste_and_go(ctx):
    """Opens the omnibox context menu (Shift+F10, the menu key) and clicks "Paste and go", its
    fifth row."""
    key(ctx, 'shift+F10')
    settle(0.5)
    xdo(ctx, 'mousemove', '--sync', '400', '191')
    xdo(ctx, 'click', '1')
    settle()


def click_bookmark(ctx):
    """Clicks the first item of the bookmarks bar."""
    xdo(ctx, 'mousemove', '--sync', *BOOKMARK_XY)
    xdo(ctx, 'click', '1')
    settle()


# The first bookmark on the bookmarks bar, below the toolbar, right of the sidebar.
BOOKMARK_XY = ('310', '48')
# With one saved tab group, its chip comes first on the bookmarks bar.
SAVED_GROUP_CHIP_XY = ('216', '48')


def click_deep_link(p, part):
    """Clicks the first link whose href has |part| in a WebUI page, searching shadow roots."""
    js = '''(() => {
      const seen = [];
      const walk = (root) => {
        for (const el of root.querySelectorAll('*')) {
          if (el.href && String(el.href).includes(%s)) seen.push(el);
          if (el.shadowRoot) walk(el.shadowRoot);
        }
      };
      for (let i = 0; i < 3 && !seen.length; i++) walk(document);
      if (!seen.length) return null;
      const r = seen[0].getBoundingClientRect();
      return [r.x + Math.min(r.width / 2, 40), r.y + r.height / 2];
    })()''' % json.dumps(part)
    box = None
    for _ in range(25):
        box = p.eval(js)
        if box:
            break
        time.sleep(0.4)
    if not box:
        return False
    p.activate()
    for kind in ('mouseMoved', 'mousePressed', 'mouseReleased'):
        params = {'type': kind, 'x': box[0], 'y': box[1]}
        if kind != 'mouseMoved':
            params.update(button='left', clickCount=1)
        p.call('Input.dispatchMouseEvent', params)
    return True


CRASH_BEGIN = 'NEON-M1B intentional renderer crash begins'
CRASH_END = 'NEON-M1B intentional renderer crash ends'


def marker(ctx, text):
    ctx.log.flush()
    os.write(ctx.log.fileno(), f'{text}\n'.encode())


def double_click_deep(p, tag):
    """Double-clicks the first |tag| element of a WebUI page, searching shadow roots."""
    js = '''(() => {
      let found = null;
      const walk = (root) => {
        for (const el of root.querySelectorAll('*')) {
          if (!found && el.localName === %s) found = el;
          if (el.shadowRoot) walk(el.shadowRoot);
        }
      };
      walk(document);
      if (!found) return null;
      const r = found.getBoundingClientRect();
      return [r.x + Math.min(r.width / 2, 60), r.y + r.height / 2];
    })()''' % json.dumps(tag)
    box = None
    for _ in range(25):
        box = p.eval(js)
        if box:
            break
        time.sleep(0.4)
    if not box:
        return False
    p.activate()
    for count in (1, 2):
        for kind in ('mousePressed', 'mouseReleased'):
            p.call('Input.dispatchMouseEvent', {'type': kind, 'x': box[0], 'y': box[1],
                                                'button': 'left', 'clickCount': count})
    return True


def bookmarks_file(ctx, profile):
    """A bookmarks bar holding one bookmark, written before the browser starts."""
    data = {'version': 1, 'roots': {
        'bookmark_bar': {'type': 'folder', 'name': 'Bookmarks bar', 'id': '1',
                         'date_added': '13300000000000000', 'children': [
                             {'type': 'url', 'name': 'bookmarked', 'id': '4',
                              'date_added': '13300000000000000',
                              'url': url(ctx, 'bookmarked', 'A')}]},
        'other': {'type': 'folder', 'name': 'Other', 'id': '2', 'children': [],
                  'date_added': '13300000000000000'},
        'synced': {'type': 'folder', 'name': 'Mobile', 'id': '3', 'children': [],
                   'date_added': '13300000000000000'}}}
    with open(os.path.join(profile, 'Default', 'Bookmarks'), 'w') as f:
        json.dump(data, f)


def phase_groups(ctx):
    """Defect 2: saved tab groups reopen in their space; a group never holds tabs of two spaces
    and groups and splits stay together when tabs are ordered by space."""
    print('== groups', flush=True)
    b = start_spaces(ctx, 'groups')

    def make_group(prefix, window=None):
        cmd(ctx, 'create_group', window)
        href, p = m2.wait_active(b, lambda h: h and h.startswith('chrome://'))
        p.call('Page.navigate', {'url': url(ctx, f'{prefix}1', 'A')})
        p.wait_loaded(f'n={prefix}1')
        cmd(ctx, 'add_tab_to_group', window)
        href, p = m2.wait_active(b, lambda h: h and h.startswith('chrome://'))
        p.call('Page.navigate', {'url': url(ctx, f'{prefix}2', 'A')})
        p.wait_loaded(f'n={prefix}2')

    def expect_group(what, prefix, space='A'):
        for n in (1, 2):
            try:
                m1.find_page(b, f'n={prefix}{n}&').activate()
            except (TimeoutError, RuntimeError):
                pass
            settle()
            expect_in(ctx, b, f'{what}: {prefix}{n}', f'n={prefix}{n}&', space, timeout=15)
        grouped = [t for t in tab_list(b) if f'n={prefix}' in (t[4] or '')]
        check(len(grouped) == 2 and len({t[2] for t in grouped}) == 1 and grouped[0][2] != -1,
              f'{what}: both tabs are back in one group '
              f'({[(t[2], name_of(t[4])) for t in grouped]})')

    make_group('g')
    for n in (1, 2):
        check(space_of(m1.find_page(b, f'n=g{n}&')) == 'A', f'group tab g{n} opened in A')
    check_contiguous(b, 'after creating a group in A')

    # A tab of B opened while the A group exists still joins B's run (M2 left ranges with a
    # group unsorted).
    key(ctx, 'alt+shift+3')
    open_page_tab(ctx, b, 'b2', 'B')
    check_contiguous(b, 'a tab opened in B after a group of A')

    # Close the group and reopen it with Ctrl+Shift+T from space B.
    key(ctx, 'alt+shift+2')
    m2.wait_active(b, lambda h: h and 'n=g2' in h)
    href, _ = m2.active(b)
    if 'n=g' not in (href or ''):
        key(ctx, 'ctrl+9')
    cmd(ctx, 'close_group')
    settle(1)
    check(not any('n=g' in t['url'] for t in b.pages()), 'the group closed')
    key(ctx, 'alt+shift+3')
    key(ctx, 'ctrl+shift+t')
    expect_group('closed group reopened from B', 'g')
    check_contiguous(b, 'after reopening the group')

    # Close it again and reopen it from its chip on the bookmarks bar, from space B.
    m1.find_page(b, 'n=g2&').activate()
    settle()
    cmd(ctx, 'close_group')
    settle(1)
    key(ctx, 'alt+shift+3')
    xdo(ctx, 'mousemove', '--sync', *SAVED_GROUP_CHIP_XY)
    xdo(ctx, 'click', '1')
    settle(1.5)
    expect_group('closed group reopened from the bookmarks bar in B', 'g')

    # A window holding a group of A, closed and reopened from B.
    key(ctx, 'alt+shift+2')
    wins = set(m2.window_ids(ctx))
    key(ctx, 'ctrl+n')
    settle(1.5)
    new = [w for w in m2.window_ids(ctx) if w not in wins]
    check(len(new) == 1, f'a second window opened ({new})')
    if new:
        win2 = new[0]
        make_group('w', win2)
        key(ctx, 'ctrl+shift+w', win2)
        settle(1.5)
        check(not any('n=w' in t['url'] for t in b.pages()), 'the second window closed')
        key(ctx, 'alt+shift+3')
        key(ctx, 'ctrl+shift+t')
        settle(2)
        expect_group('closed window with a group reopened', 'w')
        for w in set(m2.window_ids(ctx)) - wins:
            key(ctx, 'ctrl+shift+w', w)
        settle(1)

    # An extension groups a tab of B with the group of A: the B tab is taken out again.
    tabs = tab_list(b)
    g_group = next(t[2] for t in tabs if 'n=g1' in (t[4] or ''))
    b1_id = tab_id(b, 'n=b1&')
    ext(b, f'chrome.tabs.group({{tabIds: [{b1_id}], groupId: {g_group}}}).then(() => 1)')
    settle(1.5)
    tabs = tab_list(b)
    b1 = next(t for t in tabs if 'n=b1' in (t[4] or ''))
    check(b1[2] != g_group, f'a B tab put in the group of A by an extension leaves it ({b1[2]})')
    check_contiguous(b, 'after an extension mixed a group')

    # Restart with the group open, then close it, restart again and reopen it.
    b.close()
    b.start()
    ctx.window = m2.window_ids(ctx)[0]
    settle(2)
    key(ctx, 'alt+shift+2')
    expect_group('group restored after a restart', 'g')
    check_contiguous(b, 'after a restart')
    p = m1.find_page(b, 'n=g2&')
    p.activate()
    settle()
    cmd(ctx, 'close_group')
    settle(1)
    b.close()
    b.start()
    ctx.window = m2.window_ids(ctx)[0]
    settle(2)
    key(ctx, 'alt+shift+3')
    # Closing the browser over DevTools records its window as closed, so the first entry
    # reopens that window; the group is the next one.
    wins = set(m2.window_ids(ctx))
    key(ctx, 'ctrl+shift+t')
    settle(2)
    if set(m2.window_ids(ctx)) - wins:
        key(ctx, 'ctrl+shift+t')
        settle(2)
    expect_group('group closed before a restart, reopened after it', 'g')
    b.close()


def phase_split(ctx):
    """Defect 4: a new split tab opens in the container of the tab it splits, with spaces
    showing and with spaces off (classic layout, containers only)."""
    print('== split', flush=True)
    for layout, label in ((2, 'spaces'), (0, 'classic')):
        b = start_spaces(ctx, f'split-{label}', layout=layout)
        if layout == 0:
            m1.find_page(b, 'n=a1').activate()
            settle()
        else:
            open_page_tab(ctx, b, 'a2')  # something for the split's tab picker to offer
        cmd(ctx, 'new_split')
        settle(1.5)
        splits = [t for t in tab_list(b) if t[3] is not None and t[3] != -1]
        check(len(splits) == 2, f'{label}: the active tab and a new tab form a split ({splits})')
        if layout == 2:
            # The split's tab picker lists only tabs of A: no blue (B) or green (D) favicon in
            # the content area.
            path = m2.shot(ctx, os.path.join(ctx.tmp, 'picker.png'))
            got = m2.favicon_counts(path, x0=740, x1=1280, y0=60, y1=880)
            check(got.get('A', 0) >= 1 and got.get('B', 0) == 0 and got.get('D', 0) == 0,
                  f'{label}: the split tab picker offers a1 and no tab of another space ({got})')
        # The new half is the split's tab picker page (a new tab page when it has nothing to
        # offer); a URL typed there loads in A.
        settle(1.5)
        type_in_omnibox(ctx, url(ctx, f'split-{label}', 'A'))
        expect_in(ctx, b, f'{label}: the new split tab', f'n=split-{label}', 'A')
        b.close()


def phase_extensions(ctx):
    """Defect 5: tabs and windows that extensions open follow the visible space of the target
    window; an extension's own pages stay in its (default) partition."""
    print('== extensions', flush=True)
    b = start_spaces(ctx, 'extensions')
    for space, keys in (('A', 'alt+shift+2'), ('B', 'alt+shift+3')):
        key(ctx, keys)
        n = f'ext-tab-{space}'
        ext(b, f'chrome.tabs.create({{url: "{url(ctx, n, space)}"}}).then(t => t.id)')
        expect_in(ctx, b, f'chrome.tabs.create while {space} shows', f'n={n}', space)
    key(ctx, 'alt+shift+2')
    for kind, extra in (('window', ''), ('popup', ', type: "popup"')):
        n = f'ext-{kind}'
        ext(b, f'chrome.windows.create({{url: "{url(ctx, n, "A")}"{extra}}}).then(w => w.id)')
        p = m1.find_page(b, f'n={n}')
        p.wait_loaded(f'n={n}')
        check(space_of(p) == 'A', f'chrome.windows.create ({kind}) while A shows: A '
              f'({space_of(p)})')
        p.eval('window.close()')
        settle(1)
    # The extension's own page: shares localStorage with its background page.
    bg = ext_bg(b)
    bg.eval('localStorage.setItem("neon-m1b", "bg"); 1')
    for how, js in (('chrome.tabs.create', 'chrome.tabs.create({url: chrome.runtime.getURL('
                     '"dashboard.html")}).then(t => t.id)'),
                    ('runtime.openOptionsPage', 'chrome.runtime.openOptionsPage().then(() => 1)')):
        before = ids(b)
        key(ctx, 'alt+shift+2')
        bg.eval(js)
        try:
            p = find_new(b, 'dashboard.html', before)
            time.sleep(1)
            got = p.eval('localStorage.getItem("neon-m1b")')
        except (TimeoutError, RuntimeError) as e:
            got = str(e)
        check(got == 'bg', f'{how}: the extension page shares storage with its background page '
              f'({got})')
        key(ctx, 'ctrl+w')
    b.close()


def phase_tabs(ctx):
    """Every other way a tab is created, restored or moved, from space A."""
    print('== tabs', flush=True)
    b = start_spaces(ctx, 'tabs')

    def from_b_reopen(what, part):
        key(ctx, 'alt+shift+3')
        found = reopen_until(ctx, b, part)
        check(found, f'{what}: reopened')
        if found:
            expect_in(ctx, b, what, part, 'A')

    # Reopen a closed tab, and a closed window, from space B (in memory).
    open_page_tab(ctx, b, 'rc')
    key(ctx, 'ctrl+w')
    from_b_reopen('reopen closed tab', 'n=rc&')
    key(ctx, 'alt+shift+2')
    wins = set(m2.window_ids(ctx))
    key(ctx, 'ctrl+n')
    settle(1.5)
    win2 = [w for w in m2.window_ids(ctx) if w not in wins]
    if check(len(win2) == 1, 'Ctrl+N opened a window'):
        type_in_omnibox(ctx, url(ctx, 'rw', 'A'), focus=None, window=win2[0])
        expect_in(ctx, b, 'new window from A', 'n=rw&', 'A')
        key(ctx, 'ctrl+shift+w', win2[0])
        settle(1.5)
        from_b_reopen('reopen closed window', 'n=rw&')
        for w in set(m2.window_ids(ctx)) - wins:
            key(ctx, 'ctrl+shift+w', w)
        settle(1)

    # Duplicate tab, view source.
    key(ctx, 'alt+shift+2')
    p = open_page_tab(ctx, b, 'dup')
    before = ids(b)
    cmd(ctx, 'duplicate')
    expect_in(ctx, b, 'duplicate tab', 'n=dup&', 'A', before=before)
    p = open_page_tab(ctx, b, 'vs')
    n_before = len(requests_for('n=vs&'))
    key(ctx, 'ctrl+u')
    settle(2)
    reqs = requests_for('n=vs&')[n_before:]
    check(any('view-source:' in t['url'] for t in b.pages()), 'view source opened')
    check(reqs and all(r[1] == 'who=A' for r in reqs),
          f'view source fetches the page with A\'s cookie ({[r[1] for r in reqs]})')
    key(ctx, 'ctrl+w')

    # Discard (chrome.tabs.discard, the same discard as Helium's "Hibernate") and reload.
    key(ctx, 'alt+shift+2')
    open_page_tab(ctx, b, 'disc')
    open_page_tab(ctx, b, 'disc-other')
    tid = tab_id(b, 'n=disc&')
    new_id = ext(b, f'chrome.tabs.discard({tid}).then(t => t ? t.id : null)')
    check(new_id is not None, f'tab discarded ({tid} -> {new_id})')
    settle(1)
    ext(b, f'chrome.tabs.update({new_id}, {{active: true}}).then(t => t.id)')
    expect_in(ctx, b, 'discarded tab reloaded when activated', 'n=disc&', 'A')
    hibernate_tab(ctx, b, 'disc-other')

    # Move tab to new window (command), then Ctrl+T in that window.
    key(ctx, 'alt+shift+2')
    open_page_tab(ctx, b, 'mv')
    wins = set(m2.window_ids(ctx))
    cmd(ctx, 'to_new_window')
    settle(1.5)
    new = [w for w in m2.window_ids(ctx) if w not in wins]
    if check(len(new) == 1, 'move tab to new window opened a window'):
        expect_in(ctx, b, 'tab moved to a new window', 'n=mv&', 'A')
        key(ctx, 'ctrl+t', new[0])
        href, p2 = m2.wait_active(b, lambda h: h and h.startswith('chrome://'))
        if p2:
            p2.call('Page.navigate', {'url': url(ctx, 'mv-new', 'A')})
            expect_in(ctx, b, 'Ctrl+T in the moved tab\'s window', 'n=mv-new', 'A')
        key(ctx, 'ctrl+shift+w', new[0])
        settle(1)

    # Drag a tab out of the sidebar into a new window.
    key(ctx, 'alt+shift+2', ctx.window)
    open_page_tab(ctx, b, 'drag')
    wins = set(m2.window_ids(ctx))
    drag_active_tab_out(ctx)
    new = [w for w in m2.window_ids(ctx) if w not in wins]
    if check(len(new) == 1, f'dragging a tab out opened a window ({new})'):
        expect_in(ctx, b, 'tab dragged into a new window', 'n=drag&', 'A')
        key(ctx, 'ctrl+t', new[0])
        href, p2 = m2.wait_active(b, lambda h: h and h.startswith('chrome://'))
        if p2:
            p2.call('Page.navigate', {'url': url(ctx, 'drag-new', 'A')})
            expect_in(ctx, b, 'Ctrl+T in the dragged tab\'s window', 'n=drag-new', 'A')
        # Back into the main window, which shows B: the dragged tab brings A along.
        key(ctx, 'ctrl+w', new[0])
        key(ctx, 'alt+shift+3', ctx.window)
        wx, wy = window_origin(ctx, new[0])
        drag_tab_into_main(ctx, wx + 50, wy + 76)
        windows = {t[1] for t in tab_list(b) if 'n=drag' in (t[4] or '')}
        main = window_tab_id(b)
        check(main in windows, f'a tab dragged into the main window joins it ({windows})')
        expect_in(ctx, b, 'tab dragged into another window', 'n=drag', 'A')
        for w in set(m2.window_ids(ctx)) - wins:
            key(ctx, 'ctrl+shift+w', w)
        settle(1)

    # Tab search activates a tab of a hidden space and shows that space.
    key(ctx, 'alt+shift+2', ctx.window)
    key(ctx, 'ctrl+shift+a')
    settle(1.5)
    xdo(ctx, 'type', '--delay', '30', 'b1')
    settle(1)
    key(ctx, 'Return')
    href, p = m2.wait_active(b, lambda h: name_of(h) == 'b1')
    check(name_of(href) == 'b1' and p is not None and space_of(p) == 'B',
          f'tab search activates b1 and shows space B ({href})')
    m2.expect_sidebar(ctx, 'space B shows after tab search', {'D': 0, 'A': 0, 'B': 1})

    # A popup blocked without a user gesture, then opened from the blocked popup bubble.
    key(ctx, 'alt+shift+2')
    p = open_ntp(ctx, b)
    p.call('Page.navigate', {'url': f'{ctx.site}/popper?n=pb&c={HEX["A"]}'})
    p.wait_loaded('popper')
    settle(1.5)
    check(not any('n=pb-popup' in t['url'] for t in b.pages()), 'the popup was blocked')
    open_blocked_popup(ctx)
    expect_in(ctx, b, 'blocked popup opened from the bubble', 'n=pb-popup', 'A', timeout=10)

    # window.open with noopener, from a page of A.
    key(ctx, 'alt+shift+2')
    p = open_page_tab(ctx, b, 'opener')
    before = ids(b)
    p.eval(f'window.open("{url(ctx, "noop", "A")}", "_blank", "noopener"); 1', gesture=True)
    expect_in(ctx, b, 'window.open with noopener', 'n=noop', 'A', before=before)

    # Shift+click opens a link in a new window.
    key(ctx, 'alt+shift+2', ctx.window)
    p = open_page_tab(ctx, b, 'shift')
    wins = set(m2.window_ids(ctx))
    before = ids(b)
    p.click('#ctrl', modifiers=8)
    expect_in(ctx, b, 'shift+click link (new window)', 'n=shift-ctrl', 'A', before=before)
    for w in set(m2.window_ids(ctx)) - wins:
        key(ctx, 'ctrl+shift+w', w)
    settle(1)

    # Middle click on Back opens the previous page in a new tab.
    key(ctx, 'alt+shift+2', ctx.window)
    p = open_page_tab(ctx, b, 'mid')
    p.click('#next')
    p.wait_loaded('n=mid-next')
    before = ids(b)
    xdo(ctx, 'mousemove', '--sync', '51', '17')
    xdo(ctx, 'click', '2')
    expect_in(ctx, b, 'middle click on Back (new tab)', 'n=mid&', 'A', before=before)

    # A crashed renderer (a sad tab), then a reload. The crash is logged between markers that
    # the crash check at the end skips.
    key(ctx, 'alt+shift+2')
    p = open_page_tab(ctx, b, 'crash')
    marker(ctx, CRASH_BEGIN)
    try:
        p.call('Page.crash')
    except RuntimeError:
        pass
    settle(2)
    marker(ctx, CRASH_END)
    key(ctx, 'F5')
    expect_in(ctx, b, 'crashed tab reloaded', 'n=crash', 'A', same_tab=p)

    # A bookmark opened from the bookmarks manager (double click on its row).
    key(ctx, 'alt+shift+2')
    p = open_ntp(ctx, b)
    type_in_omnibox(ctx, 'chrome://bookmarks')
    href, p = m2.wait_active(b, lambda h: h and h.startswith('chrome://bookmarks'))
    if check(p is not None, 'bookmarks manager opened in A'):
        before = ids(b)
        if check(double_click_deep(p, 'bookmarks-item'), 'found a bookmark row'):
            expect_in(ctx, b, 'bookmark opened from the bookmarks manager', 'n=bookmarked', 'A',
                      before=before)

    # A download started in a tab of A carries A's cookie.
    key(ctx, 'alt+shift+2')
    p = open_page_tab(ctx, b, 'dl')
    p.eval(f'location.href = "/download?n=dlA"; 1')
    settle(2)
    reqs = requests_for('n=dlA')
    check(reqs and reqs[-1][1] == 'who=A', f'download request carries A\'s cookie '
          f'({[r[1] for r in reqs]})')

    # Picture in picture (document PiP): same origin as its opener, so A's cookie.
    p = open_page_tab(ctx, b, 'pip')
    got = p.eval('documentPictureInPicture.requestWindow({width: 300, height: 200})'
                 '.then(w => w.document.cookie)', gesture=True)
    check(got == 'who=A', f'document picture-in-picture window sees A\'s cookie ({got})')
    settle(1)

    # DevTools, the task manager and print preview open and close on a tab of A without a
    # crash, and the tab stays in A.
    key(ctx, 'alt+shift+2')
    p = open_page_tab(ctx, b, 'tools')
    for keys, label in (('F12', 'DevTools'), ('shift+Escape', 'task manager'),
                        ('ctrl+p', 'print preview')):
        wins = set(m2.window_ids(ctx))
        key(ctx, keys, ctx.window)
        settle(2.5)
        for w in set(m2.window_ids(ctx)) - wins:
            key(ctx, 'ctrl+w', w)
        key(ctx, 'Escape', ctx.window) if keys == 'ctrl+p' else None
        if keys == 'F12':
            key(ctx, 'F5', ctx.window)
        settle(1)
        check(b.proc.poll() is None, f'{label}: the browser is still running')
        check(space_of(p) == 'A', f'{label}: the tab is still in A')
    if any(t['url'].startswith('devtools://') for t in
           b.session.call('Target.getTargets')['targetInfos']):
        key(ctx, 'F12', ctx.window)

    # Pinned: pin a tab of A, switch to B, restart.
    key(ctx, 'alt+shift+2', ctx.window)
    p = open_page_tab(ctx, b, 'pin')
    cmd(ctx, 'pin')
    settle(1)
    pinned = [t for t in tab_list(b) if 'n=pin&' in (t[4] or '')]
    check(pinned and pinned[0][5], f'the tab is pinned ({pinned})')
    key(ctx, 'alt+shift+3')
    check(space_of(p) == 'A', 'the pinned tab stays in A while B shows')
    # Close a window and a tab before the restart, for the reopen checks after it.
    key(ctx, 'alt+shift+2')
    wins = set(m2.window_ids(ctx))
    key(ctx, 'ctrl+n')
    settle(1.5)
    win2 = [w for w in m2.window_ids(ctx) if w not in wins]
    if check(len(win2) == 1, 'Ctrl+N opened a window'):
        type_in_omnibox(ctx, url(ctx, 'rw2', 'A'), focus=None, window=win2[0])
        expect_in(ctx, b, 'second new window from A', 'n=rw2&', 'A')
        key(ctx, 'ctrl+shift+w', win2[0])
        settle(1.5)
    open_page_tab(ctx, b, 'rc2')
    key(ctx, 'ctrl+w')
    b.close()
    b.start()
    ctx.window = m2.window_ids(ctx)[0]
    settle(2)
    pinned = [t for t in tab_list(b) if 'n=pin&' in (t[4] or '')]
    check(pinned and pinned[0][5], f'the pinned tab is restored pinned ({pinned})')
    if pinned:
        ext(b, f'chrome.tabs.update({pinned[0][6]}, {{active: true}}).then(t => t.id)')
        expect_in(ctx, b, 'pinned tab after a restart', 'n=pin&', 'A')
    from_b_reopen('reopen a tab closed before the restart', 'n=rc2&')
    wins = set(m2.window_ids(ctx))
    from_b_reopen('reopen a window closed before the restart', 'n=rw2&')
    for w in set(m2.window_ids(ctx)) - wins:
        key(ctx, 'ctrl+shift+w', w)
    settle(1)
    b.close()


def sidebar_rows(ctx):
    """The y of each tab row of the visible space that shows a test favicon, top to bottom."""
    from PIL import Image
    img = Image.open(m2.shot(ctx, os.path.join(ctx.tmp, 'rows.png'))).convert('RGB')
    px = img.load()
    rows, last = [], -10
    for y in range(62, img.size[1] - 110):
        hit = any(max(abs(px[x, y][i] - rgb[i]) for i in range(3)) < 12
                  for rgb in m2.COLORS.values() for x in (20,))
        if hit and y - last > 20:
            rows.append(y + 7)
            last = y
    return rows


def row_of(ctx, b, name, space='A'):
    """The sidebar row of test page |name|, among the visible space's pages in tab order."""
    names = [name_of(t[4]) for t in sorted(tab_list(b), key=lambda t: (t[1], t[0]))
             if t[1] == window_tab_id(b) and space_from_url(t[4] or '') == space]
    rows = sidebar_rows(ctx)
    return rows[names.index(name)] if name in names and len(rows) == len(names) else None


def window_tab_id(b):
    """The chrome.windows id of the main window (the first one)."""
    return ext(b, 'chrome.windows.getAll().then(ws => Math.min(...ws.map(w => w.id)))')


# "Hibernate" in the tab context menu, below the point the menu opens at. M4 added "Reopen in
# space" (one 32 DIP item) above it.
HIBERNATE_OFFSET = 422


def hibernate_tab(ctx, b, name):
    """Helium's "Hibernate" from the tab's context menu in the sidebar, then a reload."""
    # The tab is not the active one (an active tab cannot be hibernated).
    key(ctx, 'alt+shift+2')
    ext(b, f'chrome.tabs.query({{}}).then(ts => chrome.tabs.update('
           f'ts.find(t => (t.url || "").includes("n=a1&")).id, {{active: true}})).then(() => 1)')
    settle()
    y = row_of(ctx, b, name)
    if not check(y is not None, f'found the sidebar row of {name}'):
        return
    xdo(ctx, 'mousemove', '--sync', '60', str(y))
    xdo(ctx, 'click', '3')
    settle(0.8)
    xdo(ctx, 'mousemove', '--sync', '140', str(y + HIBERNATE_OFFSET))
    xdo(ctx, 'click', '1')
    settle(1.5)
    # A discard replaces the tab's contents, and with them its chrome.tabs id.
    tid = tab_id(b, f'n={name}&')
    discarded = ext(b, f'chrome.tabs.get({tid}).then(t => t.discarded)')
    check(discarded, f'Hibernate discarded {name} ({discarded})')
    ext(b, f'chrome.tabs.update({tid}, {{active: true}}).then(t => t.id)')
    expect_in(ctx, b, 'hibernated tab reloaded when activated', f'n={name}&', 'A')


def open_blocked_popup(ctx):
    """Clicks the "Pop-up blocked" chip in the omnibox, then the blocked popup's link."""
    xdo(ctx, 'mousemove', '--sync', '1045', '17')
    xdo(ctx, 'click', '1')
    settle(1.2)
    xdo(ctx, 'mousemove', '--sync', '990', '94')
    xdo(ctx, 'click', '1')
    settle(1)


def drag_active_tab_out(ctx):
    """Drags the last tab row of the sidebar (the active tab) into the page area."""
    y = sidebar_rows(ctx)[-1]
    xdo(ctx, 'mousemove', '--sync', '60', str(y))
    xdo(ctx, 'mousedown', '1')
    for x, yy in ((80, y + 8), (150, y + 80), (400, 400), (700, 500), (800, 520)):
        xdo(ctx, 'mousemove', '--sync', str(x), str(yy))
        time.sleep(0.3)
    xdo(ctx, 'mouseup', '1')
    settle(2)


def window_origin(ctx, window):
    out = xdo(ctx, 'getwindowgeometry', window).stdout
    pos = out.split('Position:')[1].split()[0]
    x, y = pos.split(',')
    return int(x), int(y)


def drag_tab_into_main(ctx, x, y):
    """Drags the tab at (x, y) of another window onto the main window's sidebar."""
    xdo(ctx, 'mousemove', '--sync', str(x), str(y))
    xdo(ctx, 'mousedown', '1')
    for xx, yy in ((x + 20, y + 10), (x - 100, y - 50), (500, 300), (300, 200), (100, 150),
                   (60, 120)):
        xdo(ctx, 'mousemove', '--sync', str(xx), str(yy))
        time.sleep(0.4)
    xdo(ctx, 'mouseup', '1')
    settle(2)


STORE_IN = {
    'localStorage': 'localStorage.setItem("neon", "{v}"); 1',
    'IndexedDB': 'new Promise(r => {{ const q = indexedDB.open("neon-{v}"); q.onupgradeneeded = '
                 '() => q.result.createObjectStore("s"); q.onsuccess = () => {{ q.result.close();'
                 ' r(1); }}; }})',
    'Cache Storage': 'caches.open("neon-{v}").then(c => c.put("/neon", new Response("{v}")))'
                     '.then(() => 1)',
    'service worker': 'navigator.serviceWorker.register("/sw.js?{v}").then(() => '
                      'navigator.serviceWorker.ready).then(() => 1)',
}
READ_FROM = {
    'localStorage': 'localStorage.getItem("neon") || ""',
    'IndexedDB': 'indexedDB.databases().then(ds => ds.map(d => d.name).filter(n => '
                 'n.startsWith("neon-")).sort().join(","))',
    'Cache Storage': 'caches.keys().then(ks => ks.filter(k => k.startsWith("neon-")).sort()'
                     '.join(","))',
    'service worker': 'navigator.serviceWorker.getRegistrations().then(rs => rs.map(r => '
                      '(r.active || r.installing || r.waiting).scriptURL.split("?")[1]).sort()'
                      '.join(","))',
}


def phase_leaks(ctx):
    """Nothing stored by a site in space A is readable by the same site in space B or the default
    space: cookies, localStorage, IndexedDB, Cache Storage, service workers, HTTP auth."""
    print('== leaks', flush=True)
    b = start_spaces(ctx, 'leaks')
    page = {sp: m1.find_page(b, f'n={n}&') for sp, n in (('A', 'a1'), ('B', 'b1'), ('D', 'd1'))}
    for sp in ('A', 'D'):
        page[sp].activate()
        settle()
        for kind, js in STORE_IN.items():
            page[sp].eval(js.format(v=sp))
    # Each space sees exactly what it stored itself.
    for sp in ('A', 'B', 'D'):
        page[sp].activate()
        settle()
        page[sp].call('Page.reload')
        page[sp].wait_loaded()
        for kind, js in READ_FROM.items():
            got = page[sp].eval(js)
            want = '' if sp == 'B' else sp
            if want and kind in ('IndexedDB', 'Cache Storage'):
                want = f'neon-{sp}'
            check(got == want, f'{kind} in space {sp}: {want or "nothing"} ({got})')
        check(page[sp].cookie() == f'who={sp}', f'cookie in space {sp} is its own')

    # HTTP auth: credentials entered in A are used in A only.
    page['A'].activate()
    settle()
    p = open_ntp(ctx, b)
    p.call('Page.navigate',
           {'url': f'http://neon:secret@127.0.0.1:{ctx.port}/auth?n=authA&c={HEX["A"]}'})
    p.wait_loaded('n=authA')
    check(requests_for('n=authA')[-1][2] == AUTH, 'HTTP auth: A signed in')
    status = p.eval(f'fetch("{ctx.site}/auth?n=authA2&quiet=1").then(r => r.status)')
    check(status == 200, f'HTTP auth: A sends its credentials again by itself ({status})')
    for sp in ('B', 'D'):
        page[sp].activate()
        settle()
        status = page[sp].eval(
            f'fetch("{ctx.site}/auth?n=auth{sp}&quiet=1").then(r => r.status)')
        sent = [r[2] for r in requests_for(f'n=auth{sp}')]
        check(status == 401 and not any(sent),
              f'HTTP auth: space {sp} does not get A\'s credentials ({status}, {sent})')
    b.close()


# --- Main --------------------------------------------------------------------------------------

PHASES = [('navigate', phase_navigate), ('groups', phase_groups), ('split', phase_split),
          ('extensions', phase_extensions), ('tabs', phase_tabs), ('leaks', phase_leaks)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--helium', default=os.environ.get('NEON_HELIUM', m1.DEFAULT_HELIUM))
    ap.add_argument('--keep', action='store_true', help='keep the profiles for inspection')
    ap.add_argument('--only', action='append', default=[],
                    help='run only these phases: ' + ', '.join(n for n, _ in PHASES))
    ap.add_argument('--browser-arg', action='append', default=[],
                    help='extra browser switch for every launch')
    args = ap.parse_args()
    Browser.extra_args = args.browser_arg
    if not os.access(args.helium, os.X_OK):
        raise SystemExit(f'browser binary not found: {args.helium}')

    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, 'hardening_m1b.log')
    ctx = Ctx()
    ctx.helium, ctx.dirs, ctx.browsers = args.helium, [], []
    ctx.log = open(log_path, 'w')
    ctx.tmp = tempfile.mkdtemp(prefix='neon-m1b-')
    ctx.dirs.append(ctx.tmp)
    ctx.shots = ctx.tmp

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ctx.port = server.server_address[1]
    ctx.site = f'http://127.0.0.1:{ctx.port}'

    ctx.display = m1.free_display()
    xvfb = subprocess.Popen(['Xvfb', f':{ctx.display}', '-screen', '0', '1280x900x24',
                             '-nolisten', 'tcp'], stdout=ctx.log, stderr=subprocess.STDOUT)
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
            ctx.browsers.clear()
    finally:
        for b in ctx.browsers:
            b.kill()
        xvfb.send_signal(signal.SIGTERM)
        xvfb.wait()
        server.shutdown()
        if args.keep:
            print('profiles kept:', *ctx.dirs)
        else:
            for d in ctx.dirs:
                shutil.rmtree(d, ignore_errors=True)
        ctx.log.close()

    crashes, intentional = [], False
    for line in open(log_path, errors='replace'):
        if CRASH_BEGIN in line or CRASH_END in line:
            intentional = CRASH_BEGIN in line
        elif not intentional and ('Check failed' in line or 'FATAL' in line or
                                  'DUMP_WILL_BE_CHECK' in line or 'Received signal' in line):
            crashes.append(line)
    check(not crashes, f'no CHECK failure or crash in the browser log {crashes[:3]}')
    print(f'browser log: {log_path}')
    if failures:
        print(f'{len(failures)} check(s) failed:', *failures, sep='\n  ')
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
