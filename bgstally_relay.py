import json
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import requests
from flask import Flask, Response, jsonify, request
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
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
        self.objectives_received = 0
        self.forward_success = 0
        self.forward_errors = 0
        self.filtered_events = 0
        self.filtered_activities = 0
        self.filtered_objectives = 0
        self.last_error = "-"
        self.last_forward_status = "No forwarding yet"
        self.recent_request_timestamps: Deque[float] = deque()
        self.recent_forward_results: Deque[Tuple[float, bool]] = deque()

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
        elif kind == "objectives":
            self.objectives_received += count
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
        elif kind == "objectives":
            self.filtered_objectives += max(0, count)

    def snapshot(self) -> Dict[str, object]:
        now = time.time()
        self._trim(now)
        recent_success = sum(1 for _, ok in self.recent_forward_results if ok)
        recent_errors = sum(1 for _, ok in self.recent_forward_results if not ok)
        return {
            "uptime_seconds": int(now - self.start_time),
            "events_received": self.events_received,
            "activities_received": self.activities_received,
            "objectives_received": self.objectives_received,
            "requests_per_minute": len(self.recent_request_timestamps),
            "requests_per_second": round(len(self.recent_request_timestamps) / 60.0, 2),
            "forward_success": self.forward_success,
            "forward_errors": self.forward_errors,
            "filtered_events": self.filtered_events,
            "filtered_activities": self.filtered_activities,
            "filtered_objectives": self.filtered_objectives,
            "recent_forward_success": recent_success,
            "recent_forward_errors": recent_errors,
            "last_error": self.last_error,
            "last_forward_status": self.last_forward_status,
        }


HEALTH = HealthStats()


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


def preview_text(text: object, max_length: int = 180) -> str:
    value = str(text or "")
    value = value.replace("\r", " ").replace("\n", " ").strip()
    return value if len(value) <= max_length else value[: max_length - 3] + "..."


def request_context_summary() -> str:
    remote = request.headers.get("X-Forwarded-For") or request.remote_addr or "-"
    ua = preview_text(request.headers.get("User-Agent", "-"), 80)
    api_version = request.headers.get("apiversion", "-")
    content_length = request.content_length if request.content_length is not None else 0
    return f"remote={remote} apiversion={api_version} content_length={content_length} ua='{ua}'"


def payload_summary(payload: object) -> str:
    if isinstance(payload, list):
        cmdrs = sorted({str(item.get("cmdr") or "").strip() for item in payload if isinstance(item, dict) and item.get("cmdr")})
        events = sorted({str(item.get("event") or "").strip() for item in payload if isinstance(item, dict) and item.get("event")})
        return (
            f"type=list count={len(payload)} events=[{', '.join(events) if events else '-'}] "
            f"cmdrs=[{', '.join(cmdrs) if cmdrs else '-'}]"
        )
    if isinstance(payload, dict):
        keys = ", ".join(sorted(payload.keys())[:15])
        cmdr = payload.get("cmdr", "-")
        return f"type=dict keys=[{keys or '-'}] cmdr={cmdr or '-'}"
    if payload is None:
        return "type=null"
    return f"type={type(payload).__name__} value='{preview_text(payload, 120)}'"


