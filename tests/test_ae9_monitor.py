# SPDX-License-Identifier: GPL-2.0-only
"""SbAe9-AudioMon against the fake AE-9 (tests/fakeae9.py).

Run: python3 -m unittest discover -s tests
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402

support.setup()

from sbae9kit import desktop, testing  # noqa: E402
from sbae9kit.testing import MIC, OPTICAL, SINK, WHAT_U_HEAR  # noqa: E402

from audiomon import core, eventlog  # noqa: E402
from audiomon import theme as T  # noqa: E402
from audiomon.window import MainWindow  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])



def process(ms):
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def wait_until(pred, timeout=4.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.005)
    return pred()


class MonitorTest(unittest.TestCase):
    let_idle = True
    always_capture = False
    card = dict()

    def setUp(self):
        self.pw = support.reset(**self.card)
        self.cfg = core.load_config()
        self.cfg.update(mode="ae9", let_idle=self.let_idle, always_capture=self.always_capture,
                        page=0, frameless=False, glass=False, glass_opacity=70)
        self.log_path = os.path.join(os.environ["XDG_STATE_HOME"], f"{self.id()}.log")
        self.win = MainWindow(self.cfg, eventlog.EventLog(self.log_path))
        self.win.show()
        process(300)

    def tearDown(self):
        self.win.stop()
        self.win.timer.stop()
        self.win.close()
        self.win.deleteLater()
        process(20)

    # helpers
    @property
    def view(self):
        return self.win.monitor.view

    def wired(self, key):
        return self.view.wired.get(key, False)

    def log_text(self):
        with open(self.log_path) as f:
            return f.read()

    def assert_listen_only(self):
        """Every link the app made goes from AE-9 / stream ports into our own meters."""
        for call in self.pw.calls:
            if call[0] == "link":
                in_port = call[2]
                port = self.pw.objects.get(in_port)
                if port:
                    self.assertEqual(self.pw.props(port["info"]["props"]["node.id"]).get("application.name"),
                                     core.APP_NAME,
                                    f"linked into a node that isn't ours: {call}")


class IdleCard(MonitorTest):
    def test_finds_the_card(self):
        log = self.log_text()
        for name in (SINK, WHAT_U_HEAR, MIC, OPTICAL):
            self.assertIn(f"found {name}", log)
        self.assertIn(support.FAKE_DRIVER, log)

    def test_nothing_playing_lets_the_card_suspend(self):
        process(600)
        self.assertEqual(self.pw.state(SINK), "suspended")
        for name in (WHAT_U_HEAR, MIC, OPTICAL):
            self.assertEqual(self.pw.state(name), "suspended", name)
        self.assertTrue(self.wired("monitor"))
        for key in ("wuh", "mic", "optical"):
            self.assertFalse(self.wired(key), key)
        self.assertEqual(self.view.fixed["wuh"].note, "unwired (AE-9 idle)")
        self.assertEqual(self.win.state_lbl.text(), "Suspended")
        self.assert_listen_only()

    def test_monitor_and_stream_meters_are_passive(self):
        self.assertTrue(self.view.fixed["monitor"].capture.passive)
        for key in ("wuh", "mic", "optical"):
            self.assertFalse(self.view.fixed[key].capture.passive, key)


class Captions(MonitorTest):
    def test_short_names_on_screen_jacks_on_hover(self):
        mic = self.view.fixed["mic"]
        self.assertEqual(mic.title_lbl.text(), "Mic / Aux")
        tip = mic.title_lbl.toolTip()
        for detail in ("AE-9 Head Unit Mic / Aux", "XLR/TRS jack", "3.5 mm mic jack", "sbae9-audiomon.ae9.mic"):
            self.assertIn(detail, tip)
        self.assertIn("RCA out", self.view.fixed["monitor"].title_lbl.toolTip())
        self.assertIn("optical in jack", self.view.fixed["optical"].title_lbl.toolTip())
        self.assertEqual(mic.capture.label, "AE-9 Head Unit Mic / Aux")   # the node keeps the full name
        # log lines keep the full names (they're matched against the kernel log)
        self.assertIn("AE-9 Head Unit Mic / Aux: found", self.log_text())


class Playback(MonitorTest):
    def test_stream_gets_a_meter_and_inputs_wire_while_running(self):
        sid = self.pw.add_stream("Firefox", "YouTube", -14)
        self.assertTrue(wait_until(lambda: sid in self.view.streams))
        panel = self.view.streams[sid][0]
        self.assertTrue(panel.capture.passive)
        self.assertTrue(wait_until(lambda: self.wired("wuh") and self.wired("mic")))
        self.assertEqual(self.pw.state(SINK), "running")
        process(400)
        self.assertTrue(panel.live)
        self.assertAlmostEqual(panel.tick_peak_db, -14, delta=0.5)
        self.assertAlmostEqual(self.view.fixed["wuh"].tick_peak_db, -14, delta=0.5)   # level-exact
        self.assertEqual(self.view.silence.count, 0)

        self.pw.stop_stream(sid)
        self.assertTrue(wait_until(lambda: sid not in self.view.streams))
        self.assertTrue(wait_until(lambda: not self.wired("wuh")))
        self.assertTrue(wait_until(lambda: "idle → suspended" in self.log_text()))
        log = self.log_text()
        self.assertIn("App stream started: Firefox — YouTube", log)
        self.assertIn("App stream ended: Firefox — YouTube", log)
        self.assertIn("AE-9 sink state: running → idle", log)
        self.assertIn("AE-9 sink state: idle → suspended", log)
        self.assert_listen_only()

    def test_silent_restart_is_flagged_and_logged(self):
        self.pw.silent_restart = True
        self.pw.add_stream("mpv", "tone.flac", -12)
        self.assertTrue(wait_until(lambda: self.view.silence.active, 5))
        self.assertTrue(self.win.silent_lbl.isVisibleTo(self.win))
        self.assertEqual(self.win.dropouts_lbl.text(), "Dropouts: 1")
        self.assertTrue(self.view.fixed["wuh"].alert)
        log = self.log_text()
        self.assertIn("SILENT #1 start: output monitor -12.0 dBFS, What U Hear -inf dBFS", log)
        self.assertIn(support.FAKE_DRIVER, log.split("SILENT #1 start")[1])

        self.pw.silent_restart = False        # the card comes back
        self.assertTrue(wait_until(lambda: not self.view.silence.active))
        self.assertIn("SILENT #1 end", self.log_text())
        self.assertIn("What U Hear back at", self.log_text())
        self.assertFalse(self.win.silent_lbl.isVisibleTo(self.win))

    def test_no_silent_while_nothing_plays(self):
        self.pw.silent_restart = True
        process(1200)
        self.assertEqual(self.view.silence.count, 0)

    def test_hand_made_links_are_left_alone(self):
        """A link into our meter made in Helvum isn't removed."""
        meter = self.pw.node("sbae9-audiomon.ae9.optical")
        sid = self.pw.add_stream("Helvum test", "x", -20)
        out = [p for p in self.pw.ports(sid) if p["info"]["direction"] == "output"][0]["id"]
        inp = [p for p in self.pw.ports(meter["id"]) if p["info"]["direction"] == "input"][0]["id"]
        lid = self.pw.link_external(out, inp)
        process(800)
        self.assertIn(lid, self.pw.objects)
        self.assertNotIn(("unlink", lid), self.pw.calls)


