#!/usr/bin/env python3
"""M5 check: every space is a container. The default storage partition is the global context of
the browser's own background work and the extensions; no tab of a space uses it. Drives the real
Linux dev build with real X input (typing into the omnibox, clicks on links, menus and buttons,
right clicks) and the DevTools protocol.

Run from the neon-linux checkout (one command, from the host):

    neon/dev-container.sh sh 'python3 helium-chromium/neon/tests/all_containers_m5.py'

or inside `neon/dev-container.sh sh`:

    python3 helium-chromium/neon/tests/all_containers_m5.py [--keep] [--helium PATH]
        [--shots DIR] [--only PHASE] [--browser-arg ARG]

Exit status is 0 when every check passes and 1 otherwise. Like the earlier checks it starts its
own Xvfb, local HTTP servers on 127.0.0.1 and the browser with throwaway profiles, and stops
all of them at the end.

How state is observed:

- Every page of the test server sets a cookie named after the page (v_<page>), and the test sets
  who=<space> in each space. A page's partition is read from the cookies it sees, and at the
  end of a phase from the cookie databases on disk: <profile>/Default/Cookies for the default
  partition, Storage/ext/containers/<hash>/Cookies for a container.
- Spaces and containers are read from the Preferences file once the browser has exited.
- Where to click is read from the browser's UI DevTools (--enable-ui-devtools), as in the M4
  check: SpaceButton/<id>, SpaceEditView/<id>, the tab views, the menus.
- Windows and tabs as the extension sees them come from chrome.windows and chrome.tabs in the
  test extension's service worker (m5_extension/).

Phases:

1. first-run: a fresh profile starts in one container-backed space named Personal, with the
   switcher row hidden. Typed URLs, a link, a target=_blank link, a popup, window.open of
   about:blank (with and without opener) followed by a navigation, a blank tab from the tab
   strip (Ctrl+T, about:blank, then a URL), and WebUI pages all stay in Personal. After a
   restart, the WebUI pages and the blank tab come back as tabs of Personal (a URL typed into
   each loads in Personal's container). On disk, the default partition holds no cookie of
   anything browsed; the container holds them all.
2. manage: with one space (the row hidden), the New Tab button's context menu offers "New
   space", which creates a second space and shows the row. The first space can be deleted (its tabs
   close, the window moves to the other space); the last one cannot, in the row menu or in
   settings. A space can be moved to the top from settings.
3. old-profile: a profile written by the build before M5 (fixtures/m4-profile.tar.gz: a default
   space with two web pages, the settings page and about:blank, plus space A) starts cleanly:
   the default space becomes a container space named Personal, its tabs are reopened there
   signed out (no cookie is copied), space A keeps its tab, and a second restart changes
   nothing.
4. extensions: the test extension's options page (runtime.openOptionsPage and tabs.create) and
   its windows.create popup open in popup windows centred on the browser window, in the default
   partition, and the options page is not opened twice; a normal windows.create of an
   extension page ends the same way. tabs.create of a web page lands in the
   visible space, and with openerTabId in the opener's space. Experiment E1 (what a content
   script and the background see in a container tab) and identity.launchWebAuthFlow are
   recorded as checks.
5. flags-off: with --disable-features=Containers the profile gets no space, pages use the
   default partition and the options page opens as a tab, as in stock Helium.

The browser log must hold no CHECK failure or crash.

Needs python3-websocket, python3-pil and xdotool (all in the dev image).
"""
import argparse
import http.cookies
import http.server
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import containers_m1 as m1  # noqa: E402
import spaces_m2 as m2  # noqa: E402  (also gives Session.call a deadline)
import spaces_m4 as m4  # noqa: E402
from containers_m1 import Browser, Page, check, free_display  # noqa: E402

ROOT = m1.ROOT
LOG_DIR = m1.LOG_DIR
failures = m1.failures
HERE = os.path.dirname(os.path.abspath(__file__))
EXTENSION = os.path.join(HERE, 'm5_extension')
FIXTURE = os.path.join(HERE, 'fixtures', 'm4-profile.tar.gz')
FIXTURE_PORT = 47813   # the port of the pages in the fixture (make_m4_profile.py)
SCREEN = (1600, 1000)
EXT_ARGS = (f'--load-extension={EXTENSION}',
            '--disable-features=DisableLoadExtensionCommandLineSwitch')
COOKIE_AGE = 30 * 24 * 3600


# --- Local test server -------------------------------------------------------------------------

PAGE = '''<!doctype html><meta charset="utf-8"><title>{name}</title>
<link rel="icon" href="/fav?c={color}">
<body style="background:#{color}20; font: 15px sans-serif">
<p>page {name}</p>
<p><a id="same" href="/page?n={name}-same&c={color}">same tab</a></p>
<p><a id="blank" href="/page?n={name}-blank&c={color}" target="_blank">target blank</a></p>
<p><button id="pop" onclick="window.open('/page?n={name}-pop&c={color}', 'pop',
  'width=520,height=420')">popup</button></p>
</body>
'''


def cookie_name(page):
    return 'v_' + re.sub(r'[^A-Za-z0-9]', '_', page)


