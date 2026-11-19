#!/usr/bin/env python3
"""M2 spaces check: tabs grouped by space, one visible space per window, switching by command,
new tabs in the visible space, and restore. Drives the real Linux dev build with real X key
events and the DevTools protocol.

Run from the neon-linux checkout (one command, from the host):

    neon/dev-container.sh sh 'python3 helium-chromium/neon/tests/spaces_m2.py'

or inside `neon/dev-container.sh sh`:

    python3 helium-chromium/neon/tests/spaces_m2.py [--keep] [--helium PATH] [--browser-arg ARG]
                                                    [--shots DIR] [--only PHASE]

Exit status is 0 when every check passes and 1 otherwise. Like containers_m1.py it starts its own
Xvfb, a local HTTP server on 127.0.0.1 and the browser with throwaway profiles, picks a free
display and free ports, and stops all of them at the end.

How state is observed, without test hooks in the browser:

- A tab's space is read from its cookies: each space's container holds its own cookie
  (who=D for the default space, who=A, who=B). A new tab page is navigated first, which is
  exactly the "next navigation loads in the space's container" check.
- The active tab of a window is the page whose document.visibilityState is "visible".
- What the sidebar shows is read from screenshots: every test page has a solid favicon in its
  space's colour (default green, A red, B blue), and the favicons of each colour are counted in
  the sidebar strip of the screenshot.
- Commands and shortcuts are sent as real key events to the browser window with xdotool.
  "Close other tabs" and "close tabs to the right" have no default shortcut; the profile binds
  them through Helium's custom shortcuts pref, so they too run from a key press.

Phases:

1. spaces: two spaces (A, B) besides the default one, vertical layout. New tabs from other
   apps, Ctrl+T, the new tab button, links and Ctrl+N land in the visible space; switching by
   Ctrl+Alt+Up/Down and Alt+Shift+N; tab navigation, MRU cycling, Ctrl+1..9, close other tabs,
   close tabs to the right and the tab picked after a close stay in the space; closing the last
   tab of a space; Ctrl+Shift+T; incognito and popup windows have no spaces.
2. restart: restore twice (the second time without activating restored tabs first), visible
   space and per-space last active tab, a new tab page left in space A, Back and Forward.
3. last-tab: closing every tab of every space closes the window.
4. stock: only the default space defined: all tabs show and space shortcuts do nothing.
5. classic: two spaces but the classic layout: all tabs show, shortcuts do nothing.

Needs python3-websocket, python3-pil and xdotool (all in the dev image).
"""
import argparse
import http.server
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import zlib
import struct

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import containers_m1 as m1  # noqa: E402  (shared DevTools client and helpers)
from containers_m1 import Browser, Page, check, free_display, free_port  # noqa: E402

from PIL import Image  # noqa: E402
import json  # noqa: E402


def _call_with_deadline(self, method, params=None, session_id=None, deadline=20):
    """Session.call with a deadline: a background page the browser has frozen never answers
    Runtime.evaluate, and other protocol events keep the socket's own timeout from firing."""
    self.next_id += 1
    msg = {'id': self.next_id, 'method': method, 'params': params or {}}
    if session_id:
        msg['sessionId'] = session_id
    self.ws.send(json.dumps(msg))
    end = time.time() + deadline
    while time.time() < end:
        self.ws.settimeout(max(0.1, end - time.time()))
        try:
            reply = json.loads(self.ws.recv())
        except Exception:
            break
        if reply.get('id') == self.next_id:
            if 'error' in reply:
                raise RuntimeError(f"{method}: {reply['error']}")
            return reply.get('result', {})
    raise RuntimeError(f'{method}: no answer within {deadline} s')


m1.Session.call = _call_with_deadline

ROOT = m1.ROOT
LOG_DIR = m1.LOG_DIR

ALPHA, BETA = m1.ALPHA, m1.BETA
SPACE_A = {'id': '1a2b3c4d-0000-4000-8000-00000000000a', 'name': 'A', 'icon': 'a',
           'color': -65536, 'container_id': ALPHA['id']}
SPACE_B = {'id': '1a2b3c4d-0000-4000-8000-00000000000b', 'name': 'B', 'icon': 'b',
           'color': -16776961, 'container_id': BETA['id']}
DEFAULT = {'id': 'default', 'name': 'Default', 'icon': '', 'color': 0, 'container_id': ''}