class CardComesAndGoes(MonitorTest):
    def test_card_drop_and_return(self):
        self.pw.card_present(False)
        self.assertTrue(wait_until(lambda: self.view.fixed["wuh"].note == "not found"))
        log = self.log_text()
        for title in ("AE-9 Output (monitor)", "AE-9 What U Hear", "AE-9 Head Unit Mic / Aux"):
            self.assertIn(f"{title}: disappeared", log)
        self.assertEqual(self.win.state_lbl.text(), "Not found")

        self.pw.card_present(True)
        self.assertTrue(wait_until(lambda: self.wired("monitor")))
        log = self.log_text()
        self.assertIn(f"AE-9 Output (monitor): appeared {SINK}", log)
        self.assertIn(support.FAKE_DRIVER, log.split("appeared " + SINK)[1].splitlines()[0])

        sid = self.pw.add_stream("Firefox", "after reload", -14)   # rewires after the return
        self.assertTrue(wait_until(lambda: self.wired("wuh") and sid in self.view.streams))


class PipeWireRestart(MonitorTest):
    """A PipeWire restart (SBC's What U Hear rate picker does one) isn't the
    silent-restart bug: it's logged as a restart and SILENT is held off."""

    def test_restart_is_named_and_not_flagged(self):
        self.pw.add_stream("Music Player", "song.flac", -12)
        self.assertTrue(wait_until(lambda: self.wired("wuh")))
        self.pw.silent_restart = True         # an active SILENT when PipeWire goes
        self.assertTrue(wait_until(lambda: self.view.silence.active, 5))

        self.pw.down_since = time.monotonic()  # what Graph reports when pw-dump loses PipeWire
        self.assertTrue(wait_until(lambda: not self.view.silence.active))
        process(800)                            # loud output, silent What U Hear: still held off
        self.assertEqual(self.view.silence.count, 1)
        log = self.log_text()
        self.assertIn("PipeWire went away", log)
        self.assertIn("SILENT #1 end", log)
        self.assertIn("(PipeWire restarted)", log)

        self.pw.down_since, self.pw.restarts = None, 1
        self.assertTrue(wait_until(lambda: "PipeWire restarted: rewiring" in self.log_text()))
        self.assertIn(support.FAKE_DRIVER, self.log_text().split("PipeWire restarted: rewiring")[1])
        process(300)                            # inside the grace: no SILENT
        self.assertEqual(self.view.silence.count, 1)
        # after it, a What U Hear that's still silent is the bug again
        self.assertTrue(wait_until(lambda: self.view.silence.active, 5))
        self.assertEqual(self.view.silence.count, 2)