@dataclass
class RelayTarget:
    name: str
    base_url: str
    api_key: str = ""
    api_version: str = "1.6.0"
    enabled: bool = True
    timeout: int = DEFAULT_TIMEOUT
    cmdr_filters: List[str] = field(default_factory=list)
    forward_events: bool = True
    forward_activities: bool = True
    default_objectives: bool = False
    runtime_stats: Dict[str, Dict[str, int]] = field(default_factory=lambda: {
        "forwarded": {"events": 0, "activities": 0, "objectives": 0},
        "filtered": {"events": 0, "activities": 0, "objectives": 0},
    }, repr=False)

    def __post_init__(self) -> None:
        self.cmdr_filters = parse_cmdr_filters(self.cmdr_filters)
        if not isinstance(self.runtime_stats, dict) or "forwarded" not in self.runtime_stats or "filtered" not in self.runtime_stats:
            self.runtime_stats = {
                "forwarded": {"events": 0, "activities": 0, "objectives": 0},
                "filtered": {"events": 0, "activities": 0, "objectives": 0},
            }

    @classmethod
    def from_dict(cls, data: dict) -> "RelayTarget":
        return cls(
            name=str(data.get("name", "New Target")),
            base_url=str(data.get("base_url", "http://127.0.0.1:5000")),
            api_key=str(data.get("api_key", "")),
            api_version=str(data.get("api_version", "1.6.0")),
            enabled=bool(data.get("enabled", True)),
            timeout=int(data.get("timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT),
            cmdr_filters=parse_cmdr_filters(data.get("cmdr_filters", [])),
            forward_events=bool(data.get("forward_events", True)),
            forward_activities=bool(data.get("forward_activities", True)),
            default_objectives=bool(data.get("default_objectives", False)),
        )

    def to_config_dict(self) -> dict:
        data = asdict(self)
        data.pop("runtime_stats", None)
        return data

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
        return ", ".join(self.cmdr_filters)

    def stats_text(self, kind: str) -> str:
        forwarded = self.runtime_stats["forwarded"].get(kind, 0)
        filtered = self.runtime_stats["filtered"].get(kind, 0)
        return f"F:{forwarded} | X:{filtered}"

    def increment_forwarded(self, kind: str, count: int = 1) -> None:
        self.runtime_stats["forwarded"][kind] = self.runtime_stats["forwarded"].get(kind, 0) + max(0, count)

    def increment_filtered(self, kind: str, count: int = 1) -> None:
        self.runtime_stats["filtered"][kind] = self.runtime_stats["filtered"].get(kind, 0) + max(0, count)


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
        "Default Objectives",
        "Events Calls",
        "Activities Calls",
        "Objectives Calls",
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
        col = index.column()
        flags = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if col in {0, 7, 8, 9}:
            flags |= Qt.ItemIsUserCheckable
        elif col in {1, 2, 3, 4, 5, 6}:
            flags |= Qt.ItemIsEditable
        return flags

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        target = self.targets[index.row()]
        col = index.column()

        if col in {0, 7, 8, 9}:
            if role == Qt.CheckStateRole:
                values = {
                    0: target.enabled,
                    7: target.forward_events,
                    8: target.forward_activities,
                    9: target.default_objectives,
                }
                return Qt.Checked if values[col] else Qt.Unchecked
            if role == Qt.DisplayRole:
                return ""
            return None

        if role in (Qt.DisplayRole, Qt.EditRole):
            mapping = {
                1: target.name,
                2: target.base_url,
                3: target.api_key,
                4: target.api_version,
                5: str(target.timeout),
                6: target.cmdr_filter_text(),
                10: target.stats_text("events"),
                11: target.stats_text("activities"),
                12: target.stats_text("objectives"),
            }
            return mapping.get(col)
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if not index.isValid():
            return False
        row = index.row()
        target = self.targets[row]
        col = index.column()

        if role == Qt.CheckStateRole and col in {0, 7, 8, 9}:
            checked = value in (Qt.Checked, True, 2)
            if col == 0:
                target.enabled = checked
            elif col == 7:
                target.forward_events = checked
            elif col == 8:
                target.forward_activities = checked
            elif col == 9:
                if checked:
                    for other in self.targets:
                        other.default_objectives = False
                    target.default_objectives = True
                    self.dataChanged.emit(self.index(0, 9), self.index(max(0, len(self.targets) - 1), 9), [Qt.CheckStateRole])
                else:
                    target.default_objectives = False
            self.dataChanged.emit(index, index, [Qt.CheckStateRole, Qt.DisplayRole])
            return True

        if role == Qt.EditRole:
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
            self.dataChanged.emit(index, index, [Qt.EditRole, Qt.DisplayRole])
            return True
        return False

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
        return [t.to_config_dict() for t in self.targets]

    def load_list(self, rows: List[dict]):
        self.beginResetModel()
        self.targets = [RelayTarget.from_dict(row) for row in rows]
        self.endResetModel()

    def refresh_runtime_stats(self, row: int):
        if row < 0 or row >= len(self.targets):
            return
        left = self.index(row, 10)
        right = self.index(row, 12)
        self.dataChanged.emit(left, right, [Qt.DisplayRole])


class RelayManager(QObject):
    log_message = Signal(str, str)

    def __init__(self, target_model: RelayTableModel):
        super().__init__()
        self.target_model = target_model

    def log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {level} {message}"
        log_level = getattr(logging, level.upper(), logging.INFO)
        LOGGER.log(log_level, message)
        self.log_message.emit(level.upper(), line)

    def record_target_forwarded(self, target: RelayTarget, kind: str, count: int = 1):
        target.increment_forwarded(kind, count)
        self.target_model.refresh_runtime_stats(self.target_model.targets.index(target))

    def record_target_filtered(self, target: RelayTarget, kind: str, count: int = 1):
        target.increment_filtered(kind, count)
        self.target_model.refresh_runtime_stats(self.target_model.targets.index(target))

    def targets_for(self, kind: str) -> List[RelayTarget]:
        result: List[RelayTarget] = []
        for target in self.target_model.targets:
            if not target.enabled or not target.base_url.strip():
                continue
            if kind == "events" and not target.forward_events:
                continue
            if kind == "activities" and not target.forward_activities:
                continue
            result.append(target)
        return result

    def get_default_objectives_target(self) -> Optional[RelayTarget]:
        for target in self.target_model.targets:
            if target.enabled and target.default_objectives and target.base_url.strip():
                return target
        return None

    def filter_payload_for_target(self, kind: str, payload: object, target: RelayTarget):
        if not target.has_cmdr_filter():
            return payload
        if kind == "events" and isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict) and target.allows_cmdr(item.get("cmdr"))]
        if kind == "activities" and isinstance(payload, dict):
            return payload if target.allows_cmdr(payload.get("cmdr")) else None
        return payload

    @Slot(str, str, object)
    def forward(self, path: str, method: str, payload):
        kind = path.strip("/")
        targets = self.targets_for(kind)
        if not targets:
            self.log(f"NO_TARGET {method} {path}: no active relay targets configured | {payload_summary(payload)}", "WARNING")
            return

        self.log(f"DISPATCH {method} {path}: target_count={len(targets)} | {payload_summary(payload)}")

        for target in targets:
            filtered_payload = self.filter_payload_for_target(kind, payload, target)
            if kind == "events":
                original_count = len(payload) if isinstance(payload, list) else 0
                matched_count = len(filtered_payload) if isinstance(filtered_payload, list) else 0
                if target.has_cmdr_filter():
                    filtered_out = max(0, original_count - matched_count)
                    self.log(
                        f"FILTER {method} {path} -> {target.name}: {matched_count}/{original_count} event(s) matched [{target.cmdr_filter_text()}]",
                        "DEBUG",
                    )
                    if filtered_out:
                        HEALTH.record_filter_skip("events", filtered_out)
                        self.record_target_filtered(target, "events", filtered_out)
                if isinstance(filtered_payload, list) and not filtered_payload:
                    self.log(f"SKIP {method} {path} -> {target.name}: all events filtered out [{target.cmdr_filter_text()}]", "INFO")
                    continue
            elif kind == "activities" and target.has_cmdr_filter():
                activity_cmdr = payload.get("cmdr") if isinstance(payload, dict) else None
                if filtered_payload is None:
                    HEALTH.record_filter_skip("activities", 1)
                    self.record_target_filtered(target, "activities", 1)
                    self.log(
                        f"SKIP {method} {path} -> {target.name}: activity filtered out (CMDR: {activity_cmdr or '-'}, filter: [{target.cmdr_filter_text()}])",
                        "INFO",
                    )
                    continue
                self.log(
                    f"FILTER {method} {path} -> {target.name}: activity matched (CMDR: {activity_cmdr or '-'}, filter: [{target.cmdr_filter_text()}])",
                    "DEBUG",
                )

            url = target.endpoint_url(path)
            headers = {"Content-Type": "application/json", "apiversion": target.api_version}
            if target.api_key:
                headers["apikey"] = target.api_key

            self.log(
                f"FORWARD_BEGIN {method} {path} -> {target.name} | url={url} | timeout={target.timeout}s | headers(apiversion={target.api_version}, apikey={'set' if bool(target.api_key) else 'empty'}) | {payload_summary(filtered_payload)}",
                "DEBUG",
            )
            try:
                started = time.perf_counter()
                response = requests.request(method=method, url=url, json=filtered_payload, headers=headers, timeout=target.timeout)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                HEALTH.record_forward_success(target.name, response.status_code)
                self.record_target_forwarded(target, kind, 1)
                self.log(
                    f"FORWARD_OK {method} {path} -> {target.name} [{response.status_code}] | elapsed_ms={elapsed_ms} | response_bytes={len(response.content)} | response_preview='{preview_text(response.text)}'",
                    "INFO",
                )
            except Exception as exc:
                HEALTH.record_forward_error(target.name, str(exc))
                self.log(f"FORWARD_ERROR {method} {path} -> {target.name}: {exc} | {payload_summary(filtered_payload)}", "ERROR")

    def forward_objectives(self, path: str, method: str, payload=None, query_string: Optional[bytes] = None):
        target = self.get_default_objectives_target()
        if not target:
            HEALTH.record_filter_skip("objectives", 1)
            self.log(f"OBJECTIVES_ERROR {method} {path}: no default server configured | {payload_summary(payload)}", "ERROR")
            return None, {"error": "No default objectives server configured"}, 500

        url = target.endpoint_url(path)
        if query_string:
            url += f"?{query_string.decode('utf-8')}"
        headers = {"apiversion": target.api_version}
        if target.api_key:
            headers["apikey"] = target.api_key
        self.log(
            f"OBJECTIVES_BEGIN {method} {path} -> {target.name} | url={url} | timeout={target.timeout}s | headers(apiversion={target.api_version}, apikey={'set' if bool(target.api_key) else 'empty'}) | {payload_summary(payload)}",
            "DEBUG",
        )
        try:
            started = time.perf_counter()
            response = requests.request(method=method, url=url, json=payload, headers=headers, timeout=target.timeout)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            HEALTH.record_forward_success(target.name, response.status_code)
            self.record_target_forwarded(target, "objectives", 1)
            self.log(
                f"OBJECTIVES_OK {method} {path} -> {target.name} [{response.status_code}] | elapsed_ms={elapsed_ms} | response_bytes={len(response.content)} | response_preview='{preview_text(response.text)}'",
                "INFO",
            )
            return response, None, None
        except Exception as exc:
            HEALTH.record_forward_error(target.name, str(exc))
            self.log(f"OBJECTIVES_ERROR {method} {path} -> {target.name}: {exc} | {payload_summary(payload)}", "ERROR")
            return None, {"error": str(exc)}, 502


