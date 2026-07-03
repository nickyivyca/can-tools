"""Proto logger window: pick a vehicle + interfaces, watch per-bus rates, log.

Deliberately minimal (no decode -- that's Phase 3). The window:
  - pick a vehicle profile (sets bitrate + log folder from canbench.ini / defaults)
  - lists detected dongles (real hardware). The in-process virtual bus is shown
    only with --virtual; a localhost "network CAN" is added on demand.
  - tick which dongles to capture, optionally set a per-line passthrough target,
    hit Start, and watch frames/sec, total, last-seen
  - writes a candump .log into the profile's folder

Ownership rule: only interfaces that are *enabled* (ticked) or are a passthrough
endpoint get opened. An untouched dongle is left free for other programs (e.g.
a real-world loopback test), even though it is listed here.

All hardware/logging/forwarding happens in canbench.engine.CaptureEngine; this
file is the Qt shell that drives it and polls its TrafficMonitor on a timer.
"""
from __future__ import annotations

import datetime
import time
from pathlib import Path

from PyQt5 import QtCore, QtWidgets

from ..profiles import load_profiles
from ..buses import VIRTUAL_CHANNEL, UDP_MULTICAST_GROUP
from ..live.receiver import (detect_all_can_interfaces, reset_gs_usb_cache,
                             gs_usb_backend_available)
from ..engine import CaptureEngine

REFRESH_MS = 500

# Standard CAN bit rates (bps) offered in the dropdown, fastest first.
STANDARD_BITRATES = [1000000, 800000, 500000, 250000, 125000, 100000,
                     83333, 50000, 33333, 20000, 10000]

COL_LOG, COL_IFACE, COL_PT, COL_RATE, COL_TOTAL, COL_SEEN = range(6)


def _fmt_bitrate(bps: int) -> str:
    if bps >= 1_000_000 and bps % 1_000_000 == 0:
        return f"{bps // 1_000_000} Mbit/s"
    if bps % 1000 == 0:
        return f"{bps // 1000} kbit/s"
    return f"{bps / 1000:.3f} kbit/s"