class VirtualAe9Nodes(MonitorTest):
    """A WebRTC echo-cancel pair named after the card ("ae9_voice" source,
    "ae9_voice_ref" sink) is never taken for the AE-9, even with the card gone."""

    def test_voice_nodes_are_not_the_card(self):
        for name, cls in (("ae9_voice", "Audio/Source"), ("ae9_voice_ref", "Audio/Sink")):
            self.pw._node({"node.name": name, "media.class": cls, "node.virtual": True,
                           "node.description": "AE-9 Voice (WebRTC)"}, state="running")
        self.pw._bump()
        process(400)
        self.assertEqual(self.view.present["monitor"], self.pw.node(SINK)["id"])
        self.pw.card_present(False)
        self.assertTrue(wait_until(lambda: self.view.fixed["monitor"].note == "not found"))
        self.assertIsNone(self.view.present["mic"])
        self.assertNotIn("ae9_voice", self.log_text())


class DriverStateInTheLog(MonitorTest):
    """The watched controls and the stream's format go into the log, and a
    dropout line names the driver change just before it."""

    STREAM = ("access: RW_INTERLEAVED\nformat: {fmt}\nsubformat: STD\nchannels: 2\n"
              "rate: 48000 (48000/1)\nperiod_size: 1024\nbuffer_size: 4096\n")

    def tearDown(self):
        support.set_stream("closed")
        super().tearDown()

    def test_changes_logged_and_named_on_silent(self):
        log = self.log_text()
        self.assertIn("Driver state: Headphone Profile Off, PCM Bit Depth 24-bit, Direct Mode on, "
                      "Head Unit Output Headphones, SBX off, Speaker Config 2.1, Surround on, "
                      "Bass Redirect off, Front L level +0 dB, Front R level +0 dB, "
                      "Front L distance 0 cm, Front R distance 0 cm", log)

        support.set_stream(self.STREAM.format(fmt="S24_LE"))
        self.pw.add_stream("Music Player", "song.flac", -12)
        self.assertTrue(wait_until(lambda: "AE-9 stream: S24_LE, 2 ch, 48000 Hz" in self.log_text()))

        support.CONTROLS.set("PCM Bit Depth", "32-bit")
        support.set_stream(self.STREAM.format(fmt="S32_LE"))
        self.assertTrue(wait_until(lambda: "AE-9 stream changed: S32_LE" in self.log_text()))
        self.assertIn("Driver: PCM Bit Depth 24-bit → 32-bit", self.log_text())

        self.pw.silent_restart = True
        self.assertTrue(wait_until(lambda: self.view.silence.active, 5))
        start = self.log_text().split("SILENT #1 start")[1].splitlines()[0]
        self.assertIn("| after: stream S24_LE, 2 ch, 48000 Hz, period 1024, buffer 4096 → S32_LE", start)


