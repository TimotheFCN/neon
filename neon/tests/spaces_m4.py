#!/usr/bin/env python3
"""M4 spaces check: managing spaces. Creating, editing, deleting and reordering spaces from the
sidebar's switcher row, the Spaces settings page, and "Reopen in space" in the tab context menu.
Drives the real Linux dev build with real X input (clicks, drags, keys and typing through XTEST)
and the DevTools protocol.

Run from the neon-linux checkout (one command, from the host):

    neon/dev-container.sh sh 'python3 helium-chromium/neon/tests/spaces_m4.py'

or inside `neon/dev-container.sh sh`:

    python3 helium-chromium/neon/tests/spaces_m4.py [--keep] [--helium PATH] [--shots DIR]
                                                    [--only PHASE] [--browser-arg ARG]

Exit status is 0 when every check passes and 1 otherwise. Like the earlier checks it starts its
own Xvfb, a local HTTP server on 127.0.0.1 and the browser with throwaway profiles, picks a free
display and free ports, and stops all of them at the end.

How state is observed:

- Where to click is read from the browser's UI DevTools (--enable-ui-devtools): the views tree
  with class names, screen bounds and view properties. Neon's views carry an object name with
  their space id (SpaceButton/<id>, SpaceEditView/<id>, SpaceIconChoice/vector:home,
  SpaceColorChoice/<argb>, SpaceAddButton). A space's name in the sidebar is its icon's tooltip
  (TooltipText), a menu item's state its Enabled property. The clicks, drags, keys and typing are
  real X input through XTEST. An emoji, which the X keymap lacks, is typed by mapping a spare key
  to it first (type_unicode).
- The space list as the settings page shows it is read from its DOM, and the saved list from the
  Preferences file (the browser writes it about ten seconds after a change).
- A tab's space is read from its cookies (who=D in the first space, one per container),
  as in the M2 check. Storage on disk is read from the container's partition directory.
- The page a window shows is the page whose document.visibilityState is "visible", mapped to its
  window with Browser.getWindowForTarget. Pinned state comes from chrome.tabs in the bundled
  uBlock Origin's background page, as in the M1b check.
- Colours are checked on screenshots: the icon of a space in its colour (full strength where the
  space is shown, faded elsewhere).

The screen is 2560x1000 with two browser windows side by side; the second one must follow every
change made in the first and in the settings page it hosts.

The one phase, main, in order:

1. "+" in the row creates a space, opens the edit bubble on it and shows the space with a new
   tab page; typing names it; an icon and a colour are picked. A typed URL in that new tab page
   loads in the new container.
2. "New space" in the row's context menu does the same; an emoji is typed as its icon.
3. Every space's menu has Edit, Delete and New space, the first space's too (M5: there is no
   default space); Edit on another space renames it.
4. Dragging an icon reorders both rows, the first space's icon too; Ctrl+Alt+Up/Down follow the
   new order.
5. Settings > Appearance > Spaces lists the spaces; renaming, recolouring, moving, picking an
   icon, adding and deleting there update both sidebars, and renames and additions in the
   sidebar show in the page. The two toggles hide the row and turn swiping off.
6. Restart: order, names, icons and colours come back.
7. "Reopen in space" reopens two selected tabs (one pinned) in another space and closes them.
8. Delete from the second window's row while the first window shows the space: the confirmation
   counts its tabs in all windows, they close, the first window moves to the space before it,
   and the container's cookies, local storage and IndexedDB are gone from disk at once.
9. The other spaces are deleted down to the first one (the row hides with a single space); a
   saved tab group's container entry goes with its space.
10. Restart: the deleted containers' directories and their entries in the containers prefs are
   gone, the last space cannot be deleted in settings, and a new space starts without cookies.

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
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import containers_m1 as m1  # noqa: E402
import spaces_m2 as m2  # noqa: E402  (also gives Session.call a deadline)
from containers_m1 import Browser, Page, check, free_display, free_port  # noqa: E402

import websocket  # noqa: E402
from PIL import Image  # noqa: E402

ROOT = m1.ROOT
LOG_DIR = m1.LOG_DIR
failures = m1.failures

DEFAULT, SPACE_A, SPACE_B = m2.DEFAULT, m2.SPACE_A, m2.SPACE_B
D = DEFAULT['id']   # the first space, "Default" (a container space since M5)
COLORS = m2.COLORS
HEX = m2.HEX
SCREEN = (2560, 1000)   # two browser windows side by side

# Palette of space_style.cc, as stored (signed ARGB ints) and as object names.
BLUE, RED, YELLOW, GREEN = 0xff1a73e8, 0xffd93025, 0xfff9ab00, 0xff1e8e3e
PINK, PURPLE = 0xffd01884, 0xff9334e6


def signed(argb):
    return argb - (1 << 32) if argb >= 1 << 31 else argb


def unsigned(v):
    return v & 0xffffffff


def color_key(argb):
    """Object name suffix of a colour choice: the SkColor's bytes in memory order (little
    endian, so BGRA) as hex, as base::HexEncode writes them."""
    return argb.to_bytes(4, 'little').hex().upper()


# --- UI DevTools client ----------------------------------------------------------------------------

class UiDevTools:
    """The browser's views tree over --enable-ui-devtools."""

    def __init__(self, port):
        self.port = port
        self.ws = None
        self.next_id = 0

    def connect(self):
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                # The server has no /json list; the first (only) target is /0.
                self.ws = websocket.create_connection(f'ws://127.0.0.1:{self.port}/0',
                                                      timeout=30, suppress_origin=True)
                self.call('DOM.enable')
                return
            except Exception:
                self.ws = None
                time.sleep(0.5)
        raise RuntimeError('UI DevTools did not come up')

    def call(self, method, params=None):
        if not self.ws:
            self.connect()
        self.next_id += 1
        self.ws.send(json.dumps({'id': self.next_id, 'method': method, 'params': params or {}}))
        end = time.time() + 20
        while time.time() < end:
            reply = json.loads(self.ws.recv())
            if reply.get('id') == self.next_id:
                if 'error' in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get('result', {})
        raise RuntimeError(f'{method}: no answer')

    def nodes(self):
        """Every node as (node, depth-first list), with 'cls' and 'name' attributes."""
        root = self.call('DOM.getDocument')['root']
        out = []

        def walk(n, parents):
            attrs = n.get('attributes') or []
            d = dict(zip(attrs[::2], attrs[1::2]))
            n['cls'], n['name'] = d.get('class', n.get('nodeName')), d.get('name', '')
            n['parents'] = parents
            out.append(n)
            for c in n.get('children') or []:
                walk(c, parents + [n])
        walk(root, [])
        return out

    def bounds(self, node):
        r = self.call('DOM.getNodeBoundsInScreen', {'nodeId': node['nodeId']})['boundsInScreen']
        return r['x'], r['y'], r['width'], r['height']

    def props(self, node):
        """The node's view properties (views metadata), by name."""
        st = self.call('CSS.getMatchedStylesForNode', {'nodeId': node['nodeId']})
        out = {}
        for rule in st.get('matchedCSSRules', []):
            for p in rule.get('rule', {}).get('style', {}).get('cssProperties', []):
                out.setdefault(p['name'], p['value'])
        return out

    def find(self, pred, timeout=10, visible=True):
        """Nodes matching pred(node), with their screen bounds, in tree order."""
        deadline = time.time() + timeout
        while True:
            found = []
            for n in self.nodes():
                if pred(n):
                    b = self.bounds(n)
                    if not visible or (b[2] > 0 and b[3] > 0 and n.get('visible', True)):
                        found.append((n, b))
            if found or time.time() > deadline:
                return found
            time.sleep(0.3)

    def find_name(self, name, timeout=10, prefix=False):
        return self.find(lambda n: n['name'] == name or (prefix and n['name'].startswith(name)),
                         timeout)

    def close(self):
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self.ws = None


