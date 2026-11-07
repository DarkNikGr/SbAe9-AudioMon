# SPDX-License-Identifier: GPL-2.0-only
"""The AE-9 monitor (auto-wiring, SILENT detection) and the manual meters."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

import os

from . import __version__, core, driverstate, onesided, pipewire
from .core import CHANNEL_MAPS, FLOOR_DB, choose_channels, fmt_db, save_config
from .pipewire import Snapshot, find_ae9, is_ours
from .widgets import MeterPanel, label, log_view

# SILENT detection (AE-9 mode): the output monitor has signal but What U Hear
# stays below WUH_SILENT_DB for longer than SILENT_AFTER seconds.
MON_SIGNAL_DB = -50.0   # monitor counts as "has signal" above this (peak)
WUH_SILENT_DB = -60.0   # What U Hear counts as silent below this (peak)
SILENT_AFTER = 0.5      # seconds
WIRE_GRACE = 1.0        # ignore What U Hear for this long after (re)wiring it
PW_RESTART_GRACE = 3.0  # no SILENT for this long after PipeWire comes back
CHANGE_NOTE_FOR = 30.0  # a dropout line names a driver change this recent
HW_PARAMS_EVERY = 0.5   # seconds between looks at the stream's format while it runs
SETTLE = 3.0            # after a stream starts or a driver setting changes: no ONE-SIDED
                        # (and no SILENT after a Direct Mode switch, or while a new stream
                        # moves into the DSP), since the driver restarts or reroutes it


class ManualView(QWidget):
    """The original two meters with unlinked ports, wired by hand."""

    def __init__(self, win):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        self.panels = []
        for m in win.cfg["meters"]:
            def on_channels(ch, m=m):
                m["channels"] = ch
                save_config(win.cfg)
            p = MeterPanel(m["label"], m["name"], m["channels"],
                           on_channels=on_channels, fit=win.fit_window)
            self.panels.append(p)
            layout.addWidget(p)

    def tick(self, now, dt):
        for p in self.panels:
            p.tick(now, dt)

    def stop(self):
        for p in self.panels:
            p.stop()


FIXED_TAPS = (
    # key, full title (log lines, the meter node's name), meter node name,
    # gated (only wired while the AE-9 sink runs when "let the AE-9 idle" is on,
    # so we never hold its capture PCMs open)
    ("monitor", "AE-9 Output (monitor)", "sbae9-audiomon.ae9.monitor", False),
    ("optical_out", "AE-9 Optical Out (monitor)", "sbae9-audiomon.ae9.optical_out", False),
    ("wuh", "AE-9 What U Hear", "sbae9-audiomon.ae9.wuh", True),
    ("mic", "AE-9 Head Unit Mic / Aux", "sbae9-audiomon.ae9.mic", True),
    ("optical", "AE-9 Optical In", "sbae9-audiomon.ae9.optical", True),
    ("rear_rca", "AE-9 Rear RCA In", "sbae9-audiomon.ae9.rear_rca", True),
)

# On screen each meter shows a short name (and its state); where it is, jack by
# jack, is on hover.
SHORT_NAMES = {"monitor": "Output", "optical_out": "Optical Out", "wuh": "What U Hear",
               "mic": "Mic / Aux", "optical": "Optical In", "rear_rca": "Rear RCA In"}
DETAILS = {
    "monitor": "What PipeWire sends to the AE-9: the card's RCA out in speaker mode, or the "
               "head unit's headphone jacks (3.5 mm and 1/4\") in headphone mode.",
    "optical_out": "What PipeWire sends to the separate optical output (alsa_output.ae9_optical, "
                   "hw:AE9,4). Independent of the main output: stereo only, no SBX or EQ, and the "
                   "card's Master volume doesn't change it.",
    "wuh": "What the card actually plays, after its DSP (the card's What U Hear loopback, "
           "alsa_input.ae9_what_u_hear).",
    "mic": "The head unit's input: the XLR/TRS jack and the 3.5 mm mic jack, which share it "
           "(alsa_input.pci-…analog-stereo).",
    "optical": "The card's optical in jack (alsa_input.ae9_optical). With no light on the jack "
               "it reads \"no signal\".",
    "rear_rca": "The head unit's rear RCA L/R input jacks, recorded alongside the mic "
                "(alsa_input.ae9_rear_rca, hw:AE9,3; driver 0.10.1+ with config/ae9-rear-rca-in.conf).",
}


# PipeWire's sample format -> the bit depth it carries.
FORMAT_BITS = {"S16": 16, "S24": 24, "S32": 32, "F32": 32, "F64": 64}


def format_text(node, bits=None):
    """'48 kHz · 24-bit' for a node's current PipeWire format, '' when idle.

    The rate is the node's own; `bits` overrides the sample format's depth
    where the driver plays fewer bits than the container (the card plays
    24-bit from an S32LE stream by default)."""
    for f in (node.params or {}).get("Format") or []:
        rate = f.get("rate")
        fmt = str(f.get("format") or "").upper()
        depth = bits or FORMAT_BITS.get(fmt[:3])
        parts = []
        if rate:
            parts.append(f"{rate / 1000:g} kHz")
        if depth:
            parts.append(f"{depth}-bit")
        return " · ".join(parts)
    return ""


class SilenceDetector:
    """SILENT = output monitor has signal but What U Hear stays quiet."""

    def __init__(self, log, on_change, note=lambda now: ""):
        self.log = log
        self.on_change = on_change
        self.note = note
        self.active = False
        self.count = 0
        self.mon_last_loud = -1e9
        self.mon_loud_since = 0.0
        self.wuh_last_loud = -1e9
        self.wuh_last_data = -1e9
        self.started = 0.0
        self.worst_wuh = None

    def update(self, now, mon_db, wuh_db, wuh_ready_since, sink_state):
        if mon_db is not None and mon_db > MON_SIGNAL_DB:
            if now - self.mon_last_loud > 0.25:
                self.mon_loud_since = now
            self.mon_last_loud = now
        if wuh_db is not None:
            self.wuh_last_data = now
            if wuh_db > WUH_SILENT_DB:
                self.wuh_last_loud = now
        mon_on = now - self.mon_last_loud < 0.25

        silent = False
        if mon_on and wuh_ready_since is not None:
            onset = max(self.mon_loud_since, self.wuh_last_loud, wuh_ready_since + WIRE_GRACE)
            silent = now - onset > SILENT_AFTER

        if silent and not self.active:
            self.active = True
            self.count += 1
            self.started = onset
            no_data = now - self.wuh_last_data > SILENT_AFTER
            wuh = "no data" if no_data else f"{fmt_db(wuh_db if wuh_db is not None else FLOOR_DB)} dBFS"
            self.log(f"SILENT #{self.count} start: output monitor {fmt_db(mon_db or FLOOR_DB)} dBFS, "
                     f"What U Hear {wuh} for {now - onset:.1f}s | sink {sink_state} | {core.driver_info()}"
                     + self.note(now))
            self.on_change()
        elif not silent and self.active:
            if sink_state != "running":
                why = f"playback stopped, AE-9 sink {sink_state}"
            elif not mon_on:
                why = "output monitor went quiet"
            elif wuh_ready_since is None:
                why = "What U Hear unwired/absent"
            else:
                why = f"What U Hear back at {fmt_db(wuh_db if wuh_db is not None else FLOOR_DB)} dBFS"
            self.end(now, why)

    def end(self, now, why):
        if self.active:
            self.active = False
            self.log(f"SILENT #{self.count} end after {now - self.started:.1f}s ({why})")
            self.on_change()


class AE9View(QWidget):
    """Auto-wires meters to the AE-9 and watches for dropouts."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.log = win.log
        self.let_idle = win.cfg["let_idle"]
        self.always_capture = win.cfg["always_capture"]
        self.graph = pipewire.Graph()
        self.linker = pipewire.Linker()
        self.seen_version = -1
        self.last_reconcile = 0.0
        self.pending = {}           # (out_port, in_port) or ("rm", link_id) -> time queued
        self.present = {}           # tap key -> node id
        self.wired = {}             # tap key -> bool
        self.sink_state = None
        self.wuh_ready_since = None
        self.pw_down = False        # PipeWire lost (restarting or stopped)
        self.pw_restarts = 0        # the graph's restart count we've handled
        self.pw_back_at = -1e9      # when PipeWire last came back
        self.reported_failures = set()
        self.silence = SilenceDetector(self.log, self._silence_changed, note=self._change_note)
        self.onesided = onesided.OneSidedDetector(
            self.log, self._silence_changed, note=self._change_note,
            save_dir=os.path.join(os.path.dirname(core.LOG_PATH), "captures"))
        self.controls = driverstate.ControlWatcher()
        self.driver_state = {}      # watched control -> value
        self.last_change = None     # (text, time) of the latest driver change
        self.first_state = {}       # first values, logged together as one line
        self.first_since = None
        self.stopped = False
        self.stream_format = None   # the open playback stream, from hw_params
        self.settle = {}            # "both" / "onesided" -> (until, why)
        self.next_hw_check = 0.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(14)
        self.fixed = {}
        for key, title, node_name, _gated in FIXED_TAPS:
            passive = self.let_idle and key in ("monitor", "optical_out")
            p = MeterPanel(SHORT_NAMES[key], node_name, 2, passive=passive, fit=win.fit_window,
                           node_label=title, keep=onesided.KEEP if key in ("monitor", "wuh") else 0,
                           tooltip=f"{title}\n{DETAILS[key]}\nmeter node: {node_name}")
            p.note = "searching…"
            self.fixed[key] = p
            row.addWidget(p)
        layout.addLayout(row, 5)

        layout.addWidget(label("App streams → AE-9", "sectionTitle"))
        self.streams = {}           # stream node id -> (panel, started_at, title)
        self.streams_box = QHBoxLayout()
        self.streams_box.setContentsMargins(0, 0, 0, 0)
        self.streams_box.setSpacing(14)
        self.no_streams = label("No apps are playing to the AE-9.", "hint")
        self.streams_box.addWidget(self.no_streams)
        self.streams_box.addStretch(1)
        holder = QWidget()
        holder.setLayout(self.streams_box)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(holder)
        self.streams_scroll = scroll
        layout.addWidget(scroll, 4)
        self._size_streams()

        layout.addWidget(label("Recent events", "sectionTitle"))
        self.logview = log_view(80)
        self.logview.setMaximumHeight(120)
        layout.addWidget(self.logview)
        self.log.add_view(self.logview)

        self.log(f"SbAe9-AudioMon {__version__} AE-9 mode started (let the AE-9 idle: "
                 f"{'on' if self.let_idle else 'off'}, always capture inputs: "
                 f"{'on' if self.always_capture else 'off'}) | {core.driver_info()}")

    # -- per-frame -------------------------------------------------------

    def tick(self, now, dt):
        if self.stopped:
            return
        for p in self.fixed.values():
            p.tick(now, dt)
        for p, _, _ in self.streams.values():
            p.tick(now, dt)

        version, _ = self.graph.version, None
        if (version != self.seen_version and now - self.last_reconcile > 0.15) or \
                now - self.last_reconcile > 1.0:
            self.seen_version = version
            self.last_reconcile = now
            _, objs = self.graph.snapshot()
            if objs:
                self.reconcile(Snapshot(objs), now)

        while not self.linker.failures.empty():
            msg = self.linker.failures.get()
            if msg not in self.reported_failures:
                self.reported_failures.add(msg)
                self.log(f"pw-link failed: {msg}")

        self._watch_driver(now)
        self._watch_pipewire(now)
        pw_held = self.pw_down or now - self.pw_back_at < PW_RESTART_GRACE
        both_why = self._settling(now, "both")
        if pw_held or both_why:
            self.silence.end(now, "PipeWire restarted" if pw_held else both_why)
        else:
            self.silence.update(now, self.fixed["monitor"].tick_peak_db, self.fixed["wuh"].tick_peak_db,
                                self.wuh_ready_since, self.sink_state or "absent")

        # ONE-SIDED: only while What U Hear is wired, settled and not SILENT
        ds = self.driver_state
        dsp = ds.get("Direct Mode Switch") == "off"
        stream_ch = driverstate.channels(self.stream_format)
        if pw_held:
            held = "PipeWire restarted"
        elif both_why or self._settling(now, "onesided"):
            held = both_why or self._settling(now, "onesided")
        elif (stream_ch or 2) > 2 or self.fixed["monitor"].channels > 2:
            held = "multichannel stream (What U Hear carries only the front pair)"
        elif self.sink_state != "running":
            held = f"playback stopped, AE-9 sink {self.sink_state or 'absent'}"
        elif self.wuh_ready_since is None or now - self.wuh_ready_since < WIRE_GRACE:
            held = "What U Hear unwired/absent"
        elif self.silence.active:
            held = "SILENT"
        else:
            held = ""
        num = driverstate.number
        setup = {}
        if dsp:     # the speaker setup only applies in DSP mode
            setup["sbx"] = ds.get("SBX Switch") == "on"
            lvl = num(ds.get("Speaker Level Front L Volume")), num(ds.get("Speaker Level Front R Volume"))
            if None not in lvl:
                setup["level_offset_db"] = lvl[1] - lvl[0]
            dist = num(ds.get("Speaker Distance Front L")), num(ds.get("Speaker Distance Front R"))
            if None not in dist:
                setup["side_delay"] = onesided.delay_frames(*dist)
            setup["no_lows"] = (ds.get("Bass Redirect Switch") == "on"
                                and ds.get("Speaker Config") in ("2.1", "4.1", "5.1"))
        self.onesided.update(now, self.fixed["monitor"].capture, self.fixed["wuh"].capture,
                             not held, held, **setup)

    def _settle_for(self, now, which, why):
        until, _ = self.settle.get(which, (0.0, ""))
        if now + SETTLE > until:
            self.settle[which] = (now + SETTLE, why)

    def _settling(self, now, which):
        """Why `which` is held right now, or ""."""
        until, why = self.settle.get(which, (0.0, ""))
        return why if now < until else ""

    # -- driver state ----------------------------------------------------

    def _watch_driver(self, now):
        """Log the watched controls (once, then each change) and the playback
        stream's format whenever it opens or changes."""
        for name, value in self.controls.poll():
            old = self.driver_state.get(name)
            self.driver_state[name] = value
            if old is None:
                self.first_state[name] = value
                self.first_since = self.first_since or now
            elif old != value:
                text = f"{driverstate.short(name)} {old} → {value}"
                self.last_change = (text, now)
                self.log(f"Driver: {text}")
                self._settle_for(now, "onesided", f"settling after {text}")
                if name == "Direct Mode Switch":    # the driver restarts the running stream
                    self._settle_for(now, "both", f"Direct Mode switched {value}")
        if self.first_state and (len(self.first_state) >= len(driverstate.WATCH)
                                 or now - self.first_since > 1.0):
            self.log("Driver state: " + ", ".join(f"{driverstate.short(n)} {v}"
                                                  for n, v in self.first_state.items()))
            self.first_state, self.first_since = {}, None

        if self.sink_state != "running":
            self.stream_format = None
        elif now >= self.next_hw_check:
            self.next_hw_check = now + HW_PARAMS_EVERY
            fmt = driverstate.read_hw_params()
            if fmt and fmt != self.stream_format:
                if self.stream_format:
                    self.last_change = (f"stream {self.stream_format} → {fmt}", now)
                    self.log(f"AE-9 stream changed: {fmt}")
                    self._settle_for(now, "onesided", "stream restarted")
                else:
                    self.log(f"AE-9 stream: {fmt}")
                self.stream_format = fmt

    def _change_note(self, now):
        """' | after: <change>, 1.2 s before' for a dropout line, when a driver
        change came shortly before it."""
        if not self.last_change or now - self.last_change[1] > CHANGE_NOTE_FOR:
            return ""
        text, at = self.last_change
        return f" | after: {text}, {now - at:.1f} s before"

    def _watch_pipewire(self, now):
        """A PipeWire restart (Sound Blaster Command's What U Hear rate picker
        does one) takes every meter and link with it. Name it in the log and
        hold SILENT off until the meters are rewired, so the gap isn't taken
        for the silent-restart bug."""
        down = getattr(self.graph, "down_since", None) is not None
        restarts = getattr(self.graph, "restarts", 0)
        if down and not self.pw_down:
            self.pw_down = True
            self.log("PipeWire went away (restarting?): meters paused, SILENT held off")
            self._pipewire_reset(now)
        if restarts != self.pw_restarts:
            self.pw_restarts = restarts
            self.pw_down = False
            self.pw_back_at = now
            self.log(f"PipeWire restarted: rewiring the meters | {core.driver_info()}")
            self._pipewire_reset(now)

    def _pipewire_reset(self, now):
        self.silence.end(now, "PipeWire restarted")
        self.pending.clear()            # old link requests died with PipeWire
        self.reported_failures.clear()
        self.wuh_ready_since = None

    def _silence_changed(self):
        self.fixed["wuh"].alert = self.silence.active or self.onesided.active
        self.win.set_silent(self.silence.active, self.silence.count)
        self.win.set_one_sided(self.onesided.side if self.onesided.active else None, self.onesided.count)

    # -- graph reconciliation ------------------------------------------------

    def reconcile(self, snap, now):
        found = find_ae9(snap)
        sink = found["monitor"]

        # Card nodes coming and going
        for key, title, _, _ in FIXED_TAPS:
            node = found[key]
            cur = node.id if node else None
            if key not in self.present:
                self.log(f"{title}: " + (f"found {node.name} (id {node.id})" if node else "not found"))
            elif cur != self.present[key]:
                if node:
                    extra = f" | {core.driver_info()}" if key == "monitor" else ""
                    self.log(f"{title}: appeared {node.name} (id {node.id}){extra}")
                else:
                    self.log(f"{title}: disappeared")
            self.present[key] = cur

        state = sink.state if sink else None
        if state != self.sink_state:
            if sink:
                self.log(f"AE-9 sink state: {self.sink_state or '-'} → {state}"
                         + (f" (error: {sink.error})" if sink.error else ""))
            if state == "running":
                self._settle_for(now, "onesided", "stream starting")
                if self.driver_state.get("Direct Mode Switch") == "off":
                    # the driver starts a new stream direct, then moves it into the DSP
                    self._settle_for(now, "both", "stream moving into the DSP")
            self.sink_state = state
            self.win.set_sink_state(state)
        sink_running = state == "running"

        # Fixed taps
        for key, title, _, gated in FIXED_TAPS:
            panel = self.fixed[key]
            node = found[key]
            if node is None:
                panel.note = "no signal" if key == "optical" else "not found"
                panel.set_format("")
                self._set_wired(key, title, False, "absent")
                continue
            bits = (driverstate.playback_bits() if key in ("monitor", "optical_out")
                    else driverstate.capture_bits())
            panel.set_format(format_text(node, bits))
            src = snap.out_ports(node, monitor=(key in ("monitor", "optical_out")))
            want = choose_channels(len(src)) if src else panel.channels
            if want != panel.channels:
                panel.start(want)
            hold_off = self.let_idle and gated and not sink_running and not self.always_capture
            wired = self._wire(snap, panel, src, [] if hold_off else None, now)
            # An unlit optical jack still runs and sends digital silence (or, on
            # some drivers, nothing at all): either way that's "no signal".
            if key == "optical" and (node.state == "error" or (wired and now - panel.last_sound > 0.5)):
                panel.note = "no signal"
            elif hold_off:
                panel.note = "unwired (AE-9 idle)"
            else:
                panel.note = None if wired else "wiring…"
            self._set_wired(key, title, wired, f"AE-9 sink {state}" if hold_off else "")

        wuh_ok = self.wired.get("wuh") and found["wuh"] is not None
        if wuh_ok and self.wuh_ready_since is None:
            self.wuh_ready_since = now
        elif not wuh_ok:
            self.wuh_ready_since = None

        # App streams playing to the AE-9 sink
        current = {}
        if sink:
            for link in snap.links.values():
                if link.in_node != sink.id:
                    continue
                n = snap.nodes.get(link.out_node)
                if n and n.media_class.startswith("Stream/Output/Audio") and not is_ours(n):
                    current[n.id] = n
        for sid in list(self.streams):
            if sid not in current:
                panel, started, title = self.streams.pop(sid)
                panel.stop()
                panel.setParent(None)
                panel.deleteLater()
                self.log(f"App stream ended: {title} (node {sid}, played {now - started:.0f}s)")
        for sid, n in current.items():
            src = snap.out_ports(n)
            if sid not in self.streams:
                app = n.props.get("application.name") or n.desc or n.name
                media = n.props.get("media.name") or n.name
                title = f"{app} — {media}" if media and media != app else app
                panel = MeterPanel(app, f"sbae9-audiomon.stream.{sid}", choose_channels(max(len(src), 1)),
                                   subtitle=media, passive=self.let_idle,
                                   tooltip=f"{title}\nsource node: {n.name} (id {sid})\n"
                                           f"meter node: sbae9-audiomon.stream.{sid}")
                self.streams[sid] = (panel, now, title)
                self.streams_box.insertWidget(self.streams_box.count() - 1, panel)
                self.log(f"App stream started: {title} (node {sid}, {len(src)} ch)")
            self.streams[sid][0].set_format(format_text(n))
            self._wire(snap, self.streams[sid][0], src, None, now)
        if self.no_streams.isHidden() != bool(self.streams):
            self.no_streams.setVisible(not self.streams)
            self._size_streams()

    def _size_streams(self):
        """Give the app-stream row room only while there are streams."""
        if self.streams:
            self.streams_scroll.setMinimumHeight(310)
            self.streams_scroll.setMaximumHeight(16777215)
        else:
            self.streams_scroll.setMinimumHeight(28)
            self.streams_scroll.setMaximumHeight(28)

    def _set_wired(self, key, title, wired, why):
        if self.wired.get(key, False) != wired:
            if key in self.wired or wired:
                self.log(f"{title}: {'wired' if wired else 'unwired'}" + (f" ({why})" if why else ""))
            self.wired[key] = wired

    def _wire(self, snap, panel, src_ports, override, now):
        """Link src_ports into the panel's meter node. override=[] unwires.

        Only links from src_ports into our own meter are ever created or
        removed; anything else (e.g. links made by hand in Helvum) is left alone.
        Returns True when every wanted link exists.
        """
        meter = snap.by_name(panel.node_name)
        if meter is None:
            return False
        meter_in = {p.name: p for p in snap.node_ports.get(meter.id, []) if not p.out}
        names = CHANNEL_MAPS[panel.channels]
        wanted = set()
        if override is None:
            if len(src_ports) == 1 and src_ports[0].channel not in names:
                targets = [meter_in.get(f"input_{c}") for c in names[:2]]
                wanted = {(src_ports[0].id, t.id) for t in targets if t}
            else:
                for i, sp in enumerate(src_ports):
                    ch = sp.channel if sp.channel in names else (names[i] if i < len(names) else None)
                    t = meter_in.get(f"input_{ch}") if ch else None
                    if t:
                        wanted.add((sp.id, t.id))
        src_ids = {p.id for p in src_ports}
        in_ids = {p.id for p in meter_in.values()}
        existing = {(l.out_port, l.in_port): l.id for l in snap.links.values()
                    if l.in_port in in_ids and l.out_port in src_ids}
        for pair in wanted - existing.keys():
            if now - self.pending.get(pair, -10) > 3.0:
                self.pending[pair] = now
                self.linker.link(*pair)
        for pair, lid in existing.items():
            if pair not in wanted and now - self.pending.get(("rm", lid), -10) > 3.0:
                self.pending[("rm", lid)] = now
                self.linker.unlink(lid)
        return bool(wanted) and wanted <= existing.keys()

    def stop(self):
        self.stopped = True
        if self.silence.active:
            self.log(f"SILENT #{self.silence.count} still active at exit")
        if self.onesided.active:
            self.log(f"ONE-SIDED {self.onesided.side} #{self.onesided.count} still active at exit")
        self.controls.stop()
        self.log("SbAe9-AudioMon AE-9 mode stopped")
        self.log.remove_view(self.logview)
        self.graph.stop()
        for p in self.fixed.values():
            p.stop()
        for p, _, _ in self.streams.values():
            p.stop()