class DirectModeSwitch(MonitorTest):
    """Switching Direct Mode restarts the running stream on purpose (an xrun):
    like a PipeWire restart, it ends SILENT and holds it off for a moment."""

    def test_switch_holds_silent(self):
        self.pw.silent_restart = True
        self.pw.add_stream("mpv", "tone.flac", -12)
        self.assertTrue(wait_until(lambda: self.view.silence.active, 5))
        support.CONTROLS.set("Direct Mode Switch", "off")
        self.assertTrue(wait_until(lambda: not self.view.silence.active))
        self.assertIn("SILENT #1 end", self.log_text())
        self.assertIn("(Direct Mode switched off)", self.log_text())
        process(250)                                    # inside the settle: held
        self.assertEqual(self.view.silence.count, 1)
        self.assertTrue(wait_until(lambda: self.view.silence.active, 5))   # still silent after it
        self.assertEqual(self.view.silence.count, 2)
        self.assertIn("after: Direct Mode on → off", self.log_text().split("SILENT #2 start")[1])


class OneSidedInTheWindow(MonitorTest):
    """The fake meters have no samples, so the test feeds the two meters a
    recording where What U Hear's right side goes silent."""

    def test_badge_count_and_log(self):
        self.pw.add_stream("Music Player", "song.flac", -12)
        self.assertTrue(wait_until(lambda: self.wired("wuh")))
        self._feed_r_silent()
        self.assertTrue(wait_until(lambda: self.view.onesided.active, 5))
        self._check_flagged()

    def _feed_r_silent(self):
        import numpy as np
        from audiomon import onesided
        rng = np.random.default_rng(3)
        n, lag = int(onesided.KEEP * core.RATE), 1776
        mon = np.repeat(np.convolve(rng.standard_normal(n + lag), np.ones(8) / 8, "same")[:, None] * 0.3, 2, 1)
        mon[:, 1] = np.roll(mon[:, 1], 7)           # a little stereo
        wuh = mon[:n].copy()
        wuh[:, 1] = 0
        mon = mon[lag:]
        for key, data in (("monitor", mon), ("wuh", wuh)):
            self.view.fixed[key].capture.recent = lambda frames, d=data: (d[-frames:], time.monotonic())

    def _check_flagged(self):
        self.assertTrue(self.win.onesided_lbl.isVisibleTo(self.win))
        self.assertEqual(self.win.onesided_lbl.text(), "ONE-SIDED R")
        self.assertEqual(self.win.dropouts_lbl.text(), "Dropouts: 0 · One-sided: 1")
        self.assertTrue(self.view.fixed["wuh"].alert)
        log = self.log_text()
        self.assertIn("ONE-SIDED R #1 start: What U Hear R went silent", log)
        self.assertIn("saved the last 3 s", log)

        self.pw.stop_stream(next(iter(self.pw.streams)))
        self.assertTrue(wait_until(lambda: not self.view.onesided.active, 5))
        self.assertIn("ONE-SIDED R #1 end", self.log_text())
        self.assertFalse(self.win.onesided_lbl.isVisibleTo(self.win))

    def test_not_judged_on_a_multichannel_stream(self):
        self.pw.add_stream("mpv", "film.mkv", -12)
        self.assertTrue(wait_until(lambda: self.wired("wuh")))
        support.set_stream("format: S32_LE\nchannels: 6\nrate: 48000 (48000/1)\n")
        self._feed_r_silent()
        process(1500)
        support.set_stream("closed")
        self.assertFalse(self.view.onesided.active)
        self.assertEqual(self.view.onesided.count, 0)


class OpticalWithoutLight(MonitorTest):
    let_idle = False

    def test_optical_reads_no_signal(self):
        self.assertTrue(wait_until(lambda: self.wired("optical")))
        process(800)
        optical = self.view.fixed["optical"]
        self.assertTrue(optical.live)                 # the dark jack still sends (silent) data...
        self.assertEqual(optical.note, "no signal")  # ...which reads as no signal
        self.pw.optical_db = -20                      # light on the jack
        self.assertTrue(wait_until(lambda: optical.note is None))
        self.assertAlmostEqual(optical.tick_peak_db, -20, delta=0.5)
        self.pw.optical_db = None                     # and dark again
        self.assertTrue(wait_until(lambda: optical.note == "no signal"))


class KeepAwake(MonitorTest):
    let_idle = False

    def test_everything_wired_and_card_kept_running(self):
        self.assertTrue(wait_until(lambda: all(self.wired(k) for k in ("monitor", "wuh", "mic", "optical"))))
        process(600)
        self.assertEqual(self.pw.state(SINK), "running")
        self.assertEqual(self.pw.state(WHAT_U_HEAR), "running")
        self.assertFalse(self.view.fixed["monitor"].capture.passive)