class LoggerWindow(QtWidgets.QMainWindow):
    def __init__(self, profiles=None, show_virtual=False, ini_path=None):
        super().__init__()
        self.setWindowTitle("canbench - CAN logger")
        self.resize(820, 460)
        self.profiles = profiles or load_profiles(ini_path=ini_path)
        self.show_virtual = show_virtual

        self.engine: CaptureEngine | None = None
        self.hw_interfaces = []        # detected hardware (iface, ch, desc)
        self.extra_interfaces = []     # user-added network CAN (iface, ch, desc)
        self.interfaces = []           # full current table order
        self._row_checks = {}          # row -> QCheckBox
        self._pt_combos = {}           # row -> QComboBox (passthrough target)
        self._busid_to_row = {}        # bus_id -> table row, set at Start

        self._build_ui()
        if self.profiles.names():
            self.profile_combo.setCurrentText(self.profiles.default_profile)
            self._load_profile(self.profiles.default_profile)
        self.refresh_interfaces()

    # ---- UI construction -------------------------------------------------
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        v = QtWidgets.QVBoxLayout(central)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Vehicle:"))
        self.profile_combo = QtWidgets.QComboBox()
        self.profile_combo.addItems(self.profiles.names())
        self.profile_combo.currentTextChanged.connect(self._load_profile)
        top.addWidget(self.profile_combo)

        top.addSpacing(12)
        top.addWidget(QtWidgets.QLabel("Bitrate:"))
        self.bitrate_combo = QtWidgets.QComboBox()
        for bps in STANDARD_BITRATES:
            self.bitrate_combo.addItem(_fmt_bitrate(bps), bps)
        top.addWidget(self.bitrate_combo)

        top.addSpacing(12)
        self.log_check = QtWidgets.QCheckBox("Log to file")
        self.log_check.setChecked(True)
        top.addWidget(self.log_check)

        top.addStretch(1)
        self.refresh_btn = QtWidgets.QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_interfaces)
        top.addWidget(self.refresh_btn)
        v.addLayout(top)

        self.logdir_label = QtWidgets.QLabel()
        self.logdir_label.setStyleSheet("color: gray;")
        v.addWidget(self.logdir_label)

        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Log", "Interface", "Passthrough →", "Frames/s", "Total", "Last seen"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(COL_IFACE, QtWidgets.QHeaderView.Stretch)
        self.table.setColumnWidth(COL_LOG, 40)
        self.table.setColumnWidth(COL_PT, 180)
        v.addWidget(self.table)

        row2 = QtWidgets.QHBoxLayout()
        self.add_net_btn = QtWidgets.QPushButton("Add network CAN…")
        self.add_net_btn.clicked.connect(self.add_network_can)
        row2.addWidget(self.add_net_btn)
        row2.addStretch(1)
        v.addLayout(row2)

        bottom = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Start")
        self.start_btn.setMinimumWidth(120)
        self.start_btn.clicked.connect(self.toggle)
        bottom.addWidget(self.start_btn)
        self.total_label = QtWidgets.QLabel("")
        bottom.addWidget(self.total_label)
        bottom.addStretch(1)
        v.addLayout(bottom)

        self.status = self.statusBar()
        self.status.showMessage("Idle")

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self._refresh_stats)

    # ---- profile / interface handling -----------------------------------
    def _load_profile(self, name):
        if name not in self.profiles.profiles:
            return
        prof = self.profiles.get(name)
        self._set_bitrate(prof.bitrate)
        self.logdir_label.setText(f"Log folder: {prof.log_dir}")

    def _set_bitrate(self, bps):
        idx = self.bitrate_combo.findData(bps)
        if idx < 0:
            self.bitrate_combo.addItem(_fmt_bitrate(bps), bps)
            idx = self.bitrate_combo.findData(bps)
        self.bitrate_combo.setCurrentIndex(idx)

    def current_bitrate(self) -> int:
        return int(self.bitrate_combo.currentData())

    def refresh_interfaces(self):
        if self.engine and self.engine.running:
            return
        reset_gs_usb_cache()      # re-probe gs_usb each Refresh (recover from a transient miss)
        self.hw_interfaces = detect_all_can_interfaces(self.current_bitrate())
        self._rebuild_table()
        n_net = len(self.extra_interfaces)
        msg = (f"{len(self.hw_interfaces)} hardware"
               + (f" + {n_net} network CAN" if n_net else "")
               + (" + virtual" if self.show_virtual else ""))
        if "gs_usb" not in {i[0] for i in self.hw_interfaces}:
            if not gs_usb_backend_available():
                msg += "  |  gs_usb/Innomaker backend NOT available (need python-can[gs_usb] + libusb-package in this Python)"
            else:
                msg += "  |  no gs_usb/Innomaker device found (check USB / try Refresh)"
        self.status.showMessage(msg)

    def add_network_can(self):
        if self.engine and self.engine.running:
            return
        group, ok = QtWidgets.QInputDialog.getText(
            self, "Add network CAN",
            "UDP multicast group (localhost network CAN):",
            text=UDP_MULTICAST_GROUP)
        if not ok or not group.strip():
            return
        group = group.strip()
        self.extra_interfaces.append(("udp_multicast", group, f"Network CAN ({group})"))
        self._rebuild_table()

    def _current_interfaces(self):
        ifaces = list(self.hw_interfaces)
        if self.show_virtual:
            ifaces.append(("virtual", VIRTUAL_CHANNEL, "Virtual (in-process)"))
        ifaces += self.extra_interfaces
        return ifaces

    def _rebuild_table(self):
        self.interfaces = self._current_interfaces()
        self._row_checks.clear()
        self._pt_combos.clear()
        self.table.setRowCount(len(self.interfaces))
        for row, (iface, ch, desc) in enumerate(self.interfaces):
            chk = QtWidgets.QCheckBox()
            chk.setChecked(iface != "virtual")     # dongles + network CAN on by default; virtual opt-in
            holder = QtWidgets.QWidget()
            lay = QtWidgets.QHBoxLayout(holder)
            lay.addWidget(chk)
            lay.setAlignment(QtCore.Qt.AlignCenter)
            lay.setContentsMargins(0, 0, 0, 0)
            self.table.setCellWidget(row, COL_LOG, holder)
            self._row_checks[row] = chk

            self.table.setItem(row, COL_IFACE, QtWidgets.QTableWidgetItem(desc))

            combo = QtWidgets.QComboBox()
            combo.addItem("(none)", None)
            self.table.setCellWidget(row, COL_PT, combo)
            self._pt_combos[row] = combo

            for col in (COL_RATE, COL_TOTAL, COL_SEEN):
                self.table.setItem(row, col, QtWidgets.QTableWidgetItem("-"))

        self._rebuild_passthrough_options()

    def _rebuild_passthrough_options(self):
        for row, combo in self._pt_combos.items():
            prev = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("(none)", None)
            for other, (iface, ch, desc) in enumerate(self.interfaces):
                if other == row:
                    continue
                combo.addItem(desc, other)
            idx = combo.findData(prev)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

    # ---- start / stop ----------------------------------------------------
    def toggle(self):
        if self.engine and self.engine.running:
            self._stop()
        else:
            self._start()

    def _start(self):
        n = len(self.interfaces)
        log_checked = {r for r in range(n) if self._row_checks[r].isChecked()}
        pt_dest = {r: self._pt_combos[r].currentData() for r in range(n)}

        active = set(log_checked)
        for r, dest in pt_dest.items():
            if dest is not None:
                active.add(r)
                active.add(dest)
        if not active:
            QtWidgets.QMessageBox.warning(self, "Nothing selected",
                                          "Tick a dongle to log, or set a passthrough target.")
            return

        active_rows = sorted(active)
        row_to_busid = {r: i for i, r in enumerate(active_rows)}
        self._busid_to_row = {i: r for r, i in row_to_busid.items()}
        opened = [self.interfaces[r] for r in active_rows]
        capture_ids = {row_to_busid[r] for r in active_rows if r in log_checked}
        routes = {row_to_busid[r]: row_to_busid[pt_dest[r]]
                  for r in active_rows if pt_dest[r] is not None}

        log_path = None
        if self.log_check.isChecked() and capture_ids:
            prof = self.profiles.get(self.profile_combo.currentText())
            prof.log_dir.mkdir(parents=True, exist_ok=True)
            stem = f"canlog_{datetime.datetime.now():%Y%m%d_%H%M%S}.log"
            log_path = str(prof.log_dir / stem)

        self.engine = CaptureEngine(bitrate=self.current_bitrate())
        result = self.engine.start(opened, log_path=log_path,
                                   capture_ids=capture_ids, routes=routes)
        if not result.opened:
            msgs = "\n".join(f"{d}: {e}" for d, e in result.failed) or "no interfaces opened"
            QtWidgets.QMessageBox.critical(self, "Start failed", msgs)
            self.engine = None
            return
        if result.failed:
            self.status.showMessage("Some interfaces failed: "
                                    + "; ".join(d for d, _ in result.failed))

        # clear stat cells for rows not opened this run
        for row in range(len(self.interfaces)):
            if row not in self._busid_to_row.values():
                for col in (COL_RATE, COL_TOTAL, COL_SEEN):
                    self.table.item(row, col).setText("-")

        self._set_controls_enabled(False)
        self.start_btn.setText("Stop")
        self.timer.start()
        where = f"logging to {log_path}" if log_path else "monitor only (not logging)"
        extra = f", {len(routes)} passthrough route(s)" if routes else ""
        self.status.showMessage(f"Running - {where}{extra}")

    def _stop(self):
        path = self.engine.log_path if self.engine else None
        total = self.engine.total if self.engine else 0
        if self.engine:
            self.engine.stop()
        self.timer.stop()
        self._set_controls_enabled(True)
        self.start_btn.setText("Start")
        if path:
            self.status.showMessage(f"Stopped - wrote frames to {path}")
        else:
            self.status.showMessage(f"Stopped - {total} frames seen (not logged)")
        self.engine = None

    def _set_controls_enabled(self, enabled):
        self.profile_combo.setEnabled(enabled)
        self.bitrate_combo.setEnabled(enabled)
        self.log_check.setEnabled(enabled)
        self.refresh_btn.setEnabled(enabled)
        self.add_net_btn.setEnabled(enabled)
        for chk in self._row_checks.values():
            chk.setEnabled(enabled)
        for combo in self._pt_combos.values():
            combo.setEnabled(enabled)

    # ---- live stats ------------------------------------------------------
    def _refresh_stats(self):
        if not (self.engine and self.engine.running):
            return
        stats = self.engine.monitor.poll()
        now = time.time()
        for bus_id, row in self._busid_to_row.items():
            st = stats.get(bus_id)
            if st is None:
                continue
            self.table.item(row, COL_RATE).setText(f"{st.rate_hz:,.0f}")
            self.table.item(row, COL_TOTAL).setText(f"{st.total:,}")
            if st.last_seen is not None:
                self.table.item(row, COL_SEEN).setText(f"{now - st.last_seen:.1f}s ago")
        self.total_label.setText(f"Total: {self.engine.total:,} frames")

    def closeEvent(self, event):
        if self.engine and self.engine.running:
            self.engine.stop()
        super().closeEvent(event)