COLORS = {'D': (0, 160, 0), 'A': (230, 0, 0), 'B': (0, 0, 230)}
HEX = {k: '%02x%02x%02x' % v for k, v in COLORS.items()}
# The generic favicon of a tab that session restore has not loaded yet.
UNLOADED = (128, 128, 128)

# Commands without a default shortcut, bound through Helium's custom shortcuts pref.
IDC_WINDOW_CLOSE_TABS_TO_RIGHT = 35022
IDC_WINDOW_CLOSE_OTHER_TABS = 35023
CUSTOM_ACCELERATORS = {
    str(IDC_WINDOW_CLOSE_OTHER_TABS): {'added': ['Control+Alt+KeyO']},
    str(IDC_WINDOW_CLOSE_TABS_TO_RIGHT): {'added': ['Control+Alt+KeyR']},
}

SIDEBAR_WIDTH = 230  # px of the screenshot searched for favicons (left side, below the toolbar)
failures = m1.failures


# --- Local test server -------------------------------------------------------------------------

PAGE = '''<!doctype html><meta charset="utf-8"><title>{name}</title>
<link rel="icon" href="/fav?c={color}">
<body style="background:#{color}20">
<p>page {name}</p>
<p><a id="ctrl" href="/page?n={name}-ctrl&c={color}">ctrl click</a></p>
<p><a id="next" href="/page?n={name}-next&c={color}">same tab</a></p>
</body>
'''


def png(color_hex, size=16):
    r, g, b = bytes.fromhex(color_hex)
    raw = b''.join(b'\0' + bytes((r, g, b)) * size for _ in range(size))

    def chunk(kind, data):
        c = struct.pack('>I', len(data)) + kind + data
        return c + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        color = q.get('c', ['808080'])[0]
        if url.path == '/fav':
            body, kind = png(color), 'image/png'
        else:
            body = PAGE.format(name=q.get('n', ['x'])[0], color=color).encode()
            kind = 'text/html; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


# --- Helpers -----------------------------------------------------------------------------------

class Ctx:
    pass


def url(ctx, name, space):
    return f'{ctx.site}/page?n={name}&c={HEX[space]}'


def prefs_for(spaces, layout=2, mru=False):
    return {
        'session': {'restore_on_startup': 1},
        'helium': {'browser': {'layout': layout, 'mru_tab_cycling': mru,
                               'custom_accelerators': CUSTOM_ACCELERATORS}},
        'brave': {'containers': {'list': [ALPHA, BETA]}},
        'neon': {'spaces': {'list': spaces}},
    }


def new_profile(ctx, name, prefs):
    profile = tempfile.mkdtemp(prefix=f'neon-m2-{name}-')
    ctx.dirs.append(profile)
    m1.write_prefs(profile, prefs)
    return profile


def start(ctx, profile, *args):
    b = Browser(ctx.helium, profile, ctx.display, free_port(), ctx.log)
    ctx.browsers.append(b)
    b.start(*args)
    return b


def pages(b):
    """Page objects for the page targets, one DevTools session per target."""
    cache = b.__dict__.setdefault('page_cache', {})
    out = []
    for t in b.pages():
        if t['targetId'] not in cache:
            cache[t['targetId']] = Page(b, t['targetId'])
        out.append(cache[t['targetId']])
    return out


def page_info(p):
    try:
        r = p.b.session.call('Runtime.evaluate',
                             {'expression': '[location.href, document.visibilityState]',
                              'returnByValue': True}, session_id=p.sid, deadline=3)
        return r['result']['value']
    except Exception:
        return None, None


def visible_pages(b):
    out = []
    for p in pages(b):
        href, vis = page_info(p)
        if vis == 'visible':
            out.append((href, p))
    return out


def active(b, timeout=10, exclude_incognito=True):
    """The one visible page of the main window (the test keeps one normal window in view)."""
    deadline = time.time() + timeout
    last = []
    while time.time() < deadline:
        vis = visible_pages(b)
        last = vis
        if len(vis) == 1:
            return vis[0]
        time.sleep(0.2)
    raise RuntimeError(f'expected one visible page, got {[h for h, _ in last]}')


def wait_active(b, pred, timeout=10):
    deadline = time.time() + timeout
    href = None
    while time.time() < deadline:
        try:
            href, p = active(b, timeout=2)
            if pred(href):
                return href, p
        except RuntimeError:
            pass
        time.sleep(0.2)
    return href, None


def name_of(href):
    if not href:
        return None
    q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    return q.get('n', [href])[0]