class AlwaysCapture(MonitorTest):
    always_capture = True

    def test_inputs_wired_but_sink_can_suspend(self):
        self.assertTrue(wait_until(lambda: self.wired("wuh") and self.wired("mic")))
        process(600)
        self.assertEqual(self.pw.state(SINK), "suspended")
        self.assertEqual(self.pw.state(WHAT_U_HEAR), "running")


class NoExtraSources(MonitorTest):
    card = dict(what_u_hear=False, optical=False)

    def test_older_driver_without_what_u_hear(self):
        process(400)
        self.assertEqual(self.view.fixed["wuh"].note, "not found")
        self.assertEqual(self.view.fixed["optical"].note, "no signal")
        self.assertIn("AE-9 What U Hear: not found", self.log_text())
        self.pw.add_stream("Firefox", "YouTube", -14)
        process(1200)
        self.assertEqual(self.view.silence.count, 0)   # can't judge without What U Hear


class RearRcaIn(MonitorTest):
    """Driver 0.10.1's Rear RCA In (the head unit's rear RCA jacks): its own
    meter, wired like Mic / Aux only while the AE-9 plays."""

    def test_metered_while_playing_and_let_idle(self):
        rca = self.view.fixed["rear_rca"]
        self.assertEqual(rca.title_lbl.text(), "Rear RCA In")
        self.assertIn("rear RCA", rca.title_lbl.toolTip())
        self.assertIn(f"AE-9 Rear RCA In: found {testing.REAR_RCA}", self.log_text())
        self.assertFalse(self.wired("rear_rca"))             # card idle: not wired
        self.pw.rear_rca_db = -20
        sid = self.pw.add_stream("Firefox", "YouTube", -14)
        self.assertTrue(wait_until(lambda: self.wired("rear_rca")))
        process(400)
        self.assertAlmostEqual(rca.tick_peak_db, -20, delta=0.5)
        self.assertIsNot(self.view.fixed["mic"].capture, rca.capture)
        self.pw.stop_stream(sid)
        self.assertTrue(wait_until(lambda: not self.wired("rear_rca")))
        self.assert_listen_only()


class WithoutRearRca(MonitorTest):
    card = dict(rear_rca=False)

    def test_older_driver_or_no_config(self):
        process(400)
        self.assertEqual(self.view.fixed["rear_rca"].note, "not found")
        self.assertIn("AE-9 Rear RCA In: not found", self.log_text())
        self.assertEqual(self.view.present["mic"], self.pw.node(MIC)["id"])


class QuickToggles(MonitorTest):
    def test_bottom_bar_and_settings_stay_in_step(self):
        self.win.q_auto.click()
        self.assertFalse(self.win.settings.auto_cb.isChecked())
        self.assertIsNone(self.view)
        self.assertFalse(self.win.q_idle.isEnabled())
        self.win.settings.auto_cb.click()
        self.assertTrue(self.win.q_auto.isChecked())
        self.assertIsNotNone(self.view)
        self.win.q_idle.click()
        self.assertFalse(self.win.settings.idle_cb.isChecked())
        self.assertFalse(self.win.q_capture.isEnabled())
        self.win.go(3)
        self.assertFalse(self.win.quick.isVisibleTo(self.win))


