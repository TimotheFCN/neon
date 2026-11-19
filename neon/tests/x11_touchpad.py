"""An X server with a scriptable touchpad, and fast screen grabs, for the Neon UI checks.

Xvfb only has the XTEST devices, and XTEST cannot scroll smoothly: `xdotool click 6/7` sends wheel
buttons, which Chromium turns into notched mouse wheel events. Chromium's X11 code only makes
touchpad scroll events (ui::ScrollEvent, EventType::kScroll) from an XInput device of type TOUCHPAD
with smooth scroll valuators (ui/events/x/events_x_utils.cc, device_data_manager_x11.cc).

XServer runs Xorg with the dummy video driver and xserver's own `inputtest` input driver, with one
device of DeviceType "PointerGesture": an XI device of type TOUCHPAD whose scroll valuators
(increment 120) take deltas from a Unix socket. That is the same thing xf86-input-libinput gives
Chromium for a real touchpad: XI2 motion events with scroll valuator deltas from a TOUCHPAD device.
What it skips is libinput itself (finger detection, two-finger scroll recognition, its scroll
speed), which happens before X and is not Chromium's business.

Needs xserver-xorg-core (Xorg and inputtest_drv.so) and xserver-xorg-video-dummy (in the dev image).
"""
import ctypes
import ctypes.util
import os
import socket
import subprocess
import tempfile
import time

XORG = '/usr/lib/xorg/Xorg'
INPUTTEST = '/usr/lib/xorg/modules/input/inputtest_drv.so'
DUMMY = '/usr/lib/xorg/modules/drivers/dummy_drv.so'

CONFIG = '''Section "ServerFlags"
    Option "AutoAddDevices" "false"
    Option "AutoEnableDevices" "false"
    Option "DontVTSwitch" "true"
EndSection
Section "Device"
    Identifier "dummy"
    Driver "dummy"
    VideoRam 32000
EndSection
Section "Monitor"
    Identifier "monitor"
    HorizSync 5.0-1000.0
    VertRefresh 5.0-200.0
    Modeline "{w}x{h}" 96.00 {w} {hs1} {hs2} {ht} {h} {vs1} {vs2} {vt} -hsync +vsync
EndSection
Section "Screen"
    Identifier "screen"
    Device "dummy"
    Monitor "monitor"
    DefaultDepth 24
    SubSection "Display"
        Depth 24
        Modes "{w}x{h}"
        Virtual {w} {h}
    EndSubSection
EndSection
Section "InputDevice"
    Identifier "touchpad"
    Driver "inputtest"
    Option "DeviceType" "PointerGesture"
    Option "SocketPath" "{socket}"
EndSection
Section "ServerLayout"
    Identifier "layout"
    Screen "screen"
    InputDevice "touchpad" "SendCoreEvents"
EndSection
'''


def available():
    return all(os.path.exists(p) for p in (XORG, INPUTTEST, DUMMY))


