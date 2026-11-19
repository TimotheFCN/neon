#!/usr/bin/env python3
"""M1 containers check: a tab keeps its container, containers hold separate cookies, and both
survive a restart. Drives the real Linux dev build over the DevTools protocol.

Run from the neon-linux checkout (one command, from the host):

    neon/dev-container.sh sh 'python3 helium-chromium/neon/tests/containers_m1.py'

or inside `neon/dev-container.sh sh`:

    python3 helium-chromium/neon/tests/containers_m1.py [--keep] [--helium PATH] [--browser-arg ARG]

Exit status is 0 when every check passes and 1 otherwise. The script starts its own Xvfb, a
local HTTP server on 127.0.0.1 and the browser with a throwaway profile, and stops all three
when it ends. It does not use neon/browser.sh, because it restarts the browser on the same
profile. Ports and the X display are picked free at start, so it does not collide with a
browser started by neon/browser.sh.

Since M5 every space is a container and no tab of a space uses the default partition, so
containers are reached through spaces: a container that no space uses has its tabs reopened in a
space. The checks seed one space per container.

Phases:

1. stock: a fresh profile. Its first space, Personal, has a container; a page loads there, a
   cookie is set, the browser restarts, the session restores the tab and the cookie is still
   there. The default partition holds no cookie of the page.
2. containers: three containers (Alpha, Beta, Gamma) and a space for each are seeded in the
   profile's prefs (brave.containers.list, neon.spaces.list). Tabs are opened on the same site
   in Alpha, Beta and Gamma with `--container=<name> URL` (the first through the initial
   launch, the others by running the binary again, which hands the command line to the
   running browser), and a link from another app without a container. Checks:
   - each tab sees only its own cookie (different cookies on the same site), and the link
     from another app opens in the visible space,
   - the Alpha tab keeps its container across same-site, cross-site (browser- and
     renderer-initiated) navigation, window.open with opener, noopener window.open, a
     target=_blank link click and a ctrl+click on a link (link opened in a new tab),
   - the partition directories exist under Storage/ext/containers/.
3. restart: the browser is closed over DevTools (Browser.close) and started again on the same
   profile without URLs. The session files must hold every tab as containers+<id>:<url>.
   Every restored tab is activated and must show its container's cookie.
4. disabled: the browser is started once more with --disable-features=Containers. Container
   tabs must not load their site in the default partition.

Needs python3-websocket (in the dev image).
"""
import argparse
import hashlib
import http.server
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

import websocket

ROOT = os.environ.get('NEON_ROOT') or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..'))
DEFAULT_HELIUM = os.path.join(ROOT, 'build', 'src', 'out', 'Default', 'helium')
LOG_DIR = os.path.join(ROOT, 'build', 'logs')

ALPHA = {'id': '6f1c2d3e-0a1b-4c2d-8e3f-a1b2c3d4e5f6', 'name': 'Alpha', 'icon': 1,
         'background_color': -13669408}
BETA = {'id': '0b9a8c7d-6e5f-4a3b-9c2d-1e0f9a8b7c6d', 'name': 'Beta', 'icon': 3,
        'background_color': -2543579}
GAMMA = {'id': '3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f', 'name': 'Gamma', 'icon': 2,
         'background_color': -16744448}
COOKIE_AGE = 30 * 24 * 3600


def space_for(container, letter):
    """A space of the neon.spaces.list pref using |container|."""
    return {'id': f'5e6f7a8b-0000-4000-8000-00000000000{letter.lower()}',
            'name': letter, 'icon': letter.lower(), 'color': container['background_color'],
            'container_id': container['id']}

failures = []


def check(cond, what):
    print(('PASS ' if cond else 'FAIL ') + what, flush=True)
    if not cond:
        failures.append(what)
    return cond


# --- Local test server -------------------------------------------------------------------------

PAGE = '''<!doctype html><meta charset="utf-8"><title>{name}</title>
<p>page {name}</p>
<p><a id="blank" href="/page?n={name}-blank" target="_blank">target blank</a></p>
<p><a id="ctrl" href="/page?n={name}-ctrl">ctrl click</a></p>
'''


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = q.get('n', ['x'])[0]
        body = PAGE.format(name=name).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def free_display():
    # Xvfb also listens on an abstract socket, which the dev container shares with the host
    # (host networking), so skip any display that already has a lock or a socket anywhere.
    for n in range(117, 160):
        if os.path.exists(f'/tmp/.X{n}-lock') or os.path.exists(f'/tmp/.X11-unix/X{n}'):
            continue
        s = socket.socket(socket.AF_UNIX)
        try:
            s.connect(f'\0/tmp/.X11-unix/X{n}')
            s.close()
            continue
        except OSError:
            s.close()
        return n
    raise SystemExit('no free X display')