class Handler(http.server.BaseHTTPRequestHandler):
    """/page?n=NAME sets the cookie v_NAME, so the partition of every page load is on disk.
    /echo answers with the Cookie header it got."""

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        color = q.get('c', ['808080'])[0]
        headers = []
        if url.path == '/fav':
            body, kind = m2.png(color), 'image/png'
        elif url.path == '/echo':
            body, kind = (self.headers.get('Cookie') or '').encode(), 'text/plain'
        else:
            name = q.get('n', ['x'])[0]
            body = PAGE.format(name=name, color=color).encode()
            kind = 'text/html; charset=utf-8'
            headers.append(('Set-Cookie',
                            f'{cookie_name(name)}=1; Max-Age={COOKIE_AGE}; Path=/'))
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve(port=0):
    server = http.server.ThreadingHTTPServer(('127.0.0.1', port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --- Helpers -----------------------------------------------------------------------------------

class Ctx:
    pass


settle = m4.settle
key = m4.key
click = m4.click
center = m4.center
wait_for = m4.wait_for


def page_url(ctx, name, color='808080'):
    return f'{ctx.site}/page?n={name}&c={color}'


def start(ctx, name, prefs, *args, profile=None):
    b = m4.start(ctx, name, prefs, *args, profile=profile)
    settle(2)
    return b


class FreeSizeBrowser(Browser):
    """A browser launched without --window-size and --window-position, which the other checks
    pass and which force every window, popups included, to the same bounds."""

    def base_args(self):
        return [a for a in super().base_args()
                if not a.startswith(('--window-size=', '--window-position='))]


def start_free_size(ctx, name, prefs, *args):
    saved = m4.Browser
    m4.Browser = FreeSizeBrowser
    try:
        return start(ctx, name, prefs, *args)
    finally:
        m4.Browser = saved


def stop(ctx, b):
    m4.stop(ctx, b)


def cookies(p):
    try:
        jar = http.cookies.SimpleCookie()
        jar.load(p.eval('document.cookie') or '')
        return {k: v.value for k, v in jar.items()}
    except RuntimeError:
        return None


def who(p):
    c = cookies(p)
    return None if c is None else c.get('who', '')


def set_who(p, value):
    p.eval(f'document.cookie = "who={value}; max-age={COOKIE_AGE}; path=/"')


def disk_cookies(path):
    """{(host, name)} of a cookie database, read from a copy (the file may be open)."""
    if not os.path.exists(path):
        return set()
    tmp = tempfile.mktemp(suffix='.sqlite')
    shutil.copy(path, tmp)
    try:
        db = sqlite3.connect(tmp)
        rows = db.execute('SELECT host_key, name FROM cookies').fetchall()
        db.close()
    finally:
        os.unlink(tmp)
    return {(h, n) for h, n in rows}


def default_cookies(profile):
    return disk_cookies(os.path.join(profile, 'Default', 'Cookies'))


def container_cookies(profile, container_id):
    return disk_cookies(os.path.join(m1.partition_dir(profile, container_id), 'Cookies'))


def test_cookie_names(rows):
    return sorted(n for h, n in rows if h == '127.0.0.1' and (n == 'who' or n.startswith('v_')))


def spaces_in_prefs(profile):
    prefs = m1.read_prefs(profile)
    return prefs.get('neon', {}).get('spaces', {}).get('list', []), \
        prefs.get('brave', {}).get('containers', {}).get('list', [])


def main_window(ctx):
    """The X id of the largest browser window."""
    best = None
    for wid in m4.window_ids(ctx):
        r = m4.xdo(ctx, 'getwindowgeometry', wid)
        try:
            w, h = (int(v) for v in r.stdout.split('Geometry: ')[1].split()[0].split('x'))
        except (IndexError, ValueError):
            continue
        if not best or w * h > best[0]:
            best = (w * h, wid)
    return best[1] if best else None


def focus_main(ctx):
    wid = main_window(ctx)
    if wid:
        m4.xdo(ctx, 'windowraise', wid)
        m4.xdo(ctx, 'windowfocus', '--sync', wid)
        settle(0.4)


def type_url(ctx, address):
    """A URL typed into the omnibox of the main window: Ctrl+L, the text, Enter."""
    focus_main(ctx)
    key(ctx, 'ctrl+l')
    m4.type_text(ctx, address)
    key(ctx, 'Return')
    settle(0.8)


def new_tab(ctx, b):
    """Ctrl+T in the main window. Returns the new tab page's Page."""
    focus_main(ctx)
    before = {t['targetId'] for t in b.pages()}
    key(ctx, 'ctrl+t')
    t = wait_for(lambda: [t for t in b.pages() if t['targetId'] not in before], 10)
    return Page(b, t[0]['targetId']) if t else None


def active_page(b, timeout=10):
    href, p = m2.wait_active(b, lambda h: True, timeout=timeout)
    return href, p


def content_origin(b):
    """Screen position of the main window's page area."""
    best = None
    for n, bounds in b.ui.find(lambda n: n['cls'] == 'ContentsWebView', 5):
        if bounds[2] > 300 and b.ui.props(n).get('IsDrawn') == 'true':
            if not best or bounds[2] * bounds[3] > best[2] * best[3]:
                best = bounds
    return best


def click_element(ctx, b, p, selector, **kw):
    """A real click on |selector| of page |p|, shown in the main window."""
    rect = p.eval(f'(() => {{ const r = document.querySelector({json.dumps(selector)})'
                  '.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; })()')
    origin = content_origin(b)
    click(ctx, (int(origin[0] + rect[0]), int(origin[1] + rect[1])), **kw)


def find(b, part, exclude=(), timeout=20):
    return m1.find_page(b, part, timeout=timeout, exclude=exclude)


def loaded(b, part, exclude=(), timeout=20):
    try:
        p = find(b, part, exclude, timeout)
        p.wait_loaded(part, timeout=timeout)
        return p
    except (TimeoutError, RuntimeError):
        return None


def target_ids(b):
    return {t['targetId'] for t in b.pages()}


def ext_worker(b, timeout=20):
    """The test extension's service worker, as a Page-like DevTools session."""
    def find_sw():
        for t in b.session.call('Target.getTargets')['targetInfos']:
            if t['type'] == 'service_worker' and t['url'].endswith('/background.js'):
                return t
        return None
    t = wait_for(find_sw, timeout)
    if not t:
        raise RuntimeError('no extension service worker')
    return Page(b, t['targetId'])


def windows(sw):
    """chrome.windows.getAll with tabs: [{id, type, focused, tabs: [url]}]."""
    return sw.eval('''chrome.windows.getAll({populate: true}).then(ws => ws.map(w => ({
        id: w.id, type: w.type, left: w.left, top: w.top, width: w.width, height: w.height,
        tabs: w.tabs.map(t => t.url || t.pendingUrl)})))''')


def window_bounds(b, target_id):
    w = b.session.call('Browser.getWindowForTarget', {'targetId': target_id})['bounds']
    return w['left'], w['top'], w['width'], w['height']


def centred(inner, outer, slack=40):
    ci = (inner[0] + inner[2] / 2, inner[1] + inner[3] / 2)
    co = (outer[0] + outer[2] / 2, outer[1] + outer[3] / 2)
    return abs(ci[0] - co[0]) <= slack and abs(ci[1] - co[1]) <= slack


def menu_items(b, timeout=5):
    """The drawn items of the open menu, top to bottom, with their bounds."""
    found = b.ui.find(lambda n: n['cls'] == 'MenuItemView', timeout)
    items = [(n, bounds) for n, bounds in found
             if bounds[2] and b.ui.props(n).get('IsDrawn') == 'true']
    return sorted(items, key=lambda e: e[1][1])


def view_with_text(b, cls, text, timeout=5):
    for n, bounds in b.ui.find(lambda n: n['cls'] == cls, timeout):
        props = b.ui.props(n)
        if bounds[2] and props.get('IsDrawn') == 'true' and props.get('Text') == text:
            return bounds
    return None


def new_tab_button(b):
    """Bounds of the sidebar's "New Tab" button of the main window."""
    found = [bounds for n, bounds in b.ui.find(lambda n: 'NewTabButton' in n['cls'], 5)
             if bounds[2] and b.ui.props(n).get('IsDrawn') == 'true']
    return min(found, key=lambda r: r[0]) if found else None


def item_texts(b, items):
    return [b.ui.props(n).get('Text') or b.ui.props(n).get('AccessibleName') or '' for n, _ in items]


def settings_point(ctx, b, p, sel):
    p.eval(m4.DEEP_JS)
    settle(0.3)
    rect = p.eval(f'neonRect({json.dumps(sel)})')
    if not rect:
        raise RuntimeError(f'no {sel} on the Spaces page')
    origin = content_origin(b)
    return int(origin[0] + rect[0]), int(origin[1] + rect[1])


def settings_spaces(p):
    """[(id, name, delete disabled, delete title, move-up disabled)] from the Spaces page."""
    p.eval(m4.DEEP_JS)
    return p.eval('''(() => {
        const list = neonDeep('#spaceList');
        if (!list) return null;
        return [...list.querySelectorAll('.space-row')].map(r => [r.dataset.id,
            r.querySelector('.space-name').value,
            r.querySelector('.delete').disabled, r.querySelector('.delete').title,
            r.querySelector('.move-up').disabled]);
    })()''')


def open_settings_spaces(ctx, b):
    new_tab(ctx, b)
    type_url(ctx, 'chrome://settings/spaces')
    sp = wait_for(lambda: m4.settings_target(b), 15)
    sp.wait_loaded('settings')
    settle(1.5)
    return sp


# --- Phases ------------------------------------------------------------------------------------

def phase_first_run(ctx):
    print('== first-run: a fresh profile browses in one container-backed space', flush=True)
    prefs = {'session': {'restore_on_startup': 1}, 'helium': {'browser': {'layout': 2}}}
    b = start(ctx, 'first', prefs)
    profile = b.profile_dir
    ctx.first_profile = profile
    href, ntp = m2.wait_active(b, lambda h: h and h.startswith('chrome://'))
    check(bool(href), f'the first window opens on the new tab page ({href})')
    check(not m4.rows(b, timeout=2), 'with a single space the switcher row is hidden')
    m4.save_shot(ctx, 'first-run.png')

    # Typed into the new tab page's omnibox.
    type_url(ctx, page_url(ctx, 't1', 'e06000'))
    t1 = loaded(b, 'n=t1')
    check(t1 is not None and t1.target_id == ntp.target_id,
          'a URL typed into the first new tab page loads in that tab')
    if not t1:
        return stop(ctx, b)
    set_who(t1, 'P')
    check(who(t1) == 'P', 'its cookie is set')

    # A link in the same tab, a target=_blank link, a popup.
    click_element(ctx, b, t1, '#same')
    same = loaded(b, 'n=t1-same')
    check(same is not None and who(same) == 'P', 'a clicked link stays in the space')
    before = target_ids(b)
    click_element(ctx, b, same, '#blank')
    blank_link = loaded(b, 'n=t1-same-blank', before)
    check(blank_link is not None and who(blank_link) == 'P',
          'a target=_blank link opens a tab in the space')
    _, cur = active_page(b)
    before = target_ids(b)
    click_element(ctx, b, cur, '#pop')
    pop = loaded(b, '-pop', before)
    check(pop is not None and who(pop) == 'P', 'a popup window opens in the space\'s container')
    if pop:
        pop.eval('window.close(); 1')
        settle(0.8)

    # window.open('about:blank'), then a navigation: with an opener, and without.
    focus_main(ctx)
    _, cur = active_page(b)
    before = target_ids(b)
    cur.eval('window.neonW = window.open("about:blank"); 1', gesture=True)
    settle(1)
    cur.eval(f'window.neonW.location = "{page_url(ctx, "wob")}"; 1')
    wob = loaded(b, 'n=wob', before)
    check(wob is not None and who(wob) == 'P',
          "window.open('about:blank') then a navigation stays in the space")
    focus_main(ctx)
    _, cur = active_page(b)
    before = target_ids(b)
    cur.eval('window.open("about:blank", "_blank", "noopener"); 1', gesture=True)
    nb = wait_for(lambda: [t for t in b.pages() if t['targetId'] not in before and
                           t['url'] == 'about:blank'], 10)
    if nb:
        nbp = Page(b, nb[0]['targetId'])
        nbp.eval(f'location.href = "{page_url(ctx, "wobn")}"; 1')
    wobn = loaded(b, 'n=wobn', before)
    check(wobn is not None and who(wobn) == 'P',
          "a noopener window.open('about:blank') then a navigation stays in the space")

    # A blank tab from the tab strip: Ctrl+T, about:blank, then a URL, all typed.
    nt = new_tab(ctx, b)
    type_url(ctx, 'about:blank')
    href, _ = m2.wait_active(b, lambda h: h == 'about:blank')
    check(href == 'about:blank', 'Ctrl+T then about:blank shows a blank tab')
    type_url(ctx, page_url(ctx, 'fromblank'))
    fb = loaded(b, 'n=fromblank')
    check(fb is not None and who(fb) == 'P' and fb.target_id == nt.target_id,
          'a URL typed into that blank tab loads in the space, in the same tab')

    # WebUI pages and a blank tab, kept for the restart.
    for address in ('chrome://history/', 'chrome://settings/', 'about:blank'):
        new_tab(ctx, b)
        type_url(ctx, address)
        settle(1)
    urls = [t['url'] for t in b.pages()]
    check(any(u.startswith('chrome://history') for u in urls) and
          any(u.startswith('chrome://settings') for u in urls) and
          urls.count('about:blank') >= 1, f'history, settings and a blank tab are open')
    check(not m4.rows(b, timeout=1), 'still a single space: no row')
    stop(ctx, b)

    spaces, containers = spaces_in_prefs(profile)
    check(len(spaces) == 1 and spaces[0].get('name') == 'Personal' and
          spaces[0].get('container_id'),
          f'the profile has one space, Personal, with a container ({spaces})')
    cid = spaces[0].get('container_id') if spaces else ''
    check(any(c.get('id') == cid for c in containers),
          'its container is in the containers list')
    ctx.personal = cid
    default = test_cookie_names(default_cookies(profile))
    mine = test_cookie_names(container_cookies(profile, cid))
    check(default == [], f'on disk, the default partition holds no cookie of the pages '
          f'browsed ({default})')
    want = {'who', 'v_t1', 'v_t1_same', 'v_t1_same_blank', 'v_t1_same_blank_pop', 'v_wob',
            'v_wobn', 'v_fromblank'}
    check(want <= set(mine), f'Personal\'s container holds all of them ({sorted(want - set(mine))} '
          'missing)')

    # Restart: the WebUI pages and the blank tab are tabs of Personal.
    b = start(ctx, 'first', None, profile=profile)
    settle(2)
    for part, what in (('chrome://history', 'the history page'),
                       ('chrome://settings', 'the settings page'),
                       ('about:blank', 'the blank tab')):
        t = wait_for(lambda: [t for t in b.pages() if t['url'].startswith(part)], 15)
        if not check(bool(t), f'after a restart {what} is restored'):
            continue
        b.session.call('Target.activateTarget', {'targetId': t[0]['targetId']})
        settle(1)
        tag = 'r' + part.split('/')[-1].replace(':', '').replace('blank', 'b')[:8]
        type_url(ctx, page_url(ctx, tag))
        p = loaded(b, f'n={tag}')
        check(p is not None and who(p) == 'P' and p.target_id == t[0]['targetId'],
              f'{what} came back in Personal: a URL typed into it loads in its container')
    stop(ctx, b)
    default = test_cookie_names(default_cookies(profile))
    check(default == [], f'after the restart the default partition still holds no test cookie '
          f'({default})')


def phase_manage(ctx):
    print('== manage: new space from the sidebar, delete any space but the last', flush=True)
    profile = ctx.__dict__.get('first_profile')
    prefs = {'session': {'restore_on_startup': 1}, 'helium': {'browser': {'layout': 2}}}
    b = start(ctx, 'manage', prefs if not profile else None, profile=profile)
    focus_main(ctx)
    check(not m4.rows(b, timeout=2), 'one space: the row is hidden')
    ntb = new_tab_button(b)
    if not check(ntb is not None, 'the sidebar has its New Tab button'):
        return stop(ctx, b)
    click(ctx, center(ntb), button=3, settle_time=1)
    items = menu_items(b)
    check(len(items) >= 2, f'a right click on it opens its menu ({len(items)} items)')
    m4.save_shot(ctx, 'single-space-new-space-menu.png')
    if not items:
        return stop(ctx, b)
    ntb_items = len(items)
    # "New space" is the last item.
    click(ctx, center(items[-1][1]), settle_time=2)
    rows = m4.rows(b, timeout=5)
    check(len(rows) == 1 and len(rows[0]) == 2,
          f'"New space" adds a second space and the row appears ({[len(r) for r in rows]})')
    bubble = b.ui.find(lambda n: n['name'].startswith('SpaceEditView/'), 5)
    check(bool(bubble), 'and the edit bubble opens on it')
    key(ctx, 'Escape')
    order = m4.orders(b)[0] if m4.rows(b) else []
    personal, second = (order + ['', ''])[:2]
    href, _ = active_page(b)
    check(href and href.startswith('chrome://'), f'the new space shows a new tab page ({href})')
    type_url(ctx, page_url(ctx, 's2'))
    s2 = loaded(b, 'n=s2')
    check(s2 is not None and who(s2) == '', 'the new space has its own container (no cookie)')

    # Delete Personal, the first space, from its icon's menu.
    bounds, _ = m4.button(b, personal)
    click(ctx, center(bounds), settle_time=1.5)   # show Personal
    click(ctx, center(bounds), button=3, settle_time=1)
    items = menu_items(b)
    check(len(items) >= 4,
          f'the first space\'s menu has Edit, Clear data, Delete, New space ({len(items)})')
    delete_item = items[2] if len(items) >= 4 else None
    if delete_item:
        enabled = b.ui.props(delete_item[0]).get('Enabled')
        check(enabled == 'true', f'Delete is enabled for the first space ({enabled})')
        click(ctx, center(delete_item[1]), settle_time=1.5)
        ok = view_with_text(b, 'MdTextButton', 'Delete')
        check(ok is not None, 'a confirmation asks before deleting')
        if ok:
            click(ctx, center(ok), settle_time=2.5)
    urls = [t['url'] for t in b.pages()]
    check(not any('n=t1' in u or u.startswith('chrome://history') for u in urls),
          'Personal\'s tabs are closed')
    check(not m4.rows(b, timeout=2), 'one space is left: the row is hidden again')
    href, p = active_page(b)
    check(p is not None and 'n=s2' in (href or ''), f'the window shows the other space ({href})')

    # The last space cannot be deleted: not from settings. ("New space" stays in the New Tab
    # button's menu.)
    ntb = new_tab_button(b)
    if ntb:
        click(ctx, center(ntb), button=3, settle_time=1)
        n_items = len(menu_items(b))
        key(ctx, 'Escape')
        check(n_items == ntb_items, f'with one space again the New Tab button\'s menu still '
              f'offers "New space" ({n_items} items)')
    sp = open_settings_spaces(ctx, b)
    m4.save_shot(ctx, 'settings-spaces-one.png')
    listed = settings_spaces(sp)
    check(listed is not None and len(listed) == 1 and listed[0][2] is True and
          'last space' in listed[0][3],
          f'settings lists one space whose delete button is disabled ({listed})')

    # Add a space in settings and move it to the top: any space can be first.
    click(ctx, settings_point(ctx, b, sp, '#addSpace'), settle_time=1.5)
    listed = wait_for(lambda: (lambda l: l if l and len(l) == 2 else None)(settings_spaces(sp)), 5)
    check(listed is not None and listed[0][2] is False and listed[1][2] is False and
          listed[0][4] is True and listed[1][4] is False,
          f'with two spaces both can be deleted and the second can move up ({listed})')
    if listed:
        moved = listed[1][0]
        click(ctx, settings_point(ctx, b, sp, f'.space-row[data-id="{moved}"] .move-up'),
              settle_time=1.5)
        after = settings_spaces(sp)
        check(after and after[0][0] == moved, 'the second space moved to the top')
        order = wait_for(lambda: (lambda o: o if o and o[0][0] == moved else None)(
            m4.orders(b)), 5)
        check(bool(order), 'and the sidebar row follows')
    m4.save_shot(ctx, 'settings-spaces.png')
    stop(ctx, b)


def phase_old_profile(ctx):
    print('== old-profile: a profile from the build before M5 starts cleanly', flush=True)
    if not os.path.exists(FIXTURE):
        check(False, f'fixture {FIXTURE} exists')
        return
    profile = tempfile.mkdtemp(prefix='neon-m5-old-')
    ctx.dirs.append(profile)
    with tarfile.open(FIXTURE) as tar:
        tar.extractall(profile, filter='data')
    try:
        server = serve(FIXTURE_PORT)
    except OSError as e:
        check(False, f'port {FIXTURE_PORT} for the fixture\'s pages is free ({e})')
        return
    site = f'http://127.0.0.1:{FIXTURE_PORT}'
    try:
        before_default = test_cookie_names(default_cookies(profile))
        check(before_default == ['who'], f'the fixture\'s default partition holds who=D only '
              f'({before_default})')
        b = start(ctx, 'old', None, profile=profile)
        settle(3)
        pages = {}
        for part in ('n=d1', 'n=d2', 'n=a1', 'chrome://settings', 'about:blank'):
            t = wait_for(lambda: [t for t in b.pages() if part in t['url']], 20)
            pages[part] = t[0]['targetId'] if t else None
        check(all(pages.values()), f'every tab of the old profile is open again '
              f'({[k for k, v in pages.items() if not v]} missing)')
        for part in ('n=d1', 'n=d2'):
            if not pages[part]:
                continue
            b.session.call('Target.activateTarget', {'targetId': pages[part]})
            p = loaded(b, part)
            c = cookies(p) if p else None
            check(c is not None and 'who' not in c and f'v_{part[2:]}' in c,
                  f'the default space\'s page {part} is reopened in a container, signed out '
                  f'({c})')
        if pages['n=a1']:
            b.session.call('Target.activateTarget', {'targetId': pages['n=a1']})
            p = loaded(b, 'n=a1')
            check(p is not None and who(p) == 'A', 'space A\'s page keeps its container (who=A)')
        rows = m4.rows(b, timeout=5)
        check(len(rows) == 1 and len(rows[0]) == 2, f'the row shows two spaces '
              f'({[len(r) for r in rows]})')
        if pages['chrome://settings']:
            b.session.call('Target.activateTarget', {'targetId': pages['chrome://settings']})
            settle(1)
            type_url(ctx, f'{site}/page?n=oset')
            p = loaded(b, 'n=oset')
            check(p is not None and 'who' not in (cookies(p) or {'who': 1}),
                  'the settings tab is reopened in the first space\'s container')
        stop(ctx, b)
        spaces, _ = spaces_in_prefs(profile)
        names = [s.get('name') for s in spaces]
        check(names == ['Personal', 'A'] and all(s.get('container_id') for s in spaces) and
              all(s.get('id') != 'default' for s in spaces),
              f'the spaces are Personal (the old default space) and A, both with containers '
              f'({[(s.get("id"), s.get("name"), s.get("container_id")) for s in spaces]})')
        after_default = test_cookie_names(default_cookies(profile))
        check(after_default == ['who'], f'nothing was added to the default partition '
              f'({after_default})')
        personal = spaces[0].get('container_id') if spaces else ''
        mine = test_cookie_names(container_cookies(profile, personal))
        check('who' not in mine and {'v_d1', 'v_d2'} <= set(mine),
              f'Personal\'s container got the reopened pages\' cookies and no copy of who=D '
              f'({mine})')

        # A second start changes nothing.
        b = start(ctx, 'old', None, profile=profile)
        settle(3)
        urls = sorted(t['url'] for t in b.pages() if t['url'].startswith('http'))
        check(sum('n=d1' in u for u in urls) == 1 and sum('n=d2' in u for u in urls) == 1,
              f'a second start reopens nothing twice ({urls})')
        t = [t for t in b.pages() if 'n=d1' in t['url']]
        if t:
            b.session.call('Target.activateTarget', {'targetId': t[0]['targetId']})
            p = loaded(b, 'n=d1')
            check(p is not None and 'who' not in (cookies(p) or {'who': 1}),
                  'and the reopened page restores in its container')
        stop(ctx, b)
    finally:
        server.shutdown()
        server.server_close()


def phase_extensions(ctx):
    print('== extensions: extension pages are global, extension tabs land in a space',
          flush=True)
    prefs = m2.prefs_for([m2.SPACE_A, m2.SPACE_B])
    b = start_free_size(ctx, 'ext', prefs, *EXT_ARGS, page_url(ctx, 'ea1', 'e60000'))
    profile = b.profile_dir
    a1 = loaded(b, 'n=ea1')
    if not check(a1 is not None, 'the first page opens'):
        return stop(ctx, b)
    set_who(a1, 'A')
    check(who(a1) == 'A', 'it is in space A, the first space')
    b.remote('--container=Beta', page_url(ctx, 'eb1', '0000e6'))
    b1 = loaded(b, 'n=eb1')
    set_who(b1, 'B')
    focus_main(ctx)
    key(ctx, 'alt+shift+1')
    href, _ = m2.wait_active(b, lambda h: h and 'n=ea1' in h)
    check(href and 'n=ea1' in href, 'space A is visible again')
    sw = ext_worker(b)
    main_target = a1.target_id
    main_bounds = window_bounds(b, main_target)

    # windows.create with type popup: global, centred, default partition.
    before = target_ids(b)
    sw.eval(f'chrome.windows.create({{type: "popup", url: "{page_url(ctx, "g1")}", '
            'width: 520, height: 420}).then(() => 1)')
    g1 = loaded(b, 'n=g1', before)
    check(g1 is not None and who(g1) == '', 'an extension\'s popup window opens in the default '
          'partition (no space cookie)')
    if g1:
        set_who(g1, 'G')
        ws = windows(sw)
        mine = [w for w in ws if any('n=g1' in (u or '') for u in w['tabs'])]
        check(len(mine) == 1 and mine[0]['type'] == 'popup', f'it is a popup window ({mine})')
        gb = window_bounds(b, g1.target_id)
        check(centred(gb, main_bounds) and gb[2] < main_bounds[2],
              f'centred on the browser window ({window_bounds(b, g1.target_id)} in '
              f'{main_bounds})')
        check(who(a1) == 'A', 'the space\'s tab still sees only its own cookie')
        g1.eval('window.close(); 1')
        settle(1)

    # The options page: global window, opened once.
    before = target_ids(b)
    sw.eval('chrome.runtime.openOptionsPage().then(() => 1)')
    opt = wait_for(lambda: [t for t in b.pages() if t['url'].endswith('/options.html') and
                            t['targetId'] not in before], 10)
    check(bool(opt), 'runtime.openOptionsPage opens the options page')
    if opt:
        settle(1)
        ws = windows(sw)
        mine = [w for w in ws if any((u or '').endswith('/options.html') for u in w['tabs'])]
        check(len(mine) == 1 and mine[0]['type'] == 'popup' and len(mine[0]['tabs']) == 1,
              f'in a popup window of its own, outside any space ({mine})')
        normal = [u for w in ws if w['type'] == 'normal' for u in w['tabs']]
        check(not any((u or '').startswith('chrome-extension://') for u in normal),
              'no extension page is a tab of the browser window')
        ob = window_bounds(b, opt[0]['targetId'])
        check(centred(ob, main_bounds) and ob[2] < main_bounds[2] and ob[3] < main_bounds[3],
              f'the window is smaller than the browser window and centred on it ({ob} in '
              f'{main_bounds})')
        op = Page(b, opt[0]['targetId'])
        echo = op.eval(f'fetch("{ctx.site}/echo", {{credentials: "include"}}).then(r => r.text())')
        check('who=G' in (echo or ''), f'the options page uses the default partition '
              f'(its request carries {echo!r})')
        m4.save_shot(ctx, 'global-window.png')
        sw.eval('chrome.runtime.openOptionsPage().then(() => 1)')
        settle(1.5)
        n = sum(t['url'].endswith('/options.html') for t in b.pages())
        check(n == 1, f'opening the options page again brings back that window ({n} open)')
        before = target_ids(b)
        sw.eval('chrome.tabs.create({url: chrome.runtime.getURL("options.html")}).then(() => 1)')
        t2 = wait_for(lambda: [t for t in b.pages() if t['url'].endswith('/options.html') and
                               t['targetId'] not in before], 10)
        settle(1.5)
        ws = windows(sw)
        popups = [w for w in ws if w['type'] == 'popup' and
                  any((u or '').endswith('/options.html') for u in w['tabs'])]
        check(bool(t2) and len(popups) == 2,
              f'tabs.create of an extension page also opens a global window ({len(popups)})')
        for w in popups:
            sw.eval(f'chrome.windows.remove({w["id"]}).then(() => 1)')
        settle(1)
        # windows.create of a normal window on an extension page: the page still ends up in a
        # global window, and no empty browser window is left behind.
        normal_before = sum(w['type'] == 'normal' for w in windows(sw))
        sw.eval('chrome.windows.create({url: chrome.runtime.getURL("options.html")})'
                '.then(() => 1)')
        settle(3)
        ws = windows(sw)
        popups = [w for w in ws if w['type'] == 'popup' and
                  any((u or '').endswith('/options.html') for u in w['tabs'])]
        normal = [w for w in ws if w['type'] == 'normal']
        check(len(popups) == 1 and len(normal) == normal_before and
              all(w['tabs'] for w in normal),
              f'windows.create (normal) of an extension page ends in a global window, with no '
              f'empty browser window left ({[(w["type"], w["tabs"]) for w in ws]})')
        for w in popups:
            sw.eval(f'chrome.windows.remove({w["id"]}).then(() => 1)')
        settle(1)

    # tabs.create of a web page: the visible space; with openerTabId: the opener's space.
    focus_main(ctx)
    before = target_ids(b)
    sw.eval(f'chrome.tabs.create({{url: "{page_url(ctx, "x1")}"}}).then(() => 1)')
    x1 = loaded(b, 'n=x1', before)
    check(x1 is not None and who(x1) == 'A', f'tabs.create lands in the visible space A '
          f'({who(x1) if x1 else None})')
    btab = sw.eval(f'chrome.tabs.query({{}}).then(ts => ts.find(t => (t.url || "")'
                   f'.includes("n=eb1")).id)')
    before = target_ids(b)
    sw.eval(f'chrome.tabs.create({{url: "{page_url(ctx, "x2")}", openerTabId: {btab}}})'
            '.then(() => 1)')
    x2 = loaded(b, 'n=x2', before)
    check(x2 is not None and who(x2) == 'B', f'tabs.create with an opener in space B lands in '
          f'B ({who(x2) if x2 else None})')
    vis = wait_for(lambda: x2.eval('document.visibilityState') == 'visible', 5) if x2 else None
    check(bool(vis), 'and the window shows it')
    key(ctx, 'alt+shift+1')

    # E1: a content script and the background, for a page of space A.
    a1.eval('location.reload(); 1')
    settle(2)
    a1 = loaded(b, 'n=ea1')
    cs = wait_for(lambda: a1.eval('[document.documentElement.dataset.neonCsCookie, '
                                  'document.documentElement.dataset.neonCsFetch]')
                  if a1.eval('!!document.documentElement.dataset.neonCsFetch') else None, 10)
    check(cs is not None and 'who=A' in (cs[0] or ''),
          f'E1: a content script reads the container\'s cookies with document.cookie ({cs})')
    check(cs is not None and 'who=A' in (cs[1] or ''),
          f'E1: a content script\'s fetch to the page\'s site carries the container\'s cookies')
    bg = sw.eval(f'fetch("{ctx.site}/echo", {{credentials: "include"}}).then(r => r.text())')
    check('who=G' in (bg or '') and 'who=A' not in (bg or ''),
          f'E1: the background\'s fetch carries the default partition\'s cookies, not the '
          f'space\'s ({bg!r})')
    seen = sw.eval('chrome.tabs.query({}).then(ts => ts.map(t => t.url || ""))')
    check(any('n=ea1' in u for u in seen) and any('n=eb1' in u for u in seen),
          'E1: the background sees the tabs of every space (chrome.tabs)')

    # identity.launchWebAuthFlow keeps its own window in the default partition.
    before = target_ids(b)
    sw.eval(f'(chrome.identity.launchWebAuthFlow({{url: "{page_url(ctx, "auth")}", '
            'interactive: true}).catch(() => 0), 1)')
    auth = loaded(b, 'n=auth', before)
    if check(auth is not None, 'launchWebAuthFlow opens its page'):
        check(who(auth) == 'G', f'launchWebAuthFlow\'s window uses the default partition '
              f'({who(auth)!r})')
        ws = windows(sw)
        normal = [u for w in ws if w['type'] == 'normal' for u in w['tabs']]
        check(not any('n=auth' in (u or '') for u in normal),
              'and it is not a tab of the browser window')
        auth.eval('window.close(); 1')
    stop(ctx, b)

    default = test_cookie_names(default_cookies(profile))
    check(set(default) <= {'who', 'v_g1', 'v_auth'},
          f'on disk, the default partition holds only the global pages\' cookies ({default})')


def phase_flags_off(ctx):
    print('== flags-off: without containers the browser is stock Helium', flush=True)
    prefs = {'session': {'restore_on_startup': 1}, 'helium': {'browser': {'layout': 2}}}
    b = start(ctx, 'off', prefs, '--disable-features=Containers,'
              'DisableLoadExtensionCommandLineSwitch', f'--load-extension={EXTENSION}',
              page_url(ctx, 'off1'))
    profile = b.profile_dir
    p = loaded(b, 'n=off1')
    check(p is not None, 'a page opens')
    if p:
        set_who(p, 'S')
    check(not m4.rows(b, timeout=2), 'no switcher row')
    sw = ext_worker(b)
    sw.eval('chrome.runtime.openOptionsPage().then(() => 1)')
    settle(2)
    ws = windows(sw)
    normal = [u for w in ws if w['type'] == 'normal' for u in w['tabs']]
    check(any((u or '').endswith('/options.html') for u in normal),
          'the options page opens as a tab of the browser window')
    stop(ctx, b)
    spaces, containers = spaces_in_prefs(profile)
    check(spaces == [] and containers == [], f'no space or container was created '
          f'({spaces}, {containers})')
    default = test_cookie_names(default_cookies(profile))
    check('who' in default and 'v_off1' in default, f'the page used the default partition '
          f'({default})')


PHASES = [('first-run', phase_first_run), ('manage', phase_manage),
          ('old-profile', phase_old_profile), ('extensions', phase_extensions),
          ('flags-off', phase_flags_off)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--helium', default=os.environ.get('NEON_HELIUM', m1.DEFAULT_HELIUM))
    ap.add_argument('--keep', action='store_true', help='keep the profiles for inspection')
    ap.add_argument('--shots', default=os.path.join(ROOT, 'build', 'm5'),
                    help='where the screenshots go (default: build/m5/)')
    ap.add_argument('--only', action='append', default=[],
                    help='run only these phases: ' + ', '.join(n for n, _ in PHASES))
    ap.add_argument('--browser-arg', action='append', default=[],
                    help='extra browser switch for every launch')
    args = ap.parse_args()
    Browser.extra_args = args.browser_arg
    if not os.access(args.helium, os.X_OK):
        raise SystemExit(f'browser binary not found: {args.helium}')

    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(args.shots, exist_ok=True)
    log_path = os.path.join(LOG_DIR, 'all_containers_m5.log')
    ctx = Ctx()
    ctx.helium, ctx.dirs, ctx.browsers = args.helium, [], []
    ctx.log = open(log_path, 'w')
    ctx.shots = args.shots
    ctx.tmp = tempfile.mkdtemp(prefix='neon-m5-shots-')
    ctx.dirs.append(ctx.tmp)

    server = serve()
    ctx.site = f'http://127.0.0.1:{server.server_address[1]}'
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
