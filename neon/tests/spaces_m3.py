#!/usr/bin/env python3
"""M3 spaces check: the sidebar pager. Pages slide between spaces on a command, a click in the
switcher row or a two-finger swipe, with both tab lists live during the move. Drives the real
Linux dev build with real X input (keys, mouse clicks, touchpad scrolls) and checks frames grabbed
in the middle of each move.

Run from the neon-linux checkout (one command, from the host):

    neon/dev-container.sh sh 'python3 helium-chromium/neon/tests/spaces_m3.py'

or inside `neon/dev-container.sh sh`:

    python3 helium-chromium/neon/tests/spaces_m3.py [--keep] [--helium PATH] [--browser-arg ARG]
                                                    [--shots DIR] [--only PHASE] [--tabs N]

Exit status is 0 when every check passes and 1 otherwise.

The X server. Xvfb cannot send touchpad scrolls (see x11_touchpad.py), so the test runs its own
Xorg with the dummy video driver and xserver's inputtest driver, which adds an XInput device of
type TOUCHPAD. Chromium turns its scroll valuator deltas into ui::ScrollEvent kScroll, the events a
real touchpad gives it on X11. The end of a gesture is sent the way xf86-input-libinput sends it, a
zero scroll that Chromium turns into a fling start, or left out to exercise the quiet-period end.
Keys and clicks go through XTEST with xdotool.

What is observed, without test hooks in the browser:

- Every test page has a solid 16x16 favicon in its space's colour (the first space D green, A red, B blue).
  Frames of the sidebar are grabbed every half millisecond or so while a move runs; the favicons
  of each colour give each page's position: all tabs of a page share one x, the two pages are one
  page width apart, and each tab sits at its row's height.
- A move has ended at rest when only the target's colour is left, at the favicon x of a page at
  rest, and the tab list is pixel for pixel the one grabbed when that space was last at rest.
- The visible space is the space of the page whose document.visibilityState is "visible".
- Timings come from the frames and from the pager's histograms (Neon.Spaces.Pager.*), read over
  the DevTools protocol with Browser.getHistograms.

Phases:

1. pager: three spaces of N tabs each (default 30). Switcher row clicks, Ctrl+Alt+Up/Down,
   Alt+Shift+N, a jump over a space, two switches in a row, a two-finger swipe that is held,
   released past the threshold, released short, flung, pushed past the first and last space,
   vertical and mostly vertical scrolls, a sideways mouse wheel, and per-space scroll positions.
2. instant: with reduced motion a switch shows no intermediate frame.
3. modes: collapsed (the row becomes one icon with a menu), right-aligned, zen mode, RTL UI,
   expand on hover, and a resized sidebar, each with an animated switch.
4. drag: dragging a tab within the visible space reorders it, and dropping it past the end of the
   space keeps it in the space. Shift-click range selection and Ctrl+Shift+PgDn stay in the space.
5. stock: with a single space the sidebar is pixel for pixel M2's stock sidebar.

Needs python3-websocket, python3-pil, xdotool, Xorg with the dummy and inputtest drivers (all in
the dev image).
"""
import argparse
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import containers_m1 as m1  # noqa: E402
import spaces_m2 as m2  # noqa: E402  (also gives Session.call a deadline)
import x11_touchpad  # noqa: E402
from containers_m1 import Browser, Page, check  # noqa: E402

from PIL import Image, ImageChops, ImageDraw  # noqa: E402

ROOT = m1.ROOT
LOG_DIR = m1.LOG_DIR
failures = m1.failures

DEFAULT, SPACE_A, SPACE_B = m2.DEFAULT, m2.SPACE_A, m2.SPACE_B
COLORS = dict(m2.COLORS)
COLORS['Y'] = (230, 200, 0)   # a marked tab for the drag checks
HEX = {k: '%02x%02x%02x' % v for k, v in COLORS.items()}
SPACE_COLOR = {DEFAULT['id']: 'D', SPACE_A['id']: 'A', SPACE_B['id']: 'B'}
ORDER = ['D', 'A', 'B']

SCREEN = (1280, 900)
REGION = (0, 30, 420, 870)   # x, y, w, h of the grabs: the sidebar and a margin of page
IDC_WINDOW_PIN_TAB = 35013


# --- Frames --------------------------------------------------------------------------------------

def squares(img, rgb, size=16, tol=12):
    """Top-left corners of solid size x size squares of |rgb| (favicons) in |img|."""
    r, g, b = img.split()
    def band(ch, c):
        return ch.point(lambda v: 255 if abs(v - c) < tol else 0)
    mask = ImageChops.multiply(ImageChops.multiply(band(r, rgb[0]), band(g, rgb[1])),
                               band(b, rgb[2]))
    w, h = mask.size
    data = mask.tobytes()
    run = b'\xff' * size
    runs = []
    for y in range(h):
        row = data[y * w:(y + 1) * w]
        xs = set()
        start = row.find(run)
        while start != -1:
            end = start
            while end < w and row[end] == 255:
                end += 1
            if end - start == size:
                xs.add(start)
            start = row.find(run, end)
        runs.append(xs)
    found = []
    for y in range(h - size + 1):
        for x in runs[y]:
            if y > 0 and x in runs[y - 1]:
                continue
            if all(x in runs[y + k] for k in range(1, size)) and \
                    (y + size >= h or x not in runs[y + size]):
                found.append((x, y))
    return sorted(found, key=lambda p: (p[1], p[0]))


def page_positions(img, space):
    """(xs, ys) of the favicons of |space| in a grab."""
    sq = squares(img, COLORS[space])
    return sorted({x for x, _ in sq}), sorted(y for _, y in sq)


class Capture:
    """Grabs REGION as fast as it can on a thread, keeping the frames that differ."""

    def __init__(self, ctx, region=None):
        self.ctx, self.region = ctx, region or ctx.region
        self.frames = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        g = x11_touchpad.Grabber(self.ctx.display)
        last = None
        t0 = time.monotonic()
        while not self.stop.is_set():
            im = g.grab(*self.region)
            raw = im.tobytes()
            if raw != last:
                self.frames.append((time.monotonic() - t0, im))
                last = raw
        g.close()

    def __enter__(self):
        self.thread.start()
        time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()


def capture(ctx, action, duration=0.8, region=None):
    with Capture(ctx, region) as cap:
        t = time.monotonic()
        action()
        time.sleep(max(0.0, duration - (time.monotonic() - t)))
    return cap.frames


def contact_sheet(frames, path, crop=None, every=1, limit=14, label=True):
    """Frames side by side, with their times in milliseconds."""
    picked = frames[::every][:limit]
    if not picked:
        return None
    t0 = picked[0][0]
    ims = [im.crop(crop) if crop else im for _, im in picked]
    w, h = ims[0].size
    sheet = Image.new('RGB', (len(ims) * (w + 4), h + 18), 'white')
    draw = ImageDraw.Draw(sheet)
    for i, (t, _) in enumerate(picked):
        sheet.paste(ims[i], (i * (w + 4), 18))
        if label:
            draw.text((i * (w + 4) + 3, 3), f'{(t - t0) * 1000:.0f} ms', fill=(0, 0, 0))
    sheet.save(path)
    return path


