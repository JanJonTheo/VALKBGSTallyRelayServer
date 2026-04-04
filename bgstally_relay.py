import json
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Deque, Dict, List, Optional

import requests
from flask import Flask, jsonify, request
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QSystemTrayIcon,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)
from werkzeug.serving import make_server

APP_TITLE = "VALK BGS-Tally Relay Server"
LOCAL_API_KEY = "localapikey"
CONFIG_PATH = Path("relay_config.json")
LOG_PATH = Path("relay_server.log")
ICON_PATH = Path(__file__).resolve().with_name("VALK_logo.png")
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8085
DEFAULT_TIMEOUT = 15
MAX_LOG_BYTES = 1024 * 1024
LOG_BACKUP_COUNT = 5
AUTOSTART_NAME = APP_TITLE


class HealthStats:
    def __init__(self) -> None:
        self.start_time = time.time()
        self.events_received = 0
        self.activities_received = 0
        self.forward_success = 0
        self.forward_errors = 0
        self.filtered_events = 0
        self.filtered_activities = 0
        self.last_error = "-"
        self.last_forward_status = "No forwarding yet"
        self.recent_request_timestamps: Deque[float] = deque()
        self.recent_forward_results: Deque[tuple[float, bool]] = deque()

    def _trim(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        while self.recent_request_timestamps and now - self.recent_request_timestamps[0] > 60:
            self.recent_request_timestamps.popleft()
        while self.recent_forward_results and now - self.recent_forward_results[0][0] > 60:
            self.recent_forward_results.popleft()

    def record_incoming(self, kind: str, count: int = 1) -> None:
        now = time.time()
        if kind == "events":
            self.events_received += count
        elif kind == "activities":
            self.activities_received += count
        for _ in range(max(1, count)):
            self.recent_request_timestamps.append(now)
        self._trim(now)

    def record_forward_success(self, target_name: str, status_code: int) -> None:
        now = time.time()
        self.forward_success += 1
        self.last_forward_status = f"OK: {target_name} [{status_code}]"
        self.recent_forward_results.append((now, True))
        self._trim(now)

    def record_forward_error(self, target_name: str, error_text: str) -> None:
        now = time.time()
        self.forward_errors += 1
        self.last_error = f"{target_name}: {error_text}"
        self.last_forward_status = f"ERROR: {target_name}"
        self.recent_forward_results.append((now, False))
        self._trim(now)

    def record_filter_skip(self, kind: str, count: int = 1) -> None:
        if kind == "events":
            self.filtered_events += max(0, count)
        elif kind == "activities":
            self.filtered_activities += max(0, count)

    def snapshot(self) -> Dict[str, object]:
        now = time.time()
        self._trim(now)
        recent_success = sum(1 for _, ok in self.recent_forward_results if ok)
        recent_errors = sum(1 for _, ok in self.recent_forward_results if not ok)
        return {
            "uptime_seconds": int(now - self.start_time),
            "events_received": self.events_received,
            "activities_received": self.activities_received,
            "requests_per_minute": len(self.recent_request_timestamps),
            "requests_per_second": round(len(self.recent_request_timestamps) / 60.0, 2),
            "forward_success": self.forward_success,
            "forward_errors": self.forward_errors,
            "filtered_events": self.filtered_events,
            "filtered_activities": self.filtered_activities,
            "recent_forward_success": recent_success,
            "recent_forward_errors": recent_errors,
            "last_error": self.last_error,
            "last_forward_status": self.last_forward_status,
        }


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("valk_bgs_tally_relay")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=MAX_LOG_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    logger.info("Application started")
    return logger


LOGGER = setup_logging()
HEALTH = HealthStats()


def normalize_cmdr_name(value: object) -> str:
    return str(value or "").strip().casefold()


def parse_cmdr_filters(value: object) -> List[str]:
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        raw_items = [part.strip() for part in value.replace(";", ",").split(",")]
    else:
        raw_items = []

    result: List[str] = []
    seen = set()
    for item in raw_items:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


@dataclass
class RelayTarget:
    name: str
    base_url: str
    api_key: str = ""
    api_version: str = "1.6.0"
    enabled: bool = True
    timeout: int = DEFAULT_TIMEOUT
    forward_events: bool = True
    forward_activities: bool = True
    cmdr_filters: Optional[List[str]] = None

    def __post_init__(self) -> None:
        self.cmdr_filters = parse_cmdr_filters(self.cmdr_filters)

    def endpoint_url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path

    def has_cmdr_filter(self) -> bool:
        return bool(self.cmdr_filters)

    def allows_cmdr(self, cmdr_name: object) -> bool:
        if not self.cmdr_filters:
            return True
        normalized = normalize_cmdr_name(cmdr_name)
        return normalized in {name.casefold() for name in self.cmdr_filters}

    def cmdr_filter_text(self) -> str:
        return ", ".join(self.cmdr_filters or [])


class RelayTableModel(QAbstractTableModel):
    headers = [
        "Enabled",
        "Name",
        "Base URL",
        "API Key",
        "API Version",
        "Timeout",
        "Cmdr Filter",
        "Events",
        "Activities",
    ]

    def __init__(self, targets: Optional[List[RelayTarget]] = None):
        super().__init__()
        self.targets: List[RelayTarget] = targets or []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.targets)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self.headers[section]
        return str(section + 1)

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable | Qt.ItemIsUserCheckable

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        target = self.targets[index.row()]
        col = index.column()

        if col == 0:
            if role == Qt.CheckStateRole:
                return Qt.Checked if target.enabled else Qt.Unchecked
            if role == Qt.DisplayRole:
                return ""
        elif col == 7:
            if role == Qt.CheckStateRole:
                return Qt.Checked if target.forward_events else Qt.Unchecked
            if role == Qt.DisplayRole:
                return ""
        elif col == 8:
            if role == Qt.CheckStateRole:
                return Qt.Checked if target.forward_activities else Qt.Unchecked
            if role == Qt.DisplayRole:
                return ""
        else:
            if role in (Qt.DisplayRole, Qt.EditRole):
                mapping = {
                    1: target.name,
                    2: target.base_url,
                    3: target.api_key,
                    4: target.api_version,
                    5: str(target.timeout),
                    6: target.cmdr_filter_text(),
                }
                return mapping.get(col)
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if not index.isValid():
            return False
        target = self.targets[index.row()]
        col = index.column()

        if col == 0 and role == Qt.CheckStateRole:
            target.enabled = value == Qt.Checked
        elif col == 7 and role == Qt.CheckStateRole:
            target.forward_events = value == Qt.Checked
        elif col == 8 and role == Qt.CheckStateRole:
            target.forward_activities = value == Qt.Checked
        elif role == Qt.EditRole:
            text = str(value)
            if col == 1:
                target.name = text
            elif col == 2:
                target.base_url = text
            elif col == 3:
                target.api_key = text
            elif col == 4:
                target.api_version = text
            elif col == 5:
                try:
                    target.timeout = max(1, int(text))
                except ValueError:
                    return False
            elif col == 6:
                target.cmdr_filters = parse_cmdr_filters(text)
            else:
                return False
        else:
            return False

        self.dataChanged.emit(index, index, [role])
        return True

    def add_target(self, target: Optional[RelayTarget] = None):
        row = len(self.targets)
        self.beginInsertRows(QModelIndex(), row, row)
        self.targets.append(target or RelayTarget(name="New Target", base_url="http://127.0.0.1:5000"))
        self.endInsertRows()

    def remove_row(self, row: int):
        if row < 0 or row >= len(self.targets):
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self.targets[row]
        self.endRemoveRows()

    def to_list(self) -> List[dict]:
        return [asdict(t) for t in self.targets]

    def load_list(self, rows: List[dict]):
        self.beginResetModel()
        normalized_rows = []
        for row in rows:
            item = dict(row)
            item.setdefault("cmdr_filters", [])
            normalized_rows.append(item)
        self.targets = [RelayTarget(**row) for row in normalized_rows]
        self.endResetModel()


