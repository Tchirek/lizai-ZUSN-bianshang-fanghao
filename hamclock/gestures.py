"""Touchpad pinch and scrolls for the HamClock window, read from Wayland.

SDL2 (under pygame) turns every scroll into a wheel event and drops pinch gestures. The
compositor offers both on the window's own connection: pinches through the pointer-gestures
protocol (zwp_pointer_gestures_v1, which GTK, Qt and Firefox use) and the source of each
scroll (wheel, finger, continuous) through wl_pointer. This binds them there with pywayland,
on a private event queue so SDL's own dispatching is left alone.

pygame.event.get() reads the connection; poll() right after it hands over what came since.
"""
import atexit
import ctypes
import glob
import os

import pygame
from pywayland import ffi
from pywayland.client import Display, EventQueue
from pywayland.protocol.pointer_gestures_unstable_v1 import ZwpPointerGesturesV1
from pywayland.protocol.wayland import WlPointer, WlSeat

WHEEL, FINGER, CONTINUOUS = 0, 1, 2           # wl_pointer.axis_source
SDL_SYSWM_WAYLAND = 6


class SDLVersion(ctypes.Structure):
    _fields_ = [("major", ctypes.c_uint8), ("minor", ctypes.c_uint8), ("patch", ctypes.c_uint8)]


class SysWMInfo(ctypes.Structure):
    """SDL_SysWMinfo; for Wayland the union starts with the wl_display pointer."""
    _fields_ = [("version", SDLVersion), ("subsystem", ctypes.c_int), ("info", ctypes.c_void_p * 8)]


def wayland_display():
    """The wl_display of pygame's window, from SDL itself (pygame's get_wm_info() crashes
    under Wayland)."""
    here = os.path.dirname(pygame.__file__)
    found = sorted(glob.glob(os.path.join(here, os.pardir, "pygame.libs", "libSDL2-2*.so*")))
    sdl = ctypes.CDLL(found[0] if found else "libSDL2-2.0.so.0")
    sdl.SDL_GetWindowFromID.restype = ctypes.c_void_p
    sdl.SDL_GetWindowFromID.argtypes = [ctypes.c_uint32]
    sdl.SDL_GetWindowWMInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(SysWMInfo)]
    info = SysWMInfo()
    sdl.SDL_GetVersion(ctypes.byref(info.version))
    for window_id in range(1, 17):
        window = sdl.SDL_GetWindowFromID(window_id)
        if window and sdl.SDL_GetWindowWMInfo(window, ctypes.byref(info)):
            if info.subsystem == SDL_SYSWM_WAYLAND and info.info[0]:
                return info.info[0]
    raise OSError("not a Wayland window")


