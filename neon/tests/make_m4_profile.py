#!/usr/bin/env python3
"""Makes neon/tests/fixtures/m4-profile.tar.gz, the profile all_containers_m5.py migrates: a
profile written by a Neon build from before every space had a container (M4, the build of main
before M5), with the default space in the default storage partition.

It must run against such a build, so it is kept for the record and for remaking the fixture
after a format change, not for routine runs. From the neon-linux checkout, with the tree built
from main before M5:

    neon/dev-container.sh sh 'python3 helium-chromium/neon/tests/make_m4_profile.py'

The profile has two spaces, Default (no container) and A (container Alpha), in the vertical
layout. The default space holds a web page that set the cookie who=D, a second web page, the
settings page and about:blank; space A holds a web page with who=A. The web pages are on
http://127.0.0.1:PORT with the fixed PORT below, which all_containers_m5.py serves again.
Caches are left out of the archive.
"""
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import http.server

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import containers_m1 as m1  # noqa: E402
import spaces_m2 as m2  # noqa: E402
from containers_m1 import Page, free_display  # noqa: E402

PORT = 47813
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures',
                       'm4-profile.tar.gz')
SKIP = ('Cache', 'Code Cache', 'GPUCache', 'DawnGraphiteCache', 'DawnWebGPUCache',
        'GrShaderCache', 'GraphiteDawnCache', 'ShaderCache', 'component_crx_cache',
        'Crashpad', 'Safe Browsing', 'BrowserMetrics', 'segmentation_platform',
        'optimization_guide_model_store', 'extensions_crx_cache',
        # The bundled uBlock Origin's filter lists, rebuilt on the next start.
        'Local Extension Settings', 'IndexedDB')


def main():
    ctx = m2.Ctx()
    ctx.helium = m1.DEFAULT_HELIUM
    ctx.display = free_display()
    ctx.dirs, ctx.browsers = [], []
    ctx.log = open(os.path.join(m1.LOG_DIR, 'make-m4-profile.log'), 'w')
    xvfb = subprocess.Popen(['Xvfb', f':{ctx.display}', '-screen', '0', '1600x1000x24'],
                            stderr=subprocess.DEVNULL)
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), m2.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    site = f'http://127.0.0.1:{PORT}'
    prefs = m2.prefs_for([m2.DEFAULT, m2.SPACE_A])
    profile = m2.new_profile(ctx, 'm4fixture', prefs)
    try:
        time.sleep(1)
        b = m2.start(ctx, profile, f'{site}/page?n=d1&c={m2.HEX["D"]}')
        p = m1.find_page(b, 'n=d1')
        p.wait_loaded()
        m1.set_cookie(p, 'D')
        for url in (f'{site}/page?n=d2&c={m2.HEX["D"]}', 'chrome://settings/', 'about:blank'):
            b.session.call('Target.createTarget', {'url': url})
            time.sleep(1.5)
        b.remote('--container=Alpha', f'{site}/page?n=a1&c={m2.HEX["A"]}')
        a = m1.find_page(b, 'n=a1')
        a.wait_loaded()
        m1.set_cookie(a, 'A')
        # Back to the default space, so the profile closes showing it.
        p.activate()
        time.sleep(1)
        print('pages:', [t['url'] for t in b.pages()])
        b.close()
        os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
        with tarfile.open(FIXTURE, 'w:gz') as tar:
            for root, dirs, files in os.walk(profile):
                dirs[:] = [d for d in dirs if d not in SKIP]
                for f in files:
                    if f in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
                        continue
                    full = os.path.join(root, f)
                    tar.add(full, arcname=os.path.relpath(full, profile))
        print('wrote', FIXTURE, os.path.getsize(FIXTURE), 'bytes')
    finally:
        for b in ctx.browsers:
            b.close()
        srv.shutdown()
        xvfb.terminate()
        for d in ctx.dirs:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == '__main__':
    main()
