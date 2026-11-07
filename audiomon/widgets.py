# SPDX-License-Identifier: GPL-2.0-only
"""Meter drawing and the meter panel cards, plus small UI helpers."""

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QSizePolicy, QToolButton, QVBoxLayout,
    QWidget,
)

from . import capture
from . import theme as T
from .core import CHANNEL_CHOICES, FLOOR_DB, GREEN_MAX, YELLOW_MAX, amp_db, elide, fmt_db, html_escape
from .meters import MeterState
from .theme import make_icon, mono_font, qcolor, ui_font


def label(text, name=None, wrap=False):
    lbl = QLabel(text)
    if name:
        lbl.setObjectName(name)
    lbl.setWordWrap(wrap)
    return lbl


def hline():
    sep = QFrame()
    sep.setObjectName("sep")
    return sep


# Meter geometry + drawing

SCALE_W = 40
COL_W = 50
PAIR_GAP = 14
PAD = 8
TOP = 32
ROW_H = 15
STAT_ROWS = ("Peak", "RMS", "Avg", "Max")
STATS_H = len(STAT_ROWS) * ROW_H + 10
TICKS = (0, -3, -6, -10, -18, -30, -40, -50, -60)
COL_MAX = 120   # widest a column gets when the window is stretched
BAR_MAX = 60    # widest a bar gets
ZONES = (
    (FLOOR_DB, GREEN_MAX, 0),
    (GREEN_MAX, YELLOW_MAX, 1),
    (YELLOW_MAX, 0.0, 2),
)


def iec_scale(db):
    """IEC 60268-18 style deflection, 0..1 (more resolution near 0 dB)."""
    if db < -70:
        d = 0.0
    elif db < -60:
        d = (db + 70) * 0.25
    elif db < -50:
        d = (db + 60) * 0.5 + 2.5
    elif db < -40:
        d = (db + 50) * 0.75 + 7.5
    elif db < -30:
        d = (db + 40) * 1.5 + 15
    elif db < -20:
        d = (db + 30) * 2.0 + 30
    elif db < 0:
        d = (db + 20) * 2.5 + 50
    else:
        d = 100.0
    return d / 100.0