# --- DevTools client ---------------------------------------------------------------------------

class Session:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self.next_id = 0

    def call(self, method, params=None, session_id=None):
        self.next_id += 1
        msg = {'id': self.next_id, 'method': method, 'params': params or {}}
        if session_id:
            msg['sessionId'] = session_id
        self.ws.send(json.dumps(msg))
        while True:
            reply = json.loads(self.ws.recv())
            if reply.get('id') == self.next_id:
                if 'error' in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get('result', {})

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


class Browser:
    extra_args = []

    def __init__(self, helium, profile, display, port, log):
        self.helium, self.profile, self.display, self.port = helium, profile, display, port
        self.log = log
        self.proc = None
        self.session = None

    def base_args(self):
        return [self.helium, f'--user-data-dir={self.profile}', '--no-first-run',
                '--no-default-browser-check', '--no-sandbox', '--password-store=basic',
                '--disable-gpu', f'--remote-debugging-port={self.port}',
                '--window-position=0,0', '--window-size=1280,900'] + Browser.extra_args

    def env(self):
        return dict(os.environ, DISPLAY=f':{self.display}')

    def start(self, *extra):
        self.proc = subprocess.Popen(self.base_args() + list(extra), env=self.env(),
                                     stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f'browser exited with {self.proc.returncode}')
            try:
                version = self.http('/json/version')
                self.session = Session(version['webSocketDebuggerUrl'])
                return
            except OSError:
                time.sleep(0.3)
        raise RuntimeError('DevTools did not come up')

    def remote(self, *extra):
        """Run the binary again on the same profile; it hands its command line to us."""
        r = subprocess.run(self.base_args() + list(extra), env=self.env(), stdout=self.log,
                           stderr=subprocess.STDOUT, timeout=60)
        return r.returncode

    def http(self, path):
        with urllib.request.urlopen(f'http://127.0.0.1:{self.port}{path}', timeout=10) as r:
            return json.load(r)

    def pages(self):
        return [t for t in self.session.call('Target.getTargets')['targetInfos']
                if t['type'] == 'page']

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.session.call('Browser.close')
            except Exception:
                pass
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
        if self.session:
            self.session.close()
        self.session = None

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


