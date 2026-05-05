"""
Raspberry Shake Live Waveform Viewer — Kid-Friendly Edition
============================================================
Streams real-time seismic data via SeedLink and displays
continuously scrolling waveforms or a live spectrogram,
plus a "Shake Challenge" game for outreach events.

Read-only — does not modify anything on the Shake.

Usage:
    python viewer.py --host 192.168.1.42 --station R1234

See README.md for installation instructions.
"""

import sys
import math
import time
import socket
import threading
import argparse
from io import BytesIO

import numpy as np
from scipy.signal import spectrogram as scipy_spectrogram
import pyqtgraph as pg
from PyQt5 import QtCore, QtWidgets, QtGui
from obspy import read

# ── Configuration (defaults — override via CLI) ────────────────────
SHAKE_HOST = "169.254.33.45"
SHAKE_PORT = 18000
NETWORK = "AM"
STATION = "RBCD4"
CHANNELS = ["EHZ", "EHN", "EHE"]
WINDOW_SECONDS = 60
SAMPLE_RATE = 100
REFRESH_MS = 8               # ~120 fps
SL_PACKET_SIZE = 520

# Spectrogram
SPEC_NPERSEG = 256
SPEC_NOVERLAP = 224
SPEC_MAX_FREQ = 45

# Game
GAME_DURATION = 10           # seconds of shaking
GAME_COMPUTE_MS = 2500       # fake "computing" duration in ms

# Signal scaling — RMS value that represents "100%" on meters.
# Set this high enough that a dozen kids jumping only maxes it out
# with serious effort. Adjust based on your Shake's sensitivity and
# floor/distance. A desk tap is typically ~500-2000 counts RMS;
# a group of kids jumping nearby can reach 50k-200k+.
METER_FULL_SCALE = 100000

# ── Appearance ─────────────────────────────────────────────────────
CHANNEL_COLORS = {
    "EHZ": "#00E5FF",   # cyan
    "EHN": "#76FF03",   # lime green
    "EHE": "#FF6D00",   # orange
}
CHANNEL_LABELS = {
    "EHZ": "Up & Down (Z)",
    "EHN": "North-South (N)",
    "EHE": "East-West (E)",
}
BG_COLOR = "#1a1a2e"
PANEL_BG = "#16213e"
ACCENT = "#e94560"
ACCENT2 = "#0f3460"
TEXT_COLOR = "#f5f5f5"
SUBTITLE_COLOR = "#a8b2d1"

# Colors for intensity tiers (green -> yellow -> orange -> red -> magenta)
TIER_COLORS = [
    "#76FF03", "#76FF03",  # sleeping cat, mouse
    "#B2FF59",             # walking human
    "#FFEA00",             # bouncy dog
    "#FFB300",             # dancing elephant
    "#FF6D00",             # stampeding buffalo
    "#FF1744",             # T-Rex stomp
    "#D500F9",             # volcanic eruption
    "#E040FB",             # mega earthquake
]

# ── Shared state ──────────────────────────────────────────────────
buf_size = WINDOW_SECONDS * SAMPLE_RATE
buffers = {ch: np.zeros(buf_size) for ch in CHANNELS}
lock = threading.Lock()
connected = threading.Event()
latest_time = [None]
x_axis = np.linspace(-WINDOW_SECONDS, 0, buf_size)


# ── SeedLink background reader ────────────────────────────────────
def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed by Shake")
        buf += chunk
    return buf


def seedlink_worker():
    while True:
        try:
            print(f"[SeedLink] Connecting to {SHAKE_HOST}:{SHAKE_PORT}...")
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10)
            s.connect((SHAKE_HOST, SHAKE_PORT))

            for cmd in ["HELLO", f"STATION {STATION} {NETWORK}",
                        "SELECT 00EH?.D", "DATA"]:
                s.sendall((cmd + "\r\n").encode())
                time.sleep(0.15)
                s.recv(4096)
            s.sendall(b"END\r\n")

            print("[SeedLink] Connected — streaming data")
            s.settimeout(30)

            while True:
                pkt = recv_exact(s, SL_PACKET_SIZE)
                try:
                    st = read(BytesIO(pkt[8:]), format="MSEED")
                except Exception:
                    continue
                tr = st[0]
                ch = tr.stats.channel
                if ch not in CHANNELS:
                    continue
                connected.set()
                samples = tr.data.astype(np.float64)
                n = len(samples)
                end_time = tr.stats.endtime

                with lock:
                    if latest_time[0] is None or end_time > latest_time[0]:
                        if latest_time[0] is not None:
                            shift = int(round(
                                (end_time - latest_time[0]) * SAMPLE_RATE
                            ))
                            if shift > 0:
                                for c in CHANNELS:
                                    if shift >= buf_size:
                                        buffers[c][:] = 0
                                    else:
                                        buffers[c][:-shift] = buffers[c][shift:]
                                        buffers[c][-shift:] = 0
                        latest_time[0] = end_time

                    offset_from_end = int(round(
                        (latest_time[0] - end_time) * SAMPLE_RATE
                    ))
                    end_idx = buf_size - offset_from_end
                    start_idx = end_idx - n
                    if start_idx < 0:
                        samples = samples[-start_idx:]
                        n = len(samples)
                        start_idx = 0
                    if end_idx > buf_size:
                        end_idx = buf_size
                        samples = samples[:end_idx - start_idx]
                    if start_idx < end_idx:
                        buffers[ch][start_idx:end_idx] = samples

        except Exception as e:
            print(f"[SeedLink] {e} — reconnecting in 2s", file=sys.stderr)
            try:
                s.close()
            except Exception:
                pass
            time.sleep(2)