def panel_width(n):
    return SCALE_W + PAD + n * COL_W + (n // 2 - 1) * PAIR_GAP + PAD


def col_width(n, w):
    """Column width that fills the available width (between COL_W and COL_MAX)."""
    extra = max(0, w - panel_width(n))
    return min(COL_W + extra / n, COL_MAX)


def col_x(i, cw=COL_W):
    return SCALE_W + PAD + i * cw + (i // 2) * PAIR_GAP


class MeterWidget(QWidget):
    """Draws one meter: scale, bars, clip LEDs and the stats rows."""

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setToolTip("Click to reset stats")
        self.f_tick = ui_font(10)
        self.f_name = ui_font(12, bold=True)
        self.f_val = ui_font(12)
        self.f_row = ui_font(11)

    def sizeHint(self):
        return QSize(panel_width(self.panel.state.channels), 330)

    def minimumSizeHint(self):
        return QSize(panel_width(self.panel.state.channels), 220)

    def mousePressEvent(self, _event):
        self.panel.reset()

    def paintEvent(self, _event):
        st = self.panel.state
        w, h = self.width(), self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.surface_q(T.GRAPH_BG, 0.85))
        p.drawRoundedRect(QRectF(0, 0, w, h), 6, 6)
        if self.panel.alert:
            p.setBrush(qcolor(T.DANGER, 0.22))
            p.drawRoundedRect(QRectF(0, 0, w, h), 6, 6)

        bar_top = TOP
        bar_bot = h - STATS_H - 4
        bar_h = max(bar_bot - bar_top, 10)

        def y_of(db):
            return bar_bot - iec_scale(db) * bar_h

        def text(s, x, y, align="left", font=None, color=T.GRAPH_TEXT):
            # (x, y) is the baseline anchor
            p.setFont(font or self.f_val)
            p.setPen(QColor(color))
            box = QRectF(x - 200, y - 16, 400, 20)
            if align == "left":
                box.moveLeft(x)
                flags = Qt.AlignmentFlag.AlignLeft
            elif align == "right":
                box.moveRight(x)
                flags = Qt.AlignmentFlag.AlignRight
            else:
                flags = Qt.AlignmentFlag.AlignHCenter
            p.drawText(box, flags | Qt.AlignmentFlag.AlignBottom, s)

        # Scale + grid; centre the whole meter when the card is wider than it
        cw = col_width(st.channels, w)
        right = col_x(st.channels - 1, cw) + cw
        p.translate(max(0.0, (w - right - PAD) / 2), 0)
        last_label_y = -100
        for t in TICKS:
            y = round(y_of(t)) + 0.5
            if y - last_label_y >= 12:   # skip labels that would overlap
                text(f"{t}", SCALE_W - 4, y + 5, "right", self.f_tick, T.MUTED)
                last_label_y = y
            p.setPen(QPen(qcolor(T.OVERLAY, 0.16 if t in (0, -18) else 0.07), 1))
            p.drawLine(QPointF(SCALE_W, y), QPointF(right, y))

        rms_db = st.rms_db
        avg_db = st.avg_db
        max_db = amp_db(st.max_amp)
        for i in range(st.channels):
            x0 = col_x(i, cw)
            cx = x0 + cw / 2
            bw = min(cw - 16, BAR_MAX)
            bx = cx - bw / 2

            text(st.names[i], cx, 15, "center", self.f_name, T.TEXT)

            p.setPen(Qt.PenStyle.NoPen)
            # Clip LED (latched until reset)
            p.setBrush(qcolor(T.DANGER) if st.clipped[i] else qcolor(T.DANGER, 0.16))
            p.drawRoundedRect(QRectF(bx, 19, bw, 6), 2, 2)

            # Bar background
            p.setBrush(qcolor(T.TRACK))
            p.drawRoundedRect(QRectF(bx, bar_top, bw, bar_h), 2, 2)

            def fill_level(level, alpha):
                for lo, hi, zone in ZONES:
                    if level <= lo:
                        break
                    top = y_of(min(level, hi))
                    p.fillRect(QRectF(bx, top, bw, y_of(lo) - top), qcolor(T.ZONES[zone], alpha))

            fill_level(st.peak_db[i], 0.35)  # peak: translucent
            fill_level(rms_db[i], 1.0)       # VU/RMS: solid

            if st.hold_db[i] > FLOOR_DB + 0.5:
                y = round(y_of(st.hold_db[i]))
                p.fillRect(QRectF(bx, y - 1, bw, 2), qcolor(T.HANDLE_BORDER))   # white on dark palettes, coral on Albino

            # Stats
            vals = (st.hold_db[i], rms_db[i], avg_db[i], max_db[i])
            for r, v in enumerate(vals):
                y = bar_bot + 17 + r * ROW_H
                color = T.DANGER if (r == 3 and st.clipped[i]) else T.GRAPH_TEXT
                text(fmt_db(v), cx, y, "center", self.f_val, color)

        for r, name in enumerate(STAT_ROWS):
            text(name, 6, bar_bot + 17 + r * ROW_H, "left", self.f_row, T.MUTED)
        p.end()


# Meter panels and views

class MeterPanel(QFrame):
    """A card: header (title, status, optional channel picker, reset) + meter."""

    def __init__(self, title, node_name, channels, subtitle=None, passive=False,
                 on_channels=None, fit=None, tooltip=None, node_label=None, keep=0):
        """title: the short name shown on the card; node_label: the full name
        the meter's PipeWire node gets (Helvum shows it; defaults to title);
        subtitle: an optional second line for state, e.g. what a stream plays;
        tooltip: the detail (jacks, nodes) shown on hover."""
        super().__init__()
        self.setObjectName("card")
        self.title_text = node_label or title
        self.node_name = node_name
        self.passive = passive
        self.keep = keep            # seconds of raw samples the capture keeps (0 = none)
        self.on_channels = on_channels
        self.fit = fit
        self.capture = None
        self.live = False
        self.alert = False
        self.note = None            # status text override (set by the AE-9 view)
        self._format_text = ""
        self.tick_peak_db = None    # loudest sample this tick, None if no data
        self.last_sound = 0.0       # last time anything above digital silence arrived
        self._status_html = None

        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title_lbl = label(elide(title), "cardTitle")
        titles.addWidget(self.title_lbl)
        if subtitle:
            titles.addWidget(label(elide(subtitle, 34), "muted"))
        tip = tooltip or "\n".join(filter(None, (node_label or title, subtitle, f"meter node: {node_name}")))
        for i in range(titles.count()):
            titles.itemAt(i).widget().setToolTip(tip)

        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.RichText)

        header = QHBoxLayout()
        header.setSpacing(8)
        header.addLayout(titles, 1)
        header.addWidget(self.status)

        if on_channels:
            self.combo = QComboBox()
            self.combo.addItems([f"{c} ch" for c in CHANNEL_CHOICES])
            self.combo.setToolTip("Channel count (recreates the PipeWire node; links are dropped)")
            self.combo.setCurrentIndex(CHANNEL_CHOICES.index(channels))
            self.combo.currentIndexChanged.connect(self._on_combo)
            header.addWidget(self.combo)

        reset = QToolButton()
        reset.setIcon(make_icon("refresh", T.MUTED, active=T.TEXT, size=18))
        reset.setIconSize(QSize(18, 18))
        reset.setToolTip("Reset stats and clip")
        reset.clicked.connect(self.reset)
        header.addWidget(reset, 0, Qt.AlignmentFlag.AlignTop)

        self.state = MeterState(channels)
        self.meter = MeterWidget(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addWidget(self.meter, 1)
        # the stream's format, under the Peak/RMS/Avg/Max rows
        self.fmt_lbl = label("", "muted")
        self.fmt_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.fmt_lbl)

        self._started = False
        self.start(channels)
        self._started = True

    @property
    def channels(self):
        return self.state.channels

    def start(self, channels):
        if self.capture:
            self.capture.stop()
        self.capture = capture.Capture(self.node_name, self.title_text, channels, self.passive)
        if self.keep and hasattr(self.capture, "keep_samples"):
            self.capture.keep_samples(self.keep)
        self.state = MeterState(channels)
        self.meter.updateGeometry()
        if self.fit and self._started:
            self.fit()

    def stop(self):
        if self.capture:
            self.capture.stop()

    def reset(self):
        self.state.reset_stats()
        self.meter.update()

    def set_format(self, text):
        """The stream format shown under the stats, e.g. '48 kHz · 24-bit'."""
        if text != self._format_text:
            self._format_text = text
            self.fmt_lbl.setText(text)

    def _on_combo(self, idx):
        ch = CHANNEL_CHOICES[idx]
        if ch != self.state.channels:
            self.start(ch)
            self.on_channels(ch)

    def tick(self, now, dt):
        peak, sumsq, frames, last = self.capture.take()
        self.state.update(peak, sumsq, frames, last, now, dt)
        self.tick_peak_db = float(amp_db(peak).max()) if frames else None
        if self.tick_peak_db is not None and self.tick_peak_db > FLOOR_DB:
            self.last_sound = now
        self.live = now - last < 0.5
        if self.capture.error:
            html = f'<span style="color:{T.DANGER}">{html_escape(self.capture.error)}</span>'
        elif self.note:
            html = f'<span style="color:{T.MUTED}">{html_escape(self.note)}</span>'
        elif self.live:
            html = f'<span style="color:{T.OK}">● live</span>'
        else:
            html = f'<span style="color:{T.IDLE}">○ idle</span>'
        html = f'<span style="font-size:13px">{html}</span>'
        if html != self._status_html:
            self.status.setText(html)
            self._status_html = html
        self.meter.update()


def log_view(min_height=90):
    v = QPlainTextEdit()
    v.setReadOnly(True)
    v.setMaximumBlockCount(5000)
    v.setFont(mono_font(12))
    v.setMinimumHeight(min_height)
    return v


class StatusDot(QWidget):
    def __init__(self):
        super().__init__()
        self.color = T.IDLE
        self.setFixedSize(16, 16)

    def set_color(self, color):
        if color != self.color:
            self.color = color
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(self.color))
        p.drawEllipse(QRectF(2, 2, 12, 12))