def space_of_page(p):
    """The space of an http page, from the cookie its container holds."""
    c = p.cookie()
    return {'who=D': 'D', 'who=A': 'A', 'who=B': 'B'}.get(c, f'?{c}')


def settle(t=0.6):
    time.sleep(t)


def xdo(ctx, *args):
    return subprocess.run(['xdotool', *args], env=dict(os.environ, DISPLAY=f':{ctx.display}'),
                          capture_output=True, text=True, timeout=20)


def window_ids(ctx):
    r = xdo(ctx, 'search', '--onlyvisible', '--class', 'helium')
    return [w for w in r.stdout.split() if w]


def key(ctx, combo, window=None):
    """A real key press on the browser window (XTEST through the X server)."""
    wid = window or ctx.window
    xdo(ctx, 'windowfocus', '--sync', wid)
    xdo(ctx, 'key', '--clearmodifiers', combo)
    settle()


def shot(ctx, path):
    subprocess.run(['import', '-display', f':{ctx.display}', '-window', 'root', path],
                   check=True, timeout=30)
    return path


def favicon_counts(path, x0=0, x1=SIDEBAR_WIDTH, y0=36, y1=None):
    """Favicons of each space colour in the sidebar region of a screenshot."""
    img = Image.open(path).convert('RGB')
    w, h = img.size
    y1 = y1 or h
    px = img.load()
    counts = {}
    for space, rgb in list(COLORS.items()) + [('U', UNLOADED)]:
        n = 0
        for y in range(y0, y1):
            for x in range(x0, min(x1, w)):
                p = px[x, y]
                if abs(p[0] - rgb[0]) < 12 and abs(p[1] - rgb[1]) < 12 and abs(p[2] - rgb[2]) < 12:
                    n += 1
        counts[space] = round(n / 256)
    if not counts['U']:
        del counts['U']
    return counts


def expect_sidebar(ctx, what, expected, path=None):
    path = path or os.path.join(ctx.tmp, 'sidebar.png')
    settle(0.8)
    shot(ctx, path)
    got = favicon_counts(path)
    check(got == expected, f'{what}: sidebar favicons {got} == {expected}')
    return path


def open_from_other_app(b, *args):
    """Runs the binary again on the profile: the command line goes to the running browser, as
    when another app opens a link."""
    return b.remote(*args)


# --- Phases ------------------------------------------------------------------------------------