# --- Browser helpers -----------------------------------------------------------------------------

class Ctx:
    pass


def url(ctx, name, space, color=None):
    return f'{ctx.site}/page?n={name}&c={HEX[color or space]}'


def prefs_for(spaces, **browser):
    prefs = m2.prefs_for(spaces)
    prefs['helium']['browser'].update(browser)
    prefs['helium']['browser']['custom_accelerators'] = dict(
        m2.CUSTOM_ACCELERATORS, **{str(IDC_WINDOW_PIN_TAB): {'added': ['Control+Alt+KeyP']}})
    return prefs


def start(ctx, name, prefs, *args, extra=()):
    profile = tempfile.mkdtemp(prefix=f'neon-m3-{name}-')
    ctx.dirs.append(profile)
    m1.write_prefs(profile, prefs)
    b = Browser(ctx.helium, profile, ctx.display, m1.free_port(), ctx.log)
    saved = Browser.extra_args
    Browser.extra_args = list(saved) + list(extra)
    ctx.browsers.append(b)
    try:
        b.start(*args)
    finally:
        Browser.extra_args = saved
    b.extra = list(extra)
    b.ctx = ctx
    return b


def xdo(ctx, *args):
    return m2.xdo(ctx, *args)


def move(ctx, x, y):
    """Moves the pointer. (`mousemove --sync` never returns when the pointer is already there.)"""
    xdo(ctx, 'mousemove', str(x), str(y))
    time.sleep(0.05)


def press(ctx, combo):
    xdo(ctx, 'key', '--clearmodifiers', combo)


def focus(ctx):
    xdo(ctx, 'windowfocus', '--sync', ctx.window)


def active_name(b, timeout=10):
    """The active tab's title (test pages are titled by name), from the window title. Asking
    every page for its visibility takes seconds with many frozen background tabs."""
    ctx = b.ctx
    r = xdo(ctx, 'getwindowname', ctx.window)
    # RTL titles carry direction marks.
    title = ''.join(c for c in r.stdout.strip()
                    if c not in '\u200e\u200f\u202a\u202b\u202c\u202d\u202e'
                    '\u2066\u2067\u2068\u2069')
    return title.rsplit(' - ', 1)[0] if ' - ' in title else title


def wait_active(b, pred, timeout=10):
    deadline = time.time() + timeout
    name = None
    while time.time() < deadline:
        name = active_name(b)
        if pred(name):
            return name
        time.sleep(0.2)
    return name


def open_tabs(ctx, b, space, names, color=None):
    """Opens tabs in the visible space over the DevTools protocol (no opener: the visible
    space's container, as M2 checks for tabs from other apps and extensions)."""
    for n in names:
        b.session.call('Target.createTarget', {'url': url(ctx, n, space, color),
                                               'background': True})
    deadline = time.time() + 60
    want = set(names)
    while time.time() < deadline:
        have = {m2.name_of(t['url']) for t in b.pages()}
        if want <= have:
            return
        time.sleep(0.2)
    raise TimeoutError(f'tabs {sorted(want - have)} did not open')


def histograms(b):
    out = {}
    for h in b.session.call('Browser.getHistograms', {'query': 'Neon.Spaces'})['histograms']:
        buckets = h.get('buckets', [])
        out[h['name']] = {'count': h['count'], 'sum': h['sum'],
                          'buckets': [(x['low'], x.get('high'), x['count']) for x in buckets]}
    return out


def settle(t=0.5):
    time.sleep(t)


# --- Move checks ---------------------------------------------------------------------------------

PILL = (237, 237, 237)   # the active tab's background, also the row highlight


def runs(img, y, rgb=PILL):
    """Horizontal runs [a, b) of |rgb| in row |y|."""
    w = img.size[0]
    row = img.crop((0, y, w, y + 1)).tobytes()
    want = bytes(rgb)
    out, x = [], 0
    while x < w:
        if row[3 * x:3 * x + 3] == want:
            a = x
            while x < w and row[3 * x:3 * x + 3] == want:
                x += 1
            out.append((a, x))
        else:
            x += 1
    return out


class Rest:
    """A space's page at rest: favicon column and rows, its active tab's bar, and the pixels of
    the tab list."""

    def __init__(self, img, space, list_box):
        self.img = img
        xs, self.ys = page_positions(img, space)
        self.x = xs[0] if len(xs) == 1 else None
        self.list_box = list_box
        # The active tab's bar: a run of the active colour a few pixels below its top edge
        # (clear of the rounded corners and of the tab's contents).
        self.bar = None
        for y in range(max(10, list_box[1]), list_box[3] - 3):
            if [(a, b) for a, b in runs(img, y) if b - a > 20]:
                below = [(a, b) for a, b in runs(img, y + 3) if b - a > 20]
                self.bar = (y + 3, *below[0]) if below else None
                break

    def same_list(self, img):
        # Leaves out the overlay scrollbar, which fades in and out on its own.
        x0, y0, x1, y1 = self.list_box
        if self.bar:
            x1 = min(x1, self.bar[2] - 6)
            x0 = max(x0, self.bar[1] - 8)
        elif self.x is not None:
            x1 = min(x1, self.x + 175)
        a = self.img.crop((x0, y0, x1, y1))
        b = img.crop((x0, y0, x1, y1))
        diff = ImageChops.difference(a, b).getbbox()
        return diff is None, diff


def page_offset(img, rest, side):
    """How far the page of |rest| is moved in |img|, from its active tab's bar; |side| -1 picks
    the leftmost bar on that row, +1 the rightmost. None if the bar is not on screen."""
    if not rest or not rest.bar:
        return None
    y, left, right = rest.bar
    found = [(a, b) for a, b in runs(img, y) if b - a > 4]
    if not found:
        return None
    a, b = found[0] if side < 0 else found[-1]
    if b - a == right - left or a > left:
        return a - left      # whole, or cut off on the right
    return b - right         # cut off on the left: its right end is whole


def measure_rest(ctx, space, what):
    settle(0.6)
    img = x11_touchpad.Grabber(ctx.display).grab(*ctx.region)
    rest = Rest(img, space, ctx.list_box)
    others = [s for s in ORDER if s != space and page_positions(img, s)[0]]
    check(rest.x is not None and rest.ys and rest.bar and not others,
          f'{what}: at rest only {space} tabs show, in one column '
          f'(x {page_positions(img, space)[0]}, {len(rest.ys)} tabs, bar {rest.bar}, '
          f'others {others})')
    ctx.rests[space] = rest
    return rest