# --- Browser and input helpers -----------------------------------------------------------------

class Ctx:
    pass


def url(ctx, name, space):
    return f'{ctx.site}/page?n={name}&c={HEX.get(space, "808080")}'


def prefs_for(spaces, **extra):
    prefs = m2.prefs_for(spaces)
    prefs['neon']['spaces'].update(extra)
    return prefs


def start(ctx, name, prefs, *args, profile=None):
    if profile is None:
        profile = tempfile.mkdtemp(prefix=f'neon-m4-{name}-')
        ctx.dirs.append(profile)
        m1.write_prefs(profile, prefs)
    b = Browser(ctx.helium, profile, ctx.display, free_port(), ctx.log)
    b.ui_port = free_port()
    ctx.browsers.append(b)
    b.start(f'--enable-ui-devtools={b.ui_port}', *args)
    b.ui = UiDevTools(b.ui_port)
    b.profile_dir = profile
    return b


def stop(ctx, b):
    if b.__dict__.get('ui'):
        b.ui.close()
    b.close()


def xdo(ctx, *args):
    return subprocess.run(['xdotool', *args], env=dict(os.environ, DISPLAY=f':{ctx.display}'),
                          capture_output=True, text=True, timeout=30)


def settle(t=0.6):
    time.sleep(t)


def center(bounds):
    x, y, w, h = bounds
    return x + w // 2, y + h // 2


def move(ctx, pos):
    """Moves the pointer. (`mousemove --sync` never returns when the pointer is already there.)"""
    xdo(ctx, 'mousemove', str(pos[0]), str(pos[1]))
    time.sleep(0.15)


def click(ctx, pos, button=1, settle_time=0.6, modifier=None):
    move(ctx, pos)
    if modifier:
        xdo(ctx, 'keydown', modifier)
    xdo(ctx, 'click', str(button))
    if modifier:
        xdo(ctx, 'keyup', modifier)
    settle(settle_time)


def drag(ctx, start_pos, end_pos, steps=12):
    move(ctx, start_pos)
    xdo(ctx, 'mousedown', '1')
    time.sleep(0.15)
    for i in range(1, steps + 1):
        x = start_pos[0] + (end_pos[0] - start_pos[0]) * i // steps
        y = start_pos[1] + (end_pos[1] - start_pos[1]) * i // steps
        xdo(ctx, 'mousemove', str(x), str(y))
        time.sleep(0.04)
    time.sleep(0.2)
    xdo(ctx, 'mouseup', '1')
    settle()


def key(ctx, combo):
    xdo(ctx, 'key', '--clearmodifiers', combo)
    settle(0.4)


def type_text(ctx, text):
    xdo(ctx, 'type', '--delay', '40', text)
    settle(0.4)


def type_unicode(ctx, char, keycode=254):
    """Types one character the keymap lacks (an emoji). xdotool remaps a spare key only for the
    instant of the press, before the browser has read the new keymap, so the key arrives
    without its character. This maps keycode |keycode| to the character's keysym, waits for the
    browser to reload the keymap, presses it through XTEST, then restores the key."""
    import ctypes
    import ctypes.util
    x11 = ctypes.CDLL(ctypes.util.find_library('X11'))
    xtst = ctypes.CDLL(ctypes.util.find_library('Xtst'))
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XChangeKeyboardMapping.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                           ctypes.POINTER(ctypes.c_ulong), ctypes.c_int]
    x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int,
                                       ctypes.c_ulong]
    dpy = x11.XOpenDisplay(f':{ctx.display}'.encode())
    if not dpy:
        raise RuntimeError('cannot open the X display')
    try:
        sym = (ctypes.c_ulong * 2)(0x1000000 | ord(char), 0x1000000 | ord(char))
        x11.XChangeKeyboardMapping(dpy, keycode, 2, sym, 1)
        x11.XSync(dpy, 0)
        time.sleep(0.8)
        xtst.XTestFakeKeyEvent(dpy, keycode, 1, 0)
        xtst.XTestFakeKeyEvent(dpy, keycode, 0, 0)
        x11.XSync(dpy, 0)
        time.sleep(0.8)
        none = (ctypes.c_ulong * 2)(0, 0)
        x11.XChangeKeyboardMapping(dpy, keycode, 2, none, 1)
        x11.XSync(dpy, 0)
    finally:
        x11.XCloseDisplay(dpy)
    settle(0.3)


def shot(ctx, path):
    subprocess.run(['import', '-display', f':{ctx.display}', '-window', 'root', path],
                   check=True, timeout=30)
    return path


def save_shot(ctx, name, crop=None):
    path = os.path.join(ctx.shots, name)
    shot(ctx, path)
    if crop:
        Image.open(path).crop(crop).save(path)
    print(f'screenshot: {path}', flush=True)
    return path


def window_ids(ctx):
    r = xdo(ctx, 'search', '--onlyvisible', '--class', 'helium')
    return [w for w in r.stdout.split() if w]


def focus_window(ctx, wid):
    xdo(ctx, 'windowactivate', '--sync', wid)
    xdo(ctx, 'windowfocus', '--sync', wid)
    settle(0.3)


# --- Spaces state ------------------------------------------------------------------------------

LEFT = 1280   # the second window sits right of this x


def wait_for(pred, timeout=10, step=0.3):
    """Polls pred() until it returns a true value, which it returns (or the last value)."""
    deadline = time.time() + timeout
    value = None
    while time.time() < deadline:
        try:
            value = pred()
            if value:
                return value
        except Exception:
            pass
        time.sleep(step)
    return value


def rows(b, timeout=5):
    """The visible switcher rows, left window first: [[(space_id, bounds)]] in icon order."""
    found = b.ui.find(lambda n: n['name'].startswith('SpaceButton/') and
                      n['name'] != 'SpaceButton/compact', timeout)
    by_row = {}
    for n, bounds in found:
        if not bounds[2] or b.ui.props(n).get('IsDrawn') != 'true':
            continue
        by_row.setdefault(n['parents'][-1]['nodeId'], []).append(
            (n['name'].split('/', 1)[1], bounds, n))
    out = [sorted(r, key=lambda e: e[1][0]) for r in by_row.values()]
    return sorted(out, key=lambda r: r[0][1][0])


def orders(b):
    return [[sid for sid, _, _ in r] for r in rows(b)]


def wait_orders(b, want, timeout=8):
    """Waits until every row shows |want| (a list of space ids); returns the rows' orders."""
    got = wait_for(lambda: (lambda o: o if o and all(r == want for r in o) else None)(
        orders(b)), timeout)
    return got or orders(b)