class RelayRequestBridge(QObject):
    forward_signal = Signal(str, str, object)
    log_signal = Signal(str, str)


class RelayServerThread(QThread):
    server_started = Signal(str)
    server_stopped = Signal()
    server_error = Signal(str)

    def __init__(self, bind_host: str, port: int, bridge: RelayRequestBridge, manager: RelayManager):
        super().__init__()
        self.bind_host = bind_host
        self.port = port
        self.bridge = bridge
        self.manager = manager
        self._server = None

    def _authorize(self) -> bool:
        apikey = request.headers.get("apikey", "")
        authorized = apikey == LOCAL_API_KEY
        if not authorized:
            self.manager.log(
                f"Unauthorized request rejected: method={request.method} path={request.path} {request_context_summary()} provided_apikey={preview_text(apikey, 32) if apikey else '<empty>'}",
                "WARNING",
            )
        return authorized

    def build_app(self):
        app = Flask(__name__)
        bridge = self.bridge
        manager = self.manager

        @app.route("/", methods=["GET"])
        def root():
            return jsonify({
                "message": "Relay server is running",
                "name": APP_TITLE,
                "endpoints": {"events": "/events", "activities": "/activities", "objectives": "/objectives", "health": "/health"},
            })

        @app.route("/health", methods=["GET"])
        def health():
            return jsonify({"status": "ok", **HEALTH.snapshot()}), 200

        @app.route("/discovery", methods=["GET"])
        def discovery():
            return jsonify({
                "name": APP_TITLE,
                "description": "Local relay server for BGS-Tally events, activities and objectives.",
                "endpoints": {
                    "events": {"path": "/events", "minPeriod": 10, "maxBatch": 100},
                    "activities": {"path": "/activities", "minPeriod": 60, "maxBatch": 10},
                    "objectives": {"path": "/objectives", "minPeriod": 30, "maxBatch": 20},
                },
                "headers": {
                    "apikey": {"required": True, "description": "Local relay API key"},
                    "apiversion": {"required": False, "description": "Optional relay API version"},
                },
            })

        @app.route("/events", methods=["POST"])
        def events():
            if not self._authorize():
                return jsonify({"error": "Unauthorized"}), 401
            payload = request.get_json(silent=True)
            if not isinstance(payload, list):
                bridge.log_signal.emit("ERROR", f"API_REJECT {request.method} {request.path}: invalid payload | {request_context_summary()} | {payload_summary(payload)}")
                return jsonify({"error": "Expected a JSON array for /events"}), 400
            HEALTH.record_incoming("events", len(payload))
            bridge.log_signal.emit("INFO", f"API_CALL {request.method} {request.path}: accepted | {request_context_summary()} | {payload_summary(payload)}")
            bridge.forward_signal.emit("/events", "POST", payload)
            return jsonify({"status": "accepted", "forwarded": True, "count": len(payload)}), 200

        @app.route("/activities", methods=["PUT"])
        def activities():
            if not self._authorize():
                return jsonify({"error": "Unauthorized"}), 401
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                bridge.log_signal.emit("ERROR", f"API_REJECT {request.method} {request.path}: invalid payload | {request_context_summary()} | {payload_summary(payload)}")
                return jsonify({"error": "Expected a JSON object for /activities"}), 400
            HEALTH.record_incoming("activities", 1)
            bridge.log_signal.emit("INFO", f"API_CALL {request.method} {request.path}: accepted | {request_context_summary()} | {payload_summary(payload)}")
            bridge.forward_signal.emit("/activities", "PUT", payload)
            return jsonify({"status": "accepted", "forwarded": True}), 200

        @app.route("/objectives", methods=["GET", "POST"])
        def objectives():
            if not self._authorize():
                return jsonify({"error": "Unauthorized"}), 401
            payload = request.get_json(silent=True) if request.method == "POST" else None
            HEALTH.record_incoming("objectives", 1)
            bridge.log_signal.emit("INFO", f"API_CALL {request.method} {request.path}: accepted | {request_context_summary()} | {payload_summary(payload)}")
            response, error_body, error_status = manager.forward_objectives("/objectives", request.method, payload, request.query_string)
            if error_body is not None:
                return jsonify(error_body), error_status
            return Response(response.content, status=response.status_code, content_type=response.headers.get("Content-Type", "application/json"))

        @app.route("/objectives/<path:objective_path>", methods=["DELETE"])
        def objectives_with_path(objective_path: str):
            if not self._authorize():
                return jsonify({"error": "Unauthorized"}), 401
            HEALTH.record_incoming("objectives", 1)
            full_path = f"/objectives/{objective_path}"
            bridge.log_signal.emit("INFO", f"API_CALL DELETE {full_path}: accepted | {request_context_summary()} | query={preview_text(request.query_string.decode('utf-8') if request.query_string else '-', 120)}")
            response, error_body, error_status = manager.forward_objectives(full_path, "DELETE", None, request.query_string)
            if error_body is not None:
                return jsonify(error_body), error_status
            return Response(response.content, status=response.status_code, content_type=response.headers.get("Content-Type", "application/json"))

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
        self.resize(1500, 920)
        icon = self.app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

        self.model = RelayTableModel()
        self.manager = RelayManager(self.model)
        self.bridge = RelayRequestBridge()
        self.server_thread: Optional[RelayServerThread] = None
        self.tray_icon: Optional[QSystemTrayIcon] = None
        self.log_entries: List[Tuple[str, str]] = []

        self.bridge.forward_signal.connect(self.manager.forward)
        self.bridge.log_signal.connect(self.append_log)
        self.manager.log_message.connect(self.append_log)

        self._build_ui()
        self._setup_tray()
        self._setup_timers()
        self.load_config()

    def app_icon(self) -> QIcon:
        return QIcon(str(ICON_PATH)) if ICON_PATH.exists() else QIcon()

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
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(["DEBUG", "INFO"])
        self.log_level_combo.currentTextChanged.connect(self.on_log_level_changed)

        form_layout.addRow("Bind address", self.host_edit)
        form_layout.addRow("Port", self.port_spin)
        form_layout.addRow("Local API key", self.local_api_key_edit)
        form_layout.addRow("Log level", self.log_level_combo)
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
            ("Objectives received", "objectives"),
            ("Forward success", "success"),
            ("Forward errors", "errors"),
            ("Filtered events", "filtered_events"),
            ("Filtered activities", "filtered_activities"),
            ("Filtered objectives", "filtered_objectives"),
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
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        widths = {1: 140, 2: 220, 3: 120, 4: 85, 5: 70, 6: 180, 10: 100, 11: 100, 12: 100}
        for col, width in widths.items():
            self.table.setColumnWidth(col, width)
        layout.addWidget(self.table, 2)

        log_group = QGroupBox("Log Viewer")
        log_layout = QVBoxLayout(log_group)
        log_filter_row = QHBoxLayout()
        self.log_level_filter_combo = QComboBox()
        self.log_level_filter_combo.addItems(["All", "ERROR", "WARNING", "INFO", "DEBUG"])
        self.log_level_filter_combo.currentTextChanged.connect(self.refresh_log_view)
        self.log_text_filter = QLineEdit()
        self.log_text_filter.setPlaceholderText("Filter logs by text, server name, endpoint ...")
        self.log_text_filter.textChanged.connect(self.refresh_log_view)
        clear_log_btn = QPushButton("Clear View")
        clear_log_btn.clicked.connect(self.clear_log_view)
        log_filter_row.addWidget(QLabel("Level filter"))
        log_filter_row.addWidget(self.log_level_filter_combo)
        log_filter_row.addWidget(QLabel("Text filter"))
        log_filter_row.addWidget(self.log_text_filter, 1)
        log_filter_row.addWidget(clear_log_btn)
        log_layout.addLayout(log_filter_row)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        log_layout.addWidget(self.log_edit)
        layout.addWidget(log_group, 2)

    def _setup_timers(self):
        self.health_timer = QTimer(self)
        self.health_timer.timeout.connect(self.refresh_health_dashboard)
        self.health_timer.start(1000)
        self.refresh_health_dashboard()

    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.append_log("WARNING", "System tray is not available on this system.")
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

    @Slot(str, str)
    def append_log(self, level: str, line: str):
        self.log_entries.append((level.upper(), line))
        self.refresh_log_view()

    def refresh_log_view(self):
        selected_level = self.log_level_filter_combo.currentText() if hasattr(self, "log_level_filter_combo") else "All"
        text_filter = self.log_text_filter.text().strip().casefold() if hasattr(self, "log_text_filter") else ""
        lines = []
        for level, line in self.log_entries:
            if selected_level != "All" and level != selected_level:
                continue
            if text_filter and text_filter not in line.casefold():
                continue
            lines.append(line)
        self.log_edit.setPlainText("\n".join(lines[-2000:]))
        cursor = self.log_edit.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.log_edit.setTextCursor(cursor)

    def clear_log_view(self):
        self.log_entries.clear()
        self.log_edit.clear()

    def refresh_health_dashboard(self):
        snap = HEALTH.snapshot()
        server_running = bool(self.server_thread and self.server_thread.isRunning())
        self.health_labels["server_status"].setText("Running" if server_running else "Stopped")
        self.health_labels["rps"].setText(str(snap["requests_per_second"]))
        self.health_labels["rpm"].setText(str(snap["requests_per_minute"]))
        self.health_labels["events"].setText(str(snap["events_received"]))
        self.health_labels["activities"].setText(str(snap["activities_received"]))
        self.health_labels["objectives"].setText(str(snap["objectives_received"]))
        self.health_labels["success"].setText(str(snap["forward_success"]))
        self.health_labels["errors"].setText(str(snap["forward_errors"]))
        self.health_labels["filtered_events"].setText(str(snap["filtered_events"]))
        self.health_labels["filtered_activities"].setText(str(snap["filtered_activities"]))
        self.health_labels["filtered_objectives"].setText(str(snap["filtered_objectives"]))
        self.health_labels["last_status"].setText(str(snap["last_forward_status"]))
        self.health_labels["last_error"].setText(str(snap["last_error"]))
        self.health_labels["uptime"].setText(self.format_uptime(int(snap["uptime_seconds"])))

    @staticmethod
    def format_uptime(seconds: int) -> str:
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def on_log_level_changed(self, value: str):
        LOGGER.setLevel(getattr(logging, value.upper(), logging.INFO))
        self.manager.log(f"Log level changed to {value.upper()}", "INFO")
        if self.auto_save_check.isChecked():
            self.save_config()

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
        self.server_thread = RelayServerThread(host, port, self.bridge, self.manager)
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
        self.append_log("INFO", f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] INFO Server started on {url}")
        if self.tray_icon:
            self.tray_icon.showMessage(APP_TITLE, f"Relay server started on {url}", QSystemTrayIcon.Information, 3000)

    def on_server_stopped(self):
        self.status_label.setText("Status: stopped")
        self.append_log("INFO", f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] INFO Server stopped")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def on_server_error(self, error: str):
        self.status_label.setText("Status: error")
        self.append_log("ERROR", f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERROR Server error: {error}")
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
            "log_level": self.log_level_combo.currentText(),
            "targets": self.model.to_list(),
        }

    def save_config(self, path: Path = CONFIG_PATH):
        try:
            path.write_text(json.dumps(self.config_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
            self.append_log("INFO", f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] INFO Configuration saved: {path}")
        except Exception as exc:
            self.append_log("ERROR", f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERROR Configuration could not be saved: {exc}")

    def load_config(self, path: Path = CONFIG_PATH):
        if not path.exists():
            self.model.load_list([{
                "name": "Target Server 1",
                "base_url": "http://127.0.0.1:5000",
                "api_key": "",
                "api_version": "1.6.0",
                "enabled": True,
                "timeout": 15,
                "cmdr_filters": [],
                "forward_events": True,
                "forward_activities": True,
                "default_objectives": False,
            }])
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
            self.log_level_combo.blockSignals(True)
            self.log_level_combo.setCurrentText(str(data.get("log_level", "INFO")).upper())
            self.log_level_combo.blockSignals(False)
            self.on_log_level_changed(self.log_level_combo.currentText())
            self.append_log("INFO", f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] INFO Configuration loaded: {path}")
        except Exception as exc:
            self.append_log("ERROR", f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERROR Configuration could not be loaded: {exc}")

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
        return folder / f"{AUTOSTART_NAME}.bat" if folder else None

    def is_autostart_enabled(self) -> bool:
        path = self.autostart_shortcut_path()
        return bool(path and path.exists())

    def enable_autostart(self) -> bool:
        path = self.autostart_shortcut_path()
        if not path:
            self.append_log("WARNING", f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] WARNING Windows autostart is only supported on Windows.")
            return False
        try:
            path.write_text(f'@echo off\r\nstart "" "{sys.executable}" "{Path(__file__).resolve()}"\r\n', encoding="utf-8")
            self.append_log("INFO", f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] INFO Windows autostart enabled: {path}")
            return True
        except Exception as exc:
            self.append_log("ERROR", f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERROR Could not enable Windows autostart: {exc}")
            return False

    def disable_autostart(self) -> bool:
        path = self.autostart_shortcut_path()
        if not path:
            self.append_log("WARNING", f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] WARNING Windows autostart is only supported on Windows.")
            return False
        try:
            if path.exists():
                path.unlink()
            self.append_log("INFO", f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] INFO Windows autostart disabled")
            return True
        except Exception as exc:
            self.append_log("ERROR", f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERROR Could not disable Windows autostart: {exc}")
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