class Gestures:
    """Pinch, scroll and wheel input from the compositor; raises OSError if unavailable."""

    def __init__(self):
        address = wayland_display()
        wl = ctypes.CDLL("libwayland-client.so.0")
        wl.wl_proxy_create_wrapper.restype = ctypes.c_void_p
        wl.wl_proxy_create_wrapper.argtypes = [ctypes.c_void_p]
        wl.wl_proxy_set_queue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        wl.wl_proxy_wrapper_destroy.argtypes = [ctypes.c_void_p]
        self.wl = wl
        # SDL's connection, never closed from here
        self.display = Display()
        self.display._ptr = ffi.cast("struct wl_display *", address)
        self.queue = EventQueue(self.display)
        # objects made through this wrapper (and theirs in turn) report to our queue only
        wrapper = wl.wl_proxy_create_wrapper(address)
        wl.wl_proxy_set_queue(wrapper, int(ffi.cast("uintptr_t", self.queue._ptr)))
        self.wrapper_address = wrapper
        self.wrapper = Display()
        self.wrapper._ptr = ffi.cast("struct wl_display *", wrapper)
        atexit.register(self.close)          # before pygame's own exit closes the connection
        self.seat = self.manager = self.pointer = self.pinch = None
        self.registry = self.wrapper.get_registry()
        self.registry.dispatcher["global"] = self._global
        self.display.roundtrip(queue=self.queue)                 # globals
        if not (self.seat and self.manager):
            raise OSError("compositor offers no pointer gestures")
        self.display.roundtrip(queue=self.queue)                 # seat capabilities
        if not self.pinch:
            raise OSError("no pointer on the seat")
        self.events, self.scale, self.source = [], 1.0, None

    def close(self):
        """Free our objects, then the queue (the other way round crashes at exit)."""
        for name in ("pinch", "pointer", "manager", "seat", "registry"):
            proxy = getattr(self, name, None)
            if proxy is not None and not proxy.destroyed:
                proxy._destroy()
            setattr(self, name, None)
        if self.wrapper_address:
            self.wl.wl_proxy_wrapper_destroy(self.wrapper_address)
            self.wrapper_address = None
        if not self.queue.destroyed:
            self.queue.destroy()

    def _global(self, registry, name, interface, version):
        if interface == "wl_seat" and not self.seat:
            self.seat = registry.bind(name, WlSeat, min(version, 8))
            self.seat.dispatcher["capabilities"] = self._capabilities
        elif interface == "zwp_pointer_gestures_v1":
            self.manager = registry.bind(name, ZwpPointerGesturesV1, 1)

    def _capabilities(self, seat, caps):
        if caps & WlSeat.capability.pointer and not self.pointer:
            self.pointer = seat.get_pointer()
            for event in ("axis", "axis_source", "axis_stop", "axis_discrete", "axis_value120", "frame"):
                self.pointer.dispatcher[event] = getattr(self, "_" + event)
            self.pinch = self.manager.get_pinch_gesture(self.pointer)
            # no "begin": it names SDL's surface, which pywayland has no object for and so
            # drops the event; "end" (cancelled or not) resets the scale instead
            self.pinch.dispatcher["update"] = self._update
            self.pinch.dispatcher["end"] = self._end

    # pinch: scale is relative to where the fingers started
    def _update(self, pinch, time, dx, dy, scale, rotation):
        if scale > 0:
            self.events.append(("pinch", scale / self.scale, dx, dy))
            self.scale = scale

    def _end(self, pinch, serial, time, cancelled):
        self.scale = 1.0
        self.events.append(("pinch_end",))

    # scrolls: a frame groups a source, its axis values and maybe wheel clicks
    def _axis_source(self, pointer, source):
        self.source = source

    def _axis(self, pointer, time, axis, value):
        self.events.append(("axis", self.source, axis, value, time))

    def _axis_stop(self, pointer, time, axis):
        # the fingers lifted (once for each axis): where browsers start their momentum
        if not self.events or self.events[-1][0] != "scroll_end":
            self.events.append(("scroll_end", time / 1000))

    def _axis_discrete(self, pointer, axis, discrete):
        self.events.append(("clicks", axis, discrete))

    def _axis_value120(self, pointer, axis, value120):
        self.events.append(("clicks", axis, value120 / 120))

    def _frame(self, pointer):
        self.source = None

    def poll(self):
        """What happened since the last call: ("pinch", ratio, dx, dy), ("pinch_end",),
        ("scroll", dx, dy, t) from fingers or a TrackPoint (surface px, at compositor time t
        s), ("scroll_end", t) as the fingers lift, ("wheel", clicks)."""
        self.display.dispatch(block=False, queue=self.queue)
        out, clicks = [], {}
        for e in self.events:
            if e[0] == "clicks":
                clicks[e[1]] = clicks.get(e[1], 0) + e[2]
            elif e[0] == "axis":
                _, source, axis, value, time = e
                if source in (FINGER, CONTINUOUS):
                    vertical = axis == WlPointer.axis.vertical_scroll
                    out.append(("scroll", 0 if vertical else value, value if vertical else 0, time / 1000))
                elif source is None and axis == WlPointer.axis.vertical_scroll and not clicks:
                    clicks[axis] = value / 15                    # an old compositor: no source
            else:
                out.append(e)
        wheel = clicks.get(WlPointer.axis.vertical_scroll, 0)
        if wheel:
            out.append(("wheel", -wheel))                        # + = away from the user = in
        self.events = []
        return out