def button(b, space_id, window=0):
    for r in rows(b):
        if (r[0][1][0] >= LEFT) == (window == 1):
            for sid, bounds, n in r:
                if sid == space_id:
                    return bounds, n
    return None, None


def names(b, space_id):
    """The space's name in each row (the icon's tooltip)."""
    out = []
    for r in rows(b):
        for sid, _, n in r:
            if sid == space_id:
                out.append(b.ui.props(n).get('TooltipText'))
    return out


def wait_names(b, space_id, name, count=2, timeout=8):
    got = wait_for(lambda: (lambda v: v if v == [name] * count else None)(names(b, space_id)),
                   timeout)
    return got or names(b, space_id)


def add_button(b, window=0):
    for n, bounds in b.ui.find_name('SpaceAddButton', timeout=5):
        if (bounds[0] >= LEFT) == (window == 1):
            return bounds
    return None


def switcher_row(b, window=0):
    for n, bounds in b.ui.find(lambda n: n['cls'] == 'SpaceSwitcherRow', 5):
        if bounds[2] and (bounds[0] >= LEFT) == (window == 1):
            return bounds
    return None


def edit_view(b, timeout=5):
    """(space_id, bounds) of the open edit bubble, or (None, None)."""
    found = b.ui.find_name('SpaceEditView/', timeout=timeout, prefix=True)
    if not found:
        return None, None
    n, bounds = found[0]
    return n['name'].split('/', 1)[1], bounds


def choice(b, name):
    found = b.ui.find_name(name, timeout=5)
    return found[0][1] if found else None


def dialog_button(b, text, timeout=5):
    """A dialog button (MdTextButton) by its label."""
    for n, bounds in b.ui.find(lambda n: n['cls'] == 'MdTextButton', timeout):
        if b.ui.props(n).get('Text') == text:
            return bounds
    return None


def labels(b, pred, timeout=5):
    out = []
    for n, bounds in b.ui.find(lambda n: n['cls'] == 'Label', timeout):
        text = b.ui.props(n).get('Text', '')
        if pred(text):
            out.append(text)
    return out


def menu_items(b, timeout=5):
    """The items of the open menus, as (bounds, enabled), each menu top to bottom, the most
    recently opened menu last."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        menus = {}
        for n, bounds in b.ui.find(lambda n: n['cls'] == 'MenuItemView', 1):
            props = b.ui.props(n)
            if props.get('IsDrawn') != 'true':
                continue   # a submenu that closed
            host = next((p['nodeId'] for p in reversed(n['parents'])
                         if p['cls'] == 'MenuHostRootView'), None)
            menus.setdefault(host, []).append((bounds, props.get('Enabled') == 'true'))
        if menus:
            return [sorted(v, key=lambda e: e[0][1]) for v in menus.values()]
        time.sleep(0.3)
    return []


def window_of(b, target_id):
    w = b.session.call('Browser.getWindowForTarget', {'targetId': target_id})
    return 1 if w['bounds']['left'] >= LEFT else 0


def visible(b, window=0, timeout=10):
    """(href, Page) of the page shown in window |window|."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for href, p in m2.visible_pages(b):
            try:
                if window_of(b, p.target_id) == window:
                    return href, p
            except RuntimeError:
                pass
        time.sleep(0.3)
    return None, None


def wait_visible(b, pred, window=0, timeout=10):
    deadline = time.time() + timeout
    href = None
    while time.time() < deadline:
        href, p = visible(b, window, timeout=2)
        if href and pred(href):
            return href, p
        time.sleep(0.3)
    return href, None


def who(p):
    try:
        return p.cookie()
    except Exception as e:
        return f'error {e}'


def name_of(href):
    return m2.name_of(href) if href else None


def is_ntp(href):
    return bool(href) and href.startswith('chrome://new')


def read_prefs(profile):
    try:
        return m1.read_prefs(profile)
    except (OSError, ValueError):
        return {}


def pref_spaces(profile):
    return read_prefs(profile).get('neon', {}).get('spaces', {}).get('list', [])


def pref_space(profile, space_id):
    return next((s for s in pref_spaces(profile) if s.get('id') == space_id), None)


def wait_prefs(profile, pred, timeout=25):
    """Polls the Preferences file (the browser writes it about ten seconds after a change)."""
    return wait_for(lambda: pred(read_prefs(profile)), timeout, step=1)


def cookie_rows(profile, container_id):
    path = os.path.join(m1.partition_dir(profile, container_id), 'Cookies')
    if not os.path.exists(path):
        return []
    copy = os.path.join(tempfile.mkdtemp(prefix='neon-m4-cookies-'), 'Cookies')
    shutil.copy(path, copy)
    for extra in ('-journal', '-wal'):
        if os.path.exists(path + extra):
            shutil.copy(path + extra, copy + extra)
    con = sqlite3.connect(copy)
    try:
        return con.execute('select host_key, name, value from cookies').fetchall()
    finally:
        con.close()
        shutil.rmtree(os.path.dirname(copy), ignore_errors=True)


def dir_contains(path, needle):
    """Whether any file under |path| contains the bytes |needle|."""
    for root, _, files in os.walk(path):
        for f in files:
            try:
                with open(os.path.join(root, f), 'rb') as fh:
                    if needle in fh.read():
                        return True
            except OSError:
                pass
    return False


def pixels_near(img, bounds, rgb, tol=40):
    x, y, w, h = bounds
    crop = img.crop((x, y, x + w, y + h)).convert('RGB')
    return sum(1 for p in crop.getdata()
               if all(abs(p[i] - rgb[i]) <= tol for i in range(3)))


def rgb_of(argb):
    return (argb >> 16) & 0xff, (argb >> 8) & 0xff, argb & 0xff


def grab(ctx):
    path = os.path.join(ctx.tmp, 'grab.png')
    shot(ctx, path)
    return Image.open(path).convert('RGB')


