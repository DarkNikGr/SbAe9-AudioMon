# SPDX-License-Identifier: GPL-2.0-only
"""The main window: title bar, sidebar pages, status bar."""

import time

from PySide6.QtCore import QEvent, QSignalBlocker, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QRadioButton,
    QSlider, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from sbae9kit import frame

from . import __version__, core
from . import theme as T
from .core import CONFIG_PATH, save_config
from . import monitor, onesided
from .monitor import AE9View, ManualView
from .theme import app_icon, make_icon, rgba, stylesheet
from .widgets import StatusDot, hline, label, log_view


class Page(QWidget):
    """Content page: big title (with optional widgets on its right) + body."""

    def __init__(self, title):
        super().__init__()
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(26, 18, 26, 18)
        self.body.setSpacing(12)
        self.head = QHBoxLayout()
        self.head.setSpacing(12)
        self.head.addWidget(label(title, "pageTitle"))
        self.head.addStretch(1)
        self.body.addLayout(self.head)


class MonitorPage(Page):
    """AE-9 meters, app streams and recent events. Hosts the AE9View."""

    def __init__(self, win):
        super().__init__("Monitor")
        self.win = win
        self.view = None
        self.off_banner = QFrame()
        self.off_banner.setObjectName("banner")
        bl = QHBoxLayout(self.off_banner)
        bl.setContentsMargins(14, 10, 14, 10)
        bl.addWidget(label("Auto-wire AE-9 is off, so nothing is being monitored. "
                           "Turn it on in Settings, or use Manual Meters.", wrap=True), 1)
        self.body.addWidget(self.off_banner)
        self.holder = QVBoxLayout()
        self.body.addLayout(self.holder, 1)

    def rebuild(self):
        if self.view:
            self.view.stop()
            self.view.setParent(None)
            self.view.deleteLater()
            self.view = None
        on = self.win.cfg["mode"] == "ae9"
        self.off_banner.setVisible(not on)
        if on:
            self.view = AE9View(self.win)
            self.holder.addWidget(self.view)

    def tick(self, now, dt):
        if self.view:
            self.view.tick(now, dt)

    def stop(self):
        if self.view:
            self.view.stop()


class LogPage(Page):
    def __init__(self, win):
        super().__init__("Event Log")
        open_btn = QPushButton(" Open log file")
        open_btn.setIcon(make_icon("folder", T.TEXT, size=18))
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(win.log.path)))
        self.head.addWidget(open_btn)
        self.body.addWidget(label(
            f"Saved to {win.log.path}. Local time, to the second, so it lines up with "
            "journalctl -k (and the driver's ae9-pause-test: lines).", "hint", wrap=True))
        self.view = log_view(200)
        self.body.addWidget(self.view, 1)
        win.log.add_view(self.view, backfill=True)


class ManualPage(Page):
    """Two hand-wired meters, created the first time the page is opened."""

    def __init__(self, win):
        super().__init__("Manual Meters")
        self.win = win
        self.view = None
        self.body.addWidget(label(
            "Two meters with unlinked input ports (sbae9-audiomon.in and .out). Wire anything into them with Helvum, "
            "qpwgraph or pw-link. They run alongside the AE-9 monitor.", "hint", wrap=True))
        self.holder = QVBoxLayout()
        self.body.addLayout(self.holder, 1)

    def showEvent(self, e):
        if self.view is None:
            self.view = ManualView(self.win)
            self.holder.addWidget(self.view)
        super().showEvent(e)

    def tick(self, now, dt):
        if self.view:
            self.view.tick(now, dt)

    def stop(self):
        if self.view:
            self.view.stop()