def check_move(ctx, frames, src, dst, what, direction, expect_mid=True, sheet=None,
               allow_extra=()):
    """|expect_mid| None skips the mid-move checks (moves through a third space)."""
    """Checks the frames of a move from space |src| to space |dst|. |direction| is +1 when |dst|
    comes in from the right, -1 from the left. A frame is mid-move when both pages' active tab
    bars are on screen; the pages' offsets come from those bars."""
    rs, rd = ctx.rests.get(src), ctx.rests.get(dst)
    mids, widths, bad, extra, track = [], set(), [], [], []
    for t, im in frames:
        others = [s for s in ORDER if s not in (src, dst) + tuple(allow_extra)
                  and page_positions(im, s)[0]]
        if others:
            extra.append((round(t * 1000), others))
        if src == dst:
            continue
        same_row = rs and rd and rs.bar and rd.bar and rs.bar[0] == rd.bar[0]
        if same_row and len([r for r in runs(im, rs.bar[0]) if r[1] - r[0] > 4]) < 2:
            continue   # one bar on the shared row: whose it is cannot be told
        o_s = page_offset(im, rs, -direction if same_row else -1)
        o_d = page_offset(im, rd, direction if same_row else -1)
        if o_s is None or o_d is None or o_s == o_d:
            continue
        mids.append(t)
        widths.add(o_d - o_s)
        track.append(o_s)
        # Whole favicons sit in their page's column, at their rows.
        for space, rest, off in ((src, rs, o_s), (dst, rd, o_d)):
            xs, ys = page_positions(im, space)
            if xs and (xs != [rest.x + off] or not set(ys) <= set(rest.ys)):
                bad.append((round(t * 1000), space, xs, rest.x + off,
                            sorted(set(ys) - set(rest.ys))[:3]))
    t_last, last = frames[-1]
    xs_l, ys_l = page_positions(last, dst)
    if sheet:
        moving = [i for i, (t, _) in enumerate(frames) if t in mids]
        first = moving[0] if moving else 0
        shown = frames[max(0, first - 1):]
        step = max(1, len(shown) // 12)
        contact_sheet(shown[::step][:12] + [frames[-1]], sheet, crop=ctx.sheet_crop)
    if expect_mid:
        check(len(mids) >= 2, f'{what}: both pages show, live, in {len(mids)} frames mid-move '
                              f'(of {len(frames)})')
        check(not bad, f'{what}: in every mid-move frame the tabs sit in their page\'s column '
                       f'and at their rows {bad[:3]}')
        check(len(widths) == 1 and all(direction * w > 0 for w in widths),
              f'{what}: the pages stay one page width apart and {dst} comes in from the '
              f'{"right" if direction > 0 else "left"} (gaps {sorted(widths)})')
        steps = [b - a for a, b in zip(track, track[1:])]
        check(all(-direction * s >= 0 for s in steps),
              f'{what}: the pages move one way only ({track[:16]})')
    elif expect_mid is False and src != dst:
        check(not mids, f'{what}: no frame shows both pages ({len(mids)} did)')
    check(not extra, f'{what}: no other space shows during the move {extra[:3]}')
    same, diff = rd.same_list(last) if rd else (False, None)
    at_rest = rd is not None and xs_l == [rd.x] and not (
        src != dst and page_positions(last, src)[0])
    check(at_rest and same, f'{what}: ends exactly at rest on {dst} (x {xs_l}, '
                            f'tab list identical to {dst} at rest: {same}, diff box {diff})')
    return {'mid_frames': len(mids), 'frames': len(frames), 'width': sorted(widths),
            'start_ms': round(mids[0] * 1000) if mids else None,
            'end_ms': round(mids[-1] * 1000) if mids else None,
            'mid_times_ms': [round(t * 1000, 1) for t in mids]}


# --- Phase: pager --------------------------------------------------------------------------------

def switch_to_new_space(ctx, b, combo):
    """Shows an empty space (it opens a new tab page), retrying the key once."""
    for _ in range(3):
        focus(ctx)
        press(ctx, combo)
        if wait_active(b, lambda nm: nm == 'New Tab', timeout=5) == 'New Tab':
            settle(0.3)
            return
    raise RuntimeError(f'{combo} did not show the empty space')


def type_url(ctx, address):
    """Types |address| into the omnibox of the active tab (a new tab page) and loads it."""
    press(ctx, 'ctrl+l')
    xdo(ctx, 'type', '--delay', '5', address)
    press(ctx, 'Return')


def new_tab_page(b):
    for t in b.pages():
        if t['url'].startswith('chrome://new'):
            return Page(b, t['targetId'])
    raise RuntimeError('no new tab page')


def select_last(ctx, b, name):
    """Ctrl+9, and waits until the tab list has scrolled to it."""
    press(ctx, 'ctrl+9')
    wait_active(b, lambda nm: nm == name)
    settle(0.5)


def find_window(ctx, title, timeout=20):
    """The browser window whose active tab is |title|. Searching by class alone can find the
    window of a browser that is closing, or a menu."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = xdo(ctx, 'search', '--onlyvisible', '--name', title)
        ids = [w for w in r.stdout.split() if w]
        if ids:
            return ids[-1]
        time.sleep(0.2)
    raise RuntimeError(f'no window titled {title}')


def setup_spaces(ctx, b, n):
    """Opens n tabs in each of D, A, B; leaves A visible."""
    m1.find_page(b, 'n=a1').wait_loaded('n=a1')
    ctx.window = find_window(ctx, 'a1')
    focus(ctx)
    open_tabs(ctx, b, 'A', [f'a{i}' for i in range(2, n + 1)])
    select_last(ctx, b, f'a{n}')
    switch_to_new_space(ctx, b, 'alt+shift+3')
    ntp = [t['targetId'] for t in b.pages() if t['url'].startswith('chrome://new')]
    open_tabs(ctx, b, 'B', [f'b{i}' for i in range(1, n + 1)])
    for target in ntp:
        b.session.call('Target.closeTarget', {'targetId': target})
    wait_active(b, lambda nm: nm and nm.startswith('b'))
    select_last(ctx, b, f'b{n}')
    switch_to_new_space(ctx, b, 'alt+shift+1')
    ntp = [t['targetId'] for t in b.pages() if t['url'].startswith('chrome://new')]
    open_tabs(ctx, b, 'D', [f'd{i}' for i in range(1, n + 1)])
    for target in ntp:
        b.session.call('Target.closeTarget', {'targetId': target})
    wait_active(b, lambda nm: nm and nm.startswith('d'))
    select_last(ctx, b, f'd{n}')
    press(ctx, 'alt+shift+2')
    settle(1.5)


CHIPS = {'A': [(255, 0, 0), (255, 115, 115)], 'B': [(0, 0, 255), (115, 115, 255)]}


def find_row_icon(img, space, top=None):
    """Centre of a space's icon in the switcher row (A and B by their colours, D as the outlined
    icon left of A's), or None."""
    w, h = img.size
    top = h - 140 if top is None else top
    px = img.load()
    if space == 'D':
        a = find_row_icon(img, 'A', top)
        if not a:
            return None
        pts = [(x, y) for y in range(a[1] - 9, a[1] + 10) for x in range(0, a[0] - 12)
               if sum(px[x, y]) < 600 and abs(px[x, y][0] - px[x, y][1]) < 12]
    else:
        pts = [(x, y) for y in range(top, h) for x in range(w) if px[x, y] in CHIPS[space]]
    if len(pts) < 20:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2


def highlight_center(img, ctx, icon):
    """x centre of the switcher row's highlight, in the row of |icon|."""
    if not icon:
        return None
    r = [(a, b) for a, b in runs(img, icon[1] - 12) if b - a >= 10]
    return (r[0][0] + r[-1][1]) / 2 if r else None


def phase_pager(ctx):
    print('== pager', flush=True)
    n = ctx.tabs
    b = start(ctx, 'pager', prefs_for([DEFAULT, SPACE_A, SPACE_B]), '--container=Alpha',
              url(ctx, 'a1', 'A'))
    ctx.b = b
    setup_spaces(ctx, b, n)
    move(ctx, '700', '600')
    check(active_name(b) == f'a{n}', f'A shows its last opened tab ({active_name(b)})')

    rest_a = measure_rest(ctx, 'A', 'space A')
    full = x11_touchpad.Grabber(ctx.display).grab(0, 0, *SCREEN)
    full.save(os.path.join(ctx.shots, 'm3-rest-space-a.png'))

    # 1. Click B's icon in the switcher row (real mouse click).
    icon_b = find_row_icon(rest_a.img, 'B')
    icon_a = find_row_icon(rest_a.img, 'A')
    icon_d = find_row_icon(rest_a.img, 'D')
    check(icon_a and icon_b and icon_d and icon_d[0] < icon_a[0] < icon_b[0],
          f'the switcher row shows D, A, B in order ({icon_d}, {icon_a}, {icon_b})')
    hl = highlight_center(rest_a.img, ctx, icon_a)
    check(hl is not None and icon_a and abs(hl - icon_a[0]) <= 2,
          f'the row highlights A at rest (highlight {hl}, A at {icon_a})')
    ox, oy = REGION[0], REGION[1]

    def click_icon(icon):
        move(ctx, str(icon[0] + ox), str(icon[1] + oy))
        xdo(ctx, 'click', '1')

    # B's rest is not known yet: switch once by click, measure, and come back by click.
    frames = capture(ctx, lambda: click_icon(icon_b), 0.9)
    move(ctx, '700', '600')
    rest_b = measure_rest(ctx, 'B', 'space B after a row click')
    check(active_name(b) == f'b{n}', f'clicking B in the row shows B ({active_name(b)})')
    x11_touchpad.Grabber(ctx.display).grab(0, 0, *SCREEN).save(
        os.path.join(ctx.shots, 'm3-rest-space-b.png'))
    hl = highlight_center(rest_b.img, ctx, icon_b)
    check(hl is not None and abs(hl - icon_b[0]) <= 2,
          f'the row highlights B after the switch (highlight {hl}, B at {icon_b})')
    r = check_move(ctx, frames, 'A', 'B', 'row click A to B', +1,
                   sheet=os.path.join(ctx.shots, 'm3-sheet-row-click.png'))
    # The highlight follows the pages.
    hls = [highlight_center(im, ctx, icon_a) for _, im in frames]
    hls = [h for h in hls if h is not None]
    between = [h for h in hls if icon_a[0] + 2 < h < icon_b[0] - 2]
    check(len(between) >= 2 and hls == sorted(hls),
          f'the row highlight slides from A to B with the pages ({len(between)} frames between)')
    ctx.report['row_click'] = r

    frames = capture(ctx, lambda: click_icon(icon_a), 0.9)
    check_move(ctx, frames, 'B', 'A', 'row click B to A', -1)
    check(active_name(b) == f'a{n}', f'clicking A shows A again ({active_name(b)})')

    # 2. Commands.
    move(ctx, '700', '600')
    focus(ctx)
    frames = capture(ctx, lambda: press(ctx, 'ctrl+alt+Up'), 0.9)
    measure_rest(ctx, 'D', 'space D after Ctrl+Alt+Up')
    check(active_name(b) == f'd{n}', f'Ctrl+Alt+Up shows D ({active_name(b)})')
    check_move(ctx, frames, 'A', 'D', 'Ctrl+Alt+Up A to D', -1,
               sheet=os.path.join(ctx.shots, 'm3-sheet-command.png'))
    frames = capture(ctx, lambda: press(ctx, 'ctrl+alt+Down'), 0.9)
    check_move(ctx, frames, 'D', 'A', 'Ctrl+Alt+Down D to A', +1)
    frames = capture(ctx, lambda: press(ctx, 'ctrl+alt+Down'), 0.9)
    r = check_move(ctx, frames, 'A', 'B', 'Ctrl+Alt+Down A to B', +1)
    ctx.report['command'] = r
    ctx.report['command_frames'] = [(round(t * 1000, 1), page_positions(im, 'A')[0],
                                     page_positions(im, 'B')[0]) for t, im in frames]
    frames = capture(ctx, lambda: press(ctx, 'ctrl+alt+Down'), 0.6)
    check(len(frames) <= 1, f'Ctrl+Alt+Down on the last space does nothing ({len(frames)} frames)')
    # A jump over a space slides straight to it.
    frames = capture(ctx, lambda: press(ctx, 'alt+shift+1'), 0.9)
    check_move(ctx, frames, 'B', 'D', 'Alt+Shift+1 jumps from B to D', -1)
    # Two switches in a row: the second starts from where the first is.
    def twice():
        press(ctx, 'ctrl+alt+Down')
        time.sleep(0.08)
        press(ctx, 'ctrl+alt+Down')
    frames = capture(ctx, twice, 1.2)
    check(active_name(b) == f'b{n}', f'two quick Ctrl+Alt+Down go D to B ({active_name(b)})')
    check_move(ctx, frames, 'D', 'B', 'interrupted D to A to B', +1, expect_mid=None,
               allow_extra=('A',), sheet=os.path.join(ctx.shots, 'm3-sheet-interrupted.png'))
    two = [t for t, im in frames if sum(bool(page_positions(im, s)[0]) for s in ORDER) > 2]
    check(not two, f'interrupted switch: never more than two pages on screen {two[:3]}')
    # Back to A for the swipes.
    press(ctx, 'ctrl+alt+Up')
    settle(0.8)

    phase_swipe(ctx, b)
    phase_wheel_and_scroll(ctx, b)

    ctx.report['histograms'] = histograms(b)
    b.close()


def phase_swipe(ctx, b):
    print('== swipe', flush=True)
    n = ctx.tabs
    tp = ctx.touchpad
    check(active_name(b) == f'a{n}', f'A is visible before the swipes ({active_name(b)})')
    # Fingers over the sidebar, on the space's name (no tab under the pointer to hover).
    move(ctx, '100', '45')
    settle(0.3)
    rest_a = ctx.rests['A']

    # Hold mid-swipe: the pages follow the fingers. scroll(+d) moves the content left by d.
    # This first swipe of the run ends without a lift event, as on X11 drivers that send none:
    # the tracker ends it after a quiet period. (Once a lift has ended a gesture, it waits for
    # lifts instead.)
    with Capture(ctx) as cap:
        tp.swipe(70, steps=14, interval=0.01, lift=False)
        for _ in range(12):
            tp.scroll(0.05, 0)
            time.sleep(0.03)
        held = cap.frames[-1]
        tp.swipe(30, steps=6, interval=0.01, lift=False)
        for _ in range(8):
            tp.scroll(0.05, 0)
            time.sleep(0.03)
        held2 = cap.frames[-1]
        tp.swipe(30, steps=6, interval=0.01, lift=False)
        time.sleep(0.8)
    frames = cap.frames
    t, img = held
    off_a = page_offset(img, rest_a, -1)
    off_b = page_offset(img, ctx.rests['B'], +1)
    check(off_a is not None and off_b is not None and off_b - off_a > 0,
          f'swipe held mid-way: A and B show side by side (offsets {off_a}, {off_b})')
    # 70 units of finger travel, less the 15 DIP axis lock.
    width = (ctx.report.get('command') or {}).get('width') or [None]
    def moved_at(frame):
        xs_b, _ = page_positions(frame[1], 'B')
        return width[0] - (xs_b[0] - ctx.rests['B'].x) if xs_b and width[0] else None
    m1_, m2_ = moved_at(held), moved_at(held2)
    ctx.report['swipe_held'] = {'moved_after_70': m1_, 'moved_after_100': m2_}
    check(m1_ is not None and m2_ is not None and abs((m2_ - m1_) - 30) <= 1 and
          off_b - off_a == width[0],
          f'swipe held mid-way: the pages follow the fingers one to one (moved {m1_} DIP after '
          f'70 units, {m2_} after 100; pages {off_b - off_a if None not in (off_a, off_b) else None}'
          f' apart)')
    img.save(os.path.join(ctx.shots, 'm3-swipe-held.png'))
    check(active_name(b) == f'b{n}', f'releasing past the threshold, with no lift event, shows B '
                                     f'after the quiet period ({active_name(b)})')
    r = check_move(ctx, frames, 'A', 'B', 'swipe A to B', +1,
                   sheet=os.path.join(ctx.shots, 'm3-sheet-swipe.png'))
    ctx.report['swipe'] = r

    # With a lift event (as xf86-input-libinput sends) the swipe ends at once.
    frames = capture(ctx, lambda: tp.swipe(-130, steps=20, interval=0.01), 0.9)
    check(active_name(b) == f'a{n}', f'a swipe ended by a lift shows A ({active_name(b)})')
    check_move(ctx, frames, 'B', 'A', 'swipe B to A with a lift', -1)
    frames = capture(ctx, lambda: tp.swipe(130, steps=20, interval=0.01), 0.9)
    check(active_name(b) == f'b{n}', f'and back to B ({active_name(b)})')

    # Release short of the threshold: snaps back.
    frames = capture(ctx, lambda: tp.swipe(-50, steps=10, interval=0.02), 1.0)
    check(active_name(b) == f'b{n}', f'a short swipe snaps back to B ({active_name(b)})')
    moved = [t for t, im in frames if page_offset(im, ctx.rests['A'], -1) is not None]
    check(bool(moved), f'the short swipe showed A while the fingers moved ({len(moved)} frames)')
    check_move(ctx, frames, 'B', 'B', 'short swipe snaps back', 0, expect_mid=False,
               allow_extra=('A',))

    # A quick flick turns the page whatever the distance.
    frames = capture(ctx, lambda: tp.swipe(-55, steps=5, interval=0.008), 1.0)
    check(active_name(b) == f'a{n}', f'a quick flick of 40 DIP turns to A ({active_name(b)})')

    # Past the first space the pages resist and nothing switches.
    press(ctx, 'alt+shift+1')
    settle(0.8)
    with Capture(ctx) as cap:
        tp.swipe(-120, steps=24, interval=0.01, lift=False)
        for _ in range(10):
            tp.scroll(-0.05, 0)
            time.sleep(0.03)
        img = cap.frames[-1][1]
        tp.lift()
        time.sleep(0.8)
    xs_d, _ = page_positions(img, 'D')
    rest_d = ctx.rests['D']
    pulled = xs_d[0] - rest_d.x if xs_d else None
    check(pulled is not None and 0 < pulled < 0.6 * 105,
          f'past the first space the pages move less than the fingers ({pulled} DIP for 105)')
    others = [s for s in ('A', 'B') if page_positions(img, s)[0]]
    check(not others, f'past the first space no other page shows ({others})')
    check(active_name(b) == f'd{n}', f'the first space stays visible ({active_name(b)})')
    check(ctx.rests['D'].same_list(cap.frames[-1][1])[0], 'and the pager is back at rest')
    img.save(os.path.join(ctx.shots, 'm3-swipe-rubber-band.png'))

    # Past the last space.
    press(ctx, 'alt+shift+3')
    settle(0.8)
    frames = capture(ctx, lambda: tp.swipe(120, steps=24, interval=0.01), 1.0)
    check(active_name(b) == f'b{n}', f'the last space stays visible ({active_name(b)})')
    check(ctx.rests['B'].same_list(frames[-1][1])[0], 'past the last space the pager returns to rest')

    press(ctx, 'alt+shift+2')
    settle(0.8)
    # Over the tab list for the vertical scrolls.
    move(ctx, '100', '400')

    # Vertical scrolling moves the tab list and never the pages.
    rest_a = ctx.rests['A']
    frames = capture(ctx, lambda: tp.swipe(0, -160, steps=16, interval=0.01), 0.8)
    last = frames[-1][1]
    xs, ys = page_positions(last, 'A')
    side = [t for t, im in frames if page_positions(im, 'B')[0] or page_positions(im, 'D')[0]]
    check(xs == [rest_a.x] and ys and not rest_a.same_list(last)[0] and not side,
          f'a vertical scroll scrolls the tab list (first row {rest_a.ys[0]} -> {ys[:1]}), '
          f'pages stay ({len(side)} frames showed another page)')
    # Mostly vertical with some sideways motion: still only the list moves.
    frames = capture(ctx, lambda: tp.swipe(60, -110, steps=16, interval=0.01), 0.8)
    side = [t for t, im in frames if page_positions(im, 'B')[0] or page_positions(im, 'D')[0]]
    xs, _ = page_positions(frames[-1][1], 'A')
    check(not side and xs == [rest_a.x],
          f'a mostly vertical gesture never moves the pages ({len(side)} frames)')
    check(active_name(b) == f'a{n}', f'and A stays visible ({active_name(b)})')


def phase_wheel_and_scroll(ctx, b):
    print('== wheel and scroll positions', flush=True)
    n = ctx.tabs
    tp = ctx.touchpad
    # A sideways notched wheel (XTEST buttons 6 and 7) turns one page per notch.
    move(ctx, '100', '45')
    rest_a = ctx.rests['A']
    ctx.rests['A'] = Rest(x11_touchpad.Grabber(ctx.display).grab(*ctx.region), 'A', ctx.list_box)
    frames = capture(ctx, lambda: xdo(ctx, 'click', '7'), 0.9)
    check(active_name(b) == f'b{n}', f'wheel right shows the next space ({active_name(b)})')
    # A is scrolled away from its active tab: follow B's page alone.
    offs = [page_offset(im, ctx.rests['B'], +1) for _, im in frames]
    moving = [o for o in offs if o is not None and 0 < o < 190]
    check(len(moving) >= 2 and moving == sorted(moving, reverse=True),
          f'the wheel notch slides B in from the right ({moving[:10]})')
    check(ctx.rests['B'].same_list(frames[-1][1])[0], 'and ends at rest on B')
    ctx.rests['A'] = rest_a
    xdo(ctx, 'click', '6')
    settle(0.8)
    check(active_name(b) == f'a{n}', f'wheel left shows the previous space ({active_name(b)})')

    # Per-space scroll position. A is scrolled (the vertical swipe above).
    img = x11_touchpad.Grabber(ctx.display).grab(*ctx.region)
    xs, ys_a = page_positions(img, 'A')
    check(not ctx.rests['A'].same_list(img)[0], 'A is scrolled away from where it was at rest')
    scrolled_a = Rest(img, 'A', ctx.list_box)
    ctx.rests['A'] = scrolled_a
    frames = capture(ctx, lambda: (focus(ctx), press(ctx, 'ctrl+alt+Down')), 0.9)
    _, ys_b = page_positions(frames[-1][1], 'B')
    check(ys_b == ctx.rests['B'].ys, 'B keeps its own scroll position (top)')
    frames = capture(ctx, lambda: press(ctx, 'ctrl+alt+Up'), 0.9)
    # A's active tab is scrolled out of view: the move is checked by where it ends.
    check_move(ctx, frames, 'B', 'A', 'back to the scrolled A', -1, expect_mid=None)
    _, ys = page_positions(frames[-1][1], 'A')
    check(scrolled_a.same_list(frames[-1][1])[0], 'A comes back at its scroll position')
    contact_sheet(frames[::max(1, len(frames) // 10)], os.path.join(ctx.shots,
                  'm3-sheet-scroll-memory.png'), crop=ctx.sheet_crop)


# --- Phase: instant ------------------------------------------------------------------------------

def phase_instant(ctx):
    print('== instant', flush=True)
    b = start(ctx, 'instant', prefs_for([DEFAULT, SPACE_A, SPACE_B]), '--container=Alpha',
              url(ctx, 'a1', 'A'), extra=['--force-prefers-reduced-motion'])
    setup_spaces(ctx, b, 3)
    move(ctx, '700', '600')
    rests = ctx.rests
    ctx.rests = {}
    measure_rest(ctx, 'A', 'reduced motion: A')
    focus(ctx)
    frames = capture(ctx, lambda: press(ctx, 'ctrl+alt+Down'), 0.8)
    measure_rest(ctx, 'B', 'reduced motion: B')
    check_move(ctx, frames, 'A', 'B', 'reduced motion: switch', +1, expect_mid=False)
    move(ctx, '100', '400')
    frames = capture(ctx, lambda: ctx.touchpad.swipe(-120, steps=12, interval=0.01), 0.8)
    check(active_name(b) == 'a3', f'reduced motion: a swipe still switches ({active_name(b)})')
    ctx.rests = rests
    b.close()


# --- Phase: modes --------------------------------------------------------------------------------

def mode_run(ctx, name, prefs_extra, extra=(), region=None, sheet_crop=None, hover=None,
             direction=+1, collapsed=False, expand_on_hover=False):
    """A small profile in one sidebar mode: rest shots, and animated switches A -> B -> A."""
    print(f'== mode {name}', flush=True)
    saved = ctx.region, ctx.list_box, ctx.sheet_crop, ctx.rests
    if region:
        ctx.region = region
    w, h = ctx.region[2], ctx.region[3]
    ctx.list_box = (0, 0, w, h - 72)
    ctx.sheet_crop = sheet_crop or ctx.sheet_crop
    ctx.rests = {}
    try:
        prefs = prefs_for([DEFAULT, SPACE_A, SPACE_B], **prefs_extra)
        if collapsed:
            prefs['vertical_tabs'] = {'collapsed_state': True,
                                      'expand_on_hover': expand_on_hover}
        b = start(ctx, name, prefs, '--container=Alpha', url(ctx, 'a1', 'A'), extra=extra)
        setup_spaces(ctx, b, 3)
        if hover:
            move(ctx, hover[0] + 40, hover[1])
            move(ctx, hover[0], hover[1])
            settle(1.5)
        else:
            move(ctx, 700, 600)
        rest_a = measure_rest(ctx, 'A', f'{name}: A')
        full = x11_touchpad.Grabber(ctx.display).grab(0, 0, *SCREEN)
        full.save(os.path.join(ctx.shots, f'm3-mode-{name}.png'))
        focus(ctx)
        frames = capture(ctx, lambda: press(ctx, 'ctrl+alt+Down'), 0.9)
        rest_b = measure_rest(ctx, 'B', f'{name}: B')
        r = check_move(ctx, frames, 'A', 'B', f'{name}: Ctrl+Alt+Down A to B', direction,
                       sheet=os.path.join(ctx.shots, f'm3-sheet-mode-{name}.png'))
        ctx.report[f'mode_{name}'] = r
        frames = capture(ctx, lambda: press(ctx, 'ctrl+alt+Up'), 0.9)
        check_move(ctx, frames, 'B', 'A', f'{name}: Ctrl+Alt+Up B to A', -direction)
        return b, rest_a, rest_b
    finally:
        ctx.region, ctx.list_box, ctx.sheet_crop, ctx.rests = saved


def phase_modes(ctx):
    right = (SCREEN[0] - 420, 30, 420, 870)

    def want(name):
        return not ctx.modes or name in ctx.modes

    if want('collapsed'):
        mode_collapsed(ctx)
    if want('right-aligned'):
        b, *_ = mode_run(ctx, 'right-aligned', {'vertical_right_aligned': True}, region=right,
                         sheet_crop=(160, 0, 420, 870))
        b.close()
    if want('zen'):
        b, *_ = mode_run(ctx, 'zen', {'zen_mode': True}, sheet_crop=(0, 0, 260, 870),
                         hover=(1, 600))
        b.close()
    if want('rtl'):
        b, *_ = mode_run(ctx, 'rtl', {}, extra=['--force-ui-direction=rtl'], region=right,
                         sheet_crop=(160, 0, 420, 870), direction=-1)
        b.close()
    if want('expand-on-hover'):
        b, *_ = mode_run(ctx, 'expand-on-hover', {}, sheet_crop=(0, 0, 260, 870),
                         hover=(10, 400), collapsed=True, expand_on_hover=True)
        b.close()
    if want('resized'):
        mode_resized(ctx)


def mode_collapsed(ctx):
    """Collapsed: the row shows the visible space's icon, which opens a menu of the spaces."""
    b, rest_a, rest_b = mode_run(ctx, 'collapsed', {}, region=(0, 30, 120, 870),
                                 sheet_crop=(0, 0, 70, 870), collapsed=True)
    saved = ctx.region, ctx.list_box, ctx.rests
    ctx.region, ctx.list_box, ctx.rests = (0, 30, 120, 870), (0, 0, 120, 798), {}
    try:
        ctx.rests['A'] = rest_a
        check(rest_a.x is not None and rest_a.x < 30,
              f'collapsed: the tabs are favicons only (x {rest_a.x})')
        icon = find_row_icon(rest_a.img, 'A')
        others = [s for s in ('B', 'D') if find_row_icon(rest_a.img, s)]
        check(icon is not None and not others,
              f'collapsed: the row shows only the visible space ({icon}, others {others})')
        if not icon:
            return
        move(ctx, icon[0] + ctx.region[0], icon[1] + ctx.region[1])
        xdo(ctx, 'click', '1')
        settle(0.8)
        menu = x11_touchpad.Grabber(ctx.display).grab(0, 0, *SCREEN)
        menu.save(os.path.join(ctx.shots, 'm3-mode-collapsed-menu.png'))
        # The menu lists the spaces with their icons: click B's row.
        px = menu.load()
        pts = [(x, y) for y in range(300, SCREEN[1]) for x in range(0, 400)
               if px[x, y] == (0, 0, 255)]
        check(len(pts) > 20, 'collapsed: the row icon opens a menu with the spaces')
        if len(pts) <= 20:
            return
        x = max(p[0] for p in pts) + 30
        y = (min(p[1] for p in pts) + max(p[1] for p in pts)) // 2
        move(ctx, x, y)
        frames = capture(ctx, lambda: xdo(ctx, 'click', '1'), 1.0)
        move(ctx, 700, 600)
        check(active_name(b) == 'b3', f'collapsed: picking B in the menu shows B '
                                      f'({active_name(b)})')
        ctx.rests['B'] = rest_b
        check_move(ctx, frames, 'A', 'B', 'collapsed: menu A to B', +1)
    finally:
        b.close()
        ctx.region, ctx.list_box, ctx.rests = saved


def mode_resized(ctx):
    """Resize the sidebar by dragging its edge, then switch: the pages are the new width."""
    print('== mode resized', flush=True)
    saved = ctx.region, ctx.list_box, ctx.rests
    ctx.region, ctx.list_box, ctx.rests = (0, 30, 420, 870), (0, 0, 420, 798), {}
    try:
        b = start(ctx, 'resize', prefs_for([DEFAULT, SPACE_A, SPACE_B]), '--container=Alpha',
                  url(ctx, 'a1', 'A'))
        setup_spaces(ctx, b, 3)
        move(ctx, 700, 600)
        measure_rest(ctx, 'A', 'resize: A before')
        focus(ctx)
        frames = capture(ctx, lambda: press(ctx, 'ctrl+alt+Down'), 0.9)
        measure_rest(ctx, 'B', 'resize: B before')
        before = check_move(ctx, frames, 'A', 'B', 'resize: before', +1)['width']
        edge = abs(before[0]) if before else 200
        move(ctx, edge - 1, 500)
        xdo(ctx, 'mousedown', '1')
        for dx in range(8, 81, 8):
            move(ctx, edge - 1 + dx, 500)
        xdo(ctx, 'mouseup', '1')
        move(ctx, 900, 600)
        settle(1)
        ctx.rests = {}
        measure_rest(ctx, 'B', 'resize: B after')
        frames = capture(ctx, lambda: (focus(ctx), press(ctx, 'ctrl+alt+Up')), 0.9)
        measure_rest(ctx, 'A', 'resize: A after')
        after = check_move(ctx, frames, 'B', 'A', 'resize: after', -1,
                           sheet=os.path.join(ctx.shots, 'm3-sheet-mode-resized.png'))['width']
        check(before and after and abs(after[0]) - abs(before[0]) >= 60,
              f'resize: the pages take the new sidebar width ({before} -> {after})')
        x11_touchpad.Grabber(ctx.display).grab(0, 0, *SCREEN).save(
            os.path.join(ctx.shots, 'm3-mode-resized.png'))
        b.close()
    finally:
        ctx.region, ctx.list_box, ctx.rests = saved


# --- Phase: drag and selection -------------------------------------------------------------------

def favicon_rows(ctx, space_colors):
    img = x11_touchpad.Grabber(ctx.display).grab(*ctx.region)
    rows = []
    for c in space_colors:
        for x, y in squares(img, COLORS[c]):
            rows.append((y, c, x))
    return sorted(rows)


def phase_drag(ctx):
    print('== drag and selection', flush=True)
    b = start(ctx, 'drag', prefs_for([DEFAULT, SPACE_A, SPACE_B]), '--container=Alpha',
              url(ctx, 'a1', 'A'))
    setup_spaces(ctx, b, 3)
    # A marked tab (yellow favicon) in A, after a3: A shows a1 a2 a3 y.
    open_tabs(ctx, b, 'A', ['ay'], color='Y')
    move(ctx, '700', '600')
    settle(1)
    rows = favicon_rows(ctx, ['A', 'Y'])
    order = [c for _, c, _ in rows]
    check(order == ['A', 'A', 'A', 'Y'], f'A shows a1 a2 a3 and the marked tab ({order})')
    oy = ctx.region[1]
    if len(rows) == 4:
        # Drag the marked tab to the top of A.
        y_from = rows[3][0] + 8 + oy
        y_to = rows[0][0] + 2 + oy
        move(ctx, '60', str(y_from))
        xdo(ctx, 'mousedown', '1')
        for y in range(y_from, y_to - 1, -6):
            move(ctx, '60', str(y))
            time.sleep(0.02)
        move(ctx, '60', str(y_to))
        time.sleep(0.2)
        xdo(ctx, 'mouseup', '1')
        settle(1)
        order = [c for _, c, _ in favicon_rows(ctx, ['A', 'Y'])]
        check(order == ['Y', 'A', 'A', 'A'], f'dragging a tab within A reorders it ({order})')
        # Drag it far below the last tab of A, where B's tabs are in the model.
        rows = favicon_rows(ctx, ['A', 'Y'])
        y_from = rows[0][0] + 8 + oy
        move(ctx, '60', str(y_from))
        xdo(ctx, 'mousedown', '1')
        for y in range(y_from, 700, 12):
            move(ctx, '60', str(y))
            time.sleep(0.02)
        time.sleep(0.2)
        xdo(ctx, 'mouseup', '1')
        settle(1)
        order = [c for _, c, _ in favicon_rows(ctx, ['A', 'Y', 'B', 'D'])]
        check(order == ['A', 'A', 'A', 'Y'], f'dropped past the end it stays the last tab of A '
                                             f'({order})')
        focus(ctx)
        press(ctx, 'ctrl+alt+Down')
        settle(1)
        order = [c for _, c, _ in favicon_rows(ctx, ['A', 'Y', 'B', 'D'])]
        check(order == ['B', 'B', 'B'], f'B did not receive it ({order})')
        press(ctx, 'ctrl+alt+Up')
        settle(1)

    # Ctrl+Shift+PgDn on the last tab of A does not move it among B's tabs.
    focus(ctx)
    press(ctx, 'ctrl+9')
    settle(0.5)
    check(active_name(b) == 'ay', f'Ctrl+9 selects the last tab of A ({active_name(b)})')
    press(ctx, 'ctrl+shift+Next')
    settle(0.8)
    press(ctx, 'ctrl+Next')
    settle(0.5)
    check(active_name(b) == 'a1', f'Ctrl+Shift+PgDn on the last tab of A leaves it last: the next '
                                  f'tab wraps to a1 ({active_name(b)})')

    # Range selection across pinned and unpinned tabs stays in the space. Pin a1 in A, b1 in B.
    press(ctx, 'ctrl+alt+p')            # a1 is active: pin it
    settle(0.8)
    press(ctx, 'ctrl+alt+Down')
    settle(0.8)
    press(ctx, 'ctrl+1')
    settle(0.3)
    press(ctx, 'ctrl+alt+p')            # pin b1
    settle(0.8)
    press(ctx, 'ctrl+alt+Up')
    settle(1)
    move(ctx, '700', '600')
    settle(0.5)
    rows = favicon_rows(ctx, ['A', 'Y'])
    check(len(rows) == 4, f'A shows its pinned tab and three others ({len(rows)})')
    if len(rows) == 4:
        top, bottom = rows[0], rows[-1]
        # Click the pinned a1, then shift-click the last tab: a range over pinned and unpinned.
        move(ctx, str(top[2] + 8), str(top[0] + 8 + oy))
        xdo(ctx, 'click', '1')
        settle(0.4)
        move(ctx, '100', str(bottom[0] + 8 + oy))
        xdo(ctx, 'keydown', 'shift')
        xdo(ctx, 'click', '1')
        xdo(ctx, 'keyup', 'shift')
        settle(0.4)
        focus(ctx)
        before = {m2.name_of(t['url']) for t in b.pages()}
        press(ctx, 'ctrl+w')       # closes the selected tabs
        settle(1.2)
        after = {m2.name_of(t['url']) for t in b.pages()}
        closed = sorted(n for n in before - after if n)
        check(set(closed) == {'a1', 'a2', 'a3', 'ay'},
              f'shift-click range over pinned and unpinned tabs selects only A\'s ({closed})')
    b.close()


# --- Phase: stock --------------------------------------------------------------------------------

def phase_stock(ctx):
    print('== stock', flush=True)
    # One space. (Since M5 a tab opened in a container that no space uses is reopened in the
    # space, see spaces_m2.py; the pages here only differ by their favicon colour.)
    b = start(ctx, 'stock', prefs_for([DEFAULT]), url(ctx, 'a1', 'A'))
    m1.find_page(b, 'n=a1').wait_loaded('n=a1')
    ctx.window = find_window(ctx, 'a1')
    b.remote(url(ctx, 'b1', 'B'))
    m1.find_page(b, 'n=b1').wait_loaded('n=b1')
    b.remote(url(ctx, 'd1', 'D'))
    m1.find_page(b, 'n=d1').wait_loaded('n=d1')
    focus(ctx)
    press(ctx, 'ctrl+t')
    settle(1)
    href, p = m2.wait_active(b, lambda h: h and h.startswith('chrome://'))
    if p:
        p.navigate(url(ctx, 'd2', 'D'))
    move(ctx, '640', '450')
    press(ctx, 'ctrl+alt+Down')
    settle(1.5)
    shot = os.path.join(ctx.shots, 'm3-stock.png')
    x11_touchpad.Grabber(ctx.display).grab(0, 0, *SCREEN).save(shot)
    img = Image.open(shot).convert('RGB')
    rows = [c for _, c, _ in sorted((y, c, x) for c in ('A', 'B', 'D')
                                    for x, y in squares(img, COLORS[c]))]
    check(rows == ['A', 'B', 'D', 'D'], f'stock: every tab shows ({rows})')
    row_icons = [s for s in ('A', 'B') if find_row_icon(img.crop((0, 30, 420, 900)), s)]
    check(not row_icons, f'stock: no switcher row ({row_icons})')
    ref = os.path.join(ROOT, 'build', 'm2-stock.png')
    if os.path.exists(ref):
        box = (0, 36, 196, 600)
        old = Image.open(ref).convert('RGB').crop(box)
        diff = ImageChops.difference(old, img.crop(box)).getbbox()
        check(diff is None, f'stock: the sidebar is pixel for pixel M2\'s stock sidebar '
                            f'(build/m2-stock.png; differing box {diff})')
    else:
        print(f'  (no {ref} to compare with)')
    b.close()


# --- Main ----------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--helium', default=os.environ.get('NEON_HELIUM', m1.DEFAULT_HELIUM))
    ap.add_argument('--keep', action='store_true', help='keep the profiles for inspection')
    ap.add_argument('--shots', default=os.path.join(ROOT, 'build', 'm3'),
                    help='where screenshots and contact sheets go (default: build/m3/)')
    ap.add_argument('--only', action='append', default=[],
                    help='run only these phases: pager, instant, modes, drag, stock')
    ap.add_argument('--mode', action='append', default=[],
                    help='in the modes phase, run only these modes')
    ap.add_argument('--tabs', type=int, default=30, help='tabs per space in the pager phase')
    ap.add_argument('--browser-arg', action='append', default=[],
                    help='extra browser switch for every launch, for example '
                         '--browser-arg=--force-prefers-reduced-motion (a run that must fail)')
    args = ap.parse_args()
    if not os.access(args.helium, os.X_OK):
        raise SystemExit(f'browser binary not found: {args.helium}')
    if not x11_touchpad.available():
        raise SystemExit('Xorg with the dummy and inputtest drivers is needed (dev image)')
    # Many tabs, few renderers: the tabs share processes per storage partition.
    Browser.extra_args = ['--renderer-process-limit=6'] + args.browser_arg

    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(args.shots, exist_ok=True)
    log_path = os.path.join(LOG_DIR, 'spaces_m3.log')
    ctx = Ctx()
    ctx.helium, ctx.dirs, ctx.browsers = args.helium, [], []
    ctx.log = open(log_path, 'w')
    ctx.shots, ctx.tabs, ctx.modes = args.shots, args.tabs, args.mode
    ctx.region = REGION
    ctx.list_box = (0, 0, REGION[2], REGION[3] - 72)
    ctx.sheet_crop = (0, 0, 260, REGION[3])
    ctx.rests = {}
    ctx.report = {}

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), m2.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ctx.site = f'http://127.0.0.1:{server.server_address[1]}'

    ctx.display = m1.free_display()
    xserver = x11_touchpad.XServer(ctx.display, *SCREEN, log=ctx.log).start()
    ctx.dirs.append(xserver.dir)
    ctx.touchpad = x11_touchpad.Touchpad(xserver.socket)
    phases = [('pager', phase_pager), ('instant', phase_instant), ('modes', phase_modes),
              ('drag', phase_drag), ('stock', phase_stock)]
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
        ctx.touchpad.close()
        xserver.stop()
        server.shutdown()
        if args.keep:
            print('profiles kept:', *ctx.dirs)
        else:
            for d in ctx.dirs:
                shutil.rmtree(d, ignore_errors=True)
        ctx.log.close()

    with open(os.path.join(args.shots, 'm3-report.json'), 'w') as f:
        json.dump(ctx.report, f, indent=1, default=str)
    crashes = [line for line in open(log_path, errors='replace')
               if 'Check failed' in line or 'FATAL' in line or 'DUMP_WILL_BE_CHECK' in line
               or 'Received signal' in line]
    check(not crashes, f'no CHECK failure or crash in the browser log {crashes[:3]}')
    print(f'browser log: {log_path}')
    print(f'shots and report: {args.shots}')
    if failures:
        print(f'{len(failures)} check(s) failed:', *failures, sep='\n  ')
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