def check_tint(ctx, b, space_id, argb, what):
    """The space's icon in every row is drawn in |argb|: at full strength where the space is
    shown, faded (alpha 0x8c over the row's background) where it is not."""
    img = grab(ctx)
    rgb = rgb_of(argb)
    counts = []
    for r in rows(b):
        for sid, bounds, _ in r:
            if sid != space_id:
                continue
            bg = img.getpixel((bounds[0] - 3, bounds[1] + bounds[3] // 2))
            a = 0x8c / 255
            faded = tuple(round(rgb[i] * a + bg[i] * (1 - a)) for i in range(3))
            counts.append(max(pixels_near(img, bounds, rgb, 30),
                              pixels_near(img, bounds, faded, 12)))
    check(len(counts) == 2 and all(c >= 80 for c in counts),
          f'{what}: the icon is tinted {argb:08x} in both rows ({counts} px)')


def type_url(ctx, address):
    key(ctx, 'ctrl+l')
    type_text(ctx, address)
    key(ctx, 'Return')


def browser_windows(ctx):
    """[(x, window id)] of the browser windows (not bubbles or menus), left first."""
    out = []
    for wid in window_ids(ctx):
        r = xdo(ctx, 'getwindowgeometry', wid)
        try:
            x = int(r.stdout.split('Position: ')[1].split(',')[0])
            w = int(r.stdout.split('Geometry: ')[1].split('x')[0])
        except (IndexError, ValueError):
            continue
        if w >= 800:
            out.append((x, wid))
    return sorted(out)


def window_ids_by_x(ctx):
    return [wid for _, wid in browser_windows(ctx)]


def raise_window(ctx, window):
    wid = window_ids_by_x(ctx)[window]
    xdo(ctx, 'windowraise', wid)
    xdo(ctx, 'windowfocus', '--sync', wid)
    settle(0.4)


def crop_window(window):
    return (LEFT * window, 0, LEFT * (window + 1), SCREEN[1])


def ext_bg(b):
    for t in b.session.call('Target.getTargets')['targetInfos']:
        if t['type'] == 'background_page' and t['url'].startswith('chrome-extension://'):
            return Page(b, t['targetId'])
    raise RuntimeError('no extension background page')


def tab_list(b):
    """[(index, windowId, groupId, url, pinned, id)] for every tab, from chrome.tabs."""
    return ext_bg(b).eval('''chrome.tabs.query({}).then(ts => ts.map(t =>
        [t.index, t.windowId, t.groupId, t.url || t.pendingUrl, t.pinned, t.id]))''')


def sidebar_tabs(b):
    """Bounds of the tabs drawn in the first window's sidebar, top to bottom."""
    out = []
    for n, bounds in b.ui.find(lambda n: n['cls'] == 'TabView', 5):
        if bounds[2] and 0 <= bounds[0] < LEFT and bounds[1] > 30 and \
                b.ui.props(n).get('IsDrawn') == 'true':
            if not any(abs(center(o)[1] - center(bounds)[1]) < 12 for o in out):
                out.append(bounds)
    return sorted(out, key=lambda e: (e[1], e[0]))


def tab_named(ctx, b, name):
    """Clicks the first window's tabs until the one showing page |name| is active; returns its
    bounds."""
    for bounds in sidebar_tabs(b):
        click(ctx, center(bounds), settle_time=0.8)
        href, _ = visible(b, 0, timeout=4)
        if name_of(href) == name:
            return bounds
    return None


# --- The scenario ------------------------------------------------------------------------------

# Space colours as stored (unsigned ARGB) and the hex of their test pages' favicons.
PAGE_HEX = {'W': '1e8e3e', 'P': '9334e6', 'N': '00897b'}


def page_url(ctx, name, hexcolor):
    return f'{ctx.site}/page?n={name}&c={hexcolor}'


def settings_target(b):
    for t in b.pages():
        if t['url'].startswith('chrome://settings'):
            return Page(b, t['targetId'])
    return None


DEEP_JS = r'''
window.neonDeep = (sel) => {
  const walk = (root) => {
    const hit = root.querySelector(sel);
    if (hit) return hit;
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) {
        const f = walk(el.shadowRoot);
        if (f) return f;
      }
    }
    return null;
  };
  return walk(document);
};
window.neonSpacesPage = () => neonDeep('settings-neon-spaces-page');
window.neonIn = (sel) => {
  const page = neonSpacesPage();
  return page && page.shadowRoot.querySelector(sel);
};
window.neonRect = (sel) => {
  let el;
  if (sel.endsWith(' >>> input')) {
    el = neonIn(sel.slice(0, -' >>> input'.length));
    el = el && el.inputElement;
  } else {
    el = neonIn(sel);
  }
  if (!el) return null;
  el.scrollIntoViewIfNeeded();
  const r = el.getBoundingClientRect();
  return [r.x + r.width / 2, r.y + r.height / 2, r.width, r.height];
};
'''


def settings_state(p):
    p.eval(DEEP_JS)
    return p.eval('''(() => {
      const page = neonSpacesPage();
      if (!page) return null;
      return Array.from(page.shadowRoot.querySelectorAll('.space-row')).map(r =>
          [r.dataset.id, r.querySelector('.space-name').value, r.dataset.icon,
           Number(r.dataset.color), Number(r.dataset.tabs)]);
    })()''')


def place_second_window(b):
    """After a restart both windows come back at the origin (no window manager under Xvfb):
    puts the window of the settings tab back on the right."""
    sp = settings_target(b)
    if not sp:
        return
    w = b.session.call('Browser.getWindowForTarget', {'targetId': sp.target_id})
    b.session.call('Browser.setWindowBounds', {'windowId': w['windowId'], 'bounds': {
        'left': LEFT, 'top': 0, 'width': LEFT, 'height': SCREEN[1]}})
    settle(1.5)


def settings_point(ctx, b, p, sel):
    """Screen point of the element |sel| of the Spaces page (a selector in its shadow root)."""
    p.eval(DEEP_JS)
    rect = p.eval(f'neonRect({json.dumps(sel)})')
    if not rect:
        raise RuntimeError(f'no {sel} on the Spaces page')
    settle(0.3)
    rect = p.eval(f'neonRect({json.dumps(sel)})')
    web = [bounds for n, bounds in b.ui.find(lambda n: n['cls'] == 'ContentsWebView', 5)
           if bounds[2] and bounds[0] >= LEFT]
    x0, y0 = web[0][0], web[0][1]
    return int(x0 + rect[0]), int(y0 + rect[1])


def row_sel(space_id, inner):
    return f'.space-row[data-id="{space_id}"] {inner}'


def phase_main(ctx):
    print('== main: create, edit, reorder, settings, restart, reopen, delete', flush=True)
    prefs = prefs_for([DEFAULT, SPACE_A])
    prefs['helium']['browser']['custom_accelerators'] = dict(
        m2.CUSTOM_ACCELERATORS, **{str(35013): {'added': ['Control+Alt+KeyP']}})
    b = start(ctx, 'main', prefs, url(ctx, 'd1', 'D'))
    profile = b.profile_dir
    p = m1.find_page(b, 'n=d1')
    p.wait_loaded('n=d1')
    m1.set_cookie(p, 'D')
    for n, hexc in (('a1', 'e60000'), ('a2', 'ff8800')):
        b.remote('--container=Alpha', page_url(ctx, n, hexc))
        q = m1.find_page(b, f'n={n}')
        q.wait_loaded(f'n={n}')
        m1.set_cookie(q, 'A')
    A = SPACE_A['id']
    settle(1)

    # A second window, right of the first, opened from the default space: it must follow every
    # change.
    first = window_ids_by_x(ctx)[0]
    raise_window(ctx, 0)
    click(ctx, center(button(b, D)[0]), settle_time=1)
    key(ctx, 'ctrl+n')
    wait_for(lambda: len(browser_windows(ctx)) == 2, 15)
    second = [w for w in window_ids_by_x(ctx) if w != first]
    if second:
        xdo(ctx, 'windowmove', second[0], str(LEFT), '0')
        xdo(ctx, 'windowsize', second[0], str(LEFT), str(SCREEN[1]))
    settle(1.5)
    check(len(browser_windows(ctx)) == 2 and len(rows(b)) == 2,
          f'two windows with a switcher row each ({len(rows(b))} rows)')
    check(orders(b) == [[D, A]] * 2, f'both rows show D, A {orders(b)}')
    raise_window(ctx, 0)

    # --- 1. Create with "+" -----------------------------------------------------------------------
    save_shot(ctx, 'row-with-add-button.png', crop=(0, SCREEN[1] - 200, 260, SCREEN[1]))
    before = orders(b)[0]
    click(ctx, center(add_button(b)), settle_time=1.5)
    W, bubble = edit_view(b)
    after = wait_orders(b, before + [W])
    check(W and after == [before + [W]] * 2,
          f'"+" adds a space at the end of both rows {after}')
    check(bubble is not None, '"+" opens the edit bubble on the new space')
    href, ntp = wait_visible(b, is_ntp)
    check(is_ntp(href), f'"+" shows the new space with a new tab page ({href})')
    save_shot(ctx, 'edit-bubble.png', crop=(0, 300, 640, SCREEN[1]))
    type_text(ctx, 'Work')
    check(wait_names(b, W, 'Work') == ['Work', 'Work'],
          f'typing in the bubble renames the space in both rows {names(b, W)}')
    click(ctx, center(choice(b, 'SpaceIconChoice/vector:work')))
    click(ctx, center(choice(b, f'SpaceColorChoice/{color_key(GREEN)}')))
    settle(0.8)
    check_tint(ctx, b, W, GREEN, 'colour picked in the bubble')
    click(ctx, center(dialog_button(b, 'Done')))
    check(edit_view(b, timeout=1)[0] is None, 'Done closes the bubble')
    # The new tab page of the new space loads typed URLs in the space's container.
    raise_window(ctx, 0)
    type_url(ctx, page_url(ctx, 'w1', PAGE_HEX['W']))
    w1 = m1.find_page(b, 'n=w1')
    w1.wait_loaded('n=w1')
    check(who(w1) == '', f'a page typed into the new space has none of the other spaces\' '
          f'cookies ({who(w1)!r})')
    m1.set_cookie(w1, 'W')

    # --- 2. Create from the row's context menu ------------------------------------------------------
    row = switcher_row(b)
    click(ctx, (row[0] + 10, row[1] + row[3] // 2), button=3, settle_time=1)
    items = menu_items(b)
    check(len(items) == 1 and len(items[-1]) == 1,
          f'the context menu of the row itself has only "New space" ({items})')
    if items:
        click(ctx, center(items[-1][0][0]), settle_time=1.5)
    P, _ = edit_view(b)
    check(P and P not in (W, A) and wait_orders(b, before + [W, P]) == [before + [W, P]] * 2,
          f'"New space" adds a space in both rows {orders(b)}')
    href, _ = wait_visible(b, is_ntp)
    check(is_ntp(href), f'"New space" shows it with a new tab page ({href})')
    type_text(ctx, 'Play')
    emoji = [bounds for n, bounds in b.ui.find(
        lambda n: n['cls'] == 'Textfield' and any(
            q['name'].startswith('SpaceEditView/') for q in n['parents']), 5)]
    if len(emoji) == 2:
        click(ctx, center(emoji[1]))
        type_unicode(ctx, '\U0001F3AE')
    click(ctx, center(choice(b, f'SpaceColorChoice/{color_key(PURPLE)}')))
    check(wait_names(b, P, 'Play') == ['Play', 'Play'], f'second space named Play {names(b, P)}')
    check_tint(ctx, b, P, PURPLE, 'colour of the second space')
    click(ctx, center(dialog_button(b, 'Done')))
    raise_window(ctx, 0)
    type_url(ctx, page_url(ctx, 'p1', PAGE_HEX['P']))
    p1 = m1.find_page(b, 'n=p1')
    p1.wait_loaded('n=p1')
    m1.set_cookie(p1, 'P')
    check(who(p1) == 'who=P' and who(w1) == 'who=W',
          f'the two new spaces have separate cookies ({who(p1)!r}, {who(w1)!r})')
    got = wait_prefs(profile, lambda pr: (lambda w, pl: w and pl and w.get('name') == 'Work' and
                                          pl.get('name') == 'Play' and w)(
        *[next((s for s in pr.get('neon', {}).get('spaces', {}).get('list', [])
                if s.get('id') == i), None) for i in (W, P)]))
    w_pref, p_pref = pref_space(profile, W), pref_space(profile, P)
    check(bool(got) and w_pref['icon'] == 'vector:work' and unsigned(w_pref['color']) == GREEN,
          f'Work is saved with its icon and colour {w_pref}')
    check(bool(p_pref) and p_pref['icon'] == '\U0001F3AE' and unsigned(p_pref['color']) == PURPLE,
          f'Play is saved with the typed emoji and its colour {p_pref}')

    # --- 3. Edit and delete entries of an icon's context menu ----------------------------------------
    bounds, _ = button(b, D)
    click(ctx, center(bounds), button=3, settle_time=1)
    items = menu_items(b)
    check(items and [e for _, e in items[-1]] == [True, True, True],
          f'the first space\'s menu has Edit, an enabled Delete and New space {items}')
    key(ctx, 'Escape')
    bounds, _ = button(b, A)
    click(ctx, center(bounds), button=3, settle_time=1)
    items = menu_items(b)
    check(items and [e for _, e in items[-1]] == [True, True, True],
          f'another space\'s menu has Edit, Delete and New space {items}')
    save_shot(ctx, 'row-context-menu.png', crop=(0, SCREEN[1] - 260, 400, SCREEN[1]))
    if items:
        click(ctx, center(items[-1][0][0]), settle_time=1.2)   # Edit
    check(edit_view(b)[0] == A, 'Edit opens the bubble on that space')
    key(ctx, 'ctrl+a')
    type_text(ctx, 'Alpha')
    click(ctx, center(choice(b, 'SpaceIconChoice/vector:home')))
    click(ctx, center(dialog_button(b, 'Done')))
    check(wait_names(b, A, 'Alpha') == ['Alpha', 'Alpha'], f'A renamed Alpha {names(b, A)}')

    # --- 4. Reorder by dragging -------------------------------------------------------------------
    order = orders(b)[0]
    src, _ = button(b, P)
    dst, _ = button(b, A)
    drag(ctx, center(src), center(dst))
    want = [D, P, A, W]
    check(wait_orders(b, want) == [want] * 2, f'dragging Play onto Alpha reorders both rows '
          f'{orders(b)} (was {order})')
    src, _ = button(b, D)
    last, _ = button(b, W)
    drag(ctx, center(src), (last[0] + 40, last[1] + 14))
    moved = [P, A, W, D]
    check(wait_orders(b, moved) == [moved] * 2,
          f'the first space can be dragged to the end too {orders(b)}')
    src, _ = button(b, D)
    first, _ = button(b, P)
    drag(ctx, center(src), (first[0] - 6, first[1] + 14))
    check(wait_orders(b, want) == [want] * 2, f'and back to the front {orders(b)}')
    raise_window(ctx, 0)
    click(ctx, center(button(b, D)[0]), settle_time=1)
    wait_visible(b, lambda h: name_of(h) == 'd1')
    seq = []
    for _ in range(3):
        key(ctx, 'ctrl+alt+Down')
        settle(0.8)
        href, _ = visible(b)
        seq.append(name_of(href))
    check(seq == ['p1', 'a2', 'w1'] or seq == ['p1', 'a1', 'w1'],
          f'next space follows the new order: D -> Play -> Alpha -> Work ({seq})')
    key(ctx, 'ctrl+alt+Up')
    settle(0.8)
    check(name_of(visible(b)[0]) in ('a1', 'a2'), 'previous space follows it too')

    # --- 5. Settings page -------------------------------------------------------------------------
    raise_window(ctx, 1)
    key(ctx, 'ctrl+t')
    type_url(ctx, 'chrome://settings/spaces')
    sp = wait_for(lambda: settings_target(b), 15)
    sp.wait_loaded('settings')
    state = wait_for(lambda: (lambda st: st if st and len(st) == 4 else None)(settings_state(sp)),
                     10)
    check(state and [r[0] for r in state] == want and
          [r[1] for r in state] == ['Default', 'Play', 'Alpha', 'Work'],
          f'the settings page lists the spaces in order {state}')
    check(state and state[2][2] == 'vector:home' and state[3][2] == 'vector:work',
          'the settings page shows the icons')
    save_shot(ctx, 'settings-page.png', crop=crop_window(1))

    # Rename Work from the page.
    click(ctx, settings_point(ctx, b, sp, row_sel(W, '.space-name >>> input')))
    key(ctx, 'ctrl+a')
    type_text(ctx, 'Office')
    key(ctx, 'Tab')
    check(wait_names(b, W, 'Office') == ['Office', 'Office'],
          f'renaming in settings renames in both sidebars {names(b, W)}')
    # Recolour Alpha.
    click(ctx, settings_point(ctx, b, sp, row_sel(A, f'.swatch[data-color="{RED}"]')))
    settle(0.8)
    check_tint(ctx, b, A, RED, 'colour picked in settings')
    # Move Play down.
    click(ctx, settings_point(ctx, b, sp, row_sel(P, '.move-down')))
    want = [D, A, P, W]
    check(wait_orders(b, want) == [want] * 2, f'moving down in settings reorders the sidebars '
          f'{orders(b)}')
    # The sidebar's changes show in settings at once.
    raise_window(ctx, 0)
    click(ctx, center(button(b, W)[0]), button=3, settle_time=1)
    items = menu_items(b)
    if items:
        click(ctx, center(items[-1][0][0]), settle_time=1.2)
    key(ctx, 'ctrl+a')
    type_text(ctx, 'Desk')
    click(ctx, center(dialog_button(b, 'Done')))
    got = wait_for(lambda: (lambda st: st if st and any(r[1] == 'Desk' for r in st) else None)(
        settings_state(sp)), 8)
    check(bool(got), f'a rename in the sidebar shows in settings {settings_state(sp)}')
    click(ctx, center(add_button(b)), settle_time=1.5)
    N5, _ = edit_view(b)
    key(ctx, 'Escape')
    got = wait_for(lambda: (lambda st: st if st and len(st) == 5 else None)(settings_state(sp)), 8)
    check(bool(got) and got[-1][0] == N5, f'a space added in the sidebar shows in settings {got}')
    # Delete it from settings: it holds the new tab page "+" opened.
    raise_window(ctx, 1)
    click(ctx, settings_point(ctx, b, sp, row_sel(N5, '.delete')), settle_time=1)
    text = sp.eval("neonIn('#deleteDialog [slot=body]').textContent")
    check('1 open tab will close' in (text or ''), f'the settings dialog counts the tabs ({text!r})')
    save_shot(ctx, 'settings-delete-dialog.png', crop=crop_window(1))
    click(ctx, settings_point(ctx, b, sp, '#confirmDelete'), settle_time=1.5)
    want = [D, A, P, W]
    check(wait_orders(b, want) == [want] * 2, f'deleting in settings removes the space from both '
          f'sidebars {orders(b)}')
    href, _ = visible(b, 0)
    check(name_of(href) == 'w1', f'the window that showed it moved to the space before it ({href})')
    # Add from settings.
    click(ctx, settings_point(ctx, b, sp, '#addSpace'), settle_time=1.5)
    got = wait_for(lambda: (lambda o: o if o and len(o[0]) == 5 else None)(orders(b)), 8)
    check(bool(got) and all(len(r) == 5 for r in got), f'"Add space" in settings adds it to both '
          f'sidebars {orders(b)}')
    N6 = orders(b)[0][-1]
    # Icon from the page's menu, with keys: open the select, pick "Home"... for Play: Pets.
    click(ctx, settings_point(ctx, b, sp, row_sel(P, '.space-icon')), settle_time=0.8)
    type_text(ctx, 'Pe')
    key(ctx, 'Return')
    got = wait_for(lambda: (lambda st: st if st and any(
        r[0] == P and r[2] == 'vector:pets' for r in st) else None)(settings_state(sp)), 8)
    check(bool(got), f'picking an icon in settings saves it ({settings_state(sp)})')
    # The two toggles.
    click(ctx, settings_point(ctx, b, sp, '#showSwitcherRow'), settle_time=1)
    check(bool(wait_for(lambda: not rows(b, timeout=1), 8)),
          'turning the row off hides it in both windows')
    save_shot(ctx, 'settings-row-off.png', crop=crop_window(0))
    click(ctx, settings_point(ctx, b, sp, '#showSwitcherRow'), settle_time=1)
    check(len(wait_for(lambda: (lambda r: r if len(r) == 2 else None)(rows(b)), 8) or []) == 2,
          'turning it back on shows both rows')
    settle(0.8)
    click(ctx, settings_point(ctx, b, sp, '#swipeToSwitch'), settle_time=0.8)
    raise_window(ctx, 0)
    click(ctx, center(button(b, A)[0]), settle_time=1)
    before_href, _ = visible(b, 0)
    move(ctx, (100, 300))
    xdo(ctx, 'click', '7')
    settle(1)
    after_href, _ = visible(b, 0)
    check(before_href == after_href,
          f'with swiping off a sideways wheel does not switch ({before_href} -> {after_href})')
    raise_window(ctx, 1)
    click(ctx, settings_point(ctx, b, sp, '#swipeToSwitch'), settle_time=0.8)
    move(ctx, (100, 300))
    xdo(ctx, 'click', '7')
    settle(1)
    after_href, _ = visible(b, 0)
    check(before_href != after_href,
          f'with swiping on it switches again ({before_href} -> {after_href})')
    sp_target = sp.target_id

    # --- 6. Restart: names, icons, colours and order come back --------------------------------------
    pre = [(s['id'], s['name'], s['icon'], unsigned(s['color'])) for s in pref_spaces(profile)]
    stop(ctx, b)
    saved = [(s['id'], s['name'], s['icon'], unsigned(s['color'])) for s in pref_spaces(profile)]
    check([i for i, *_ in saved] == [D, A, P, W, N6] and saved[3][1] == 'Desk' and
          saved[2][2] == 'vector:pets' and saved[1][3] == RED,
          f'the space list is saved on exit {saved}')
    b = start(ctx, 'main', None, profile=profile)
    settle(4)
    place_second_window(b)
    want = [D, A, P, W, N6]
    check(wait_orders(b, want, timeout=15) == [want] * 2,
          f'after a restart both windows show the same order {orders(b)}')
    check(names(b, W) == ['Desk', 'Desk'] and names(b, P) == ['Play', 'Play'],
          f'and the same names {names(b, W)} {names(b, P)}')
    check_tint(ctx, b, A, RED, 'colours after restart')

    # --- 7. Reopen in space -------------------------------------------------------------------------
    raise_window(ctx, 0)
    click(ctx, center(button(b, A)[0]), settle_time=1.2)
    wait_visible(b, lambda h: name_of(h) in ('a1', 'a2'))
    tab_named(ctx, b, 'a2')
    key(ctx, 'ctrl+alt+p')   # pin a2
    settle(0.8)
    # a1 in a tab group: a group never spans two spaces, so the reopened tab leaves it.
    a1_id = next(t[5] for t in tab_list(b) if name_of(t[3]) == 'a1')
    group_id = ext_bg(b).eval(f'chrome.tabs.group({{tabIds: [{a1_id}]}})')
    settle(1)
    t1 = tab_named(ctx, b, 'a1')
    settle(1)
    t1 = next((t for t in sidebar_tabs(b) if t1 and abs(center(t)[1] - center(t1)[1]) < 12), t1)
    others = [t for t in sidebar_tabs(b) if t1 and abs(center(t)[1] - center(t1)[1]) >= 12]
    check(t1 is not None and len(others) == 1, f'Alpha shows its two tabs ({t1}, {others})')
    t2 = others[0] if others else t1
    click(ctx, center(t2), modifier='ctrl', settle_time=0.5)
    old_ids = {t['targetId'] for t in b.pages() if 'n=a1' in t['url'] or 'n=a2' in t['url']}
    click(ctx, center(t1), button=3, settle_time=1)
    menus = menu_items(b)
    # Find the item that opens the spaces submenu: its menu ends with a separator and one item.
    found = None
    main_menu = max(menus, key=len) if menus else []
    seen = set()
    for bounds, enabled in main_menu:
        move(ctx, center(bounds))
        settle(0.9)
        subs = [m for m in menu_items(b, timeout=1) if bounds not in [e[0] for e in m]]
        # The submenu this item opened (submenus opened before stay in the views tree): the
        # four other spaces and "New space".
        new = [m for m in subs if m[0][0] not in seen]
        seen.update(m[0][0] for m in subs)
        if len(new) == 1 and len(new[0]) == len(want):
            found = new[0]
            break
    check(found is not None, 'the tab menu has a "Reopen in space" submenu listing the other '
          'spaces and "New space"')
    save_shot(ctx, 'tab-menu-reopen-in-space.png', crop=(0, 0, 760, SCREEN[1]))
    if found:
        # Items: Default, Play, Desk, N6, New space. Pick Play.
        click(ctx, center(found[1][0]), settle_time=2)
    tabs = {name_of(t[3]): t for t in tab_list(b) if name_of(t[3]) in ('a1', 'a2')}
    new_pages = [t for t in b.pages() if ('n=a1' in t['url'] or 'n=a2' in t['url'])]
    check(len(new_pages) == 2 and not ({t['targetId'] for t in new_pages} & old_ids),
          f'both selected tabs were reopened and the originals closed {len(new_pages)}')
    cookies = sorted(who(Page(b, t['targetId'])) for t in new_pages)
    check(cookies == ['who=P', 'who=P'], f'they reloaded in Play\'s container ({cookies})')
    check(tabs.get('a2', [None] * 5)[4] is True and tabs.get('a1', [None] * 5)[4] is False,
          f'the pinned tab stays pinned, the other not ({tabs})')
    check(tabs.get('a1', [None, None, None])[2] == -1,
          f'the reopened tab is not in the group its original was in ({tabs.get("a1")})')
    in_group = [name_of(t[3]) for t in tab_list(b) if t[2] == group_id]
    check(not in_group, f'and no tab is left in that group ({in_group})')
    href, _ = visible(b, 0)
    check(name_of(href) in ('a1', 'a2', 'p1'), f'the window shows Play ({href})')

    # --- 8. Delete from the sidebar ---------------------------------------------------------------
    # Storage to look for on disk afterwards.
    p_container = pref_space(profile, P)['container_id']
    # The page Play shows (a background tab after the restart may be frozen or not loaded).
    _, p1 = visible(b, 0)
    marker = f'NEONMARKER{int(time.time())}'
    p1.eval(f'localStorage.setItem("neon", "{marker}")')
    p1.eval('''new Promise(r => { const q = indexedDB.open("neondb", 1);
        q.onupgradeneeded = () => q.result.createObjectStore("s");
        q.onsuccess = () => { const t = q.result.transaction("s", "readwrite");
          t.objectStore("s").put("v", "k"); t.oncomplete = () => r(true); }; })''')
    pdir = m1.partition_dir(profile, p_container)
    rows_before = wait_for(lambda: cookie_rows(profile, p_container), 40, step=2) or []
    check(any(r[1] == 'who' for r in rows_before), f'Play\'s cookie is on disk before ({rows_before})')
    # From the second window, while the first shows Play.
    raise_window(ctx, 1)
    click(ctx, center(button(b, P, window=1)[0]), button=3, settle_time=1)
    items = menu_items(b)
    if items:
        click(ctx, center(items[-1][1][0]), settle_time=1.2)   # Delete
    msg = labels(b, lambda t: 'open tab' in t)
    check(any('3 open tabs will close' in m for m in msg), f'the confirmation counts Play\'s 3 tabs '
          f'in all windows ({msg})')
    save_shot(ctx, 'delete-confirmation.png', crop=(LEFT, SCREEN[1] - 360, LEFT + 640, SCREEN[1]))
    click(ctx, center(dialog_button(b, 'Delete')), settle_time=2)
    want = [D, A, W, N6]
    check(wait_orders(b, want) == [want] * 2, f'Play is gone from both rows {orders(b)}')
    left = [t['url'] for t in b.pages() if any(f'n={n}' in t['url'] for n in ('p1', 'a1', 'a2'))]
    check(not left, f'its tabs closed in every window ({left})')
    href, q = wait_visible(b, is_ntp)
    check(is_ntp(href), f'the first window moved to the space before it, Alpha, which had no tab '
          f'left: a new tab page ({href})')
    raise_window(ctx, 0)
    type_url(ctx, page_url(ctx, 'a3', 'e60000'))
    a3 = m1.find_page(b, 'n=a3')
    a3.wait_loaded('n=a3')
    check(who(a3) == 'who=A', f'that new tab page is in Alpha\'s container ({who(a3)!r})')
    gone = wait_for(lambda: not any(r[1] == 'who' for r in cookie_rows(profile, p_container)), 60,
                    step=2)
    check(bool(gone), f'Play\'s cookies are deleted on disk at once '
          f'({cookie_rows(profile, p_container)})')
    ls_gone = wait_for(lambda: not dir_contains(pdir, marker.encode()), 60, step=2)
    check(bool(ls_gone), 'its local storage no longer holds the marker on disk')
    idb = os.path.join(pdir, 'IndexedDB')
    idb_gone = wait_for(lambda: not os.path.exists(idb) or not any(
        'neondb' in open(os.path.join(r, f), 'rb').read().decode('latin1')
        for r, _, fs in os.walk(idb) for f in fs), 30, step=2)
    check(bool(idb_gone), 'its IndexedDB database is deleted')
    containers = wait_prefs(profile, lambda pr: (lambda ids: ids if p_container not in ids else None)(
        [c.get('id') for c in pr.get('brave', {}).get('containers', {}).get('list', [])]))
    check(containers is not None, f'its container is removed from the containers list')

    # A saved tab group in Alpha, recorded in neon.spaces.saved_group_containers.
    a3_tab = next(t for t in tab_list(b) if name_of(t[3]) == 'a3')
    ext_bg(b).eval(f'chrome.tabs.group({{tabIds: [{a3_tab[5]}]}})')
    recorded = wait_prefs(profile, lambda pr: SPACE_A['container_id'] in pr.get('neon', {}).get(
        'spaces', {}).get('saved_group_containers', {}).values(), timeout=30)
    check(bool(recorded), 'a saved group in Alpha is recorded with Alpha\'s container')

    # --- 9. Delete the remaining spaces, the last with no tab -------------------------------------
    for sid, label, tabs_text in ((A, 'Alpha', '1 open tab will close'),
                                  (W, 'Desk', '1 open tab will close'),
                                  (N6, 'last', 'No tabs are open')):
        raise_window(ctx, 0)
        bounds, _ = button(b, sid)
        if not bounds:
            check(False, f'{label}: no icon to delete')
            continue
        click(ctx, center(bounds), button=3, settle_time=1)
        items = menu_items(b)
        if items:
            click(ctx, center(items[-1][1][0]), settle_time=1.2)
        msg = labels(b, lambda t: 'open tab' in t or 'No tabs' in t)
        check(any(tabs_text in m for m in msg), f'{label}: the confirmation says "{tabs_text}" '
              f'({msg})')
        click(ctx, center(dialog_button(b, 'Delete')), settle_time=2)
    check(not rows(b, timeout=2), 'with only the first space left the row is hidden')
    group = wait_prefs(profile, lambda pr: SPACE_A['container_id'] not in pr.get('neon', {}).get(
        'spaces', {}).get('saved_group_containers', {}).values())
    check(bool(group), 'deleting Alpha dropped its saved group container entry')
    pages_left = [name_of(t['url']) or t['url'] for t in b.pages()
                  if not t['url'].startswith('chrome://settings')]
    check('d1' in pages_left and not any(n in pages_left for n in ('a3', 'w1')),
          f'only the first space\'s tabs are left ({pages_left})')
    save_shot(ctx, 'stock-after-deleting-all.png', crop=crop_window(0))
    dirs = {sid: m1.partition_dir(profile, c) for sid, c in (
        (P, p_container), (A, SPACE_A['container_id']), (W, pref_space(profile, W) and
                                                         pref_space(profile, W)['container_id']))
            if c}

    # --- 10. Restart: the deleted containers' storage is gone --------------------------------------
    stop(ctx, b)
    b = start(ctx, 'main', None, profile=profile)
    settle(3)
    place_second_window(b)
    gone = wait_for(lambda: all(not os.path.exists(d) for d in dirs.values()), 30, step=2)
    check(bool(gone), f'after a restart the deleted containers\' directories are gone '
          f'{[d for d in dirs.values() if os.path.exists(d)]}')
    def ids_of(value):
        if isinstance(value, dict):
            return list(value.keys()) + [v.get('id') for v in value.values() if isinstance(v, dict)]
        return [c.get('id') if isinstance(c, dict) else c for c in value or []]

    def remembered(pr):
        c = pr.get('brave', {}).get('containers', {})
        return set(ids_of(c.get('used')) + ids_of(c.get('list')))
    deleted = {p_container, SPACE_A['container_id']}
    forgotten = wait_prefs(profile, lambda pr: not (remembered(pr) & deleted), timeout=30)
    check(bool(forgotten), f'and neither the containers list nor the used containers remember '
          f'them ({sorted(remembered(read_prefs(profile)) & deleted)})')
    check(not rows(b, timeout=2), 'the row is still hidden (one space)')
    # A new space after all that starts empty.
    raise_window(ctx, 1)
    sp = settings_target(b)
    if not sp:
        key(ctx, 'ctrl+t')
        type_url(ctx, 'chrome://settings/spaces')
        sp = wait_for(lambda: settings_target(b), 15)
    sp.wait_loaded('settings')
    settle(1)
    sp.eval(DEEP_JS)
    last = sp.eval('''(() => {
      const page = neonSpacesPage();
      if (!page) return null;
      return Array.from(page.shadowRoot.querySelectorAll('.space-row .delete')).map(d =>
          [d.disabled, d.title]);
    })()''')
    check(last and len(last) == 1 and last[0][0] and 'last space' in last[0][1],
          f'the last space cannot be deleted in settings ({last})')
    click(ctx, settings_point(ctx, b, sp, '#addSpace'), settle_time=1.5)
    check(len(rows(b)) == 2, 'a space added from settings brings the rows back')
    raise_window(ctx, 0)
    click(ctx, center(button(b, orders(b)[0][-1])[0]), settle_time=1.5)
    type_url(ctx, page_url(ctx, 'n1', PAGE_HEX['N']))
    n1 = m1.find_page(b, 'n=n1')
    n1.wait_loaded('n=n1')
    check(who(n1) == '', f'and a new space has no cookies ({who(n1)!r})')
    stop(ctx, b)



def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--helium', default=os.environ.get('NEON_HELIUM', m1.DEFAULT_HELIUM))
    ap.add_argument('--keep', action='store_true', help='keep the profiles for inspection')
    ap.add_argument('--shots', default=os.path.join(ROOT, 'build', 'm4'),
                    help='where the screenshots go (default: build/m4/)')
    ap.add_argument('--only', action='append', default=[],
                    help='run only these phases: main (the only one)')
    ap.add_argument('--browser-arg', action='append', default=[],
                    help='extra browser switch for every launch')
    args = ap.parse_args()
    Browser.extra_args = args.browser_arg
    if not os.access(args.helium, os.X_OK):
        raise SystemExit(f'browser binary not found: {args.helium}')

    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(args.shots, exist_ok=True)
    log_path = os.path.join(LOG_DIR, 'spaces_m4.log')
    ctx = Ctx()
    ctx.helium, ctx.dirs, ctx.browsers = args.helium, [], []
    ctx.log = open(log_path, 'w')
    ctx.shots = args.shots
    ctx.tmp = tempfile.mkdtemp(prefix='neon-m4-shots-')
    ctx.dirs.append(ctx.tmp)

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), m2.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ctx.site = f'http://127.0.0.1:{server.server_address[1]}'

    ctx.display = free_display()
    xvfb = subprocess.Popen(['Xvfb', f':{ctx.display}', '-screen', '0',
                             f'{SCREEN[0]}x{SCREEN[1]}x24', '-nolisten', 'tcp'],
                            stdout=ctx.log, stderr=subprocess.STDOUT)
    time.sleep(1)
    phases = [('main', phase_main)]
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