class Page:
    """A page target reached through a flattened session on the browser connection."""

    def __init__(self, browser, target_id):
        self.b, self.target_id = browser, target_id
        self.sid = browser.session.call('Target.attachToTarget',
                                        {'targetId': target_id, 'flatten': True})['sessionId']

    def call(self, method, params=None):
        return self.b.session.call(method, params, session_id=self.sid)

    def eval(self, expr, gesture=False):
        r = self.call('Runtime.evaluate', {'expression': expr, 'returnByValue': True,
                                           'awaitPromise': True, 'userGesture': gesture})
        if 'exceptionDetails' in r:
            raise RuntimeError(f'eval {expr!r}: {r["exceptionDetails"]}')
        return r['result'].get('value')

    def url(self):
        return self.eval('location.href')

    def wait_loaded(self, url_part=None, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                href, state = self.eval('[location.href, document.readyState]')
                if state == 'complete' and href != 'about:blank' and (
                        url_part is None or url_part in href):
                    return href
            except RuntimeError:
                pass
            time.sleep(0.2)
        raise TimeoutError(f'{self.target_id} did not load {url_part or ""}')

    def navigate(self, url):
        self.call('Page.navigate', {'url': url})
        return self.wait_loaded(url)

    def cookie(self):
        return self.eval('document.cookie')

    def activate(self):
        self.b.session.call('Target.activateTarget', {'targetId': self.target_id})

    def click(self, selector, modifiers=0):
        self.activate()
        time.sleep(0.5)
        box = self.eval(f'(() => {{ const r = document.querySelector("{selector}")'
                        '.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; })()')
        x, y = box
        for kind in ('mouseMoved', 'mousePressed', 'mouseReleased'):
            params = {'type': kind, 'x': x, 'y': y, 'modifiers': modifiers}
            if kind != 'mouseMoved':
                params.update(button='left', clickCount=1)
            self.call('Input.dispatchMouseEvent', params)


def find_page(browser, part, timeout=20, exclude=()):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for t in browser.pages():
            if part in t['url'] and t['targetId'] not in exclude:
                return Page(browser, t['targetId'])
        time.sleep(0.2)
    urls = [t['url'] for t in browser.pages()]
    raise TimeoutError(f'no tab with {part!r} in {urls}')


def write_prefs(profile, prefs):
    os.makedirs(os.path.join(profile, 'Default'), exist_ok=True)
    with open(os.path.join(profile, 'Default', 'Preferences'), 'w') as f:
        json.dump(prefs, f)


def read_prefs(profile):
    with open(os.path.join(profile, 'Default', 'Preferences')) as f:
        return json.load(f)


def partition_dir(profile, container_id):
    digest = hashlib.sha256(container_id.encode()).digest()[:6].hex().upper()
    return os.path.join(profile, 'Default', 'Storage', 'ext', 'containers', digest)


def read_session_files(profile):
    data = b''
    d = os.path.join(profile, 'Default', 'Sessions')
    for name in sorted(os.listdir(d)):
        with open(os.path.join(d, name), 'rb') as f:
            data += f.read()
    return data


def set_cookie(page, value):
    page.eval(f'document.cookie = "who={value}; max-age={COOKIE_AGE}; path=/"')


# --- Phases ------------------------------------------------------------------------------------

def phase_stock(ctx):
    print('== stock: a fresh profile browses in its first space', flush=True)
    profile = tempfile.mkdtemp(prefix='neon-m1-stock-')
    ctx['dirs'].append(profile)
    write_prefs(profile, {'session': {'restore_on_startup': 1}})
    b = Browser(ctx['helium'], profile, ctx['display'], free_port(), ctx['log'])
    ctx['browsers'].append(b)
    site = ctx['site']
    b.start(f'{site}/page?n=stock')
    p = find_page(b, 'n=stock')
    p.wait_loaded('n=stock')
    set_cookie(p, 'S')
    check(p.cookie() == 'who=S', 'stock: cookie set in the first space')
    b.close()
    spaces = read_prefs(profile).get('neon', {}).get('spaces', {}).get('list', [])
    check(len(spaces) == 1 and spaces[0].get('container_id'),
          f'stock: the profile has one space, with a container ({spaces})')
    cid = spaces[0].get('container_id', '') if spaces else ''
    root = os.path.join(profile, 'Default', 'Storage', 'ext', 'containers')
    dirs = sorted(os.listdir(root)) if os.path.isdir(root) else []
    check(dirs == [os.path.basename(partition_dir(profile, cid))],
          f'stock: the only container partition is that space\'s ({dirs})')
    check(b'who' not in open(os.path.join(profile, 'Default', 'Cookies'), 'rb').read()
          if os.path.exists(os.path.join(profile, 'Default', 'Cookies')) else True,
          'stock: the default partition holds no cookie of the page')
    b.start()
    p = find_page(b, 'n=stock')
    p.activate()
    p.wait_loaded('n=stock')
    check(p.cookie() == 'who=S', 'stock: tab restored after restart with its cookie')
    b.close()


def phase_containers(ctx):
    print('== containers', flush=True)
    profile = tempfile.mkdtemp(prefix='neon-m1-containers-')
    ctx['dirs'].append(profile)
    write_prefs(profile, {'session': {'restore_on_startup': 1},
                          'brave': {'containers': {'list': [ALPHA, BETA, GAMMA]}},
                          'neon': {'spaces': {'list': [space_for(ALPHA, 'A'),
                                                       space_for(BETA, 'B'),
                                                       space_for(GAMMA, 'G')]}}})
    b = Browser(ctx['helium'], profile, ctx['display'], free_port(), ctx['log'])
    ctx['browsers'].append(b)
    site, other = ctx['site'], ctx['other_site']

    b.start('--container=Alpha', f'{site}/page?n=a')
    a = find_page(b, 'n=a')
    a.wait_loaded('n=a')
    check(b.remote('--container=Beta', f'{site}/page?n=b') == 0, 'second launch hands off Beta tab')
    bt = find_page(b, 'n=b')
    bt.wait_loaded('n=b')
    check(b.remote('--container=Gamma', f'{site}/page?n=d') == 0,
          'second launch hands off Gamma tab')
    d = find_page(b, 'n=d')
    d.wait_loaded('n=d')

    for page, value in ((a, 'A'), (bt, 'B'), (d, 'D')):
        set_cookie(page, value)
    check(a.cookie() == 'who=A', 'Alpha tab sees only its own cookie')
    check(bt.cookie() == 'who=B', 'Beta tab sees only its own cookie')
    check(d.cookie() == 'who=D', 'Gamma tab sees only its own cookie')
    # A link from another app has no container: it opens in the visible space (Gamma's, the
    # last one opened), not in the default partition.
    check(b.remote(f'{site}/page?n=other-app') == 0, 'second launch hands off a plain link')
    oa = find_page(b, 'n=other-app')
    oa.wait_loaded('n=other-app')
    check(oa.cookie() == 'who=D', f'a link from another app opens in the visible space '
          f'({oa.cookie()!r})')

    # Same-tab navigation, same site and cross-site.
    a.navigate(f'{site}/page?n=a-same')
    check(a.cookie() == 'who=A', 'Alpha keeps its container across same-site navigation')
    a.navigate(f'{other}/page?n=a-cross')
    set_cookie(a, 'A-other')
    a.navigate(f'{site}/page?n=a-back')
    check(a.cookie() == 'who=A',
          'Alpha keeps its container across browser-initiated cross-site navigation')
    a.eval(f'location.href = "{other}/page?n=a-cross2"')
    a.wait_loaded('n=a-cross2')
    check(a.cookie() == 'who=A-other',
          'Alpha keeps its container across renderer-initiated cross-site navigation')
    a.eval(f'location.href = "{site}/page?n=a-home"')
    a.wait_loaded('n=a-home')
    check(a.cookie() == 'who=A', 'Alpha is back on the first site with its cookie')
    d.navigate(f'{other}/page?n=d-other')
    check(d.cookie() == '', 'Gamma tab does not see the cookie Alpha set on the second site')
    d.navigate(f'{site}/page?n=d')

    # New windows and tabs opened from the Alpha tab.
    seen = {t['targetId'] for t in b.pages()}
    a.eval(f'window.open("{site}/page?n=a-popup", "pop", "width=500,height=400"); 1',
           gesture=True)
    popup = find_page(b, 'n=a-popup', exclude=seen)
    popup.wait_loaded('n=a-popup')
    check(popup.cookie() == 'who=A', 'popup with opener stays in Alpha')
    check(popup.eval('window.opener !== null'), 'popup really has an opener')

    a.eval(f'window.open("{site}/page?n=a-noopener", "_blank", "noopener"); 1', gesture=True)
    noopener = find_page(b, 'n=a-noopener', exclude=seen)
    noopener.wait_loaded('n=a-noopener')
    check(noopener.cookie() == 'who=A', 'noopener window.open stays in Alpha')
    check(noopener.eval('window.opener === null'), 'noopener window really has no opener')

    a.click('#blank')
    blank = find_page(b, 'n=a-home-blank', exclude=seen)
    blank.wait_loaded('n=a-home-blank')
    check(blank.cookie() == 'who=A', 'target=_blank link stays in Alpha')

    a.click('#ctrl', modifiers=2)
    ctrl = find_page(b, 'n=a-home-ctrl', exclude=seen)
    ctrl.wait_loaded('n=a-home-ctrl')
    check(ctrl.cookie() == 'who=A', 'ctrl+click (link opened in a new tab) stays in Alpha')

    # Beta and Gamma are unaffected.
    check(bt.cookie() == 'who=B', 'Beta tab still has its own cookie')
    check(d.cookie() == 'who=D', 'Gamma tab still has its own cookie')

    check(os.path.isdir(partition_dir(profile, ALPHA['id'])),
          'Alpha partition directory exists under Storage/ext/containers')
    check(os.path.isdir(partition_dir(profile, BETA['id'])),
          'Beta partition directory exists under Storage/ext/containers')

    # Restart.
    print('== restart', flush=True)
    b.close()
    used = read_prefs(profile).get('brave', {}).get('containers', {}).get('used', {})
    check(ALPHA['id'] in used and BETA['id'] in used,
          'both containers are recorded as used (brave.containers.used)')
    session = read_session_files(profile)
    check(f'containers+{ALPHA["id"]}:{site}/page?n=a-home'.encode() in session and
          f'containers+{BETA["id"]}:{site}/page?n=b'.encode() in session,
          'session file stores container tabs as containers+<id>:<url>')
    check(f'containers+{GAMMA["id"]}:{site}/page?n=d'.encode() in session,
          'session file stores the Gamma tab with its prefix')
    b.start()
    expected = {'n=a-home': 'who=A', 'n=b': 'who=B', 'n=d': 'who=D',
                'n=a-noopener': 'who=A', 'n=a-home-blank': 'who=A',
                'n=a-home-ctrl': 'who=A'}
    for part, cookie in expected.items():
        # Match the exact query value: n=a-home must not match n=a-home-blank.
        try:
            page = None
            deadline = time.time() + 20
            while page is None and time.time() < deadline:
                for t in b.pages():
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(t['url']).query)
                    if 'n=' + q.get('n', [''])[0] == part:
                        page = Page(b, t['targetId'])
                        break
                else:
                    time.sleep(0.3)
            if page is None:
                raise TimeoutError(part)
            page.activate()
            href = page.wait_loaded(part)
            check(not href.startswith('containers+'), f'restored {part} has a plain URL')
            check(page.cookie() == cookie, f'restored {part} is in its container ({cookie})')
        except (TimeoutError, RuntimeError) as e:
            check(False, f'restored {part}: {e}')
    b.close()

    # With containers turned off, a container tab must not load in the default partition.
    print('== restart with containers disabled', flush=True)
    b.start('--disable-features=Containers')
    time.sleep(3)
    urls = [t['url'] for t in b.pages()]
    leaked = [u for u in urls if u.startswith(site)]
    check(not leaked, f'disabled: no container tab restored as a plain URL {leaked}')
    for t in b.pages():
        if 'n=b' in t['url'] or 'n=a' in t['url']:
            page = Page(b, t['targetId'])
            page.activate()
            time.sleep(1)
            try:
                c = page.cookie()
            except RuntimeError:
                c = ''
            check(c not in ('who=D', 'who=A', 'who=B') or not page.url().startswith(site),
                  f'disabled: {t["url"][:60]} does not load in the default partition')
    b.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--helium', default=os.environ.get('NEON_HELIUM', DEFAULT_HELIUM))
    ap.add_argument('--keep', action='store_true', help='keep the profiles for inspection')
    ap.add_argument('--browser-arg', action='append', default=[],
                    help='extra browser switch for every launch, for example '
                         '--browser-arg=--disable-features=Containers (a run that must fail)')
    args = ap.parse_args()
    Browser.extra_args = args.browser_arg
    if not os.access(args.helium, os.X_OK):
        raise SystemExit(f'browser binary not found: {args.helium}')

    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, 'containers_m1.log')
    ctx = {'helium': args.helium, 'dirs': [], 'browsers': [],
           'log': open(log_path, 'w')}

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    ctx['site'] = f'http://127.0.0.1:{port}'
    ctx['other_site'] = f'http://localhost:{port}'

    ctx['display'] = free_display()
    xvfb = subprocess.Popen(['Xvfb', f':{ctx["display"]}', '-screen', '0', '1280x900x24',
                             '-nolisten', 'tcp'], stdout=ctx['log'], stderr=subprocess.STDOUT)
    time.sleep(1)
    try:
        for phase in (phase_stock, phase_containers):
            try:
                phase(ctx)
            except Exception as e:  # report and go on to cleanup
                check(False, f'{phase.__name__} raised {type(e).__name__}: {e}')
    finally:
        for b in ctx['browsers']:
            b.close() if b.proc and b.proc.poll() is None and b.session else b.kill()
        xvfb.send_signal(signal.SIGTERM)
        xvfb.wait()
        server.shutdown()
        if args.keep:
            print('profiles kept:', *ctx['dirs'])
        else:
            for d in ctx['dirs']:
                shutil.rmtree(d, ignore_errors=True)
        ctx['log'].close()

    print(f'browser log: {log_path}')
    if failures:
        print(f'{len(failures)} check(s) failed:', *failures, sep='\n  ')
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