def phase_spaces(ctx):
    print('== spaces', flush=True)
    profile = new_profile(ctx, 'spaces', prefs_for([DEFAULT, SPACE_A, SPACE_B]))
    ctx.spaces_profile = profile
    b = start(ctx, profile, '--container=Alpha', url(ctx, 'a1', 'A'))
    b.ctx_site = ctx.site
    p = m1.find_page(b, 'n=a1')
    p.wait_loaded('n=a1')
    m1.set_cookie(p, 'A')
    ctx.window = window_ids(ctx)[0]

    # 4. A link from another app opens in the visible space (A).
    open_from_other_app(b, url(ctx, 'a2', 'A'))
    a2 = m1.find_page(b, 'n=a2')
    a2.wait_loaded('n=a2')
    check(space_of_page(a2) == 'A', 'link from another app opens in the visible space (A)')
    href, _ = active(b)
    check(name_of(href) == 'a2', f'it is the active tab ({name_of(href)})')

    # Beta through --container, then another app link lands in B.
    open_from_other_app(b, '--container=Beta', url(ctx, 'b1', 'B'))
    b1 = m1.find_page(b, 'n=b1')
    b1.wait_loaded('n=b1')
    m1.set_cookie(b1, 'B')
    open_from_other_app(b, url(ctx, 'b2', 'B'))
    b2 = m1.find_page(b, 'n=b2')
    b2.wait_loaded('n=b2')
    check(space_of_page(b2) == 'B', 'link from another app opens in the new visible space (B)')
    open_from_other_app(b, url(ctx, 'b3', 'B'))
    m1.find_page(b, 'n=b3').wait_loaded('n=b3')
    expect_sidebar(ctx, 'space B visible after other-app links', {'D': 0, 'A': 0, 'B': 3},
                   os.path.join(ctx.shots, 'm2-space-b.png'))

    # 5. Alt+Shift+1: the default space has no tab, so a new tab page opens in it.
    key(ctx, 'alt+shift+1')
    href, p = wait_active(b, lambda h: h and h.startswith('chrome://'))
    check(p is not None, f'Alt+Shift+1 opens a new tab page in the empty default space ({href})')
    if p:
        p.call('Page.navigate', {'url': url(ctx, 'd1', 'D')})
        p.wait_loaded('n=d1')
        m1.set_cookie(p, 'D')
        check(space_of_page(p) == 'D', 'that tab loads its first page in the default partition')
    expect_sidebar(ctx, 'default space visible', {'D': 1, 'A': 0, 'B': 0})

    # Switching activates the space's most recently active tab.
    key(ctx, 'alt+shift+2')
    href, _ = active(b)
    check(name_of(href) == 'a2', f'Alt+Shift+2 shows A with its last active tab a2 ({name_of(href)})')
    expect_sidebar(ctx, 'space A visible', {'D': 0, 'A': 2, 'B': 0},
                   os.path.join(ctx.shots, 'm2-space-a.png'))
    key(ctx, 'ctrl+alt+Down')
    href, _ = active(b)
    check(name_of(href) == 'b3', f'Ctrl+Alt+Down shows B with its last active tab b3 ({name_of(href)})')
    expect_sidebar(ctx, 'space B visible after Ctrl+Alt+Down', {'D': 0, 'A': 0, 'B': 3},
                   os.path.join(ctx.shots, 'm2-space-b-switched.png'))
    key(ctx, 'ctrl+alt+Down')
    href, _ = active(b)
    check(name_of(href) == 'b3', 'Ctrl+Alt+Down on the last space stays there')
    key(ctx, 'ctrl+alt+Up')
    href, _ = active(b)
    check(name_of(href) == 'a2', f'Ctrl+Alt+Up goes back to A ({name_of(href)})')
    key(ctx, 'ctrl+alt+Up')
    href, _ = active(b)
    check(name_of(href) == 'd1', f'Ctrl+Alt+Up again shows the default space ({name_of(href)})')

    # 4. Ctrl+T in A.
    key(ctx, 'alt+shift+2')
    key(ctx, 'ctrl+t')
    href, p = wait_active(b, lambda h: h and h.startswith('chrome://'))
    check(p is not None, f'Ctrl+T opens a new tab page and the space stays visible ({href})')
    expect_sidebar(ctx, 'Ctrl+T tab shows in space A', {'D': 0, 'A': 2, 'B': 0})
    if p:
        p.call('Page.navigate', {'url': url(ctx, 'a3', 'A')})
        p.wait_loaded('n=a3')
        check(space_of_page(p) == 'A', 'Ctrl+T tab in A loads its first page in A\'s container')
    expect_sidebar(ctx, 'space A after Ctrl+T', {'D': 0, 'A': 3, 'B': 0})

    # The new tab button (bottom of the vertical tab strip).
    before = {t['targetId'] for t in b.pages()}
    click_new_tab_button(ctx)
    href, p = wait_active(b, lambda h: h and h.startswith('chrome://'))
    check(p is not None, f'new tab button opens a new tab page in the visible space ({href})')
    if p:
        p.call('Page.navigate', {'url': url(ctx, 'a4', 'A')})
        p.wait_loaded('n=a4')
        check(space_of_page(p) == 'A', 'new tab button tab loads its first page in A\'s container')

    # Links opened in a new tab from a page: ctrl+click.
    href, p = active(b)
    p.click('#ctrl', modifiers=2)
    ctrl = m1.find_page(b, '-ctrl', exclude=before)
    ctrl.wait_loaded('-ctrl')
    check(space_of_page(ctrl) == 'A', 'ctrl+click link opens in A')
    href, _ = active(b)
    check(name_of(href) == 'a4', 'the background link tab keeps A visible')
    expect_sidebar(ctx, 'space A with link tab', {'D': 0, 'A': 5, 'B': 0})

    # 6. In-space navigation in A (tabs a1 a2 a3 a4 a4-ctrl in model order).
    order_a = ['a1', 'a2', 'a3', 'a4', 'a4-ctrl']
    key(ctx, 'ctrl+1')
    href, _ = active(b)
    check(name_of(href) == 'a1', f'Ctrl+1 selects the first tab of the space ({name_of(href)})')
    key(ctx, 'ctrl+9')
    href, _ = active(b)
    check(name_of(href) == 'a4-ctrl', f'Ctrl+9 selects the last tab of the space ({name_of(href)})')
    key(ctx, 'ctrl+Next')
    href, _ = active(b)
    check(name_of(href) == 'a1', f'Ctrl+PgDn wraps within the space ({name_of(href)})')
    key(ctx, 'ctrl+Prior')
    href, _ = active(b)
    check(name_of(href) == 'a4-ctrl', f'Ctrl+PgUp wraps within the space ({name_of(href)})')
    seen = set()
    for _ in range(6):
        key(ctx, 'ctrl+Next')
        seen.add(name_of(active(b)[0]))
    check(seen == set(order_a), f'Ctrl+PgDn visits only the space\'s tabs ({sorted(seen)})')
    expect_sidebar(ctx, 'space A after cycling', {'D': 0, 'A': 5, 'B': 0})

    # Tab picked after a close stays in the space: close a1 (first of A, B is not adjacent
    # but the default space's tab is right before it).
    key(ctx, 'ctrl+1')
    key(ctx, 'ctrl+w')
    href, _ = active(b)
    check(name_of(href) in order_a, f'closing a1 activates a tab of A ({name_of(href)})')
    # Close tabs to the right (custom shortcut) from a3: closes a4, a4-ctrl only.
    key(ctx, 'ctrl+2')  # a3
    href, _ = active(b)
    check(name_of(href) == 'a3', f'Ctrl+2 is a3 ({name_of(href)})')
    key(ctx, 'ctrl+alt+r')
    settle(1)
    names = {name_of(t['url']) for t in b.pages()}
    check({'a4', 'a4-ctrl'}.isdisjoint(names) and {'b1', 'b2', 'b3', 'd1'} <= names,
          f'close tabs to the right closes only A tabs ({sorted(n for n in names if n)})')
    # Close other tabs from a3: closes a2 only.
    key(ctx, 'ctrl+alt+o')
    settle(1)
    names = {name_of(t['url']) for t in b.pages()}
    check('a2' not in names and {'a3', 'b1', 'b2', 'b3', 'd1'} <= names,
          f'close other tabs closes only A tabs ({sorted(n for n in names if n)})')
    expect_sidebar(ctx, 'space A with one tab', {'D': 0, 'A': 1, 'B': 0})

    # Reopen closed tab from a non-default space (Ctrl+Shift+T reopens a2, a4 or a4-ctrl).
    key(ctx, 'ctrl+shift+t')
    settle(1)
    href, p = active(b)
    check(name_of(href) in ('a2', 'a4', 'a4-ctrl') and space_of_page(p) == 'A',
          f'Ctrl+Shift+T reopens an A tab in A ({name_of(href)})')
    key(ctx, 'ctrl+w')

    # 7. Closing the last tab of a space leaves a new tab page there.
    key(ctx, 'alt+shift+1')
    href, _ = active(b)
    check(name_of(href) == 'd1', 'default space shows d1')
    key(ctx, 'ctrl+w')
    href, p = wait_active(b, lambda h: h and h.startswith('chrome://'))
    check(p is not None, f'closing the last tab of the default space leaves a new tab page ({href})')
    expect_sidebar(ctx, 'default space with only a new tab page', {'D': 0, 'A': 0, 'B': 0})
    # Closing that untouched new tab page moves to another space.
    key(ctx, 'ctrl+w')
    href, _ = wait_active(b, lambda h: h and h.startswith('http'))
    check(name_of(href) in ('a3', 'b1', 'b2', 'b3'),
          f'closing the untouched new tab page shows another space ({name_of(href)})')
    check(not any(t['url'].startswith('chrome://new') for t in b.pages()),
          'no new tab page is left behind')
    key(ctx, 'alt+shift+1')
    href, p = wait_active(b, lambda h: h and h.startswith('chrome://'))
    check(p is not None, 'the empty default space opens a new tab page again when shown')
    if p:
        p.call('Page.navigate', {'url': url(ctx, 'd2', 'D')})
        p.wait_loaded('n=d2')
        check(space_of_page(p) == 'D', 'it is in the default partition')

    # New window (Ctrl+N) from B shows B.
    key(ctx, 'alt+shift+3')
    href, _ = active(b)
    check(name_of(href) == 'b3', f'Alt+Shift+3 shows B ({name_of(href)})')
    before = {t['targetId'] for t in b.pages()}
    wins_before = set(window_ids(ctx))
    key(ctx, 'ctrl+n')
    settle(1.5)
    new = [t for t in b.pages() if t['targetId'] not in before]
    check(len(new) == 1, f'Ctrl+N opened one new tab ({[t["url"] for t in new]})')
    if new:
        p = Page(b, new[0]['targetId'])
        p.call('Page.navigate', {'url': url(ctx, 'bw', 'B')})
        p.wait_loaded('n=bw')
        check(space_of_page(p) == 'B', 'the new window opens on the source window\'s space (B)')
    for w in set(window_ids(ctx)) - wins_before:
        key(ctx, 'ctrl+shift+w', w)
    settle(1)

    # 9. Incognito: no spaces. Ctrl+T works, space keys do nothing.
    wins_before = set(window_ids(ctx))
    key(ctx, 'ctrl+shift+n')
    settle(1.5)
    inc = [w for w in window_ids(ctx) if w not in wins_before]
    check(len(inc) == 1, f'an incognito window opened ({inc})')
    if inc:
        before = len(b.pages())
        key(ctx, 'ctrl+t', inc[0])
        key(ctx, 'ctrl+alt+Down', inc[0])
        key(ctx, 'alt+shift+2', inc[0])
        after = b.pages()
        check(len(after) == before + 1,
              f'incognito: Ctrl+T adds a tab, space keys add none ({before} -> {len(after)})')
        key(ctx, 'ctrl+shift+w', inc[0])
        settle(1)
    # Popup window: opens normally from B, has no spaces.
    href, p = active(b)
    check(name_of(href) == 'b3', f'B is still visible in the main window ({name_of(href)})')
    p.eval(f'window.open("{url(ctx, "pop", "B")}", "pop", "width=500,height=400"); 1', gesture=True)
    pop = m1.find_page(b, 'n=pop')
    pop.wait_loaded('n=pop')
    check(space_of_page(pop) == 'B', 'popup keeps its opener\'s container')
    pop.eval('window.close()')
    settle(1)

    # Leave state for the restart phase: A shows a3 plus a new tab page; B shows b1 b2 b3;
    # B is visible with b2 active. a3 has history (a3 -> a3-next).
    key(ctx, 'alt+shift+2')
    href, p = active(b)
    p.click('#next')
    p.wait_loaded('a3-next')
    key(ctx, 'ctrl+t')
    # b3 leaves its page for about:blank (renderer-initiated). The session stores the blank
    # entry with B's container; restore must keep it in B.
    key(ctx, 'alt+shift+3')
    href, p = active(b)
    check(name_of(href) == 'b3', f'B shows b3 ({name_of(href)})')
    p.eval('location.href = "about:blank"; 1')
    href, _ = wait_active(b, lambda h: h == 'about:blank')
    check(href == 'about:blank', 'b3 navigated to about:blank and B stays visible')
    key(ctx, 'alt+shift+3')
    key(ctx, 'ctrl+2')
    href, _ = active(b)
    check(name_of(href) == 'b2', f'B shows b2 before restart ({name_of(href)})')
    ctx.spaces_browser = b