# ── Helpers ───────────────────────────────────────────────────────
def make_colormap():
    from matplotlib.cm import inferno
    return (inferno(np.linspace(0, 1, 256)) * 255).astype(np.uint8)


# Real-world intensity comparisons for the Shake Game.
# Each entry: (threshold_fraction, label, description, emoji)
INTENSITY_SCALE = [
    (0.00, "Sleeping Cat",       "Like a cat purring on a pillow",          "1F408"),
    (0.10, "Tiptoeing Mouse",    "Barely a whisper in the ground",          "1F42D"),
    (0.20, "Walking Human",      "Like footsteps in a hallway",             "1F6B6"),
    (0.30, "Bouncy Dog",         "A happy dog jumping around!",             "1F436"),
    (0.40, "Dancing Elephant",   "Now we're shaking the floor!",            "1F418"),
    (0.55, "Stampeding Buffalo", "A whole herd running across the plains!", "1F403"),
    (0.70, "T-Rex Stomp",        "The ground is trembling!",                "1F996"),
    (0.85, "Volcanic Eruption",  "Earth-shaking power!",                    "1F30B"),
    (0.95, "Mega Earthquake",    "Off the charts seismic energy!",          "1F4A5"),
]


def get_intensity_index(fraction):
    """Return the index into INTENSITY_SCALE for a score fraction 0-1."""
    idx = 0
    for i, (threshold, *_) in enumerate(INTENSITY_SCALE):
        if fraction >= threshold:
            idx = i
    return idx


def emoji_char(codepoint_hex):
    """Convert a hex codepoint string to an actual emoji character."""
    return chr(int(codepoint_hex, 16))


def grab_z_snapshot():
    """Grab a copy of the Z-channel buffer under lock."""
    with lock:
        return buffers["EHZ"].copy()


# ── Stylesheets ───────────────────────────────────────────────────
BUTTON_STYLE = """
QPushButton {{
    background: {bg};
    color: {fg};
    border: 2px solid {border};
    border-radius: 12px;
    padding: 10px 24px;
    font-size: 18px;
    font-weight: bold;
    font-family: 'Segoe UI', 'Helvetica Neue', sans-serif;
}}
QPushButton:hover {{
    background: {hover};
}}
QPushButton:disabled {{
    background: #444;
    color: #888;
    border-color: #555;
}}
"""

BIG_BUTTON_STYLE = """
QPushButton {{
    background: {bg};
    color: {fg};
    border: 3px solid {border};
    border-radius: 20px;
    padding: 16px 48px;
    font-size: 28px;
    font-weight: bold;
    font-family: 'Segoe UI', 'Helvetica Neue', sans-serif;
}}
QPushButton:hover {{
    background: {hover};
    border-color: {fg};
}}
"""

TAB_STYLE = f"""
QTabWidget::pane {{
    border: none;
    background: {BG_COLOR};
}}
QTabBar::tab {{
    background: {ACCENT2};
    color: {TEXT_COLOR};
    border: none;
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
    padding: 12px 32px;
    font-size: 18px;
    font-weight: bold;
    font-family: 'Segoe UI', 'Helvetica Neue', sans-serif;
    margin-right: 4px;
    min-width: 180px;
}}
QTabBar::tab:selected {{
    background: {ACCENT};
    color: white;
}}
QTabBar::tab:hover {{
    background: #e94560cc;
}}
"""