class RelayManager(QObject):
    log_message = Signal(str)

    def __init__(self, target_model: RelayTableModel):
        super().__init__()
        self.target_model = target_model

    def targets_for(self, kind: str) -> List[RelayTarget]:
        result = []
        for target in self.target_model.targets:
            if not target.enabled:
                continue
            if kind == "events" and not target.forward_events:
                continue
            if kind == "activities" and not target.forward_activities:
                continue
            if not target.base_url.strip():
                continue
            result.append(target)
        return result

    def filter_payload_for_target(self, kind: str, payload, target: RelayTarget):
        if not target.has_cmdr_filter():
            return payload

        if kind == "events":
            filtered = [item for item in payload if target.allows_cmdr(item.get("cmdr"))]
            return filtered

        if kind == "activities":
            return payload if target.allows_cmdr(payload.get("cmdr")) else None

        return payload

    @Slot(str, str, object)
    def forward(self, path: str, method: str, payload):
        kind = path.strip("/")
        targets = self.targets_for(kind)
        if not targets:
            self.log(f"No active relay targets configured for {path}.")
            return

        for target in targets:
            filtered_payload = self.filter_payload_for_target(kind, payload, target)

            if kind == "events":
                original_count = len(payload) if isinstance(payload, list) else 0
                filtered_count = len(filtered_payload) if isinstance(filtered_payload, list) else 0
                if target.has_cmdr_filter():
                    filtered_out = max(0, original_count - filtered_count)
                    self.log(
                        f"FILTER {method} {path} -> {target.name}: "
                        f"{filtered_count}/{original_count} event(s) matched "
                        f"[{target.cmdr_filter_text()}]"
                    )
                    if filtered_out:
                        HEALTH.record_filter_skip("events", filtered_out)

                if isinstance(filtered_payload, list) and not filtered_payload:
                    self.log(
                        f"SKIP {method} {path} -> {target.name}: all events filtered out "
                        f"[{target.cmdr_filter_text()}]"
                    )
                    continue

            if kind == "activities" and target.has_cmdr_filter():
                activity_cmdr = payload.get("cmdr") if isinstance(payload, dict) else None
                if filtered_payload is None:
                    HEALTH.record_filter_skip("activities", 1)
                    self.log(
                        f"SKIP {method} {path} -> {target.name}: activity filtered out "
                        f"(CMDR: {activity_cmdr or '-'}, filter: [{target.cmdr_filter_text()}])"
                    )
                    continue
                self.log(
                    f"FILTER {method} {path} -> {target.name}: activity matched "
                    f"(CMDR: {activity_cmdr or '-'}, filter: [{target.cmdr_filter_text()}])"
                )

            url = target.endpoint_url(path)
            headers = {
                "Content-Type": "application/json",
                "apiversion": target.api_version,
            }
            if target.api_key:
                headers["apikey"] = target.api_key

            try:
                response = requests.request(
                    method=method,
                    url=url,
                    json=filtered_payload,
                    headers=headers,
                    timeout=target.timeout,
                )
                HEALTH.record_forward_success(target.name, response.status_code)
                if kind == "events" and isinstance(filtered_payload, list) and target.has_cmdr_filter():
                    self.log(
                        f"{method} {path} -> {target.name} [{response.status_code}] {url} "
                        f"(forwarded {len(filtered_payload)} filtered event(s))"
                    )
                else:
                    self.log(f"{method} {path} -> {target.name} [{response.status_code}] {url}")
            except Exception as exc:
                HEALTH.record_forward_error(target.name, str(exc))
                self.log(f"ERROR {method} {path} -> {target.name}: {exc}")

    def log(self, message: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}"
        LOGGER.info(message)
        self.log_message.emit(line)