def click_new_tab_button(ctx):
    """Clicks Helium's vertical new tab button, the row right below the last tab, found from
    the favicons in a screenshot."""
    img = Image.open(shot(ctx, os.path.join(ctx.tmp, 'ntb.png'))).convert('RGB')
    px = img.load()
    last = 36
    for y in range(36, img.size[1]):
        for x in range(8, 40):
            p = px[x, y]
            if any(all(abs(p[i] - rgb[i]) < 12 for i in range(3)) for rgb in COLORS.values()):
                last = y
    xdo(ctx, 'mousemove', '--sync', '60', str(last + 25))
    xdo(ctx, 'click', '1')
    settle(1)


def phase_restart(ctx):
    print('== restart', flush=True)
    b = ctx.spaces_browser
    b.close()
    # First restart: check nothing, activate nothing (lazy restore keeps tabs unloaded).
    b.start()
    settle(3)
    b.close()
    # Second restart, with Helium's MRU tab cycling turned on (Ctrl+Tab and Ctrl+PgDn).
    prefs = m1.read_prefs(b.profile)
    prefs.setdefault('helium', {}).setdefault('browser', {})['mru_tab_cycling'] = True
    m1.write_prefs(b.profile, prefs)
    b.start()
    b.ctx_site = ctx.site
    settle(2)
    ctx.window = window_ids(ctx)[0]
    href, p = active(b)
    check(name_of(href) == 'b2', f'restored window shows B with b2 active ({name_of(href)})')
    if p:
        check(space_of_page(p) == 'B', 'b2 is still in B')
    # b1 is not loaded yet (lazy restore) and shows the generic favicon; b3 is blank (globe).
    expect_sidebar(ctx, 'restored: space B visible', {'D': 0, 'A': 0, 'B': 1, 'U': 1},
                   os.path.join(ctx.shots, 'm2-restored-b.png'))
    key(ctx, 'alt+shift+2')
    href, p = active(b)
    check(href is not None and href.startswith('chrome://'),
          f'A remembers its last active tab, the new tab page ({href})')
    if p and href and href.startswith('chrome://'):
        p.call('Page.navigate', {'url': url(ctx, 'a-ntp', 'A')})
        p.wait_loaded('n=a-ntp')
        check(space_of_page(p) == 'A',
              'the restored new tab page of A loads its next page in A\'s container')
    key(ctx, 'ctrl+1')
    href, p = active(b)
    p.wait_loaded('a3-next')
    check(name_of(href) == 'a3-next' and space_of_page(p) == 'A', f'a3-next restored in A ({href})')
    key(ctx, 'alt+Left')
    p.wait_loaded('n=a3&')
    check(space_of_page(p) == 'A', 'Back after restart stays in A')
    key(ctx, 'alt+Right')
    p.wait_loaded('a3-next')
    check(space_of_page(p) == 'A', 'Forward after restart stays in A')
    # Helium's MRU cycling stays in the space: hold Ctrl, press Tab several times.
    seen = set()
    xdo(ctx, 'windowfocus', '--sync', ctx.window)
    xdo(ctx, 'keydown', 'ctrl')
    for _ in range(5):
        xdo(ctx, 'key', 'Tab')
        time.sleep(0.4)
        h = active(b)[0]
        seen.add(name_of(h) if h.startswith('http') else 'ntp')
    xdo(ctx, 'keyup', 'ctrl')
    settle()
    check(seen == {'a3-next', 'a-ntp'}, f'Ctrl+Tab MRU cycling stays in A ({sorted(seen)})')
    key(ctx, 'alt+shift+1')
    href, p = active(b)
    check(name_of(href) == 'd2' and space_of_page(p) == 'D', f'default space restored ({href})')
    expect_sidebar(ctx, 'restored: default space', {'D': 1, 'A': 0, 'B': 0})
    # b1 was not activated across two restarts (lazy restore saved it unloaded the second
    # time). It still loads in B's container.
    key(ctx, 'alt+shift+3')
    key(ctx, 'ctrl+1')
    href, p = wait_active(b, lambda h: name_of(h) == 'b1')
    if p:
        p.wait_loaded('n=b1')
    check(p is not None and space_of_page(p) == 'B',
          f'b1, never activated over two restarts, loads in B ({href})')
    key(ctx, 'ctrl+3')
    href, p = wait_active(b, lambda h: h == 'about:blank')
    check(p is not None, f'b3, left on about:blank, is restored in B ({href})')
    if p:
        p.call('Page.navigate', {'url': url(ctx, 'b-blank', 'B')})
        p.wait_loaded('n=b-blank')
        check(space_of_page(p) == 'B', 'the restored blank tab loads its next page in B')
    b.close()


