"""Proto logger window: pick a vehicle + interfaces, watch per-bus rates, log.

Deliberately minimal (no decode -- that's Phase 3). The window:
  - lets you pick a vehicle profile (sets bitrate + log folder)
  - lists detected dongles plus the software (virtual/localhost) interfaces
  - you tick which to capture, hit Start, and watch frames/sec, total, last-seen
  - optionally writes a candump .log into the profile's folder

All hardware/logging work happens in canbench.engine.CaptureEngine; this file is
just the Qt shell that drives it and polls its TrafficMonitor on a timer.
"""
from __future__ import annotations

import datetime
import time
from pathlib import Path

from PyQt5 import QtCore, QtWidgets

from ..profiles import load_profiles
from ..buses import list_interfaces, SOFTWARE_INTERFACES
from ..engine import CaptureEngine

REFRESH_MS = 500


class LoggerWindow(QtWidgets.QMainWindow):
    def __init__(self, profiles=None):
        super().__init__()
        self.setWindowTitle("canbench - CAN logger")
        self.resize(720, 420)
        self.profiles = profiles or load_profiles()
        self.engine: CaptureEngine | None = None
        self.interfaces = []            # [(iface, ch, desc), ...] current table order
        self._row_checks = {}           # row -> QCheckBox
        self._selected_rows = []        # bus_id -> table row (set at start)

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
        self.bitrate_spin = QtWidgets.QSpinBox()
        self.bitrate_spin.setRange(1000, 1000000)
        self.bitrate_spin.setSingleStep(1000)
        self.bitrate_spin.setValue(500000)
        self.bitrate_spin.setGroupSeparatorShown(True)
        top.addWidget(self.bitrate_spin)

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

        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Log", "Interface", "Frames/s", "Total", "Last seen"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.table.setColumnWidth(0, 44)
        v.addWidget(self.table)

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
        self.bitrate_spin.setValue(prof.bitrate)
        self.logdir_label.setText(f"Log folder: {prof.log_dir}")

    def refresh_interfaces(self):
        if self.engine and self.engine.running:
            return
        bitrate = self.bitrate_spin.value()
        self.interfaces = list_interfaces(bitrate)
        self._row_checks.clear()
        self.table.setRowCount(len(self.interfaces))
        n_hw = 0
        for row, (iface, ch, desc) in enumerate(self.interfaces):
            chk = QtWidgets.QCheckBox()
            is_hw = iface not in SOFTWARE_INTERFACES
            chk.setChecked(is_hw)          # default: capture real dongles; software opt-in
            n_hw += 1 if is_hw else 0
            holder = QtWidgets.QWidget()
            lay = QtWidgets.QHBoxLayout(holder)
            lay.addWidget(chk)
            lay.setAlignment(QtCore.Qt.AlignCenter)
            lay.setContentsMargins(0, 0, 0, 0)
            self.table.setCellWidget(row, 0, holder)
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(desc))
            for col in (2, 3, 4):
                self.table.setItem(row, col, QtWidgets.QTableWidgetItem("-"))
            self._row_checks[row] = chk
        self.status.showMessage(f"{n_hw} hardware + {len(self.interfaces) - n_hw} software interface(s)")

    # ---- start / stop ----------------------------------------------------
    def toggle(self):
        if self.engine and self.engine.running:
            self._stop()
        else:
            self._start()

    def _start(self):
        selected = [(row, self.interfaces[row]) for row in sorted(self._row_checks)
                    if self._row_checks[row].isChecked()]
        if not selected:
            QtWidgets.QMessageBox.warning(self, "No interfaces", "Tick at least one interface to capture.")
            return

        self._selected_rows = [row for row, _ in selected]
        chosen = [iface for _, iface in selected]

        log_path = None
        if self.log_check.isChecked():
            prof = self.profiles.get(self.profile_combo.currentText())
            prof.log_dir.mkdir(parents=True, exist_ok=True)
            stem = f"canlog_{datetime.datetime.now():%Y%m%d_%H%M%S}.log"
            log_path = str(prof.log_dir / stem)

        self.engine = CaptureEngine(bitrate=self.bitrate_spin.value())
        result = self.engine.start(chosen, log_path=log_path)

        if not result.opened:
            msgs = "\n".join(f"{d}: {e}" for d, e in result.failed) or "no interfaces opened"
            QtWidgets.QMessageBox.critical(self, "Start failed", msgs)
            self.engine = None
            return
        if result.failed:
            self.status.showMessage("Some interfaces failed: "
                                    + "; ".join(d for d, _ in result.failed))

        self._set_controls_enabled(False)
        self.start_btn.setText("Stop")
        self.timer.start()
        where = f"logging to {log_path}" if log_path else "monitor only (not logging)"
        self.status.showMessage(f"Running - {where}")

    def _stop(self):
        path = self.engine.log_path if self.engine else None
        total = self.engine.total if self.engine else 0
        if self.engine:
            self.engine.stop()
        self.timer.stop()
        self._set_controls_enabled(True)
        self.start_btn.setText("Start")
        if path:
            self.status.showMessage(f"Stopped - wrote {total} frames to {path}")
        else:
            self.status.showMessage(f"Stopped - {total} frames (not logged)")
        self.engine = None

    def _set_controls_enabled(self, enabled):
        self.profile_combo.setEnabled(enabled)
        self.bitrate_spin.setEnabled(enabled)
        self.log_check.setEnabled(enabled)
        self.refresh_btn.setEnabled(enabled)
        for chk in self._row_checks.values():
            chk.setEnabled(enabled)

    # ---- live stats ------------------------------------------------------
    def _refresh_stats(self):
        if not (self.engine and self.engine.running):
            return
        stats = self.engine.monitor.poll()
        now = time.time()
        for bus_id, row in enumerate(self._selected_rows):
            st = stats.get(bus_id)
            if st is None:
                continue
            self.table.item(row, 2).setText(f"{st.rate_hz:,.0f}")
            self.table.item(row, 3).setText(f"{st.total:,}")
            if st.last_seen is not None:
                self.table.item(row, 4).setText(f"{now - st.last_seen:.1f}s ago")
        self.total_label.setText(f"Total: {self.engine.total:,} frames")

    def closeEvent(self, event):
        if self.engine and self.engine.running:
            self.engine.stop()
        super().closeEvent(event)