class Glass(MonitorTest):
    def tearDown(self):
        super().tearDown()
        T.GLASS = False
        T.GLASS_OPACITY = 0.70
        app.setStyleSheet("")

    def test_xwayland_only_for_glass_on_wayland(self):
        env = {"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0"}
        desktop.use_xwayland(core.APP_ID, False, env)
        self.assertNotIn("QT_QPA_PLATFORM", env)
        desktop.use_xwayland(core.APP_ID, True, env)
        self.assertEqual(env.get("QT_QPA_PLATFORM"), "xcb" if desktop.xcb_available() else None)
        desktop.use_xwayland(core.APP_ID, False, env)            # the next launch, Glass off: back to Wayland
        self.assertNotIn("QT_QPA_PLATFORM", env)
        mine = {"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0", "QT_QPA_PLATFORM": "wayland"}
        desktop.use_xwayland(core.APP_ID, True, mine)            # one the user set is left alone
        self.assertEqual(mine["QT_QPA_PLATFORM"], "wayland")
        x11 = {"DISPLAY": ":0"}
        desktop.use_xwayland(core.APP_ID, True, x11)             # plain X11 session: nothing to do
        self.assertNotIn("QT_QPA_PLATFORM", x11)

    def test_surfaces_go_see_through_and_opacity_is_live(self):
        self.assertNotIn("rgba(18,15,33", T.stylesheet())   # title bar solid without Glass
        T.GLASS = True
        T.GLASS_OPACITY = 0.7
        self.assertIn("#titleBar { background: rgba(18,15,33,0.7);", T.stylesheet())
        self.assertIn("#root { background: transparent; }", T.stylesheet())
        self.assertAlmostEqual(T.surface_q(T.GRAPH_BG, 0.85).alphaF(), 0.595, places=2)
        self.cfg["glass"] = True
        self.win.settings.sync()
        self.win.settings.opacity.setValue(50)       # keyboard-style change applies at once
        self.assertEqual(self.cfg["glass_opacity"], 50)
        self.assertEqual(T.GLASS_OPACITY, 0.5)
        self.assertIn("rgba(18,15,33,0.5)", app.styleSheet())
        self.assertEqual(self.win.settings.opacity_lbl.text(), "50%")

    def test_toggling_glass_reopens_the_app(self):
        calls = []
        self.win.restart = lambda drop=(): calls.append(drop)
        self.assertFalse(self.win.settings.opacity.isEnabled())
        self.win.settings.glass_cb.click()
        self.assertEqual(calls, [("--glass", "--no-glass")])
        self.assertTrue(self.cfg["glass"])
        self.assertTrue(self.win.settings.opacity.isEnabled())
        self.assertIn("Glass turned on; reopening the window", self.log_text())


if __name__ == "__main__":
    unittest.main()


class OpticalOut(MonitorTest):
    """The separate optical output gets its own meter, next to the main one."""

    def test_meter_found_and_wired(self):
        panel = self.view.fixed["optical_out"]
        self.assertEqual(panel.title_lbl.text(), "Optical Out")
        self.assertIn("hw:AE9,4", panel.title_lbl.toolTip())
        self.assertTrue(wait_until(lambda: self.wired("optical_out")))
        self.assertIn(support.OPTICAL_OUT, self.log_text())

    def test_meter_reads_what_pipewire_sends_to_it(self):
        self.pw.optical_out_db = -18
        self.pw.add_stream("mpv", "movie.mkv", -6)      # the main sink playing too
        panel = self.view.fixed["optical_out"]
        self.assertTrue(wait_until(lambda: panel.tick_peak_db is not None
                                   and panel.tick_peak_db > -20))
        self.assertAlmostEqual(panel.tick_peak_db, -18, delta=0.5)
        self.assert_listen_only()


class Formats(MonitorTest):
    """Every meter shows the stream's rate and bit depth under its stats."""

    def test_every_meter_shows_rate_and_bits(self):
        for key in ("monitor", "optical_out", "wuh", "mic", "optical", "rear_rca"):
            panel = self.view.fixed[key]
            self.assertTrue(wait_until(lambda p=panel: "kHz" in p.fmt_lbl.text()), key)
            self.assertIn("-bit", panel.fmt_lbl.text(), key)
        # playback is 24-bit on the card (PCM Bit Depth), capture 32-bit
        self.assertEqual(self.view.fixed["monitor"].fmt_lbl.text(), "48 kHz · 24-bit")
        self.assertEqual(self.view.fixed["optical_out"].fmt_lbl.text(), "48 kHz · 24-bit")
        self.assertEqual(self.view.fixed["wuh"].fmt_lbl.text(), "96 kHz · 32-bit")
        self.assertEqual(self.view.fixed["rear_rca"].fmt_lbl.text(), "48 kHz · 32-bit")

    def test_app_stream_shows_its_format(self):
        sid = self.pw.add_stream("Firefox", "YouTube", -14)
        self.assertTrue(wait_until(lambda: sid in self.view.streams))
        panel = self.view.streams[sid][0]
        self.pw.objects[sid]["info"]["params"] = {"Format": [
            {"mediaType": "audio", "mediaSubtype": "raw", "format": "F32LE", "rate": 48000,
             "channels": 2, "position": ["FL", "FR"]}]}
        self.pw._bump()
        self.assertTrue(wait_until(lambda: "48 kHz" in panel.fmt_lbl.text()))
        self.assertIn("32-bit", panel.fmt_lbl.text())