def phase_last_tab(ctx):
    print('== last-tab', flush=True)
    profile = new_profile(ctx, 'last', prefs_for([DEFAULT, SPACE_A, SPACE_B]))
    b = start(ctx, profile, '--container=Alpha', url(ctx, 'a1', 'A'))
    m1.find_page(b, 'n=a1').wait_loaded('n=a1')
    b.remote('--container=Beta', url(ctx, 'b1', 'B'))
    m1.find_page(b, 'n=b1').wait_loaded('n=b1')
    ctx.window = window_ids(ctx)[0]
    key(ctx, 'ctrl+w')   # b1 -> new tab page in B
    href, _ = wait_active(b, lambda h: h and h.startswith('chrome://'))
    check(href is not None and href.startswith('chrome://'), 'last tab of B leaves a new tab page')
    key(ctx, 'ctrl+w')   # untouched new tab page -> A shows a1
    href, _ = wait_active(b, lambda h: name_of(h) == 'a1')
    check(name_of(href) == 'a1', f'closing it shows A ({name_of(href)})')
    # a1 is now the only tab: B and the default space are empty, so closing it closes the
    # window instead of leaving a new tab page.
    key(ctx, 'ctrl+w')
    try:
        b.proc.wait(timeout=20)
        exited = True
    except subprocess.TimeoutExpired:
        exited = False
    check(exited, 'the window closes when every space is empty')


