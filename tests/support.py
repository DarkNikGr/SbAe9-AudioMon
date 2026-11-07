# SPDX-License-Identifier: GPL-2.0-only
"""Test harness: SbAe9-AudioMon on the shared fake AE-9 (sbae9kit.testing).

setup() must run before anything imports the app. It:
  - points XDG_CONFIG / CACHE / STATE / DATA_HOME at a temp folder (settings,
    the debug log and icons never touch the real ones) and runs Qt offscreen,
  - makes any real subprocess start fail,
  - swaps the app's PipeWire graph (pw-dump), pw-link worker and pw-record
    meters for the kit's fakes, and the driver info for a fixed string,
  - shortens the modelled waits (TIME_SCALE) and the app's SILENT timings.

The fake card is support.PW (reset() makes a fresh one per test).
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sbae9kit import testing  # noqa: E402

FAKE_DRIVER = "snd_hda_codec_creative_ae9 0.9.2 srcversion FAKE0000000000000000000"
PW = None
CONTROLS = None     # the fake ControlWatcher of the window made last
HW_PARAMS = None    # the fake /proc/asound/AE9/pcm0p/sub0/hw_params
_done = False

# What driver 0.10.0 starts with on the real machine.
DRIVER_STATE = {"Headphone Profile": "Off", "PCM Bit Depth": "24-bit", "Direct Mode Switch": "on",
                "Head Unit Output": "Headphones", "SBX Switch": "off", "Speaker Config": "2.1",
                "FX Surround Switch": "on", "Bass Redirect Switch": "off",
                "Speaker Level Front L Volume": "+0 dB", "Speaker Level Front R Volume": "+0 dB",
                "Speaker Distance Front L": "0 cm", "Speaker Distance Front R": "0 cm"}


class FakeControls:
    """Stands in for driverstate.ControlWatcher: set() is a control changing."""

    def __init__(self, *a, **k):
        global CONTROLS
        CONTROLS = self
        self.error = None
        self.values = dict(DRIVER_STATE)
        self._pending = list(self.values.items())

    def set(self, name, value):
        self.values[name] = value
        self._pending.append((name, value))

    def poll(self):
        out, self._pending = self._pending, []
        return out

    def stop(self):
        pass


def set_stream(text):
    """Write the fake hw_params ("closed" or a format like the kernel's)."""
    with open(HW_PARAMS, "w") as f:
        f.write(text)


OPTICAL_OUT = "alsa_output.ae9_optical"     # the separate optical output, hw:AE9,4


def _add_optical_out(pw):
    """Add the optical output sink the kit's fake predates (config/ae9-optical-out.conf)."""
    nid = pw._node({"node.name": OPTICAL_OUT, "node.description": "Sound Blaster AE-9 Optical",
                    "media.class": "Audio/Sink", "api.alsa.path": "hw:AE9,4",
                    "priority.session": 2000}, state="suspended")
    pw.objects[nid]["info"]["params"] = {"Format": [
        {"mediaType": "audio", "mediaSubtype": "raw", "format": "S32LE", "rate": 48000,
         "channels": 2, "position": ["FL", "FR"]}]}
    pw._card_nodes.append(nid)
    for ch in ("FL", "FR"):
        pw._port(nid, "in", f"playback_{ch}", ch)
    for ch in ("FL", "FR"):
        pw._port(nid, "out", f"monitor_{ch}", ch, monitor=True)
    pw.optical_out_db = None

    level_of = pw.level_of

    def with_optical(src_nid):
        o = pw.objects.get(src_nid)
        if o and o["info"]["props"].get("node.name") == OPTICAL_OUT:
            return pw.optical_out_db if pw.optical_out_db is not None else float("-inf")
        return level_of(src_nid)

    pw.level_of = with_optical

    # The kit's fake predates the Format params the app now shows; give the
    # AE-9 nodes the formats the real card reports (pw-dump: S32LE).
    for name, rate, ch in ((testing.SINK, 48000, 2), ("alsa_input.pci-0000_06_00.0.analog-stereo", 96000, 2),
                           ("alsa_input.ae9_what_u_hear", 96000, 2), ("alsa_input.ae9_optical", 48000, 2),
                           ("alsa_input.ae9_rear_rca", 48000, 2)):
        n = pw.node(name)
        if n:
            n["info"]["params"] = {"Format": [
                {"mediaType": "audio", "mediaSubtype": "raw", "format": "S32LE", "rate": rate,
                 "channels": ch, "position": ["FL", "FR"]}]}
    pw._bump()


def reset(**card):
    """A fresh fake card for the next test or scenario."""
    global PW
    PW = testing.FakePipeWire(**dict(dict(rear_rca=True), **card))   # driver 0.10.1: Rear RCA In too
    _add_optical_out(PW)
    return PW


def current():
    return PW


def setup(offscreen=True, time_scale=0.06, quiet=True):
    """Install the fakes. time_scale 0.06 turns PipeWire's 5 s idle -> suspend
    into 0.3 s; the app's What U Hear grace and SILENT wait shrink to match.
    Runs once per process (every test module calls it)."""
    global _done
    if _done:
        return
    _done = True
    global HW_PARAMS
    tmp = testing.isolate("sbae9-audiomon", offscreen=offscreen)
    testing.block_subprocesses()
    testing.TIME_SCALE = time_scale

    from audiomon import capture, core, driverstate, eventlog, monitor, pipewire
    driverstate.ControlWatcher = FakeControls
    HW_PARAMS = driverstate.HW_PARAMS = os.path.join(tmp, "hw_params")
    set_stream("closed")
    pipewire.Graph = current
    pipewire.Linker = lambda: testing.FakeLinker(current)
    capture.Capture = testing.make_fake_capture(current, core.CHANNEL_MAPS, core.RATE, app_name=core.APP_NAME)
    core.driver_info = lambda: FAKE_DRIVER
    if time_scale <= 0.1:          # test speed (the demo keeps the real timings)
        monitor.WIRE_GRACE = 0.3
        monitor.SILENT_AFTER = 0.25
        monitor.PW_RESTART_GRACE = 0.5
        monitor.SETTLE = 0.5
    if quiet:
        eventlog.print = lambda *a, **k: None   # the log also echoes to stdout
    reset()