class SettingsPage(Page):
    def __init__(self, win):
        super().__init__("Settings")
        self.win = win
        cfg = win.cfg

        def check(text, hint, checked, slot):
            cb = QCheckBox(text)
            cb.setChecked(checked)
            cb.toggled.connect(slot)
            self.body.addWidget(cb)
            h = label(hint, "hint", wrap=True)
            h.setContentsMargins(30, 0, 0, 6)
            self.body.addWidget(h)
            return cb

        self.body.addWidget(label("Monitoring", "sectionTitle"))
        self.auto_cb = check(
            "Auto-wire AE-9",
            "Meter the AE-9's output, the optical output, What U Hear, Mic / Aux, Optical In, Rear RCA In and "
            "every app stream playing to the AE-9. Listen-only: never changes defaults, moves "
            "streams or changes mixer controls (it only reads a few, for the log).",
            cfg["mode"] == "ae9", win.set_auto)
        self.idle_cb = check(
            "Let the AE-9 idle",
            "Meters never keep the card awake. The output, optical output and app meters are passive, and "
            "What U Hear / Mic / Aux / Optical In / Rear RCA In are only wired while the AE-9 sink is running. "
            "Off: everything stays wired and the AE-9 never suspends.",
            cfg["let_idle"], win.set_let_idle)
        self.capture_cb = check(
            "Always capture inputs",
            "Comparison mode: keep What U Hear / Mic / Aux / Optical In / Rear RCA In wired while the card idles "
            "(their capture converters and DMA stay running). The sink can still suspend.",
            cfg["always_capture"], win.set_always_capture)

        self.body.addSpacing(6)
        self.body.addWidget(label("Window", "sectionTitle"))
        self.frame_cb = check(
            "Use the desktop's title bar",
            "Off: the app draws its own title bar, like Sound Blaster Command.",
            not cfg["frameless"], lambda on: win.set_frameless(not on))
        self.glass_cb = check(
            "Glass",
            "See-through backgrounds, like Sound Blaster Command's Glass; text, meters and "
            "controls stay solid. On KDE, what's behind is frosted (the app runs through "
            "XWayland for that). Switching Glass on or off reopens the window, which restarts "
            "monitoring for about a second (logged).",
            cfg["glass"], win.set_glass)
        row = QHBoxLayout()
        row.setContentsMargins(30, 0, 0, 6)
        row.setSpacing(12)
        row.addWidget(QLabel("Opacity"))
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(*T.GLASS_RANGE)
        self.opacity.setSingleStep(5)
        self.opacity.setPageStep(10)
        self.opacity.setFixedWidth(220)
        self.opacity.setValue(T.clamp_glass(cfg["glass_opacity"]))
        self.opacity.valueChanged.connect(self._opacity_moved)
        self.opacity.sliderReleased.connect(lambda: win.set_glass_opacity(self.opacity.value()))
        row.addWidget(self.opacity)
        self.opacity_lbl = label(f"{self.opacity.value()}%", "bigValue")
        self.opacity_lbl.setMinimumWidth(50)
        row.addWidget(self.opacity_lbl)
        row.addStretch(1)
        self.body.addLayout(row)

        # palette, shared with Sound Blaster Command (sbae9kit.palettes)
        prow = QHBoxLayout()
        prow.setContentsMargins(0, 8, 0, 0)
        prow.setSpacing(12)
        prow.addWidget(QLabel("Palette"))
        self.palette = QComboBox()
        for key, pal in T.PALETTES.items():
            self.palette.addItem(pal["label"], key)
        self.palette.setCurrentIndex(max(0, self.palette.findData(T.CURRENT)))
        self.palette.setToolTip("The same palettes as Sound Blaster Command (the meters restart briefly)")
        self.palette.currentIndexChanged.connect(lambda i: win.set_palette(self.palette.itemData(i)))
        prow.addWidget(self.palette)
        prow.addSpacing(24)
        prow.addWidget(QLabel("UI scale"))
        self.scale = QSlider(Qt.Orientation.Horizontal)
        self.scale.setRange(*T.SCALES)
        self.scale.setSingleStep(5)
        self.scale.setPageStep(10)
        self.scale.setFixedWidth(180)
        self.scale.setValue(T.SCALE)
        self.scale_lbl = label(f"{T.SCALE}%", "bigValue")
        self.scale_lbl.setMinimumWidth(50)
        self.scale_apply = QPushButton("Apply")
        self.scale_apply.setToolTip("Saves the scale and reopens the window at the new size")
        self.scale_apply.setEnabled(False)
        self.scale.valueChanged.connect(self._scale_moved)
        self.scale_apply.clicked.connect(lambda: win.set_scale(self.scale.value()))
        prow.addWidget(self.scale)
        prow.addWidget(self.scale_lbl)
        prow.addWidget(self.scale_apply)
        prow.addStretch(1)
        self.body.addLayout(prow)

        self.body.addSpacing(6)
        self.body.addWidget(label("Device", "sectionTitle"))
        self.info = label("", "hint", wrap=True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.addWidget(self.info)
        self.body.addStretch(1)
        self.sync()

    def _scale_moved(self, v):
        snapped = T.clamp_scale(v)
        if snapped != v:
            self.scale.setValue(snapped)
            return
        self.scale_lbl.setText(f"{v}%")
        self.scale_apply.setEnabled(v != T.SCALE)

    def _opacity_moved(self, v):
        snapped = T.clamp_glass(v)
        if snapped != v:
            self.opacity.setValue(snapped)
            return
        self.opacity_lbl.setText(f"{v}%")
        if not self.opacity.isSliderDown():   # keyboard / wheel: apply now
            self.win.set_glass_opacity(v)

    def sync(self):
        self.opacity.setEnabled(self.win.cfg["glass"])
        on = self.win.cfg["mode"] == "ae9"
        self.idle_cb.setEnabled(on)
        self.capture_cb.setEnabled(on and self.win.cfg["let_idle"])

    def showEvent(self, e):
        self.info.setText(
            f"SbAe9-AudioMon {__version__}<br>"
            f"Driver: {core.driver_info()}<br>"
            f"SILENT: output monitor above {monitor.MON_SIGNAL_DB:.0f} dBFS while What U Hear stays below "
            f"{monitor.WUH_SILENT_DB:.0f} dBFS for over {monitor.SILENT_AFTER} s ({monitor.WIRE_GRACE:.0f} s grace after "
            f"wiring What U Hear)<br>"
            f"ONE-SIDED: one What U Hear channel silent, noisy or {onesided.LEVEL_SKEW_DB:.0f} dB off while the "
            f"other still matches the output, for over {onesided.ONE_SIDED_AFTER} s; the last "
            f"{onesided.SAVE_SECONDS:.0f} s are saved as a WAV<br>"
            f"Log: {self.win.log.path}<br>Config: {CONFIG_PATH}")
        super().showEvent(e)


class AppTitleBar(frame.TitleBar):
    """The window's header on the kit's TitleBar (drag to move, double-click
    to maximize when frameless): the app's name, AE-9, window buttons."""

    def __init__(self, win):
        super().__init__(win)
        h = QHBoxLayout(self)
        h.setContentsMargins(18, 8, 10, 6)
        h.setSpacing(4)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(label("SbAe9-AudioMon", "appTitle"))
        names.addWidget(label("AE-9", "deviceName"))
        h.addLayout(names)
        h.addStretch(1)
        self.buttons = []
        for name, tip, slot in (("minimize", "Minimize", win.showMinimized),
                                ("maximize", "Maximize",
                                 lambda: win.showNormal() if win.isMaximized() else win.showMaximized()),
                                ("close", "Close", win.close)):
            b = QToolButton()
            b.setObjectName("winClose" if name == "close" else "winButton")
            b.setIcon(make_icon(name, T.MUTED, active=T.TEXT, size=18))
            b.setIconSize(QSize(18, 18))
            b.setFixedSize(34, 28)
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(slot)
            h.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
            self.buttons.append(b)


NAV = (("Monitor", "wave"), ("Event Log", "log"), None, ("Manual Meters", "mixer"))
NAV_BOTTOM = (("Settings", "settings"),)


class MainWindow(QWidget):
    def __init__(self, cfg, log):
        super().__init__()
        self.cfg = cfg
        self.log = log
        self.setObjectName("root")
        self.setWindowTitle("SbAe9-AudioMon — AE-9")
        self.setWindowIcon(app_icon())
        self.resize(1320, 860)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.title_bar = AppTitleBar(self)
        v.addWidget(self.title_bar)

        mid = QHBoxLayout()
        mid.setSpacing(0)
        v.addLayout(mid, 1)

        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(210)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 10, 0, 12)
        sl.setSpacing(2)
        sl.addWidget(label("AE-9 Monitoring", "sideCaption"))
        sl.addSpacing(4)
        self.stack = QStackedWidget()
        self.stack.setObjectName("content")
        self.nav_buttons = []

        self.monitor = MonitorPage(self)
        self.log_page = LogPage(self)
        self.manual = ManualPage(self)
        self.settings = SettingsPage(self)
        pages = {"Monitor": self.monitor, "Event Log": self.log_page,
                 "Manual Meters": self.manual, "Settings": self.settings}

        def add(entry):
            name, ic = entry
            idx = self.stack.addWidget(pages[name])
            b = QPushButton(" " + name)
            b.setCheckable(True)
            b.setIcon(make_icon(ic, T.TEXT, active=T.ACCENT_HI, size=22))
            b.setIconSize(QSize(22, 22))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, i=idx: self.go(i))
            self.nav_buttons.append(b)
            sl.addWidget(b)

        for entry in NAV:
            if entry is None:
                sl.addWidget(hline())
            else:
                add(entry)
        sl.addStretch(1)
        sl.addWidget(hline())
        for entry in NAV_BOTTOM:
            add(entry)
        mid.addWidget(side)
        mid.addWidget(self.stack, 1)

        # bottom status bar
        bottom = QFrame()
        bottom.setObjectName("bottomBar")
        bottom.setFixedHeight(56)
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(20, 0, 22, 0)
        bl.setSpacing(16)
        self.dot = StatusDot()
        bl.addWidget(self.dot)
        bl.addWidget(label("AE-9", "logo"))
        sep = QFrame()
        sep.setFixedSize(2, 26)
        sep.setStyleSheet(f"background:{T.TEXT};")
        bl.addWidget(sep)
        self.state_lbl = label("", "profileLabel")
        bl.addWidget(self.state_lbl)

        # Quick access to Settings -> Monitoring (shown on the Monitor page)
        self.quick = QWidget()
        ql = QHBoxLayout(self.quick)
        ql.setContentsMargins(10, 0, 0, 0)
        ql.setSpacing(16)
        qsep = QFrame()
        qsep.setFixedSize(1, 22)
        qsep.setStyleSheet(f"background:{rgba(T.OVERLAY, 0.25)};")
        ql.addWidget(qsep)

        def quick(text, tip, slot):
            rb = QRadioButton(text)
            rb.setObjectName("quickToggle")
            rb.setAutoExclusive(False)      # independent on/off switches
            rb.setToolTip(tip)
            rb.setCursor(Qt.CursorShape.PointingHandCursor)
            rb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            rb.toggled.connect(slot)
            ql.addWidget(rb)
            return rb

        self.q_auto = quick("Auto-wire", "Auto-wire AE-9 (Settings → Monitoring)", self.set_auto)
        self.q_idle = quick("Let idle", "Let the AE-9 idle (Settings → Monitoring)", self.set_let_idle)
        self.q_capture = quick("Always capture", "Always capture inputs (Settings → Monitoring)",
                               self.set_always_capture)
        bl.addWidget(self.quick)
        bl.addStretch(1)
        self.silent_lbl = label("SILENT", "silentBadge")
        self.silent_lbl.setToolTip(f"Output monitor has signal but What U Hear is below "
                                   f"{monitor.WUH_SILENT_DB:.0f} dBFS for more than {monitor.SILENT_AFTER}s")
        self.silent_lbl.hide()
        bl.addWidget(self.silent_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        self.onesided_lbl = label("ONE-SIDED", "silentBadge")
        self.onesided_lbl.setToolTip("One What U Hear channel stopped matching the output (silent, noisy "
                                     "or off in level) while the other still does. See the Event Log.")
        self.onesided_lbl.hide()
        bl.addWidget(self.onesided_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        self.dropouts_lbl = label("", "bigValue")
        bl.addWidget(self.dropouts_lbl)
        v.addWidget(bottom)

        if T.GLASS:
            # see-through backgrounds; must be set before the window is created
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.restart = None          # set by main(): reopen the app (Glass, UI scale)
        self.repalette = None        # set by main(): rebuild the window in a new palette
        self.grips = frame.make_grips(self)
        self.frameless = False
        self.set_frameless(cfg["frameless"], save=False)

        self._silent = False
        self._silent_count = 0
        self._onesided = None       # "L" / "R" while ONE-SIDED is flagged
        self._onesided_count = 0
        self._sink_state = None
        self.sync_controls()
        self.monitor.rebuild()
        self._refresh_status()
        page = cfg.get("page", 0)
        self.go(page if isinstance(page, int) and 0 <= page < self.stack.count() else 0)

        self.last_tick = time.monotonic()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)

    # -- navigation / settings -------------------------------------------

    def go(self, idx):
        self.stack.setCurrentIndex(idx)
        self.quick.setVisible(self.stack.currentWidget() is self.monitor)
        for i, b in enumerate(self.nav_buttons):
            b.setChecked(i == idx)
        if self.cfg.get("page") != idx:
            self.cfg["page"] = idx
            save_config(self.cfg)

    def sync_controls(self):
        """Keep the Settings checkboxes and the bottom-bar toggles in step."""
        cfg = self.cfg
        auto = cfg["mode"] == "ae9"
        for widget, value in ((self.settings.auto_cb, auto), (self.q_auto, auto),
                              (self.settings.idle_cb, cfg["let_idle"]), (self.q_idle, cfg["let_idle"]),
                              (self.settings.capture_cb, cfg["always_capture"]),
                              (self.q_capture, cfg["always_capture"])):
            blocker = QSignalBlocker(widget)
            widget.setChecked(value)
            del blocker
        self.settings.sync()
        self.q_idle.setEnabled(auto)
        self.q_capture.setEnabled(auto and cfg["let_idle"])

    def set_auto(self, on):
        if (self.cfg["mode"] == "ae9") == on:
            return
        self.cfg["mode"] = "ae9" if on else "manual"
        save_config(self.cfg)
        self.sync_controls()
        self.monitor.rebuild()
        self._sink_state = None
        self._silent = False
        self._onesided = None
        self._silent_count = self._onesided_count = 0
        self.dropouts_lbl.setText("")
        self._refresh_status()

    def set_let_idle(self, on):
        if self.cfg["let_idle"] == on:
            return
        self.cfg["let_idle"] = on
        save_config(self.cfg)
        self.sync_controls()
        self.log(f"'Let the AE-9 idle' turned {'on' if on else 'off'}; rebuilding meters")
        self.monitor.rebuild()

    def set_always_capture(self, on):
        if self.cfg["always_capture"] == on:
            return
        self.cfg["always_capture"] = on
        save_config(self.cfg)
        self.sync_controls()
        self.log(f"'Always capture inputs' turned {'on' if on else 'off'}; rebuilding meters")
        self.monitor.rebuild()

    def set_frameless(self, on, save=True):
        was_visible = self.isVisible()
        self.frameless = on
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, on)  # hides the window
        for b in self.title_bar.buttons:
            b.setVisible(on)
        self._place_grips()
        if save:
            self.cfg["frameless"] = on
            save_config(self.cfg)
        if was_visible:
            self.show()

    def _place_grips(self):
        frame.place_grips(self, self.grips,
                          self.frameless and not (self.isMaximized() or self.isFullScreen()))

    def showEvent(self, e):
        super().showEvent(e)
        if T.GLASS:
            T.blur_behind(self)

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.Type.WindowStateChange:
            self._place_grips()

    def set_palette(self, name):
        if name == T.CURRENT or name not in T.PALETTES:
            return
        self.cfg["palette"] = name
        save_config(self.cfg)
        if self.repalette:
            self.log(f"Palette: {T.PALETTES[name]['label']}; rebuilding the window (meters restart)")
            self.repalette(name)

    def set_scale(self, pct):
        pct = T.clamp_scale(pct)
        self.cfg["ui_scale"] = pct
        save_config(self.cfg)
        if self.restart and pct != T.SCALE:
            self.log(f"UI scale {pct}%; reopening the window (monitoring restarts)")
            self.restart(drop=("--scale",))

    def set_glass(self, on):
        if self.cfg["glass"] == on:
            return
        self.cfg["glass"] = on
        save_config(self.cfg)
        self.settings.sync()
        if self.restart:
            self.log(f"Glass turned {'on' if on else 'off'}; reopening the window "
                     "(monitoring restarts)")
            self.restart(drop=("--glass", "--no-glass"))

    def set_glass_opacity(self, pct):
        pct = T.clamp_glass(pct)
        if self.cfg["glass_opacity"] == pct and T.GLASS_OPACITY == pct / 100:
            return
        self.cfg["glass_opacity"] = pct
        save_config(self.cfg)
        T.GLASS_OPACITY = pct / 100
        QApplication.instance().setStyleSheet(stylesheet())
        for w in [self] + self.findChildren(QWidget):
            w.update()

    def resizeEvent(self, e):
        self._place_grips()
        super().resizeEvent(e)

    # -- status bar ------------------------------------------------------

    def set_sink_state(self, state):
        self._sink_state = state
        self._refresh_status()

    def set_silent(self, active, count):
        self._silent = active
        self._silent_count = count
        self._set_counts()

    def set_one_sided(self, side, count):
        """side: "L" / "R" while flagged, else None."""
        self._onesided = side
        self._onesided_count = count
        self._set_counts()

    def _set_counts(self):
        text = f"Dropouts: {self._silent_count}"
        if self._onesided_count:
            text += f" · One-sided: {self._onesided_count}"
        self.dropouts_lbl.setText(text)
        self._refresh_status()

    def _refresh_status(self):
        if self.cfg["mode"] != "ae9":
            self.dot.set_color(T.DIM)
            self.state_lbl.setText("Auto-wire off")
            self.dropouts_lbl.setText("")
            self.silent_lbl.hide()
            self.onesided_lbl.hide()
            return
        s = self._sink_state
        colors = {"running": T.OK, "idle": T.WARN, "suspended": T.IDLE}
        self.dot.set_color(T.DANGER if self._silent or self._onesided else colors.get(s, T.DANGER))
        self.state_lbl.setText((s or "not found").capitalize())
        self.state_lbl.setToolTip("AE-9 sink state")
        self.silent_lbl.setVisible(self._silent)
        self.onesided_lbl.setText(f"ONE-SIDED {self._onesided or ''}".strip())
        self.onesided_lbl.setVisible(bool(self._onesided))
        if not self.dropouts_lbl.text():
            self.dropouts_lbl.setText("Dropouts: 0")

    def fit_window(self):
        """Shrink the window back to its natural width (e.g. after fewer channels)."""
        def shrink():
            if self.isMaximized():
                return
            if self.layout():
                self.layout().activate()
            # Asking for width 1 gets clamped to the minimum the layout allows.
            self.resize(1, self.height())
        QTimer.singleShot(0, shrink)

    def _tick(self):
        now = time.monotonic()
        dt = min(now - self.last_tick, 0.25)
        self.last_tick = now
        self.monitor.tick(now, dt)
        self.manual.tick(now, dt)

    def stop(self):
        if getattr(self, "_stopped", False):
            return
        self._stopped = True
        self.monitor.stop()
        self.manual.stop()