def phase_layout_off(ctx, name, spaces, layout):
    print(f'== {name}', flush=True)
    profile = new_profile(ctx, name, prefs_for(spaces, layout))
    b = start(ctx, profile, '--container=Alpha', url(ctx, 'a1', 'A'))
    b.ctx_site = ctx.site
    m1.find_page(b, 'n=a1').wait_loaded('n=a1')
    b.remote('--container=Beta', url(ctx, 'b1', 'B'))
    m1.find_page(b, 'n=b1').wait_loaded('n=b1')
    b.remote(url(ctx, 'd1', 'D'))
    d1 = m1.find_page(b, 'n=d1')
    d1.wait_loaded('n=d1')
    check(d1.eval('document.cookie') == '', f'{name}: other-app link opens in the default partition')
    ctx.window = window_ids(ctx)[0]
    key(ctx, 'ctrl+t')
    href, p = wait_active(b, lambda h: h and h.startswith('chrome://'))
    if p:
        p.call('Page.navigate', {'url': url(ctx, 'd2', 'D')})
        p.wait_loaded('n=d2')
        m1.set_cookie(p, 'D')
        check(space_of_page(p) == 'D', f'{name}: Ctrl+T opens in the default partition')
    key(ctx, 'alt+shift+2')
    key(ctx, 'ctrl+alt+Up')
    href, _ = active(b)
    check(name_of(href) == 'd2', f'{name}: space shortcuts do nothing ({name_of(href)})')
    seen = set()
    for _ in range(4):
        key(ctx, 'ctrl+Next')
        seen.add(name_of(active(b)[0]))
    check(seen == {'a1', 'b1', 'd1', 'd2'}, f'{name}: Ctrl+PgDn visits every tab ({sorted(seen)})')
    if layout == 2:
        expect_sidebar(ctx, f'{name}: all tabs show', {'D': 2, 'A': 1, 'B': 1},
                       os.path.join(ctx.shots, f'm2-{name}.png'))
    else:
        path = os.path.join(ctx.shots, f'm2-{name}.png')
        shot(ctx, path)
        got = favicon_counts(path, x0=0, x1=1280, y0=0, y1=60)
        check(got == {'D': 2, 'A': 1, 'B': 1}, f'{name}: all tabs show in the tab strip ({got})')
    b.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--helium', default=os.environ.get('NEON_HELIUM', m1.DEFAULT_HELIUM))
    ap.add_argument('--keep', action='store_true', help='keep the profiles for inspection')
    ap.add_argument('--shots', default=os.path.join(ROOT, 'build'),
                    help='where the screenshots go (default: build/)')
    ap.add_argument('--only', action='append', default=[],
                    help='run only these phases: spaces (with restart), last-tab, stock, classic')
    ap.add_argument('--browser-arg', action='append', default=[],
                    help='extra browser switch for every launch, for example '
                         '--browser-arg=--disable-features=Containers (a run that must fail)')
    args = ap.parse_args()
    Browser.extra_args = args.browser_arg
    if not os.access(args.helium, os.X_OK):
        raise SystemExit(f'browser binary not found: {args.helium}')

    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(args.shots, exist_ok=True)
    log_path = os.path.join(LOG_DIR, 'spaces_m2.log')
    ctx = Ctx()
    ctx.helium, ctx.dirs, ctx.browsers = args.helium, [], []
    ctx.log = open(log_path, 'w')
    ctx.shots = args.shots
    ctx.tmp = tempfile.mkdtemp(prefix='neon-m2-shots-')
    ctx.dirs.append(ctx.tmp)

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ctx.site = f'http://127.0.0.1:{server.server_address[1]}'

    ctx.display = free_display()
    xvfb = subprocess.Popen(['Xvfb', f':{ctx.display}', '-screen', '0', '1280x900x24',
                             '-nolisten', 'tcp'], stdout=ctx.log, stderr=subprocess.STDOUT)
    time.sleep(1)
    phases = [('spaces', phase_spaces), ('spaces', phase_restart), ('last-tab', phase_last_tab),
              ('stock', lambda c: phase_layout_off(c, 'stock', [DEFAULT], 2)),
              ('classic', lambda c: phase_layout_off(c, 'classic', [DEFAULT, SPACE_A, SPACE_B], 0))]
    try:
        for name, phase in phases:
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