# ── Live View Tab ─────────────────────────────────────────────────
class LiveViewTab(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.mode = "waveform"  # or "spectrogram"
        self._spec_counter = 0
        self._got_first_data = False
        self._meter_max = METER_FULL_SCALE

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        # ── Top bar ──────────────────────────────────────────────
        top_bar = QtWidgets.QHBoxLayout()

        title = QtWidgets.QLabel("Live Seismic Waves")
        title.setStyleSheet(
            f"color: {TEXT_COLOR}; font-size: 22px; font-weight: bold; "
            "font-family: 'Segoe UI', 'Helvetica Neue', sans-serif; "
            "background: transparent;"
        )
        top_bar.addWidget(title)

        self.status_label = QtWidgets.QLabel("Waiting for sensor...")
        self.status_label.setStyleSheet(
            f"color: {SUBTITLE_COLOR}; font-size: 14px; background: transparent;"
        )
        top_bar.addWidget(self.status_label, stretch=1)

        # Mode toggle button
        self.mode_btn = QtWidgets.QPushButton("Show Spectrogram")
        self.mode_btn.setStyleSheet(BUTTON_STYLE.format(
            bg=ACCENT2, fg=TEXT_COLOR, border="#0f3460",
            hover="#1a4a8a"
        ))
        self.mode_btn.setFixedHeight(44)
        self.mode_btn.clicked.connect(self.toggle_mode)
        top_bar.addWidget(self.mode_btn)

        layout.addLayout(top_bar)

        # ── Waveform stack ───────────────────────────────────────
        self.wave_container = QtWidgets.QWidget()
        wave_layout = QtWidgets.QVBoxLayout(self.wave_container)
        wave_layout.setContentsMargins(0, 0, 0, 0)
        wave_layout.setSpacing(4)

        self.plots = {}
        self.curves = {}
        for ch in CHANNELS:
            pw = pg.PlotWidget()
            pw.setBackground(PANEL_BG)
            pw.showGrid(x=True, y=True, alpha=0.12)
            pw.setLabel("left", CHANNEL_LABELS[ch],
                        color=CHANNEL_COLORS[ch], size="12pt")
            pw.getAxis("left").setTextPen(CHANNEL_COLORS[ch])
            pw.getAxis("bottom").setTextPen(SUBTITLE_COLOR)
            pw.setMouseEnabled(x=False, y=False)
            pw.hideButtons()
            pw.setXRange(-WINDOW_SECONDS, 0, padding=0)
            curve = pw.plot(
                pen=pg.mkPen(color=CHANNEL_COLORS[ch], width=1.5)
            )
            self.plots[ch] = pw
            self.curves[ch] = curve
            wave_layout.addWidget(pw, stretch=1)

        # Link x-axes
        for ch in CHANNELS[1:]:
            self.plots[ch].setXLink(self.plots[CHANNELS[0]])

        layout.addWidget(self.wave_container, stretch=5)

        # ── Spectrogram container (hidden by default) ────────────
        self.spec_container = QtWidgets.QWidget()
        spec_layout = QtWidgets.QVBoxLayout(self.spec_container)
        spec_layout.setContentsMargins(0, 0, 0, 0)

        spec_widget = pg.PlotWidget()
        spec_widget.setBackground(PANEL_BG)
        spec_widget.setLabel("left", "Frequency (Hz)",
                             color="#bb86fc", size="13pt")
        spec_widget.setLabel("bottom", "Time (seconds)",
                             color=SUBTITLE_COLOR, size="13pt")
        spec_widget.getAxis("left").setTextPen("#bb86fc")
        spec_widget.getAxis("bottom").setTextPen(SUBTITLE_COLOR)
        spec_widget.setMouseEnabled(x=False, y=False)
        spec_widget.hideButtons()
        spec_widget.setXRange(-WINDOW_SECONDS, 0, padding=0)
        spec_widget.setYRange(0, SPEC_MAX_FREQ, padding=0)
        self.spec_image = pg.ImageItem()
        spec_widget.addItem(self.spec_image)
        self.spec_image.setLookupTable(make_colormap())
        self.spec_plot = spec_widget
        spec_layout.addWidget(spec_widget)

        self.spec_container.setVisible(False)
        layout.addWidget(self.spec_container, stretch=5)

        # ── Signal strength meter ────────────────────────────────
        meter_frame = QtWidgets.QFrame()
        meter_frame.setStyleSheet(
            f"background: {PANEL_BG}; border: 2px solid {ACCENT2}; "
            "border-radius: 10px;"
        )
        meter_inner = QtWidgets.QHBoxLayout(meter_frame)
        meter_inner.setContentsMargins(16, 8, 16, 8)

        meter_title = QtWidgets.QLabel("Signal Strength")
        meter_title.setStyleSheet(
            f"color: {TEXT_COLOR}; font-size: 16px; font-weight: bold; "
            "border: none; background: transparent;"
        )
        meter_inner.addWidget(meter_title)

        self.meter_bar = QtWidgets.QProgressBar()
        self.meter_bar.setRange(0, 100)
        self.meter_bar.setValue(0)
        self.meter_bar.setTextVisible(False)
        self.meter_bar.setFixedHeight(26)
        self.meter_bar.setStyleSheet(
            "QProgressBar { background: #21262d; border: none; "
            "border-radius: 13px; }"
            "QProgressBar::chunk { background: qlineargradient("
            "x1:0, y1:0, x2:1, y2:0, "
            "stop:0 #76FF03, stop:0.4 #FFEA00, stop:0.7 #FF6D00, "
            "stop:1.0 #FF1744); border-radius: 13px; }"
        )
        meter_inner.addWidget(self.meter_bar, stretch=1)

        self.meter_value = QtWidgets.QLabel("--")
        self.meter_value.setStyleSheet(
            f"color: {TEXT_COLOR}; font-size: 20px; font-weight: bold; "
            "font-family: monospace; border: none; background: transparent;"
        )
        meter_inner.addWidget(self.meter_value)

        self.meter_level = QtWidgets.QLabel("Quiet")
        self.meter_level.setFixedWidth(100)
        self.meter_level.setAlignment(QtCore.Qt.AlignCenter)
        self.meter_level.setStyleSheet(
            "color: #76FF03; font-size: 16px; font-weight: bold; "
            "border: none; background: transparent;"
        )
        meter_inner.addWidget(self.meter_level)

        layout.addWidget(meter_frame)

    def toggle_mode(self):
        if self.mode == "waveform":
            self.mode = "spectrogram"
            self.wave_container.setVisible(False)
            self.spec_container.setVisible(True)
            self.mode_btn.setText("Show Waveforms")
        else:
            self.mode = "waveform"
            self.wave_container.setVisible(True)
            self.spec_container.setVisible(False)
            self.mode_btn.setText("Show Spectrogram")

    def tick(self):
        if not connected.is_set():
            return

        if not self._got_first_data:
            self.status_label.setText(
                f"Station {STATION}  |  "
                f"{', '.join(CHANNELS)}  |  {SAMPLE_RATE} Hz"
            )
            self.status_label.setStyleSheet(
                "color: #00E5FF; font-size: 14px; background: transparent;"
            )
            self._got_first_data = True

        do_heavy = False
        self._spec_counter += 1
        if self._spec_counter >= 8:   # every 8th frame at 120fps ~ 15Hz
            self._spec_counter = 0
            do_heavy = True

        with lock:
            snapshots = {ch: buffers[ch].copy() for ch in CHANNELS}

        # Waveforms
        if self.mode == "waveform":
            for ch in CHANNELS:
                data = snapshots[ch] - np.mean(snapshots[ch])
                self.curves[ch].setData(x_axis, data)

        z_data = snapshots["EHZ"]
        z_centered = z_data - np.mean(z_data)

        # Signal strength meter
        recent = z_centered[-SAMPLE_RATE:]
        rms = np.sqrt(np.mean(recent ** 2))
        peak = np.max(np.abs(recent))

        pct = min(100, int(rms / self._meter_max * 100))
        self.meter_bar.setValue(pct)
        self.meter_value.setText(f"{peak:.0f}")

        if pct < 25:
            level, color = "Quiet", "#76FF03"
        elif pct < 55:
            level, color = "Light", "#FFEA00"
        elif pct < 80:
            level, color = "Moderate", "#FF6D00"
        else:
            level, color = "Strong!", "#FF1744"
        self.meter_level.setText(level)
        self.meter_level.setStyleSheet(
            f"color: {color}; font-size: 16px; font-weight: bold; "
            "border: none; background: transparent;"
        )

        # Spectrogram (heavy, every Nth frame)
        if do_heavy and self.mode == "spectrogram":
            f, t, Sxx = scipy_spectrogram(
                z_centered, fs=SAMPLE_RATE,
                nperseg=SPEC_NPERSEG, noverlap=SPEC_NOVERLAP,
                mode="magnitude",
            )
            freq_mask = f <= SPEC_MAX_FREQ
            f = f[freq_mask]
            Sxx = Sxx[freq_mask, :]
            Sxx_log = np.log1p(Sxx * 100)

            self.spec_image.setImage(Sxx_log.T, autoLevels=True)
            self.spec_image.setRect(
                pg.QtCore.QRectF(
                    -WINDOW_SECONDS, 0, WINDOW_SECONDS, SPEC_MAX_FREQ
                )
            )


# ── Shake Game Tab ────────────────────────────────────────────────
class ShakeGameTab(QtWidgets.QWidget):
    """
    States: idle -> countdown -> recording -> computing -> results
    """
    def __init__(self):
        super().__init__()
        self.state = "idle"
        self._record_buf = None       # pre-allocated numpy array
        self._record_write_pos = 0
        self._record_start = 0.0
        self._compute_start = 0.0
        self._final_score = 0.0
        self._score_fraction = 0.0
        self._score_max = METER_FULL_SCALE
        self._anim_tick = 0           # frame counter for animations
        self._countdown_timer = None
        self._score_anim_target = 0
        self._score_anim_current = 0.0

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(0)

        self.stack = QtWidgets.QStackedWidget()
        layout.addWidget(self.stack)

        self._build_idle_screen()
        self._build_countdown_screen()
        self._build_recording_screen()
        self._build_computing_screen()
        self._build_results_screen()

        self.stack.setCurrentIndex(0)

    def _styled_label(self, text, size=24, color=TEXT_COLOR, bold=True):
        lbl = QtWidgets.QLabel(text)
        weight = "bold" if bold else "normal"
        lbl.setStyleSheet(
            f"color: {color}; font-size: {size}px; font-weight: {weight}; "
            "font-family: 'Segoe UI', 'Helvetica Neue', sans-serif; "
            "background: transparent;"
        )
        lbl.setAlignment(QtCore.Qt.AlignCenter)
        lbl.setWordWrap(True)
        return lbl

    # ── Idle screen ──────────────────────────────────────────────
    def _build_idle_screen(self):
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setAlignment(QtCore.Qt.AlignCenter)
        v.setSpacing(16)

        v.addStretch(2)

        # Big emoji header
        emoji_row = self._styled_label(
            "\U0001F30D  \U0001F4A5  \U0001F30D", size=60
        )
        v.addWidget(emoji_row)

        self.idle_title = self._styled_label(
            "SHAKE CHALLENGE!", size=56, color=ACCENT
        )
        v.addWidget(self.idle_title)

        v.addSpacing(8)

        sub = self._styled_label(
            "Jump, stomp, and shake as hard as you can!\n"
            "The seismometer will measure your power!",
            size=24, color=SUBTITLE_COLOR, bold=False
        )
        v.addWidget(sub)

        v.addSpacing(24)

        # How it works — styled cards
        steps_container = QtWidgets.QHBoxLayout()
        steps_container.setAlignment(QtCore.Qt.AlignCenter)
        steps_container.setSpacing(20)
        step_data = [
            ("\U0001F3AC", "Press START", "#00E5FF"),
            ("\U0001F4A3", "Shake for 10s", "#FFEA00"),
            ("\U0001F3C6", "See your score!", "#FF6D00"),
        ]
        for emoji, text, color in step_data:
            card = QtWidgets.QFrame()
            card.setFixedSize(200, 120)
            card.setStyleSheet(
                f"background: {PANEL_BG}; border: 2px solid {color}40; "
                "border-radius: 16px;"
            )
            card_layout = QtWidgets.QVBoxLayout(card)
            card_layout.setAlignment(QtCore.Qt.AlignCenter)
            e_lbl = QtWidgets.QLabel(emoji)
            e_lbl.setAlignment(QtCore.Qt.AlignCenter)
            e_lbl.setStyleSheet(
                "font-size: 36px; background: transparent; border: none;"
            )
            card_layout.addWidget(e_lbl)
            t_lbl = QtWidgets.QLabel(text)
            t_lbl.setAlignment(QtCore.Qt.AlignCenter)
            t_lbl.setStyleSheet(
                f"color: {color}; font-size: 16px; font-weight: bold; "
                "background: transparent; border: none; "
                "font-family: 'Segoe UI', 'Helvetica Neue', sans-serif;"
            )
            card_layout.addWidget(t_lbl)
            steps_container.addWidget(card)
        v.addLayout(steps_container)

        v.addSpacing(32)

        self.start_btn = QtWidgets.QPushButton("\U0001F680  START!")
        self.start_btn.setFixedSize(300, 80)
        self.start_btn.setStyleSheet(BIG_BUTTON_STYLE.format(
            bg="#00C853", fg="white", border="#00E676",
            hover="#00E676"
        ))
        self.start_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.start_btn.clicked.connect(self.start_game)
        btn_container = QtWidgets.QHBoxLayout()
        btn_container.setAlignment(QtCore.Qt.AlignCenter)
        btn_container.addWidget(self.start_btn)
        v.addLayout(btn_container)

        v.addStretch(3)
        self.stack.addWidget(page)  # index 0

    # ── Countdown screen (3-2-1) ─────────────────────────────────
    def _build_countdown_screen(self):
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setAlignment(QtCore.Qt.AlignCenter)

        v.addStretch(1)
        self.countdown_label = self._styled_label("3", size=200, color="#FFEA00")
        v.addWidget(self.countdown_label)

        self.countdown_sub = self._styled_label(
            "Get ready...", size=32, color=SUBTITLE_COLOR, bold=False
        )
        v.addWidget(self.countdown_sub)
        v.addStretch(1)

        self.stack.addWidget(page)  # index 1

    # ── Recording screen ─────────────────────────────────────────
    def _build_recording_screen(self):
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setAlignment(QtCore.Qt.AlignCenter)
        v.setSpacing(12)

        v.addStretch(1)

        self.recording_title = self._styled_label(
            "\U0001F4A5 SHAKE NOW! \U0001F4A5", size=56, color="#FF1744"
        )
        v.addWidget(self.recording_title)

        self.time_left_label = self._styled_label(
            "10", size=120, color="#FFEA00"
        )
        v.addWidget(self.time_left_label)

        self.time_caption = self._styled_label(
            "seconds left", size=22, color=SUBTITLE_COLOR, bold=False
        )
        v.addWidget(self.time_caption)

        v.addSpacing(8)

        # Live waveform during recording
        self.live_wave = pg.PlotWidget()
        self.live_wave.setBackground(PANEL_BG)
        self.live_wave.setFixedHeight(160)
        self.live_wave.setMouseEnabled(x=False, y=False)
        self.live_wave.hideButtons()
        self.live_wave.getAxis("bottom").setStyle(showValues=False)
        self.live_wave.getAxis("left").setStyle(showValues=False)
        self.live_wave.getAxis("bottom").setTextPen(SUBTITLE_COLOR)
        self.live_wave.getAxis("left").setTextPen(SUBTITLE_COLOR)
        self.live_wave_curve = self.live_wave.plot(
            pen=pg.mkPen(color="#FF1744", width=2)
        )
        v.addWidget(self.live_wave)

        # Live intensity bar during recording
        self.live_bar = QtWidgets.QProgressBar()
        self.live_bar.setRange(0, 100)
        self.live_bar.setValue(0)
        self.live_bar.setTextVisible(False)
        self.live_bar.setFixedHeight(36)
        self.live_bar.setFixedWidth(600)
        self.live_bar.setStyleSheet(
            "QProgressBar { background: #21262d; border: 2px solid #30363d; "
            "border-radius: 18px; }"
            "QProgressBar::chunk { background: qlineargradient("
            "x1:0, y1:0, x2:1, y2:0, "
            "stop:0 #76FF03, stop:0.4 #FFEA00, stop:0.7 #FF6D00, "
            "stop:1.0 #FF1744); border-radius: 16px; }"
        )
        bar_container = QtWidgets.QHBoxLayout()
        bar_container.setAlignment(QtCore.Qt.AlignCenter)
        bar_container.addWidget(self.live_bar)
        v.addLayout(bar_container)

        v.addStretch(1)
        self.stack.addWidget(page)  # index 2

    # ── Computing screen ─────────────────────────────────────────
    def _build_computing_screen(self):
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setAlignment(QtCore.Qt.AlignCenter)
        v.setSpacing(16)

        v.addStretch(1)

        self.computing_emoji = self._styled_label(
            "\U0001F50D", size=64
        )
        v.addWidget(self.computing_emoji)

        self.computing_label = self._styled_label(
            "Analyzing your shake...", size=36, color="#bb86fc"
        )
        v.addWidget(self.computing_label)

        # Progress bar for computing
        self.compute_bar = QtWidgets.QProgressBar()
        self.compute_bar.setRange(0, 100)
        self.compute_bar.setValue(0)
        self.compute_bar.setTextVisible(False)
        self.compute_bar.setFixedHeight(20)
        self.compute_bar.setFixedWidth(400)
        self.compute_bar.setStyleSheet(
            "QProgressBar { background: #21262d; border: 2px solid #30363d; "
            "border-radius: 10px; }"
            "QProgressBar::chunk { background: qlineargradient("
            "x1:0, y1:0, x2:1, y2:0, "
            "stop:0 #7C4DFF, stop:1.0 #E040FB); border-radius: 8px; }"
        )
        bar_c = QtWidgets.QHBoxLayout()
        bar_c.setAlignment(QtCore.Qt.AlignCenter)
        bar_c.addWidget(self.compute_bar)
        v.addLayout(bar_c)

        # Recorded waveform plot
        self.result_wave = pg.PlotWidget()
        self.result_wave.setBackground(PANEL_BG)
        self.result_wave.setFixedHeight(180)
        self.result_wave.setMouseEnabled(x=False, y=False)
        self.result_wave.hideButtons()
        self.result_wave.getAxis("bottom").setTextPen(SUBTITLE_COLOR)
        self.result_wave.getAxis("left").setTextPen(SUBTITLE_COLOR)
        self.result_wave.setLabel("bottom", "Time (s)",
                                 color=SUBTITLE_COLOR, size="10pt")
        self.result_wave_curve = self.result_wave.plot(
            pen=pg.mkPen(color="#00E5FF", width=2)
        )
        v.addWidget(self.result_wave)

        v.addStretch(1)
        self.stack.addWidget(page)  # index 3

    # ── Results screen ───────────────────────────────────────────
    def _build_results_screen(self):
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setAlignment(QtCore.Qt.AlignCenter)
        v.setSpacing(10)

        v.addStretch(1)

        self.result_emoji = self._styled_label("", size=90)
        v.addWidget(self.result_emoji)

        self.result_title = self._styled_label(
            "Your Shake Power:", size=26, color=SUBTITLE_COLOR, bold=False
        )
        v.addWidget(self.result_title)

        self.result_name = self._styled_label(
            "T-Rex Stomp!", size=52, color=ACCENT
        )
        v.addWidget(self.result_name)

        self.result_desc = self._styled_label(
            "The ground is trembling!", size=24, color=TEXT_COLOR, bold=False
        )
        v.addWidget(self.result_desc)

        v.addSpacing(12)

        # Score bar
        score_bar_container = QtWidgets.QHBoxLayout()
        score_bar_container.setAlignment(QtCore.Qt.AlignCenter)

        self.score_bar = QtWidgets.QProgressBar()
        self.score_bar.setRange(0, 100)
        self.score_bar.setValue(0)
        self.score_bar.setTextVisible(False)
        self.score_bar.setFixedHeight(34)
        self.score_bar.setFixedWidth(600)
        self.score_bar.setStyleSheet(
            "QProgressBar { background: #21262d; border: 2px solid #30363d; "
            "border-radius: 17px; }"
            "QProgressBar::chunk { background: qlineargradient("
            "x1:0, y1:0, x2:1, y2:0, "
            "stop:0 #76FF03, stop:0.4 #FFEA00, stop:0.7 #FF6D00, "
            "stop:1.0 #FF1744); border-radius: 15px; }"
        )
        score_bar_container.addWidget(self.score_bar)
        v.addLayout(score_bar_container)

        v.addSpacing(6)

        # Scale reference — a row of emoji labels
        self.scale_widget = QtWidgets.QWidget()
        self.scale_layout = QtWidgets.QHBoxLayout(self.scale_widget)
        self.scale_layout.setContentsMargins(0, 0, 0, 0)
        self.scale_layout.setSpacing(2)
        self.scale_labels = []
        for i, (_, name, _, emo) in enumerate(INTENSITY_SCALE):
            lbl = QtWidgets.QLabel(emoji_char(emo))
            lbl.setAlignment(QtCore.Qt.AlignCenter)
            lbl.setToolTip(name)
            lbl.setStyleSheet(
                "font-size: 22px; background: transparent; padding: 2px;"
            )
            self.scale_layout.addWidget(lbl, stretch=1)
            self.scale_labels.append(lbl)
        v.addWidget(self.scale_widget)

        v.addSpacing(8)

        self.score_number = self._styled_label(
            "Score: 0", size=40, color="#FFEA00"
        )
        v.addWidget(self.score_number)

        v.addSpacing(20)

        # Play again button
        self.reset_btn = QtWidgets.QPushButton("\U0001F504  PLAY AGAIN!")
        self.reset_btn.setFixedSize(320, 80)
        self.reset_btn.setStyleSheet(BIG_BUTTON_STYLE.format(
            bg="#00C853", fg="white", border="#00E676",
            hover="#00E676"
        ))
        self.reset_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.reset_btn.clicked.connect(self.reset_game)
        btn_container = QtWidgets.QHBoxLayout()
        btn_container.setAlignment(QtCore.Qt.AlignCenter)
        btn_container.addWidget(self.reset_btn)
        v.addLayout(btn_container)

        v.addStretch(1)
        self.stack.addWidget(page)  # index 4

    # ── Game flow ────────────────────────────────────────────────
    def start_game(self):
        self.state = "countdown"
        self._countdown_value = 3
        self.countdown_label.setText("3")
        self.countdown_label.setStyleSheet(
            "color: #FFEA00; font-size: 200px; font-weight: bold; "
            "font-family: 'Segoe UI', 'Helvetica Neue', sans-serif; "
            "background: transparent;"
        )
        self.countdown_sub.setText("Get ready...")
        self.stack.setCurrentIndex(1)

        # Reuse or create timer
        if self._countdown_timer is None:
            self._countdown_timer = QtCore.QTimer(self)
            self._countdown_timer.timeout.connect(self._countdown_tick)
        self._countdown_timer.start(1000)

    def _countdown_tick(self):
        self._countdown_value -= 1
        if self._countdown_value > 0:
            self.countdown_label.setText(str(self._countdown_value))
        elif self._countdown_value == 0:
            self.countdown_label.setText("GO!")
            self.countdown_label.setStyleSheet(
                "color: #00E676; font-size: 200px; font-weight: bold; "
                "font-family: 'Segoe UI', 'Helvetica Neue', sans-serif; "
                "background: transparent;"
            )
            self.countdown_sub.setText("\U0001F4A5 SHAKE SHAKE SHAKE! \U0001F4A5")
        else:
            self._countdown_timer.stop()
            self._start_recording()

    def _start_recording(self):
        self.state = "recording"
        self._anim_tick = 0
        # Pre-allocate buffer: ~120fps * 10s * 100 samples = way more than
        # needed, but we write in SAMPLE_RATE-sized chunks per frame.
        # Max chunks = (GAME_DURATION * 1000 / REFRESH_MS) frames, each
        # contributing SAMPLE_RATE samples.
        max_samples = GAME_DURATION * SAMPLE_RATE * 2  # generous headroom
        self._record_buf = np.zeros(max_samples)
        self._record_write_pos = 0
        self._record_start = time.time()
        self.time_left_label.setText(str(GAME_DURATION))
        self.live_bar.setValue(0)
        self.stack.setCurrentIndex(2)

    def _finish_recording(self):
        self.state = "computing"
        self._compute_start = time.time()
        self.compute_bar.setValue(0)
        self.stack.setCurrentIndex(3)

        # Trim buffer to actual data and compute score
        data = self._record_buf[:self._record_write_pos].copy()
        self._record_buf = None  # free memory

        if len(data) > 0:
            data = data - np.mean(data)
            t_axis = np.linspace(0, GAME_DURATION, len(data))
            self.result_wave_curve.setData(t_axis, data)
            self.result_wave.setXRange(0, GAME_DURATION, padding=0.02)
            self._final_score = np.sqrt(np.mean(data ** 2))
        else:
            self.result_wave_curve.setData([], [])
            self._final_score = 0.0

    def _show_results(self):
        self.state = "results"
        self._anim_tick = 0

        fraction = min(1.0, self._final_score / self._score_max)
        self._score_fraction = fraction
        tier_idx = get_intensity_index(fraction)
        tier_color = TIER_COLORS[tier_idx]

        _, label, desc, emoji_cp = INTENSITY_SCALE[tier_idx]
        self._score_anim_target = int(fraction * 1000)
        self._score_anim_current = 0.0

        self.result_emoji.setText(emoji_char(emoji_cp))
        self.result_name.setText(label + "!")
        self.result_name.setStyleSheet(
            f"color: {tier_color}; font-size: 52px; font-weight: bold; "
            "font-family: 'Segoe UI', 'Helvetica Neue', sans-serif; "
            "background: transparent;"
        )
        self.result_desc.setText(desc)
        self.score_bar.setValue(0)
        self.score_number.setText("Score: 0")

        # Highlight the matched tier in the scale
        for i, lbl in enumerate(self.scale_labels):
            if i == tier_idx:
                lbl.setStyleSheet(
                    f"font-size: 32px; background: {TIER_COLORS[i]}30; "
                    "border: 2px solid " + TIER_COLORS[i] + "; "
                    "border-radius: 8px; padding: 2px;"
                )
            else:
                lbl.setStyleSheet(
                    "font-size: 22px; background: transparent; "
                    "padding: 2px; border: none;"
                )

        self.stack.setCurrentIndex(4)

    def reset_game(self):
        self.state = "idle"
        self._record_buf = None
        self._record_write_pos = 0
        self.stack.setCurrentIndex(0)

    def tick(self):
        """Called every frame from the main timer."""
        self._anim_tick += 1

        if self.state == "idle":
            # Pulse the title color
            t = self._anim_tick * 0.03
            r = int(180 + 75 * math.sin(t))
            g = int(50 + 30 * math.sin(t * 0.7))
            b = int(80 + 40 * math.sin(t * 1.3))
            self.idle_title.setStyleSheet(
                f"color: rgb({r},{g},{b}); font-size: 56px; "
                "font-weight: bold; "
                "font-family: 'Segoe UI', 'Helvetica Neue', sans-serif; "
                "background: transparent;"
            )

        elif self.state == "recording":
            elapsed = time.time() - self._record_start
            remaining = max(0, GAME_DURATION - elapsed)
            secs_display = int(remaining) + 1 if remaining > 0 else 0
            self.time_left_label.setText(str(secs_display))

            # Pulse the "SHAKE NOW" text between red and orange
            t = self._anim_tick * 0.15
            pulse = abs(math.sin(t))
            r = int(255)
            g = int(23 + 100 * pulse)
            b = int(68 - 40 * pulse)
            self.recording_title.setStyleSheet(
                f"color: rgb({r},{g},{b}); font-size: 56px; "
                "font-weight: bold; "
                "font-family: 'Segoe UI', 'Helvetica Neue', sans-serif; "
                "background: transparent;"
            )

            # Grab Z data for live waveform + bar
            z_snap = grab_z_snapshot()
            z_centered = z_snap - np.mean(z_snap)
            recent = z_centered[-SAMPLE_RATE:]
            rms = np.sqrt(np.mean(recent ** 2))
            pct = min(100, int(rms / METER_FULL_SCALE * 100))
            self.live_bar.setValue(pct)

            # Update live waveform (last 3 seconds for readability)
            display_samples = z_centered[-SAMPLE_RATE * 3:]
            t_axis = np.linspace(-3, 0, len(display_samples))
            self.live_wave_curve.setData(t_axis, display_samples)
            self.live_wave.setXRange(-3, 0, padding=0)

            # Collect samples into pre-allocated buffer
            n = min(len(recent), len(self._record_buf) - self._record_write_pos)
            if n > 0:
                self._record_buf[
                    self._record_write_pos:self._record_write_pos + n
                ] = recent[:n]
                self._record_write_pos += n

            if elapsed >= GAME_DURATION:
                self._finish_recording()

        elif self.state == "computing":
            elapsed = time.time() - self._compute_start
            progress = min(100, int(elapsed / (GAME_COMPUTE_MS / 1000) * 100))
            self.compute_bar.setValue(progress)

            # Cycle the emoji for fun
            compute_emojis = [
                "\U0001F50D", "\U0001F4CA", "\U0001F9EE",
                "\U0001F4BB", "\U00002699", "\U0001F52C",
            ]
            idx = (self._anim_tick // 15) % len(compute_emojis)
            self.computing_emoji.setText(compute_emojis[idx])

            if elapsed * 1000 >= GAME_COMPUTE_MS:
                self._show_results()

        elif self.state == "results":
            # Animate the score bar and number filling up
            if self._score_anim_current < self._score_anim_target:
                speed = max(1, self._score_anim_target / 60)  # fill in ~1s
                self._score_anim_current = min(
                    self._score_anim_target,
                    self._score_anim_current + speed
                )
                display = int(self._score_anim_current)
                bar_pct = int(
                    self._score_anim_current / max(1, self._score_anim_target)
                    * self._score_fraction * 100
                )
                self.score_bar.setValue(bar_pct)
                self.score_number.setText(f"Score: {display}")


# ── Main Window ───────────────────────────────────────────────────
class ShakeViewer(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Raspberry Shake {STATION} — Shake Explorer")
        self.resize(1400, 900)
        self.setStyleSheet(f"background-color: {BG_COLOR};")

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        main_layout = QtWidgets.QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Tab widget
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setStyleSheet(TAB_STYLE)

        self.live_tab = LiveViewTab()
        self.game_tab = ShakeGameTab()

        self.tabs.addTab(self.live_tab, "\U0001F30A  Live View")
        self.tabs.addTab(self.game_tab, "\U0001F3AE  Shake Challenge")

        main_layout.addWidget(self.tabs)

        # Main update timer
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self._tick)
        self.timer.start(REFRESH_MS)

    def _tick(self):
        # Only update the visible tab to save CPU
        idx = self.tabs.currentIndex()
        if idx == 0:
            self.live_tab.tick()
        else:
            self.game_tab.tick()


def parse_args():
    p = argparse.ArgumentParser(
        description="Live waveform / spectrogram viewer for Raspberry Shake."
    )
    p.add_argument("--host", default=SHAKE_HOST,
                   help="IP address or hostname of the Shake (default: %(default)s)")
    p.add_argument("--port", type=int, default=SHAKE_PORT,
                   help="SeedLink port (default: %(default)s)")
    p.add_argument("--station", default=STATION,
                   help="Station code, e.g. R1234 (default: %(default)s)")
    p.add_argument("--network", default=NETWORK,
                   help="Network code (default: %(default)s)")
    p.add_argument("--full-scale", type=float, default=METER_FULL_SCALE,
                   dest="full_scale",
                   help="RMS counts that map to 100%% on the meter "
                        "(default: %(default)s). Tune for your environment.")
    return p.parse_args()


def main():
    global SHAKE_HOST, SHAKE_PORT, STATION, NETWORK, METER_FULL_SCALE
    args = parse_args()
    SHAKE_HOST = args.host
    SHAKE_PORT = args.port
    STATION = args.station
    NETWORK = args.network
    METER_FULL_SCALE = args.full_scale

    t = threading.Thread(target=seedlink_worker, daemon=True)
    t.start()

    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")

    # Set dark palette for Fusion style
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.Window, QtGui.QColor(BG_COLOR))
    palette.setColor(QtGui.QPalette.WindowText, QtGui.QColor(TEXT_COLOR))
    palette.setColor(QtGui.QPalette.Base, QtGui.QColor(PANEL_BG))
    palette.setColor(QtGui.QPalette.Text, QtGui.QColor(TEXT_COLOR))
    palette.setColor(QtGui.QPalette.Button, QtGui.QColor(ACCENT2))
    palette.setColor(QtGui.QPalette.ButtonText, QtGui.QColor(TEXT_COLOR))
    app.setPalette(palette)

    viewer = ShakeViewer()
    viewer.show()
    print("Shake Explorer open — close the window or Ctrl+C to quit.")
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