class RelayRequestBridge(QObject):
    forward_signal = Signal(str, str, object)
    log_signal = Signal(str)


class RelayServerThread(QThread):
    server_started = Signal(str)
    server_stopped = Signal()
    server_error = Signal(str)

    def __init__(self, bind_host: str, port: int, bridge: RelayRequestBridge):
        super().__init__()
        self.bind_host = bind_host
        self.port = port
        self.bridge = bridge
        self._server = None

    def _authorize(self):
        apikey = request.headers.get("apikey", "")
        return apikey == LOCAL_API_KEY

    def build_app(self):
        app = Flask(__name__)
        bridge = self.bridge

        @app.route("/", methods=["GET"])
        def root():
            return jsonify(
                {
                    "message": "Relay server is running",
                    "name": APP_TITLE,
                    "endpoints": {
                        "events": "/events",
                        "activities": "/activities",
                        "health": "/health",
                    },
                }
            )

        @app.route("/health", methods=["GET"])
        def health():
            return jsonify({"status": "ok", **HEALTH.snapshot()}), 200

        @app.route("/discovery", methods=["GET"])
        def discovery():
            return jsonify(
                {
                    "name": APP_TITLE,
                    "description": "Local relay server for BGS-Tally events and activities.",
                    "endpoints": {
                        "events": {"path": "/events", "minPeriod": 10, "maxBatch": 100},
                        "activities": {"path": "/activities", "minPeriod": 60, "maxBatch": 10},
                    },
                    "headers": {
                        "apikey": {"required": True, "description": "Local relay API key"},
                        "apiversion": {"required": False, "description": "Optional relay API version"},
                    },
                }
            )

        @app.route("/events", methods=["POST"])
        def events():
            if not self._authorize():
                return jsonify({"error": "Unauthorized"}), 401

            payload = request.get_json(silent=True)
            if not isinstance(payload, list):
                return jsonify({"error": "Expected a JSON array for /events"}), 400

            HEALTH.record_incoming("events", len(payload))
            bridge.log_signal.emit(f"Received /events with {len(payload)} event(s)")
            bridge.forward_signal.emit("/events", "POST", payload)
            return jsonify({"status": "accepted", "forwarded": True, "count": len(payload)}), 200

        @app.route("/activities", methods=["PUT"])
        def activities():
            if not self._authorize():
                return jsonify({"error": "Unauthorized"}), 401

            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                return jsonify({"error": "Expected a JSON object for /activities"}), 400

            HEALTH.record_incoming("activities", 1)
            bridge.log_signal.emit("Received /activities")
            bridge.forward_signal.emit("/activities", "PUT", payload)
            return jsonify({"status": "accepted", "forwarded": True}), 200

        return app

    def run(self):
        try:
            app = self.build_app()
            self._server = make_server(self.bind_host, self.port, app, threaded=True)
            self.server_started.emit(f"http://{self.bind_host}:{self.port}")
            self._server.serve_forever()
        except Exception as exc:
            self.server_error.emit(str(exc))
        finally:
            self.server_stopped.emit()

    def stop(self):
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1280, 860)
        icon = self.app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

        self.model = RelayTableModel()
        self.bridge = RelayRequestBridge()
        self.manager = RelayManager(self.model)
        self.server_thread: Optional[RelayServerThread] = None
        self.tray_icon: Optional[QSystemTrayIcon] = None

        self.bridge.forward_signal.connect(self.manager.forward)
        self.bridge.log_signal.connect(self.append_log)
        self.manager.log_message.connect(self.append_log)

        self._build_ui()
        self._setup_tray()
        self._setup_timers()
        self.load_config()

    def app_icon(self) -> QIcon:
        if ICON_PATH.exists():
            return QIcon(str(ICON_PATH))
        return QIcon()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        toolbar = QToolBar("Main")
        self.addToolBar(toolbar)

        act_load = QAction("Load Configuration", self)
        act_load.triggered.connect(self.load_config_dialog)
        toolbar.addAction(act_load)

        act_save = QAction("Save Configuration", self)
        act_save.triggered.connect(self.save_config_dialog)
        toolbar.addAction(act_save)

        top_grid = QGridLayout()
        form_layout = QFormLayout()
        self.host_edit = QLineEdit(DEFAULT_BIND_HOST)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(DEFAULT_PORT)
        self.local_api_key_edit = QLineEdit(LOCAL_API_KEY)
        self.local_api_key_edit.setReadOnly(True)
        self.auto_save_check = QCheckBox("Auto-save configuration")
        self.auto_save_check.setChecked(True)
        self.minimize_to_tray_check = QCheckBox("Minimize to tray on close")
        self.minimize_to_tray_check.setChecked(True)
        self.autostart_check = QCheckBox("Start with Windows")
        self.autostart_check.toggled.connect(self.on_autostart_toggled)

        form_layout.addRow("Bind address", self.host_edit)
        form_layout.addRow("Port", self.port_spin)
        form_layout.addRow("Local API key", self.local_api_key_edit)
        form_layout.addRow("Options", self.auto_save_check)
        form_layout.addRow("Tray", self.minimize_to_tray_check)
        form_layout.addRow("Windows autostart", self.autostart_check)
        top_grid.addLayout(form_layout, 0, 0)

        self.health_group = QGroupBox("Health Dashboard")
        health_layout = QGridLayout(self.health_group)
        self.health_labels: Dict[str, QLabel] = {}
        health_items = [
            ("Server status", "server_status"),
            ("Requests / sec", "rps"),
            ("Requests / min", "rpm"),
            ("Events received", "events"),
            ("Activities received", "activities"),
            ("Forward success", "success"),
            ("Forward errors", "errors"),
            ("Filtered events", "filtered_events"),
            ("Filtered activities", "filtered_activities"),
            ("Last forward status", "last_status"),
            ("Last error", "last_error"),
            ("Uptime", "uptime"),
        ]
        for row, (title, key) in enumerate(health_items):
            title_label = QLabel(f"{title}:")
            value_label = QLabel("-")
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            health_layout.addWidget(title_label, row, 0)
            health_layout.addWidget(value_label, row, 1)
            self.health_labels[key] = value_label
        top_grid.addWidget(self.health_group, 0, 1)
        top_grid.setColumnStretch(1, 1)
        layout.addLayout(top_grid)

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Start Server")
        self.stop_btn = QPushButton("Stop Server")
        self.stop_btn.setEnabled(False)
        self.add_btn = QPushButton("Add Target")
        self.remove_btn = QPushButton("Remove Target")
        self.hide_btn = QPushButton("Hide to Tray")

        self.start_btn.clicked.connect(self.start_server)
        self.stop_btn.clicked.connect(self.stop_server)
        self.add_btn.clicked.connect(self.add_target)
        self.remove_btn.clicked.connect(self.remove_target)
        self.hide_btn.clicked.connect(self.hide_to_tray)

        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(self.hide_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.add_btn)
        btn_row.addWidget(self.remove_btn)
        layout.addLayout(btn_row)

        self.status_label = QLabel("Status: stopped")
        layout.addWidget(self.status_label)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(1, 160)
        self.table.setColumnWidth(2, 250)
        self.table.setColumnWidth(3, 140)
        self.table.setColumnWidth(4, 90)
        self.table.setColumnWidth(5, 70)
        self.table.setColumnWidth(6, 220)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table, 2)

        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        layout.addWidget(self.log_edit, 1)

    def _setup_timers(self):
        self.health_timer = QTimer(self)
        self.health_timer.timeout.connect(self.refresh_health_dashboard)
        self.health_timer.start(1000)
        self.refresh_health_dashboard()

    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.append_log("System tray is not available on this system.")
            return

        self.tray_icon = QSystemTrayIcon(self.app_icon(), self)
        self.tray_icon.setToolTip(APP_TITLE)
        tray_menu = QMenu(self)
        show_action = tray_menu.addAction("Show")
        hide_action = tray_menu.addAction("Hide")
        tray_menu.addSeparator()
        start_action = tray_menu.addAction("Start Server")
        stop_action = tray_menu.addAction("Stop Server")
        tray_menu.addSeparator()
        quit_action = tray_menu.addAction("Exit")

        show_action.triggered.connect(self.restore_from_tray)
        hide_action.triggered.connect(self.hide_to_tray)
        start_action.triggered.connect(self.start_server)
        stop_action.triggered.connect(self.stop_server)
        quit_action.triggered.connect(self.exit_application)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()

    def append_log(self, message: str):
        self.log_edit.appendPlainText(message)

    def refresh_health_dashboard(self):
        snap = HEALTH.snapshot()
        server_running = bool(self.server_thread and self.server_thread.isRunning())
        self.health_labels["server_status"].setText("Running" if server_running else "Stopped")
        self.health_labels["rps"].setText(str(snap["requests_per_second"]))
        self.health_labels["rpm"].setText(str(snap["requests_per_minute"]))
        self.health_labels["events"].setText(str(snap["events_received"]))
        self.health_labels["activities"].setText(str(snap["activities_received"]))
        self.health_labels["success"].setText(str(snap["forward_success"]))
        self.health_labels["errors"].setText(str(snap["forward_errors"]))
        self.health_labels["filtered_events"].setText(str(snap["filtered_events"]))
        self.health_labels["filtered_activities"].setText(str(snap["filtered_activities"]))
        self.health_labels["last_status"].setText(str(snap["last_forward_status"]))
        self.health_labels["last_error"].setText(str(snap["last_error"]))
        self.health_labels["uptime"].setText(self.format_uptime(int(snap["uptime_seconds"])))

    def format_uptime(self, seconds: int) -> str:
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def add_target(self):
        self.model.add_target()
        if self.auto_save_check.isChecked():
            self.save_config()

    def remove_target(self):
        index = self.table.currentIndex()
        if not index.isValid():
            return
        self.model.remove_row(index.row())
        if self.auto_save_check.isChecked():
            self.save_config()

    def start_server(self):
        if self.server_thread and self.server_thread.isRunning():
            return

        host = self.host_edit.text().strip() or DEFAULT_BIND_HOST
        port = self.port_spin.value()
        self.server_thread = RelayServerThread(host, port, self.bridge)
        self.server_thread.server_started.connect(self.on_server_started)
        self.server_thread.server_stopped.connect(self.on_server_stopped)
        self.server_thread.server_error.connect(self.on_server_error)
        self.server_thread.start()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_label.setText("Status: starting server ...")
        if self.auto_save_check.isChecked():
            self.save_config()

    def stop_server(self):
        if self.server_thread:
            self.server_thread.stop()
            self.server_thread.wait(3000)
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_label.setText("Status: stopped")

    def on_server_started(self, url: str):
        self.status_label.setText(f"Status: running on {url}")
        self.append_log(f"Server started on {url}")
        if self.tray_icon:
            self.tray_icon.showMessage(APP_TITLE, f"Relay server started on {url}", QSystemTrayIcon.Information, 3000)

    def on_server_stopped(self):
        self.status_label.setText("Status: stopped")
        self.append_log("Server stopped")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def on_server_error(self, error: str):
        self.status_label.setText("Status: error")
        self.append_log(f"Server error: {error}")
        QMessageBox.critical(self, "Server Error", error)
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if self.tray_icon:
            self.tray_icon.showMessage(APP_TITLE, f"Server error: {error}", QSystemTrayIcon.Critical, 5000)

    def config_dict(self) -> dict:
        return {
            "bind_host": self.host_edit.text().strip() or DEFAULT_BIND_HOST,
            "port": self.port_spin.value(),
            "minimize_to_tray": self.minimize_to_tray_check.isChecked(),
            "windows_autostart": self.autostart_check.isChecked(),
            "targets": self.model.to_list(),
        }

    def save_config(self, path: Path = CONFIG_PATH):
        try:
            path.write_text(json.dumps(self.config_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
            self.append_log(f"Configuration saved: {path}")
        except Exception as exc:
            self.append_log(f"Configuration could not be saved: {exc}")

    def load_config(self, path: Path = CONFIG_PATH):
        if not path.exists():
            self.model.load_list(
                [
                    {
                        "name": "Target Server 1",
                        "base_url": "http://127.0.0.1:5000",
                        "api_key": "",
                        "api_version": "1.6.0",
                        "enabled": True,
                        "timeout": 15,
                        "cmdr_filters": [],
                        "forward_events": True,
                        "forward_activities": True,
                    }
                ]
            )
            self.autostart_check.blockSignals(True)
            self.autostart_check.setChecked(self.is_autostart_enabled())
            self.autostart_check.blockSignals(False)
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.host_edit.setText(data.get("bind_host", DEFAULT_BIND_HOST))
            self.port_spin.setValue(int(data.get("port", DEFAULT_PORT)))
            self.minimize_to_tray_check.setChecked(bool(data.get("minimize_to_tray", True)))
            self.model.load_list(data.get("targets", []))
            self.autostart_check.blockSignals(True)
            self.autostart_check.setChecked(self.is_autostart_enabled())
            self.autostart_check.blockSignals(False)
            self.append_log(f"Configuration loaded: {path}")
        except Exception as exc:
            self.append_log(f"Configuration could not be loaded: {exc}")

    def save_config_dialog(self):
        filename, _ = QFileDialog.getSaveFileName(self, "Save Configuration", str(CONFIG_PATH), "JSON (*.json)")
        if filename:
            self.save_config(Path(filename))

    def load_config_dialog(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Load Configuration", str(CONFIG_PATH), "JSON (*.json)")
        if filename:
            self.load_config(Path(filename))

    def autostart_folder(self) -> Optional[Path]:
        if sys.platform != "win32":
            return None
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"

    def autostart_shortcut_path(self) -> Optional[Path]:
        folder = self.autostart_folder()
        if not folder:
            return None
        return folder / f"{AUTOSTART_NAME}.bat"

    def is_autostart_enabled(self) -> bool:
        path = self.autostart_shortcut_path()
        return bool(path and path.exists())

    def enable_autostart(self) -> bool:
        path = self.autostart_shortcut_path()
        if not path:
            self.append_log("Windows autostart is only supported on Windows.")
            return False
        try:
            path.write_text(f'@echo off\r\nstart "" "{sys.executable}" "{Path(__file__).resolve()}"\r\n', encoding="utf-8")
            self.append_log(f"Windows autostart enabled: {path}")
            return True
        except Exception as exc:
            self.append_log(f"Could not enable Windows autostart: {exc}")
            return False

    def disable_autostart(self) -> bool:
        path = self.autostart_shortcut_path()
        if not path:
            self.append_log("Windows autostart is only supported on Windows.")
            return False
        try:
            if path.exists():
                path.unlink()
            self.append_log("Windows autostart disabled")
            return True
        except Exception as exc:
            self.append_log(f"Could not disable Windows autostart: {exc}")
            return False

    def on_autostart_toggled(self, checked: bool):
        ok = self.enable_autostart() if checked else self.disable_autostart()
        if not ok:
            self.autostart_check.blockSignals(True)
            self.autostart_check.setChecked(self.is_autostart_enabled())
            self.autostart_check.blockSignals(False)
            return
        if self.auto_save_check.isChecked():
            self.save_config()

    def hide_to_tray(self):
        if self.tray_icon:
            self.hide()
            self.tray_icon.showMessage(APP_TITLE, "Application is still running in the system tray.", QSystemTrayIcon.Information, 2500)
        else:
            self.hide()

    def restore_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.isVisible():
                self.hide()
            else:
                self.restore_from_tray()

    def closeEvent(self, event: QCloseEvent):
        if self.minimize_to_tray_check.isChecked() and self.tray_icon:
            event.ignore()
            self.hide_to_tray()
            return
        if self.server_thread and self.server_thread.isRunning():
            self.stop_server()
        if self.auto_save_check.isChecked():
            self.save_config()
        super().closeEvent(event)

    def exit_application(self):
        if self.server_thread and self.server_thread.isRunning():
            self.stop_server()
        if self.auto_save_check.isChecked():
            self.save_config()
        if self.tray_icon:
            self.tray_icon.hide()
        QApplication.instance().quit()


def ensure_icon_file():
    src = Path("/mnt/data/VALK_logo.png")
    if src.exists() and not ICON_PATH.exists():
        try:
            ICON_PATH.write_bytes(src.read_bytes())
        except Exception:
            pass


def main():
    ensure_icon_file()
    app = QApplication(sys.argv)
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