class XServer:
    """Xorg (dummy video, inputtest touchpad) on display :|display|, run as the current user."""

    def __init__(self, display, width=1280, height=900, log=None):
        self.display, self.width, self.height, self.log = display, width, height, log
        self.dir = tempfile.mkdtemp(prefix='neon-xorg-')
        self.socket = os.path.join(self.dir, 'touchpad.sock')
        self.proc = None

    def start(self):
        w, h = self.width, self.height
        conf = os.path.join(self.dir, 'xorg.conf')
        with open(conf, 'w') as f:
            f.write(CONFIG.format(w=w, h=h, hs1=w + 72, hs2=w + 200, ht=w + 400, vs1=h + 3,
                                  vs2=h + 13, vt=h + 53, socket=self.socket))
        self.proc = subprocess.Popen(
            [XORG, f':{self.display}', '-config', conf, '-logfile',
             os.path.join(self.dir, 'Xorg.log'), '-nolisten', 'tcp', '-noreset',
             '-novtswitch', '-sharevts', '-keeptty'],
            stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 20
        while not os.path.exists(self.socket):
            if self.proc.poll() is not None or time.time() > deadline:
                raise RuntimeError(f'Xorg did not start, see {self.dir}/Xorg.log')
            time.sleep(0.1)
        return self

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


# --- The inputtest protocol (xserver hw/xfree86/drivers/inputtest/xf86-input-inputtest-protocol.h)

_MAX_VALUATORS = 64
_EV_CLIENT_VERSION, _EV_WAIT_FOR_SYNC, _EV_MOTION = 0, 1, 2


class _Header(ctypes.Structure):
    _fields_ = [('length', ctypes.c_uint32), ('type', ctypes.c_int32)]


class _Valuators(ctypes.Structure):
    _fields_ = [('has_unaccelerated', ctypes.c_uint32),
                ('mask', ctypes.c_uint8 * ((_MAX_VALUATORS + 7) // 8)),
                ('valuators', ctypes.c_double * _MAX_VALUATORS),
                ('unaccelerated', ctypes.c_double * _MAX_VALUATORS)]


class _ClientVersion(ctypes.Structure):
    _fields_ = [('header', _Header), ('major', ctypes.c_uint16), ('minor', ctypes.c_uint16)]


class _WaitForSync(ctypes.Structure):
    _fields_ = [('header', _Header)]


class _Motion(ctypes.Structure):
    _fields_ = [('header', _Header), ('is_absolute', ctypes.c_uint32), ('valuators', _Valuators)]


class Touchpad:
    """Sends two-finger scrolls. The inputtest driver takes one client per server lifetime, so keep
    one Touchpad for the whole run.

    Units: scroll valuator units, 120 per wheel click. Chromium turns a delta of d into a scroll
    offset of -d DIP (content moves against the valuator), so scroll(dx > 0) moves content left, as
    fingers moving left do with natural scrolling."""

    HSCROLL, VSCROLL = 2, 3

    def __init__(self, path, timeout=10):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.time() + timeout
        while True:
            try:
                self.sock.connect(path)
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.1)
        self.sock.sendall(bytes(_ClientVersion(
            _Header(ctypes.sizeof(_ClientVersion), _EV_CLIENT_VERSION), 1, 1)))
        self.sock.recv(64)

    def scroll(self, dx, dy, force=False):
        m = _Motion()
        m.header = _Header(ctypes.sizeof(_Motion), _EV_MOTION)
        for axis, value in ((self.HSCROLL, dx), (self.VSCROLL, dy)):
            if value or force:
                m.valuators.mask[axis // 8] |= 1 << (axis % 8)
                m.valuators.valuators[axis] = float(value)
        self.sock.sendall(bytes(m))

    def lift(self):
        """What xf86-input-libinput sends when the fingers lift (libinput's scroll stop): a scroll
        of zero on both axes. Chromium's X11 code turns it into a fling start."""
        self.scroll(0.0, 0.0, force=True)

    def sync(self):
        self.sock.sendall(bytes(_WaitForSync(_Header(ctypes.sizeof(_WaitForSync),
                                                     _EV_WAIT_FOR_SYNC))))
        self.sock.recv(64)

    def swipe(self, dx, dy=0.0, steps=20, interval=0.01, lift=True):
        """Moves the fingers by (dx, dy) in |steps| events, |interval| seconds apart."""
        for _ in range(steps):
            self.scroll(dx / steps, dy / steps)
            time.sleep(interval)
        if lift:
            self.lift()

    def close(self):
        self.sock.close()


# --- Fast grabs of part of the X screen (about half a millisecond for a sidebar)

class _XImage(ctypes.Structure):
    _fields_ = [('width', ctypes.c_int), ('height', ctypes.c_int), ('xoffset', ctypes.c_int),
                ('format', ctypes.c_int), ('data', ctypes.POINTER(ctypes.c_char)),
                ('byte_order', ctypes.c_int), ('bitmap_unit', ctypes.c_int),
                ('bitmap_bit_order', ctypes.c_int), ('bitmap_pad', ctypes.c_int),
                ('depth', ctypes.c_int), ('bytes_per_line', ctypes.c_int),
                ('bits_per_pixel', ctypes.c_int)]


_x11 = None


def _lib():
    global _x11
    if _x11 is None:
        _x11 = ctypes.CDLL(ctypes.util.find_library('X11') or 'libX11.so.6')
        _x11.XOpenDisplay.restype = ctypes.c_void_p
        _x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        _x11.XDefaultRootWindow.restype = ctypes.c_ulong
        _x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        _x11.XGetImage.restype = ctypes.POINTER(_XImage)
        _x11.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int]
        _x11.XDestroyImage.argtypes = [ctypes.POINTER(_XImage)]
        _x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    return _x11


class Grabber:
    def __init__(self, display):
        lib = _lib()
        self.dpy = lib.XOpenDisplay(f':{display}'.encode())
        if not self.dpy:
            raise RuntimeError(f'cannot open display :{display}')
        self.root = lib.XDefaultRootWindow(self.dpy)

    def grab(self, x, y, w, h):
        from PIL import Image
        lib = _lib()
        img = lib.XGetImage(self.dpy, self.root, x, y, w, h, 0xffffffff, 2)  # ZPixmap
        im = img.contents
        data = ctypes.string_at(im.data, im.bytes_per_line * h)
        stride = im.bytes_per_line
        lib.XDestroyImage(img)
        return Image.frombuffer('RGB', (w, h), data, 'raw', 'BGRX', stride, 1)

    def close(self):
        _lib().XCloseDisplay(self.dpy)
